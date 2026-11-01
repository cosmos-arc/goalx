"""
错价桶只读展示面（misprice-bucket-display 票 02，裁决①=c 双件）。

十年回测的真错价桶（signal-attribution run5，|t|≥2 且 n≥100 过探索纪律
后 7 桶面）：本模块把其中判据可平移的面投到今日页——复盘面（已完赛，
gold 行收盘时态判据天然齐）+ 临场态投影（未开球，as-of now 判据逐步
变齐）——追加只读 flags，不进 had 闸门/EV/候选/bets 任何决策路径
（同票 01 纪律）。

口径（裁决②）：桶判据与特征计算平移自 signal_attribution 单一出处
（features_of），**效应量/t 值不可搬**——live 文案只写"十年聚合发现"。
临场面把 as-of now 的锚书三向合成 gold 行同形（close1x2=now 现价、
q1h/q24h=锚窗末价），经同一 features_of 判据——现价随临场推进可再动，
亮标≠收盘判据（词条 caution 已注投影态）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

import duckdb
from loguru import logger

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import gold_reader, mapping, quote_evidence
from goalx_backend.evaluation.signal_attribution import bucket_of, features_of

# 上线面=判据严格平移的符号类（devig 概率位移，无量纲）。书数族
# （ah_phase_pre:low/mid、ah_phase_inplay:high）因采集密度量纲漂移撤下
# （十月 ah_n_inplay q67=1437 vs 十年冻结 214——切点对今日失真，复盘冒烟
# 实证）；ou_line_drift 输入 market_quote 曾停摆月余（票 03④ 根因：
# remat 不覆盖该 silver，gold 对 10 月全缺），票 03① gold-rebuild 日频
# 保鲜后随重校裁决（票 03③）一并定。三面待增量 2 重校（滚动分位/
# 密度归一）后另裁。
SIGNIFICANT_FACES: tuple[tuple[str, str, str, str], ...] = (
    ("late_move_h:pos", "late_move_h", "drift", "pos"),
    ("late_move24_h:pos", "late_move24_h", "drift", "pos"),
    ("late_move24_h:neg", "late_move24_h", "drift", "neg"),
)

# 临场锚窗（与 gold._anchor_slices 同语义：开球前 1h/24h 末价切点）
_1H = timedelta(hours=1)
_24H = timedelta(hours=24)
# 切点序=(kickoff-1h, kickoff-24h, now)——读口返回按同序对齐的三向列表
_LIVE_CUT_COUNT = 3


def faces_for_gold_row(gold_row: dict[str, Any]) -> list[str]:
    """Gold 信号行 → 命中的显著桶面 flag 列表（特征缺列不判，空≠无）。"""
    features = features_of(gold_row)
    out: list[str] = []
    for flag, key, kind, bucket in SIGNIFICANT_FACES:
        if kind != "drift":
            # 防回潮守卫：dist 面须随增量 2 的重校切点一起回归（滚动分位/
            # 密度归一），裸加元组会按 (0,0) 静默错分桶（量纲 bug 复发形态）
            raise ValueError(f"面 {flag} 非符号类，缺重校切点（增量 2 义务）")
        value = features.get(key)
        if value is None:
            continue
        if bucket_of(value, kind, (0.0, 0.0)) == bucket:
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
        # kickoff 口径单一真相源（caliber-loop 票 01）：v1 legacy 北京墙钟
        # naive 串由属主转换，坏串 None 跳过——不手搓第二份解析
        kickoff = fx_store.kickoff_to_utc(str(row.kickoff_utc))
        if kickoff is None or kickoff > now:
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
    hit = 0
    for row in rows:
        sid = past_sids.get(int(row.fixture_id))
        if sid is None:
            continue
        gold_row = gold.get(sid)
        if gold_row is not None:
            row.flags.extend(faces_for_gold_row(gold_row))
            hit += 1
    # 复盘面观测（空≠无 F2）：gold 缺席行与"不在桶"零 flag 同形——计数
    # 进 serve 日志供晨检 grep，长期 gold-miss=past_sids 全量即重建断线
    logger.debug(
        "错价桶复盘：rows={} 已开球已链={} gold命中={}",
        len(rows),
        len(past_sids),
        hit,
    )


def _synthetic_gold_row(
    tri: list[tuple[float, float, float] | None], kickoff: datetime, now: datetime
) -> dict[str, Any] | None:
    """
    切点三向（q1h 位/q24h 位/now 位，读口按传入切点序对齐）→ gold 行同形。

    锚窗未过的档不进合成行（该面判据未齐）；锚数据缺席同缺列（空≠无）；
    now 位无价 → None（无现价可言，整场让位）。
    """
    q1h, q24h, now_q = tri if len(tri) == _LIVE_CUT_COUNT else (None, None, None)
    if now_q is None:
        return None
    row: dict[str, Any] = {
        "close1x2_h": now_q[0],
        "close1x2_d": now_q[1],
        "close1x2_a": now_q[2],
    }
    if q1h is not None and now >= kickoff - _1H:
        row.update({"q1h1x2_h": q1h[0], "q1h1x2_d": q1h[1], "q1h1x2_a": q1h[2]})
    if q24h is not None and now >= kickoff - _24H:
        row.update({"q24h1x2_h": q24h[0], "q24h1x2_d": q24h[1], "q24h1x2_a": q24h[2]})
    return row


def attach_live_faces(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection | None,
    rows: list[Any],
    *,
    now: datetime,
) -> None:
    """
    今日页行内联临场态投影（裁决①=c 增量 2，api 层装配）。

    只对未开球且已链源T 的行计算：锚书三向按 as-of now（close1x2 位）与
    锚窗末价（q1h/q24h 位）合成 gold 行同形，经同一 features_of 判据亮
    "当前在桶"（读口=quote_evidence.srct_anchor_triplets_at_cutoffs，
    ADR-0008 SQL 归 data 属主）。锚未到=该面判据未齐不标（PIT：锚窗末价
    在锚时刻前仍在变）；duck 连接/查询缺席降级零动作。
    """
    live: dict[int, tuple[str, datetime]] = {}
    for row in rows:
        kickoff = fx_store.kickoff_to_utc(str(row.kickoff_utc))
        if kickoff is None or kickoff <= now:
            continue
        sid = mapping.srct_sid_for_fixture(conn, int(row.fixture_id))
        if sid is not None:
            live[int(row.fixture_id)] = (sid, kickoff)
    if not live or duck_con is None:
        return
    # 逐行各窗（correctness F1）：共享链不同开球时刻的行不合并——锚窗跟
    # 本行 kickoff；切点序=(kickoff-1h, kickoff-24h, now)，返回按序对齐
    order = list(live.items())
    results = quote_evidence.srct_anchor_triplets_at_cutoffs(
        duck_con,
        [(sid, (kickoff - _1H, kickoff - _24H, now)) for _fid, (sid, kickoff) in order],
    )
    by_fid = {fid: results[i] for i, (fid, _bound) in enumerate(order)}
    hit = 0
    for row in rows:
        tri = by_fid.get(int(row.fixture_id))
        if tri is None:
            continue
        synthetic = _synthetic_gold_row(tri, live[int(row.fixture_id)][1], now)
        if synthetic is None:
            continue
        flags = faces_for_gold_row(synthetic)
        if flags:
            row.flags.extend(flags)
            hit += 1
    # 临场面观测（空≠无）：与复盘面同款计数进 serve 日志（now 位有价=在价）
    priced = sum(1 for r in results if len(r) == _LIVE_CUT_COUNT and r[2] is not None)
    logger.debug(
        "错价桶临场：rows={} 未开球已链={} 锚书在价={} 亮标={}",
        len(rows),
        len(live),
        priced,
        hit,
    )
