"""
haircut 校准器（票 30，ADR 0007）：竞彩快照 vs 同场欧洲共识的配对样本。

- 配对样本：同场 fixture 的 sporttery had 最新快照 vs odds_api 多 book 共识
  （均值价）Shin 去晦后的公允赔率；每选择贡献一个样本
  ``haircut = 1 − o_jc / o_fair``；
- 分布按 scope（联赛 / overall）估计：中位数 + 四分位，落
  ``haircut_calibrations``（UPSERT，同数据同值可复现）；
- 回测切换接口：``calibrated_haircut`` 给出（值, 来源, 样本数），样本不足
  回落默认 −10% 并标注 source=default；
- v1 只校准 had（欧赔侧唯一可映射玩法；hhad/ttg 等欧共识同构市场接入后
  扩展 market_code 维度即可）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

DEFAULT_HAIRCUT = 0.10
MIN_SAMPLES = 30  # 样本不足回落默认（票 30 验收）


@dataclass(frozen=True)
class HaircutSample:
    """一个配对样本（某场某选择的 竞彩价 vs 公允价）。"""

    fixture_id: int
    competition: str
    selection: str
    jc_odds: float
    fair_odds: float

    @property
    def haircut(self) -> float:
        """1 − 竞彩价/公允价（正数 = 竞彩更差）。"""
        return 1.0 - self.jc_odds / self.fair_odds


def build_haircut_samples(
    conn: sqlite3.Connection, *, market_code: str = "had"
) -> list[HaircutSample]:
    """全部可配对样本（确定性排序：fixture, selection）。"""
    rows = conn.execute(
        """
        SELECT f.id AS fixture_id, c.name AS competition,
               s.selection_code, s.source, s.odds, s.captured_at
        FROM odds_snapshots s
        JOIN fixtures f ON f.id = s.fixture_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE s.market_code = ?
          AND (s.source = 'sporttery' OR s.source LIKE 'odds_api:%')
        ORDER BY s.captured_at, s.id
        """,
        (market_code,),
    ).fetchall()
    jc_latest: dict[tuple[int, str], tuple[str, float]] = {}
    books: dict[int, dict[str, dict[str, tuple[str, float]]]] = {}
    competitions: dict[int, str] = {}
    for row in rows:
        fixture_id = int(row["fixture_id"])
        selection = str(row["selection_code"])
        source = str(row["source"])
        odds = float(row["odds"])
        captured = str(row["captured_at"])
        competitions.setdefault(fixture_id, str(row["competition"]))
        if source == "sporttery":
            jc_latest[(fixture_id, selection)] = (captured, odds)
        else:
            book = source.removeprefix("odds_api:")
            books.setdefault(fixture_id, {}).setdefault(selection, {})[book] = (
                captured,
                odds,
            )
    samples: list[HaircutSample] = []
    for (fixture_id, selection), (_, jc_odds) in sorted(jc_latest.items()):
        fixture_books = books.get(fixture_id, {})
        per_sel = [fixture_books.get(sel) for sel in SELECTIONS]
        if not all(per_sel):
            continue  # 三向不完整 → 无共识
        consensus = tuple(
            sum(price for _, price in sel_prices.values()) / len(sel_prices)
            for sel_prices in per_sel
            if sel_prices is not None
        )
        probs = om.shin_implied(consensus)
        fair_odds = 1.0 / probs[SELECTIONS.index(selection)]
        samples.append(
            HaircutSample(
                fixture_id=fixture_id,
                competition=competitions.get(fixture_id, ""),
                selection=selection,
                jc_odds=jc_odds,
                fair_odds=fair_odds,
            )
        )
    return samples


def _quartiles(values: list[float]) -> list[float]:
    """升序四分位（线性插值）；空列表返回 [0,0,0]。"""
    if not values:
        return [0.0, 0.0, 0.0]
    ordered = sorted(values)

    def quantile(q: float) -> float:
        pos = q * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        frac = pos - low
        return ordered[low] * (1 - frac) + ordered[high] * frac

    return [quantile(0.25), quantile(0.5), quantile(0.75)]


def calibrate_haircuts(
    conn: sqlite3.Connection,
    *,
    market_code: str = "had",
    min_samples: int = MIN_SAMPLES,
) -> list[dict[str, Any]]:
    """按联赛 + overall 估计 haircut 分布并落库；返回写入的报告行。"""
    samples = build_haircut_samples(conn, market_code=market_code)
    by_scope: dict[str, list[HaircutSample]] = {"overall": samples}
    for sample in samples:
        by_scope.setdefault(sample.competition, []).append(sample)
    written: list[dict[str, Any]] = []
    for scope, group in sorted(by_scope.items()):
        values = [s.haircut for s in group]
        quartiles = _quartiles(values)
        if len(values) >= min_samples:
            haircut, source = quartiles[1], "calibrated"
        else:
            haircut, source = DEFAULT_HAIRCUT, "default"
        conn.execute(
            """
            INSERT INTO haircut_calibrations
            (scope, market_code, haircut, n_samples, quartiles, source, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, market_code) DO UPDATE SET
                haircut=excluded.haircut, n_samples=excluded.n_samples,
                quartiles=excluded.quartiles, source=excluded.source,
                computed_at=excluded.computed_at
            """,
            (
                scope,
                market_code,
                haircut,
                len(values),
                json.dumps([round(q, 6) for q in quartiles]),
                source,
                utc_now_iso(),
            ),
        )
        written.append(
            {
                "scope": scope,
                "market_code": market_code,
                "haircut": haircut,
                "n_samples": len(values),
                "quartiles": [round(q, 6) for q in quartiles],
                "source": source,
            }
        )
    conn.commit()
    return written


def calibrated_haircut(
    conn: sqlite3.Connection,
    *,
    scope: str = "overall",
    market_code: str = "had",
    default: float = DEFAULT_HAIRCUT,
) -> tuple[float, str, int]:
    """
    回测切换接口：取某 scope 的校准 haircut。

    Returns ``(haircut, source, n_samples)``；无校准行或样本不足行均按其
    落落值返回（default 行存的就是默认值）。
    """
    row = conn.execute(
        """
        SELECT haircut, source, n_samples FROM haircut_calibrations
        WHERE scope = ? AND market_code = ?
        """,
        (scope, market_code),
    ).fetchone()
    if row is None:
        return default, "default", 0
    return float(row["haircut"]), str(row["source"]), int(row["n_samples"])
