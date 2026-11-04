"""价格存活检查点读数（report-audit-20261103 票 09 预注册）。

主指标=确认价 EV 中位数（共识参考 as-of 确认时刻）+ fixture 簇 bootstrap
95% CI；协报告=正候选率/价差存活(+5/15/30min 与收盘)/票 08 影子对表。
分母=锁定非 manual_research 纸面注的 had 腿；参考缺单列不冒充 0。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path

import pytest

from goalx_backend.betting.bets import (
    BetDraft,
    create_bet_with_legs,
    record_purchase,
    resolve_paper_price,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import price_edge as pe
from goalx_backend.models import (
    LegInput,
    MatchCodeInput,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

# 快照观测定死在"过去"（决策时刻是运行时钟 now，PIT 资格恒成立）
SNAP_AT = "2026-01-01T08:00:00+00:00"
_seq = count(1)


def _seed_fixture(
    conn: sqlite3.Connection, kickoff: str, jc_h: float, *, with_eu: bool = True
) -> int:
    """一场可决策的场子：JC 三向（h 价高于共识隐含=正候选）± 共识书。"""
    seq = next(_seq)
    competition = fx_store.upsert_competition(conn, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(conn, f"主队{seq}")
    away = fx_store.upsert_team(conn, f"客队{seq}")
    fixture_id = fx_store.upsert_fixture(conn, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        conn,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date=kickoff[:10],
            code=f"周六{seq:03d}",
        ),
    )
    for sel, price in (("h", jc_h), ("d", 3.4), ("a", 1.8)):
        fx_store.insert_odds_snapshot(
            conn,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="sporttery",
                odds=price,
                captured_at=SNAP_AT,
                observed_at=SNAP_AT,
                source_updated_at=SNAP_AT,
            ),
        )
    if with_eu:
        for book in ("pinnacle", "average"):
            for sel, price in (("h", 2.02), ("d", 3.38), ("a", 3.55)):
                fx_store.insert_odds_snapshot(
                    conn,
                    SnapshotInput(
                        fixture_id=fixture_id,
                        market_code="had",
                        selection_code=sel,
                        source=f"odds_api:{book}",
                        odds=price,
                        captured_at=SNAP_AT,
                        observed_at=SNAP_AT,
                        source_updated_at=SNAP_AT,
                    ),
                )
    conn.commit()
    return fixture_id


def _lock_paper_bet(conn: sqlite3.Connection, fixture_id: int, jc_h: float) -> int:
    """走完整决策链：建注（发现）→ 锁定（决策+确认）；返回 bet id。"""
    legs = [
        LegInput(
            fixture_id=fixture_id,
            market_code="had",
            selection_code="h",
            locked_odds=jc_h,
        )
    ]
    bound, source, _mismatches = resolve_paper_price(conn, legs)
    assert source == "system_paper"
    bet_id = create_bet_with_legs(
        conn, BetDraft(mode="paper", stake=2.0, legs=bound), price_source=source
    )
    record_purchase(conn, [bet_id])
    return bet_id


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _setup(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "price-edge.db")
    migrate(conn)
    return conn


def test_main_metric_and_survival(tmp_path: Path) -> None:
    """主指标中位数 EV 手算对表 + 存活 +5min/收盘留存（价未动=1.0）。"""
    conn = _setup(tmp_path)
    try:
        kickoff = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
        fixture_id = _seed_fixture(conn, kickoff, jc_h=2.6)
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code="had",
                sale_state="on_sale",
                single_eligible=True,
                observed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        bet_id = _lock_paper_bet(conn, fixture_id, jc_h=2.6)

        report = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        assert report["denominator"]["n_bets"] == 1
        assert report["denominator"]["n_legs"] == 1
        assert report["denominator"]["missing_reference"] == 0
        metric = report["main_metric"]
        # EV 手算对表：确认价 × as-of 共识 Shin 概率 − 1
        confirm_row = conn.execute(
            "SELECT odds, recorded_at FROM bet_price_timeline"
            " WHERE bet_id = ? AND stage = 'confirm'",
            (bet_id,),
        ).fetchone()
        probs = qe.eu_consensus_asof(conn, fixture_id, str(confirm_row["recorded_at"]))
        assert probs is not None
        expected = float(confirm_row["odds"]) * probs["h"] - 1.0
        assert metric["n_computable"] == 1
        assert metric["median_ev_confirm"] == pytest.approx(expected)
        assert metric["positive_candidate_rate"] == 1.0
        assert metric["n_effective_fixtures"] == 1
        # 单 fixture 簇：bootstrap 不出区间（<2 簇如实 None）
        assert metric["bootstrap95"] is None
        # 存活（票 02 成熟度）：报告时刻 discovery+偏移与开球（明日）均未到
        # → 全部 pending 不进可算分母，未到点与缺价/缺参考分开统计
        surv = report["survival"]
        assert surv["n_candidates"] == 1
        for offset in surv["offsets"]:
            assert offset["n_not_matured"] == 1
            assert offset["n_ev_computable"] == 0
            assert offset["n_missing_price_or_reference"] == 0
            assert offset["median_ev"] is None
            assert offset["median_retention"] is None
        close = surv["close"]
        assert close["n_not_matured"] == 1
        assert close["n_ev_computable"] == 0
        assert close["median_retention"] is None
        assert surv["median_half_life_offset_s"] is None
        # 协议面：单销售日 → provisional；判读不注入（correctness F2）；
        # 影子无行 → None 诚实
        assert report["protocol"]["sale_days"] == 1
        assert report["protocol"]["metric_status"] == "provisional"
        assert "route_verdict" not in metric
        assert report["stop_condition"]["supply_insufficient"] is False
        assert report["freshness_shadow"]["summary"] is None
    finally:
        conn.close()


def test_matured_faces_enter_denominator(tmp_path: Path) -> None:
    """票 02（报告一案例 7）：到点后进分母——discovery+偏移与开球均早于
    read_at 时可算、留存照读；售止腿价保留（售止前最后价）资格单列。"""
    conn = _setup(tmp_path)
    try:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        day = yesterday.strftime("%Y-%m-%d")
        kickoff = f"{day}T12:00:00+00:00"
        fixture_id = _seed_fixture(conn, kickoff, jc_h=2.6)
        bet_id = _lock_paper_bet(conn, fixture_id, jc_h=2.6)
        # 时间线回拨到昨日（discovery 10:00 → +5/15/30min 与开球均已到点）
        for stage, hh, mm in (
            ("discovery", 10, 0),
            ("decision", 10, 0),
            ("confirm", 10, 1),
        ):
            conn.execute(
                "UPDATE bet_price_timeline SET recorded_at = ?"
                " WHERE bet_id = ? AND stage = ?",
                (f"{day}T{hh:02d}:{mm:02d}:00+00:00", bet_id, stage),
            )
        # 10:10 停售观测：+15min 起观察时点已停售（价保留），+5min 未停
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code="had",
                sale_state="stopped",
                observed_at=f"{day}T10:10:00+00:00",
            ),
        )
        conn.commit()

        report = pe.build_price_edge_report(
            conn, date_from=day, date_to=day, duck_con=None
        )
        md = pe._render_markdown(report)
        assert "未到点" in md  # 新列头渲染
        assert "其中售止" in md
        assert report["read_at"] in md  # 读数时点可见
        surv = report["survival"]
        o5, o15, o30 = surv["offsets"]
        assert o5["n_not_matured"] == 0
        assert o5["n_ev_computable"] == 1
        assert o5["n_sale_stopped"] == 0
        assert o5["median_retention"] == pytest.approx(1.0)  # 价未动
        assert o15["n_sale_stopped"] == 1  # 到点已停售：资格单列
        assert o15["n_ev_computable"] == 1  # 价照读=售止前最后价
        assert o15["median_retention"] == pytest.approx(1.0)
        assert o30["n_sale_stopped"] == 1  # 30min 档同口径钉死
        close = surv["close"]
        assert close["n_not_matured"] == 0  # 开球已过 → 收盘面进分母
        assert close["n_ev_computable"] == 1
        assert close["n_sale_stopped"] == 1  # 收盘=售止前最后价（口径注记）
        assert close["median_retention"] == pytest.approx(1.0)
    finally:
        conn.close()


def test_missing_reference_counted_not_faked(tmp_path: Path) -> None:
    """共识缺的腿分母外单列 missing_reference，EV 不冒充、不进中位数。"""
    conn = _setup(tmp_path)
    try:
        base = datetime.now(UTC) + timedelta(days=2)
        with_eu = _seed_fixture(conn, base.isoformat(timespec="seconds"), jc_h=2.6)
        no_eu = _seed_fixture(
            conn,
            (base + timedelta(hours=5)).isoformat(timespec="seconds"),
            jc_h=2.6,
            with_eu=False,
        )
        _lock_paper_bet(conn, with_eu, jc_h=2.6)
        _lock_paper_bet(conn, no_eu, jc_h=2.6)

        report = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        assert report["denominator"]["n_legs"] == 2
        assert report["denominator"]["missing_reference"] == 1
        assert report["main_metric"]["n_computable"] == 1
    finally:
        conn.close()


def test_manual_research_excluded_from_denominator(tmp_path: Path) -> None:
    """manual_research 隔离出分母（is_official_paper 消费侧判据）。"""
    conn = _setup(tmp_path)
    try:
        kickoff = (datetime.now(UTC) + timedelta(days=3)).isoformat(timespec="seconds")
        fixture_id = _seed_fixture(conn, kickoff, jc_h=2.6)
        legs = [
            LegInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code="h",
                locked_odds=9.9,  # 客户端价≠服务器价 → manual 通道
            )
        ]
        bound, source, mismatches = resolve_paper_price(conn, legs)
        assert source == "manual_research"
        assert mismatches
        bet_id = create_bet_with_legs(
            conn, BetDraft(mode="paper", stake=2.0, legs=bound), price_source=source
        )
        record_purchase(conn, [bet_id])

        report = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        assert report["denominator"]["n_bets"] == 0
        assert report["main_metric"]["n_computable"] == 0
        assert report["main_metric"]["median_ev_confirm"] is None
    finally:
        conn.close()


def test_null_price_source_bet_included(tmp_path: Path) -> None:
    """历史 price_source NULL 不回溯排除（裁决③）：IS NOT 惯用法钉死。"""
    conn = _setup(tmp_path)
    try:
        kickoff = (datetime.now(UTC) + timedelta(days=6)).isoformat(timespec="seconds")
        fixture_id = _seed_fixture(conn, kickoff, jc_h=2.6)
        legs = [
            LegInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code="h",
                locked_odds=2.6,
            )
        ]
        # 内部调用不带标签（旧行为）：price_source 落 NULL，仍属正式纸面面
        bet_id = create_bet_with_legs(
            conn, BetDraft(mode="paper", stake=2.0, legs=legs)
        )
        record_purchase(conn, [bet_id])
        source = conn.execute(
            "SELECT price_source FROM bets WHERE id = ?", (bet_id,)
        ).fetchone()
        assert source["price_source"] is None

        report = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        assert report["denominator"]["n_bets"] == 1
        assert report["main_metric"]["n_computable"] == 1
    finally:
        conn.close()


def test_bootstrap_two_fixture_clusters(tmp_path: Path) -> None:
    """≥2 fixture 簇出 bootstrap 区间（种子固定=重跑同值）。"""
    conn = _setup(tmp_path)
    try:
        base = datetime.now(UTC) + timedelta(days=4)
        f1 = _seed_fixture(conn, base.isoformat(timespec="seconds"), jc_h=2.6)
        f2 = _seed_fixture(
            conn,
            (base + timedelta(hours=5)).isoformat(timespec="seconds"),
            jc_h=2.4,
        )
        _lock_paper_bet(conn, f1, jc_h=2.6)
        _lock_paper_bet(conn, f2, jc_h=2.4)

        first = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        second = pe.build_price_edge_report(
            conn, date_from=_today(), date_to=_today(), duck_con=None
        )
        ci = first["main_metric"]["bootstrap95"]
        assert ci is not None
        lo, hi = ci
        assert lo <= first["main_metric"]["median_ev_confirm"] <= hi
        # 种子固定 → 两次构建区间逐位一致（可复现，prereg §4）
        assert ci == second["main_metric"]["bootstrap95"]
    finally:
        conn.close()


def test_supply_insufficient_stop_condition(tmp_path: Path) -> None:
    """停止条件：销售日满门槛而可算腿 <30 → 供给不足标记；票 02 判读
    优先级——供给不足归档时不得同时输出路线升级判词（描述性 EV/CI 照常）。"""
    conn = _setup(tmp_path)
    try:
        base = datetime.now(UTC) + timedelta(days=5)
        fixture_id = _seed_fixture(conn, base.isoformat(timespec="seconds"), jc_h=2.6)
        f2 = _seed_fixture(
            conn, (base + timedelta(hours=5)).isoformat(timespec="seconds"), jc_h=2.4
        )
        _lock_paper_bet(conn, fixture_id, jc_h=2.6)
        _lock_paper_bet(conn, f2, jc_h=2.4)
        # 抬门槛面：1 月逐日在售观测（分母供给不动）
        for day in range(30):
            fx_store.append_sale_status(
                conn,
                SaleStatusInput(
                    fixture_id=fixture_id,
                    market_code="had",
                    sale_state="on_sale",
                    single_eligible=None,
                    observed_at=f"2026-01-{day + 1:02d}T10:00:00+00:00",
                ),
            )
        conn.commit()
        report = pe.build_price_edge_report(
            conn, date_from="2026-01-01", date_to=_today(), duck_con=None
        )
        assert report["protocol"]["sale_days"] >= 30
        assert report["protocol"]["metric_status"] == "formal_ready"
        assert report["stop_condition"]["supply_insufficient"] is True
        metric = report["main_metric"]
        assert metric["n_computable"] == 2  # 可算腿 2 < 30 → 供给不足
        assert metric["bootstrap95"] is not None  # 描述性区间照常展示
        assert metric["bootstrap95"][0] > 0.0  # 修复前会同时输出"升级裁决"
        assert "route_verdict" not in metric  # 票 02：停止条件优先，不出判词
        assert "三向判读" not in pe._render_markdown(report)  # MD 同守卫
    finally:
        conn.close()


def test_sale_stop_market_level_precedence_pinned(tmp_path: Path) -> None:
    """票 02 correctness F1：销售资格读取共用 latest_sale_status_asof 的
    盘口级优先序——更晚的赛事级 stopped 被更早的 had 级 on_sale 遮蔽
    （低估停售），现行为钉死；频发再按 shortcut 注记升级口径。"""
    conn = _setup(tmp_path)
    try:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        day = yesterday.strftime("%Y-%m-%d")
        fixture_id = _seed_fixture(conn, f"{day}T12:00:00+00:00", jc_h=2.6)
        bet_id = _lock_paper_bet(conn, fixture_id, jc_h=2.6)
        for stage, hh, mm in (
            ("discovery", 10, 0),
            ("decision", 10, 0),
            ("confirm", 10, 1),
        ):
            conn.execute(
                "UPDATE bet_price_timeline SET recorded_at = ?"
                " WHERE bet_id = ? AND stage = ?",
                (f"{day}T{hh:02d}:{mm:02d}:00+00:00", bet_id, stage),
            )
        fx_store.append_sale_status(  # had 级在售（更早）
            conn,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code="had",
                sale_state="on_sale",
                observed_at=f"{day}T10:00:00+00:00",
            ),
        )
        fx_store.append_sale_status(  # 赛事级停售（更晚，回退探测路径）
            conn,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code=None,
                sale_state="stopped",
                observed_at=f"{day}T10:10:00+00:00",
            ),
        )
        conn.commit()
        report = pe.build_price_edge_report(
            conn, date_from=day, date_to=day, duck_con=None
        )
        o15 = report["survival"]["offsets"][1]
        assert o15["n_ev_computable"] == 1  # 价照读
        assert o15["n_sale_stopped"] == 0  # 盘口级优先序：遮蔽生效（钉死）
    finally:
        conn.close()
