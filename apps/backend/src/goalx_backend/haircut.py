"""
haircut 校准器（票 30，ADR 0007；票 35 补 as-of 配对与方法版本）。

- as-of 配对（票 35）：每场取竞彩 had 最新快照时刻 T（源调盘时间），
  欧侧按「本机观测时间 ≤ T 且距 T 不超过配对时差上限」取各 book 最新
  完整三向；观测时间未知的欧侧旧行按源解释为 captured_at。
- 固定方法 ``shin_mean_v1``：各 book 均价 → 自写 Shin 去晦 → 公允赔率；
  ``method_sensitivity`` 提供归一化/Power/逐 book 去水后聚合的对照
  （离线核验用，不据此事后换方法）。
- 分布按 scope（联赛 / overall）估计：中位数 + 四分位，落
  ``haircut_calibrations``（UPSERT，同数据同值可复现），分列场次数
  （n_fixtures）与选择数（n_samples）；样本不足回落默认 −10% 并标注
  source=default。
- v1 只校准 had（欧赔侧唯一可映射玩法）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS
from goalx_backend.quote_evidence import effective_observed_at

DEFAULT_HAIRCUT = 0.10
MIN_SAMPLES = 30  # 样本不足回落默认（票 30 验收）
HAIRCUT_METHOD_VERSION = "shin_mean_v1"
MAX_PAIR_GAP_SECONDS = 300.0  # 配对时差工程初值（票 35），可显式传参调整


@dataclass(frozen=True)
class HaircutSample:
    """一个配对样本（某场某选择的 竞彩价 vs 公允价，同一 as-of）。"""

    fixture_id: int
    competition: str
    selection: str
    jc_captured_at: str
    jc_odds: float
    eu_books: dict[str, dict[str, float]]  # book -> {"h","d","a"}
    eu_observed_at: str

    @property
    def fair_odds(self) -> float:
        """固定方法 shin_mean_v1 的公允赔率。"""
        probs = _mean_book_shin(self.eu_books)
        return 1.0 / probs[SELECTIONS.index(self.selection)]

    @property
    def haircut(self) -> float:
        """1 − 竞彩价/公允价（正数 = 竞彩更差）。"""
        return 1.0 - self.jc_odds / self.fair_odds


def _consensus_or_raise(
    books: dict[str, dict[str, float]],
) -> tuple[float, ...]:
    """各 book 均价向量；books 已校验完整三向，缺失即内部错误。"""
    consensus = om.consensus_odds([{b: books[b][s] for b in books} for s in SELECTIONS])
    if consensus is None:
        raise RuntimeError("consensus incomplete after three-way validation")
    return consensus


def _mean_book_shin(books: dict[str, dict[str, float]]) -> tuple[float, ...]:
    """固定方法：各 book 均价 → Shin（shin_mean_v1）。"""
    return om.shin_implied(_consensus_or_raise(books))


def _per_book_shin_mean(books: dict[str, dict[str, float]]) -> tuple[float, ...]:
    """对照法：逐 book 去水后聚合（平均概率）。"""
    per_book = [
        om.shin_implied(tuple(books[b][s] for s in SELECTIONS)) for b in sorted(books)
    ]
    return tuple(
        sum(book[i] for book in per_book) / len(per_book)
        for i in range(len(SELECTIONS))
    )


def _fair_odds_by_method(
    books: dict[str, dict[str, float]], method: str
) -> tuple[float, ...]:
    """按方法名重算公允赔率向量（敏感性对照）。"""
    match method:
        case "shin_mean":
            probs = _mean_book_shin(books)
        case "shin_per_book_mean":
            probs = _per_book_shin_mean(books)
        case "normalized_mean":
            probs = om.normalized_implied(_consensus_or_raise(books))
        case "power_mean":
            probs = om.power_implied(_consensus_or_raise(books))
        case _:
            raise ValueError(f"unknown method: {method}")
    return tuple(1.0 / p for p in probs)


def build_haircut_samples(
    conn: sqlite3.Connection,
    *,
    market_code: str = "had",
    max_pair_gap_seconds: float = MAX_PAIR_GAP_SECONDS,
) -> list[HaircutSample]:
    """全部 as-of 配对样本（确定性排序：fixture, selection）。"""
    rows = conn.execute(
        """
        SELECT f.id AS fixture_id, c.name AS competition,
               s.id AS id, s.selection_code, s.source, s.odds, s.captured_at,
               s.observed_at, s.source_updated_at
        FROM odds_snapshots s
        JOIN fixtures f ON f.id = s.fixture_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE s.market_code = ?
          AND (s.source = 'sporttery' OR s.source LIKE 'odds_api:%')
        ORDER BY s.captured_at, s.id
        """,
        (market_code,),
    ).fetchall()
    jc_rows: dict[int, list[sqlite3.Row]] = {}
    eu_rows: dict[int, list[sqlite3.Row]] = {}
    competitions: dict[int, str] = {}
    for row in rows:
        fixture_id = int(row["fixture_id"])
        competitions.setdefault(fixture_id, str(row["competition"]))
        if str(row["source"]) == "sporttery":
            jc_rows.setdefault(fixture_id, []).append(row)
        else:
            eu_rows.setdefault(fixture_id, []).append(row)

    samples: list[HaircutSample] = []
    for fixture_id in sorted(jc_rows):
        jc_latest = _latest_per_selection(jc_rows[fixture_id])
        if set(jc_latest) != set(SELECTIONS):
            continue  # 竞彩三向不完整 → 无配对
        pair_time = max(str(r["captured_at"]) for r in jc_latest.values())
        books = _books_asof(
            eu_rows.get(fixture_id, []), pair_time, max_pair_gap_seconds
        )
        if not books:
            continue  # as-of 时刻无同窗欧共识
        for selection in SELECTIONS:
            samples.append(
                HaircutSample(
                    fixture_id=fixture_id,
                    competition=competitions.get(fixture_id, ""),
                    selection=selection,
                    jc_captured_at=pair_time,
                    jc_odds=float(jc_latest[selection]["odds"]),
                    eu_books=books,
                    eu_observed_at=pair_time,
                )
            )
    return samples


def _latest_per_selection(rows: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    """按 captured_at 取每选项最新一行。"""
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        sel = str(row["selection_code"])
        current = latest.get(sel)
        key = (str(row["captured_at"]), int(row["id"]))
        if current is None or key >= (str(current["captured_at"]), int(current["id"])):
            latest[sel] = row
    return latest


def _books_asof(
    rows: list[sqlite3.Row], pair_time: str, max_pair_gap_seconds: float
) -> dict[str, dict[str, float]]:
    """as-of 时刻可配对的各 book 完整三向（观测晚于 as-of 的剔除）。"""
    by_book: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        observed = effective_observed_at(row)
        if observed is None or observed > pair_time:
            continue
        by_book.setdefault(str(row["source"]), []).append(row)
    books: dict[str, dict[str, float]] = {}
    for book, book_rows in by_book.items():
        latest = _latest_per_selection(book_rows)
        if set(latest) != set(SELECTIONS):
            continue  # 同公司完整三向才可比较（票 35）
        newest_observed = max(
            observed
            for observed in (effective_observed_at(r) for r in latest.values())
            if observed is not None
        )
        if _seconds_between(pair_time, newest_observed) > max_pair_gap_seconds:
            continue  # 配对时差超窗
        books[book] = {sel: float(r["odds"]) for sel, r in latest.items()}
    return books


def _seconds_between(later: str, earlier: str) -> float:
    def parse(value: str) -> datetime:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    return (parse(later) - parse(earlier)).total_seconds()


def method_sensitivity(samples: list[HaircutSample]) -> dict[str, dict[str, float]]:
    """
    离线方法敏感性对照（票 35 验收 5）：同一样本集各方法的中位 haircut。

    只作核验报告，不据此更换已固定方法（不事后选优）。
    """
    methods = ("shin_mean", "shin_per_book_mean", "normalized_mean", "power_mean")
    report: dict[str, dict[str, float]] = {}
    for method in methods:
        values: list[float] = []
        for sample in samples:
            fair = _fair_odds_by_method(sample.eu_books, method)[
                SELECTIONS.index(sample.selection)
            ]
            values.append(1.0 - sample.jc_odds / fair)
        values.sort()
        median = values[len(values) // 2] if values else 0.0
        report[method] = {"median_haircut": round(median, 6), "n": float(len(values))}
    return report


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
    max_pair_gap_seconds: float = MAX_PAIR_GAP_SECONDS,
) -> list[dict[str, Any]]:
    """按联赛 + overall 估计 haircut 分布并落库；返回写入的报告行。"""
    samples = build_haircut_samples(
        conn, market_code=market_code, max_pair_gap_seconds=max_pair_gap_seconds
    )
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
        n_fixtures = len({s.fixture_id for s in group})
        conn.execute(
            """
            INSERT INTO haircut_calibrations
            (scope, market_code, haircut, n_samples, n_fixtures, method_version,
             quartiles, source, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, market_code) DO UPDATE SET
                haircut=excluded.haircut, n_samples=excluded.n_samples,
                n_fixtures=excluded.n_fixtures,
                method_version=excluded.method_version,
                quartiles=excluded.quartiles, source=excluded.source,
                computed_at=excluded.computed_at
            """,
            (
                scope,
                market_code,
                haircut,
                len(values),
                n_fixtures,
                HAIRCUT_METHOD_VERSION,
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
                "n_fixtures": n_fixtures,
                "method_version": HAIRCUT_METHOD_VERSION,
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
