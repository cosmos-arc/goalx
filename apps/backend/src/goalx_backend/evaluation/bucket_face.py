"""
8 特征错价桶只读复盘面（misprice-bucket-display 票 02 增量 1，裁决①=c）。

十年回测的真错价桶（signal-attribution run5，|t|≥2 且 n≥100 过探索纪律
后 7 个桶面/8 个显著向）：本模块把桶判据投到**已完赛场次**的 gold 行上，
追加只读 flags——不进 had 闸门/EV/候选/bets 任何决策路径（同票 01 纪律）。

口径（裁决②）：桶判据与特征计算平移自 signal_attribution 单一出处
（features_of），**效应量/t 值不可搬**——live 文案只写"十年聚合发现"；
dist 切点用 run5 样本重放冻结（重放 n/t 与报告逐位一致，2026-11-01 验）。

增量 2（今日页临场态 as-of 投影）另票跟进：本面只做复盘（开球已过），
判据天然齐（gold 列全为收盘时态）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

import duckdb

from goalx_backend.data import gold_reader, mapping
from goalx_backend.evaluation.signal_attribution import bucket_of, features_of

# dist 桶冻结切点（run5 n=33883 样本重放的 q33/q67；drift 桶按符号无需切点）
FROZEN_CUTS: dict[str, tuple[float, float]] = {
    "ah_phase_pre": (461.0, 693.0),
    "ah_phase_inplay": (142.0, 214.0),
}

# 7 个显著桶面：(flag 值, 特征键, 桶型, 桶名)；late_move_h:pos 双向显著
# （h −0.0188 / d +0.0164）仍是一个桶一面。
SIGNIFICANT_FACES: tuple[tuple[str, str, str, str], ...] = (
    ("ah_phase_pre:low", "ah_phase_pre", "dist", "low"),
    ("ah_phase_pre:mid", "ah_phase_pre", "dist", "mid"),
    ("ah_phase_inplay:high", "ah_phase_inplay", "dist", "high"),
    ("ou_line_drift:pos", "ou_line_drift", "drift", "pos"),
    ("late_move_h:pos", "late_move_h", "drift", "pos"),
    ("late_move24_h:pos", "late_move24_h", "drift", "pos"),
    ("late_move24_h:neg", "late_move24_h", "drift", "neg"),
)


def faces_for_gold_row(gold_row: dict[str, Any]) -> list[str]:
    """Gold 信号行 → 命中的显著桶面 flag 列表（特征缺列不判，空≠无）。"""
    features = features_of(gold_row)
    out: list[str] = []
    for flag, key, kind, bucket in SIGNIFICANT_FACES:
        value = features.get(key)
        if value is None:
            continue
        cuts = FROZEN_CUTS.get(key, (0.0, 0.0))
        if bucket_of(value, kind, cuts) == bucket:
            out.append(flag)
    return out


def attach_review_faces(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection | None,
    rows: list[Any],
    *,
    now: datetime,
) -> None:
    """
    今日页行内联复盘标注（api 层装配，duck 连接缺席/视图缺席降级零动作）。

    只对开球已过的行解析 srct 链取 gold 行——未开球场次判据未齐不标
    （PIT 诚实性，裁决①）。
    """
    past_sids: dict[int, str] = {}
    for row in rows:
        try:
            kickoff = datetime.fromisoformat(str(row.kickoff_utc))
        except ValueError:
            continue
        if kickoff > now:
            continue
        sid = mapping.srct_sid_for_fixture(conn, int(row.fixture_id))
        if sid is not None:
            past_sids[int(row.fixture_id)] = sid
    if not past_sids or duck_con is None:
        return
    try:
        gold = gold_reader.fetch_signal_rows_by_sids(
            duck_con, sorted(set(past_sids.values()))
        )
    except RuntimeError:
        return  # gold 视图缺席（未建桥）：复盘面静默降级，主表不受影响
    for row in rows:
        sid = past_sids.get(int(row.fixture_id))
        if sid is None:
            continue
        gold_row = gold.get(sid)
        if gold_row is not None:
            row.flags.extend(faces_for_gold_row(gold_row))
