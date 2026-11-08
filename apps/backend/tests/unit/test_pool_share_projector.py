"""份额投影器 P0（auto-paper 票 07）回归。

钉住：实测缺位 → JC 官方 had 去水隐含作公众份额先验（share_source=
projected 显式标注）；有实测 → 整场用实测不混装；无链/无价 → 诚实缺位；
票面估值（est_odds/EV）随投影恢复可算。
"""

from __future__ import annotations

import sqlite3

from goalx_backend import odds_math as om
from goalx_backend.betting.pool_strategy import ticket_view
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.models import MatchCodeInput

_AS_OF = "2026-11-02T08:00:00+00:00"
_POOL_KICKOFF = "2026-11-02T16:00:00+00:00"  # 官方源日期精度假时刻
_JC_ODDS = (2.0, 3.2, 3.8)  # 去水隐含 h/d/a（naive 归一）


def _seed(
    db: sqlite3.Connection,
    *,
    linked: bool = True,
    jc_odds: bool = True,
    tag: str = "a",
) -> int:
    comp = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "阿森纳" if tag == "a" else f"主队{tag}")
    away = fx_store.upsert_team(db, "切尔西" if tag == "a" else f"客队{tag}")
    fid = fx_store.upsert_fixture(db, comp, "2026-11-02T19:45:00+00:00", home, away)
    sid = f"204244{ord(tag) - 96}"  # 每个宇宙独立 source id（a=1,b=2…）
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid,
            kind="jingcai",
            business_date="2026-11-02",
            code=f"周一00{ord(tag) - 96}",
            source_match_id=sid if linked else None,
        ),
    )
    if jc_odds:
        for sel, odds in zip(("h", "d", "a"), _JC_ODDS, strict=True):
            db.execute(
                "INSERT INTO odds_snapshots (fixture_id, market_code,"
                " selection_code, source, odds, captured_at, observed_at, created_at)"
                " VALUES (?, 'had', ?, 'sporttery', ?, '2026-11-02T02:00:00+00:00',"
                " '2026-11-02T02:00:00+00:00', 'x')",
                (fid, sel, odds),
            )
    pid = pool_store.upsert_pool_period(
        db, "ttt14", f"2622{ord(tag) - 96}", "2026-11-02T14:00:00+00:00"
    )
    pool_store.replace_pool_matches(
        db,
        pid,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id=sid if linked else None,
                league="英超",
                kickoff_utc=_POOL_KICKOFF,
                home_team=str(home),
                away_team=str(away),
                euro_odds=(2.1, 3.4, 3.6),
            )
        ],
    )
    return pid


def test_projected_shares_fill_wdl(db: sqlite3.Connection) -> None:
    """无实测 → JC 隐含投影：份额/派彩/EV 可算且显式 projected。"""
    pid = _seed(db)
    views = pool_store.match_views(db, pid, _AS_OF)
    implied = om.normalized_implied(_JC_ODDS)
    by_code = {s.code: s for s in views[0].selections}
    assert set(by_code) == {"h", "d", "a"}
    for sel, prob in zip(("h", "d", "a"), implied, strict=True):
        view = by_code[sel]
        assert view.share == round(prob, 4)
        assert view.share_source == "projected"
        assert view.implied_odds == round(pool_store.POOL_RETURN_RATE / prob, 2)
        # prob=欧指去水（shin），EV 链随投影份额恢复可算
        devig = om.shin_implied((2.1, 3.4, 3.6))
        expect_ev = (
            devig[("h", "d", "a").index(sel)] * (pool_store.POOL_RETURN_RATE / prob) - 1
        )
        assert view.ev == round(expect_ev, 4)


def test_measured_shares_take_precedence(db: sqlite3.Connection) -> None:
    """有实测（源B 存量）→ 整场用实测，share_source=measured 不混装。"""
    pid = _seed(db)
    pool_store.insert_public_shares(
        db,
        pid,
        [pool_store.PoolShareInput(match_seq=1, shares={"3": 0.6, "1": 0.3, "0": 0.1})],
        origin="estimated",
        source="zucai_backfill",
        captured_at="2026-09-20T10:00:00+00:00",
    )
    views = pool_store.match_views(db, pid, _AS_OF)
    by_code = {s.code: s for s in views[0].selections}
    assert by_code["h"].share == 0.6
    assert by_code["h"].share_source == "measured"
    assert by_code["d"].share == 0.3
    assert by_code["a"].share == 0.1
    assert all(s.share_source == "measured" for s in views[0].selections)


def test_unlinked_or_oddsless_stays_none(db: sqlite3.Connection) -> None:
    """无链场 / 有链无 JC 价 → 份额缺位 share_source=none（不造半套）。"""
    pid = _seed(db, linked=False)
    views = pool_store.match_views(db, pid, _AS_OF)
    assert all(s.share is None for s in views[0].selections)
    assert all(s.share_source == "none" for s in views[0].selections)

    pid2 = _seed(db, linked=True, jc_odds=False, tag="b")
    views2 = pool_store.match_views(db, pid2, _AS_OF)
    assert all(s.share is None for s in views2[0].selections)
    assert all(s.share_source == "none" for s in views2[0].selections)


def test_projection_is_point_in_time(db: sqlite3.Connection) -> None:
    """as-of 纪律（review 发现）：晚于读面时点的调盘不漏进投影。"""
    pid = _seed(db, tag="c")  # observed_at=02:00，读面 01:00 早于观测
    early = pool_store.match_views(db, pid, "2026-11-02T01:00:00+00:00")
    assert all(s.share is None for s in early[0].selections)
    assert all(s.share_source == "none" for s in early[0].selections)
    later = pool_store.match_views(db, pid, _AS_OF)  # 08:00 已可见
    assert all(s.share_source == "projected" for s in later[0].selections)


def test_ticket_valuation_restored_by_projection(db: sqlite3.Connection) -> None:
    """票面估值恢复可估（票 07 验收）：投影份额下 est_odds/EV 非 None。"""
    pid = _seed(db)
    views = pool_store.match_views(db, pid, _AS_OF)
    ticket = ticket_view({1: "h"}, views)
    assert ticket.est_odds is not None
    assert ticket.ev is not None
    # 对照：无链场（P0 前常态）估值维持诚实缺位
    pid2 = _seed(db, linked=False, tag="b")
    views2 = pool_store.match_views(db, pid2, _AS_OF)
    assert ticket_view({1: "h"}, views2).est_odds is None
