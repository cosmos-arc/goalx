"""陈盘信号测试（票 48）：纯派生 + as-of 重放确定性 + 参考取法降级。"""

from __future__ import annotations

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.models import SnapshotInput


def _seed_fixture(db) -> int:
    competition = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, "主队S")
    away = fx_store.upsert_team(db, "客队S")
    return fx_store.upsert_fixture(
        db, competition, "2026-09-21T19:00:00+00:00", home, away
    )


def _snap(
    db,
    fixture_id: int,
    source: str,
    captured_at: str,
    odds: tuple[float, float, float],
) -> None:
    for sel, price in zip(("h", "d", "a"), odds, strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source=source,
                odds=price,
                captured_at=captured_at,
                observed_at=captured_at,
            ),
        )


def test_signal_none_without_jingcai_rows(db) -> None:
    fixture_id = _seed_fixture(db)
    assert (
        qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00") is None
    )


def test_signal_staleness_and_pinnacle_drift(db) -> None:
    fixture_id = _seed_fixture(db)
    # 竞彩：两次调盘，最后一次 15:00（as_of 18:00 → 距 180 分钟）
    _snap(db, fixture_id, "sporttery", "2026-09-20T10:00:00+00:00", (2.0, 3.2, 3.8))
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    # pinnacle：定格时刻 15:00 与 as_of 18:00 各一组（客胜概率上行）
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T14:00:00+00:00",
        (2.5, 3.4, 2.8),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T17:30:00+00:00",
        (2.9, 3.4, 2.4),
    )
    signal = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert signal is not None
    assert signal.jc_last_move == "2026-09-21T15:00:00+00:00"
    assert signal.minutes_since_move == 180
    assert signal.sharp_ref == "pinnacle"
    assert signal.drift_selection == "a"  # 2.8→2.4 概率上行最大
    assert signal.drift is not None
    assert signal.drift > 0
    # 重放确定性（验收项）：同 as_of 重算恒同值
    again = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert again == signal


def test_signal_falls_back_to_consensus_and_degrades(db) -> None:
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    # 无 pinnacle：多 book 共识兜底（双端同法）
    _snap(
        db,
        fixture_id,
        "odds_api:unibet_eu",
        "2026-09-21T14:00:00+00:00",
        (2.4, 3.3, 3.0),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:winamax_fr",
        "2026-09-21T14:00:00+00:00",
        (2.5, 3.2, 3.0),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:unibet_eu",
        "2026-09-21T17:00:00+00:00",
        (2.4, 3.3, 3.0),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:winamax_fr",
        "2026-09-21T17:00:00+00:00",
        (2.6, 3.2, 2.9),
    )
    signal = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert signal is not None
    assert signal.sharp_ref == "eu_consensus[odds_api:unibet_eu,odds_api:winamax_fr]"
    assert signal.drift is not None

    # as_of 在欧赔更新前：双端同取 14:00 共识 → 漂移 0（sharp 未动，真信息）
    early = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T15:30:00+00:00")
    assert early is not None
    assert early.minutes_since_move == 30
    assert early.drift == 0.0
    assert early.sharp_ref == "eu_consensus[odds_api:unibet_eu,odds_api:winamax_fr]"


def test_signal_no_sharp_at_freeze_time_only_staleness(db) -> None:
    """定格时刻无任何欧赔（欧赔晚于竞彩调盘才来）→ 只报时长，不冒充漂移。"""
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T17:30:00+00:00",
        (2.9, 3.4, 2.4),
    )
    signal = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert signal is not None
    assert signal.minutes_since_move == 180
    assert signal.drift is None
    assert signal.sharp_ref is None


def test_signal_method_mismatch_no_drift(db) -> None:
    """主锚中途出现（定格端无 pinnacle、决策端有）→ 双端取法不一致不报漂移。"""
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    _snap(
        db,
        fixture_id,
        "odds_api:unibet_eu",
        "2026-09-21T14:00:00+00:00",
        (2.4, 3.3, 3.0),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T17:30:00+00:00",
        (2.9, 3.4, 2.4),
    )
    signal = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert signal is not None
    assert signal.drift is None


# ---- 票 19：corpus as-of 注入缝（S4 陈盘观测接线） ----


def _replay_rows(
    book: str, observed_at: str, odds: tuple[float, float, float]
) -> list[dict[str, object]]:
    """一个时点的青铜重放伪行组（ingest.srct_asof 产出行同形）。"""
    return [
        {
            "id": seq,
            "source": book,
            "selection_code": sel,
            "odds": price,
            "captured_at": observed_at,
            "observed_at": observed_at,
            "source_updated_at": observed_at,
        }
        for seq, (sel, price) in enumerate(zip(("h", "d", "a"), odds, strict=True))
    ]


def test_signal_asof_resolver_feeds_both_endpoints(db) -> None:
    """
    票 19：resolver 在场时定格端与决策端都走重放行（silver 最新视图上
    历史时点会"消失"——账本 §4.1#7 的 0 有分辨根因）；两端同书同法，
    漂移可算且身份=重放行书 id。
    """
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    anchor = "srct:1x2:177"
    by_endpoint = {
        "2026-09-21T15:00:00+00:00": _replay_rows(
            anchor, "2026-09-21T14:00:00+00:00", (2.5, 3.4, 2.8)
        ),
        "2026-09-21T18:00:00+00:00": _replay_rows(
            anchor, "2026-09-21T17:30:00+00:00", (2.9, 3.4, 2.4)
        ),
    }
    calls: list[tuple[str, str]] = []

    def resolver(sid: str, as_of: str) -> list[dict[str, object]] | None:
        calls.append((sid, as_of))
        return by_endpoint.get(as_of)

    signal = qe.stale_line_signal(
        db,
        fixture_id,
        as_of="2026-09-21T18:00:00+00:00",
        sid="95001",
        asof_resolver=resolver,
    )
    assert signal is not None
    # 定格端（jc_last_move）与决策端（as_of）都经 resolver
    assert calls == [
        ("95001", "2026-09-21T15:00:00+00:00"),
        ("95001", "2026-09-21T18:00:00+00:00"),
    ]
    assert signal.sharp_ref == anchor
    assert signal.drift_selection == "a"
    assert signal.drift is not None
    assert signal.drift > 0


def test_signal_asof_resolver_none_falls_back_untouched(db) -> None:
    """resolver 判不适用（None）→ 该端走既有行源；整体行为与无 resolver 一致。"""
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T14:00:00+00:00",
        (2.5, 3.4, 2.8),
    )
    _snap(
        db,
        fixture_id,
        "odds_api:pinnacle",
        "2026-09-21T17:30:00+00:00",
        (2.9, 3.4, 2.4),
    )

    def always_none(sid: str, as_of: str) -> list[dict[str, object]] | None:
        return None

    with_resolver = qe.stale_line_signal(
        db,
        fixture_id,
        as_of="2026-09-21T18:00:00+00:00",
        sid="95001",
        asof_resolver=always_none,
    )
    without = qe.stale_line_signal(db, fixture_id, as_of="2026-09-21T18:00:00+00:00")
    assert with_resolver is not None
    assert with_resolver == without  # None=不适用：回退既有路径，读数不因缝在场而变


def test_signal_asof_resolver_frozen_age_gate_applies(db) -> None:
    """
    定格端 12h 新鲜度闸对重放行同样生效：定格参考过旧（observed 早于
    jc_last_move 超 12h）→ 定格端空 → 只报时长不冒充漂移。
    """
    fixture_id = _seed_fixture(db)
    _snap(db, fixture_id, "sporttery", "2026-09-21T15:00:00+00:00", (2.2, 3.1, 3.5))
    anchor = "srct:1x2:177"

    def resolver(sid: str, as_of: str) -> list[dict[str, object]] | None:
        if as_of == "2026-09-21T15:00:00+00:00":
            return _replay_rows(anchor, "2026-09-20T01:00:00+00:00", (2.5, 3.4, 2.8))
        return _replay_rows(anchor, "2026-09-21T17:30:00+00:00", (2.9, 3.4, 2.4))

    signal = qe.stale_line_signal(
        db,
        fixture_id,
        as_of="2026-09-21T18:00:00+00:00",
        sid="95001",
        asof_resolver=resolver,
    )
    assert signal is not None
    assert signal.minutes_since_move == 180
    assert signal.drift is None  # 冻结端参考被 12h 闸剔掉：漂移不可比
    assert signal.sharp_ref is None
