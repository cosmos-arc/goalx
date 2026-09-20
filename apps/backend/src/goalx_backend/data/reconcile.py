"""
赛果对账与覆盖登记（票 44）：参照源观测 ↔ draw_results 事实。

并行对账阶段不改结算事实源（ADR 0001 澄清条款）：对账引擎只比较、
只出清单——一致无动作；比分/无效判定不一致、或参照源终态而库内缺失
→ 待人工清单（人工兜底通道裁决）。参照源：

- sporttery.cn uniform 族官方赛果（data/ingest/uniform.py，官方口径）；
- openfootball（data/ingest/openfootball.py，社区对账源，仅对账不入管道）。

source_coverage（定则 4，"空≠无"）：每源每覆盖日看到什么的现态维表。
absent 断言仅当 coverage_status='covered'（源声明覆盖且采集成功）；
coverage_date 语义随源而定（uniform=matchDate、源D=业务日、
openfootball=赛季键），league_key 空串=页面/文件级（无联赛细分）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from goalx_backend.data import results as rs_store
from goalx_backend.db import utc_now_iso


@dataclass
class ReferenceResult:
    """一条参照源赛果观测（已映射到 fixture 的形状）。"""

    fixture_id: int
    business_date: str
    code: str  # 展示标识：竞彩场次号或 openfootball 对阵
    home_goals: int | None
    away_goals: int | None
    half_home_goals: int | None = None
    half_away_goals: int | None = None
    void: bool = False
    void_reason: str | None = None


@dataclass
class ReconcileStats:
    """一次对账的统计与待人工清单。"""

    source: str
    observed_at: str = ""
    business_dates: list[str] = field(default_factory=list)
    compared: int = 0  # 参照源终态（比分或 void）且库内有对应场次的
    consistent: int = 0
    score_mismatch: int = 0
    void_mismatch: int = 0
    missing_result: int = 0  # 参照源终态、库内无赛果（含无 void 记录）
    unmatched: int = 0  # 参照源有、库内无对应场次（范围外，不报警）
    pending_manual: list[dict[str, str]] = field(default_factory=list)


def _manual(
    stats: ReconcileStats, ref: ReferenceResult, reason: str, detail: str = ""
) -> None:
    """进待人工清单（business_date/code/reason 定位，detail 附两侧值）。"""
    entry: dict[str, str] = {
        "business_date": ref.business_date,
        "code": ref.code,
        "reason": reason,
    }
    if detail:
        entry["detail"] = detail
    stats.pending_manual.append(entry)


def reconcile_draw_results(
    conn: sqlite3.Connection, refs: list[ReferenceResult], stats: ReconcileStats
) -> ReconcileStats:
    """
    比较纯函数：参照源终态 vs draw_results 事实（fail-closed，不冲正）。

    - 全场比分严格比较；半场只在两侧都有值时比较（源页面半场可缺）；
    - 参照源 void（官方取消/无效）vs 库内：同为 void 一致；单侧有结果
      → void_mismatch（人工裁决，可能是官方更正）；
    - 参照源未终态（比分与 void 皆无）不比较不计分母。
    """
    for ref in refs:
        stored = rs_store.get_draw_result(conn, ref.fixture_id)
        stored_score = (
            f"{stored['home_goals']}:{stored['away_goals']}"
            if stored is not None
            else "—"
        )
        if ref.void:
            stats.compared += 1
            if stored is None:
                stats.missing_result += 1
                _manual(
                    stats, ref, "reference_void_missing_fact", ref.void_reason or "void"
                )
            elif not bool(stored["void"]):
                stats.void_mismatch += 1
                _manual(
                    stats,
                    ref,
                    "stored_result_vs_reference_void",
                    f"stored {stored_score} vs void({ref.void_reason})",
                )
            else:
                stats.consistent += 1
            continue
        if ref.home_goals is None or ref.away_goals is None:
            continue
        stats.compared += 1
        ref_score = f"{ref.home_goals}:{ref.away_goals}"
        if stored is None:
            stats.missing_result += 1
            _manual(stats, ref, "reference_final_missing_fact", ref_score)
        elif bool(stored["void"]):
            stats.void_mismatch += 1
            _manual(
                stats,
                ref,
                "stored_void_vs_reference_result",
                f"stored void({stored['void_reason']}) vs {ref_score}",
            )
        elif stored_score != ref_score:
            stats.score_mismatch += 1
            _manual(
                stats, ref, "score_mismatch", f"stored {stored_score} vs {ref_score}"
            )
        elif (
            ref.half_home_goals is not None
            and ref.half_away_goals is not None
            and stored["half_home_goals"] is not None
            and stored["half_away_goals"] is not None
            and (
                int(stored["half_home_goals"]),
                int(stored["half_away_goals"]),
            )
            != (ref.half_home_goals, ref.half_away_goals)
        ):
            stats.score_mismatch += 1
            stored_half = f"{stored['half_home_goals']}:{stored['half_away_goals']}"
            ref_half = f"{ref.half_home_goals}:{ref.half_away_goals}"
            _manual(
                stats, ref, "half_score_mismatch", f"stored {stored_half} vs {ref_half}"
            )
        else:
            stats.consistent += 1
    return stats


def record_reconciliation_run(
    conn: sqlite3.Connection,
    stats: ReconcileStats,
    *,
    parse_version: str = "reconcile_v1",
) -> int:
    """写一次对账的元信息行（append-only 语义，最新行即状态）。"""
    cur = conn.execute(
        """
        INSERT INTO draw_reconciliation_runs
(source, observed_at, business_dates, compared, consistent, score_mismatch,
            void_mismatch, missing_result, unmatched, pending_manual,
            parse_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats.source,
            stats.observed_at or utc_now_iso(),
            json.dumps(stats.business_dates, ensure_ascii=False),
            stats.compared,
            stats.consistent,
            stats.score_mismatch,
            stats.void_mismatch,
            stats.missing_result,
            stats.unmatched,
            json.dumps(stats.pending_manual, ensure_ascii=False),
            parse_version,
            utc_now_iso(),
        ),
    )
    conn.commit()
    if not cur.lastrowid:
        raise RuntimeError("draw_reconciliation_runs INSERT 未产生 rowid")
    return int(cur.lastrowid)


def latest_reconciliation(conn: sqlite3.Connection, source: str) -> sqlite3.Row | None:
    """某参照源最近一次对账（无历史返回 None）。"""
    return conn.execute(
        """
        SELECT * FROM draw_reconciliation_runs
        WHERE source = ? ORDER BY id DESC LIMIT 1
        """,
        (source,),
    ).fetchone()


def upsert_source_coverage(
    conn: sqlite3.Connection,
    *,
    source: str,
    coverage_date: str,
    match_count: int,
    coverage_status: str,
    observed_at: str,
    league_key: str = "",
) -> None:
    """登记/刷新一源一覆盖日的现态（空≠无：fetched_empty 与 covered 区分）。"""
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO source_coverage
(source, coverage_date, league_key, match_count, coverage_status,
            observed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, coverage_date, league_key) DO UPDATE SET
            match_count = excluded.match_count,
            coverage_status = excluded.coverage_status,
            observed_at = excluded.observed_at,
            updated_at = excluded.updated_at
        """,
        (
            source,
            coverage_date,
            league_key,
            match_count,
            coverage_status,
            observed_at,
            now,
            now,
        ),
    )


def stats_dict(stats: ReconcileStats) -> dict[str, Any]:
    """统计 → 可 JSON 化的 dict（flow 返回值/日志用）。"""
    return {
        "source": stats.source,
        "observed_at": stats.observed_at,
        "business_dates": stats.business_dates,
        "compared": stats.compared,
        "consistent": stats.consistent,
        "score_mismatch": stats.score_mismatch,
        "void_mismatch": stats.void_mismatch,
        "missing_result": stats.missing_result,
        "unmatched": stats.unmatched,
        "pending_manual": len(stats.pending_manual),
    }
