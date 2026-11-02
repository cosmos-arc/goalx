"""池映射 id 直连（pool-mapping-goals4/01）回归。

钉住的死法：官方池 kickoff 只有日期精度（北京当日 00:00），名字+±2h 时间容差
路径对晚场（开球 >02:00 北京）系统性 miss——26156 期 5/8 头部晚场全灭。
"""

from __future__ import annotations

import sqlite3

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.models import MatchCodeInput

# 真实 26156 期缩影：纽卡晚场（北京 04:00 开球，id 直连才救得回）+
# 图尔库凌晨场（00:00 开球，名+窗路径本来就命中）。
_NIGHT_KICKOFF_UTC = "2026-11-02T20:00:00+00:00"  # 北京 11-03 04:00
_NIGHT_POOL_KICKOFF = "2026-11-02T16:00:00+00:00"  # 日期精度假时刻（北京零点）
_EARLY_KICKOFF_UTC = "2026-11-02T16:00:00+00:00"  # 北京 11-03 00:00（真=假）


def _seed_jc_match(
    db: sqlite3.Connection, code: str, kickoff_utc: str, home: str, away: str, sid: str
) -> int:
    comp = fx_store.upsert_competition(db, "英超")
    h = fx_store.upsert_team(db, home)
    a = fx_store.upsert_team(db, away)
    fid = fx_store.upsert_fixture(db, comp, kickoff_utc, h, a)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid,
            kind="jingcai",
            business_date="2026-11-02",
            code=code,
            source_match_id=sid,
        ),
    )
    return fid


def _seed_period(
    db: sqlite3.Connection, matches: list[pool_store.PoolMatchInput]
) -> int:
    pid = pool_store.upsert_pool_period(
        db, "ttt14", "26156", "2026-11-02T14:00:00+00:00"
    )
    pool_store.replace_pool_matches(db, pid, matches)
    return pid


def test_night_match_maps_via_source_match_id(db: sqlite3.Connection) -> None:
    """晚场（修复前 miss）经 id 直连命中。"""
    fid = _seed_jc_match(
        db, "周一006", _NIGHT_KICKOFF_UTC, "纽卡斯尔联", "埃弗顿", "2042443"
    )
    got = pool_store.match_fixture_id(
        db,
        _NIGHT_POOL_KICKOFF,
        "纽卡斯尔联",
        "埃弗顿",
        source_match_id="2042443",
    )
    assert got == fid


def test_night_match_without_id_stays_unmapped(db: sqlite3.Connection) -> None:
    """无 id 的晚场：名+窗路径仍 miss（原行为不变——修复不粉饰坏映射）。"""
    _seed_jc_match(db, "周一006", _NIGHT_KICKOFF_UTC, "纽卡斯尔联", "埃弗顿", "2042443")
    assert (
        pool_store.match_fixture_id(
            db, _NIGHT_POOL_KICKOFF, "纽卡斯尔联", "埃弗顿", source_match_id=None
        )
        is None
    )


def test_early_match_maps_by_name_window_without_id(db: sqlite3.Connection) -> None:
    """凌晨场（假时刻=真时刻）无 id 仍由名+窗命中——回落路径未退化。"""
    fid = _seed_jc_match(
        db, "周一001", _EARLY_KICKOFF_UTC, "国际图尔库", "赫尔辛基火花", "2042438"
    )
    got = pool_store.match_fixture_id(
        db, _EARLY_KICKOFF_UTC, "国际图尔库", "赫尔辛基火花", source_match_id=None
    )
    assert got == fid


def test_ambiguous_id_falls_back(db: sqlite3.Connection) -> None:
    """同 id 双 fixture=歧义不猜：回落名+窗（晚场即 None）。"""
    _seed_jc_match(db, "周一006", _NIGHT_KICKOFF_UTC, "纽卡斯尔联", "埃弗顿", "2042443")
    _seed_jc_match(db, "周一00X", _NIGHT_KICKOFF_UTC, "阿森纳", "热刺", "2042443")
    assert (
        pool_store.match_fixture_id(
            db, _NIGHT_POOL_KICKOFF, "纽卡斯尔联", "埃弗顿", source_match_id="2042443"
        )
        is None
    )


def test_id_date_guard_falls_back(db: sqlite3.Connection) -> None:
    """id 命中但开球日差 >2 天（脏 id/跨季）回落名+窗路径。"""
    _seed_jc_match(
        db, "周一006", "2026-11-20T20:00:00+00:00", "纽卡斯尔联", "埃弗顿", "2042443"
    )
    assert (
        pool_store.match_fixture_id(
            db, _NIGHT_POOL_KICKOFF, "纽卡斯尔联", "埃弗顿", source_match_id="2042443"
        )
        is None
    )


def test_period_rows_bridge_night_match(db: sqlite3.Connection) -> None:
    """端到端：期次骨架（详情/证据摘要共用）对晚场行给出 fixture_id。"""
    fid = _seed_jc_match(
        db, "周一006", _NIGHT_KICKOFF_UTC, "纽卡斯尔联", "埃弗顿", "2042443"
    )
    pid = _seed_period(
        db,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id="2042443",
                league="英超",
                kickoff_utc=_NIGHT_POOL_KICKOFF,
                home_team="纽卡斯尔联",
                away_team="埃弗顿",
                euro_odds=(None, None, None),
            )
        ],
    )
    pairs = pool_store.match_rows_with_fixture_ids(db, pid)
    assert pairs[0][1] == fid
