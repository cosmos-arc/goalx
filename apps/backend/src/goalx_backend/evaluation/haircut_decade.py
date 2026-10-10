"""
haircut 校准十年重估（backtest-decade 票 17，spec S17）。

模拟竞彩价 haircut（现 −10%）按 gold era 分层收盘 × 竞彩官方 SP 重估，
对照单关 overround 1.129 实测基线（research/22-27）。

**配对语义（与票 30 实时报价 as-of 配对的差别）**：SP=停售时官方终赔，
本身就是收盘口径——与 gold 收盘（era 正典链）配对是收盘×收盘，无需
时差容差窗（ADR-0007 的防市场移动纪律天然满足）。era1 无 JC SP（历史
回填链属本程序外，spec Out of Scope）——分时代维度如实限于 era2。

**样本纪律**：同票 30 门槛（n≥30 才 calibrated，不足落 default 并标注）；
scope 命名 ``sp-close:*``（不覆写票 30 的 overall/联赛行——引擎默认
消费 overall，替换与否=用户点头后另改）。sid 链经 ``source_match_links``
物化链（票 77），链覆盖外行只进 overright 统计不进配对。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from goalx_backend.data import gold_reader, quote_evidence
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.mapping import srct_sid_for_fixture
from goalx_backend.db import utc_now_iso
from goalx_backend.evaluation.backtest import era_fair_probs
from goalx_backend.evaluation.haircut import (
    DEFAULT_HAIRCUT,
    MIN_SAMPLES,
    quartiles_of,
)
from goalx_backend.markets import SELECTIONS

REPORT_BASENAME = "haircut-decade"
JC_OVERROUND_BASELINE = 1.129  # research/22-27 单关实测基线（sanity 锚）
HAIRCUT_SP_METHOD_VERSION = "sp_close_era_v1"


@dataclass(frozen=True)
class SpPair:
    """一场可配对的 JC SP×gold 收盘样本（fixture 粒度）。"""

    fixture_id: int
    sid: str
    league: str
    match_date: str
    era: str
    odds: tuple[float, float, float]
    fair_probs: dict[str, float]
    fair_source: str

    def haircuts(self) -> list[float]:
        """逐 selection：h = 1 − SP × fair_prob（正=竞彩更差）。"""
        return [
            1.0 - self.odds[i] * self.fair_probs[sel]
            for i, sel in enumerate(SELECTIONS)
        ]

    @property
    def jc_overround(self) -> float:
        """JC SP 三向 overround（1/odds 和；1.129 基线对照面）。"""
        return sum(1.0 / o for o in self.odds)


def collect_sp_pairs(
    conn: sqlite3.Connection, duck_con: duckdb.DuckDBPyConnection
) -> tuple[list[SpPair], dict[str, int]]:
    """
    JC SP（uniform 彩果，void=0 三向全）× sid 链 × gold era 收盘。

    同 fixture 多彩果行取末行（彩果唯一）；sid 链缺席/无 gold 收盘/
    无 fair 基准按缺口计数不硬配。
    """
    rows = quote_evidence.jc_sp_rows(conn)
    counted: dict[str, int] = {"jc_rows": len(rows)}
    by_fixture: dict[int, sqlite3.Row] = {}
    for row in rows:
        by_fixture[int(row["fixture_id"])] = row
    counted["jc_fixtures"] = len(by_fixture)

    linked: list[tuple[sqlite3.Row, str]] = []
    seen_sids: set[str] = set()
    no_link = 0
    for fixture_id, row in by_fixture.items():
        sid = srct_sid_for_fixture(conn, fixture_id)
        if sid is None:
            no_link += 1
            continue
        if sid in seen_sids:  # 同 sid 多 fixture（链歧义防御）取首
            continue
        seen_sids.add(sid)
        linked.append((row, sid))
    counted["no_sid_link"] = no_link

    gold_rows = gold_reader.fetch_close_rows_by_sids(
        duck_con, [sid for _, sid in linked]
    )
    pairs: list[SpPair] = []
    no_gold = 0
    for row, sid in linked:
        gold = gold_rows.get(sid)
        if gold is None:
            no_gold += 1
            continue
        fair = era_fair_probs(
            str(gold["era"]),
            (gold["psc_home"], gold["psc_draw"], gold["psc_away"]),
            (gold["avgc_home"], gold["avgc_draw"], gold["avgc_away"]),
            (gold["close1x2_h"], gold["close1x2_d"], gold["close1x2_a"]),
            (
                gold["close1x2_cons_h"],
                gold["close1x2_cons_d"],
                gold["close1x2_cons_a"],
            ),
        )
        odds = (
            float(row["odds_h"]),
            float(row["odds_d"]),
            float(row["odds_a"]),
        )
        if fair is None or any(o <= 1.0 for o in odds):
            no_gold += 1  # 无基准/坏价同缺口口径
            continue
        pairs.append(
            SpPair(
                fixture_id=int(row["fixture_id"]),
                sid=sid,
                league=str(row["league_name"]),
                match_date=str(row["match_date"]),
                era=str(gold["era"]),
                odds=odds,
                fair_probs=fair[0],
                fair_source=fair[1],
            )
        )
    counted["no_gold_close"] = no_gold
    return pairs, counted


def jc_overround_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """全量 JC SP 面的 overround 分布（不需 sid 链；1.129 基线对照）。"""
    rows = quote_evidence.jc_sp_overround_rows(conn)
    all_over = [float(r["overround"]) for r in rows]
    single_over = [float(r["overround"]) for r in rows if r["betting_single"] == 1]
    stats: dict[str, Any] = {"n_rows": len(rows)}
    for name, values in (("all", all_over), ("single_sale", single_over)):
        if not values:
            stats[name] = {"n": 0}
            continue
        q = quartiles_of(values)
        stats[name] = {
            "n": len(values),
            "mean": round(sum(values) / len(values), 4),
            "median": round(q[1], 4),
            "q1": round(q[0], 4),
            "q3": round(q[2], 4),
            "baseline_1_129_delta": round(q[1] - JC_OVERROUND_BASELINE, 4),
        }
    return stats


def calibrate_decade(
    conn: sqlite3.Connection,
    store: CorpusStore,
    duck_con: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """重估+落库（sp-close:* scope 行）+ 报告落 reports/ 双视图。"""
    pairs, counted = collect_sp_pairs(conn, duck_con)
    overround = jc_overround_stats(conn)

    by_scope: dict[str, list[SpPair]] = {"sp-close:overall": pairs}
    for pair in pairs:
        # lg: 前缀防联赛名撞 overall（correctness F5）
        by_scope.setdefault(f"sp-close:lg:{pair.league}", []).append(pair)
    written: list[dict[str, Any]] = []
    era_breakdown: dict[str, int] = {}
    for scope, group in sorted(by_scope.items()):
        values = [h for pair in group for h in pair.haircuts()]
        quartiles = quartiles_of(values)
        if len(values) >= MIN_SAMPLES:
            haircut, source = quartiles[1], "calibrated"
        else:
            haircut, source = DEFAULT_HAIRCUT, "default"  # 与票 30 同回落
        n_fixtures = len({p.fixture_id for p in group})
        conn.execute(
            """
            INSERT INTO haircut_calibrations
            (scope, market_code, haircut, n_samples, n_fixtures, method_version,
             quartiles, source, computed_at)
            VALUES (?, 'had', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, market_code) DO UPDATE SET
                haircut=excluded.haircut, n_samples=excluded.n_samples,
                n_fixtures=excluded.n_fixtures,
                method_version=excluded.method_version,
                quartiles=excluded.quartiles, source=excluded.source,
                computed_at=excluded.computed_at
            """,
            (
                scope,
                haircut,
                len(values),
                n_fixtures,
                HAIRCUT_SP_METHOD_VERSION,
                json.dumps([round(q, 6) for q in quartiles]),
                source,
                utc_now_iso(),  # 与票 30 行同口径（correctness F3）
            ),
        )
        written.append(
            {
                "scope": scope,
                "haircut": round(haircut, 6),
                "source": source,
                "n_samples": len(values),
                "n_fixtures": n_fixtures,
                "quartiles": [round(q, 6) for q in quartiles],
            }
        )
    for pair in pairs:
        era_breakdown[pair.era] = era_breakdown.get(pair.era, 0) + 1
    conn.commit()

    payload = {
        "built_at": utc_now_iso(),  # 与票 30 行同口径（correctness F3）
        "method": HAIRCUT_SP_METHOD_VERSION,
        "coverage": counted | {"era_breakdown": era_breakdown},
        "overround": overround,
        "overround_baseline": JC_OVERROUND_BASELINE,
        "calibrations": written,
        "note": (
            "era1 无 JC SP（历史回填链属程序外）；引擎默认 haircut 消费"
            " overall 行不变——sp-close 值替换与否=用户裁决；逐场三 "
            "selection 样本强相关（共享 overround），有效 n≈n_fixtures"
        ),
    }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# haircut 十年重估（票 17）",
        "",
        "- 配对：{} JC 场 × sid 链 × gold 收盘 → {} 场入样（{}）".format(
            counted.get("jc_fixtures", 0), len(pairs), era_breakdown
        ),
        "- overround 对照：单关中位 {} vs 基线 {}（Δ={}）".format(
            overround.get("single_sale", {}).get("median"),
            JC_OVERROUND_BASELINE,
            overround.get("single_sale", {}).get("baseline_1_129_delta"),
        ),
        "",
        "| scope | haircut | source | n_samples | n_fixtures | 四分位 |",
        "|---|---|---|---|---|---|",
    ]
    for row in written:
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                row["scope"],
                row["haircut"],
                row["source"],
                row["n_samples"],
                row["n_fixtures"],
                row["quartiles"],
            )
        )
    lines.append("")
    lines.append("<!-- 引擎默认 −10%（overall 行）不变；替换=用户点头 -->")
    (reports / f"{REPORT_BASENAME}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return payload
