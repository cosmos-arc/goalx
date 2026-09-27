"""跨源实体映射层测试（票 77：两步法物化、kickoff 校准、别名桥、审计）。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import duckdb
import pytest

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.ingest import clubelo
from goalx_backend.evaluation import clv
from goalx_backend.modelling import team_align
from goalx_backend.models import MatchCodeInput, Tier

KICKOFF_UTC = "2026-09-12T19:00:00+00:00"  # 北京 09-13 03:00


def seed_universe(
    rows: list[tuple[str, str, str, str]],
) -> duckdb.DuckDBPyConnection:
    """内存 duckdb 假 fixture_universe（映射投影只需四列）。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fixture_universe"
        " (sid VARCHAR, home VARCHAR, away VARCHAR, kickoff TIMESTAMP)"
    )
    for sid, home, away, kickoff in rows:
        con.execute(
            "INSERT INTO fixture_universe VALUES (?, ?, ?, ?::TIMESTAMP)",
            [sid, home, away, kickoff],
        )
    return con


def seed_fixture(
    db,
    *,
    home: str = "主队英超",
    away: str = "客队英超",
    kickoff: str = KICKOFF_UTC,
    league: str = "英超",
    tier: Tier = Tier.TIER1,
) -> int:
    competition = fx_store.upsert_competition(db, league, tier=tier)
    home_id = fx_store.upsert_team(db, home)
    away_id = fx_store.upsert_team(db, away)
    return fx_store.upsert_fixture(db, competition, kickoff, home_id, away_id)


# --- 两步确定性键解析（SidIndex） ---


def test_sid_index_primary_key_hit() -> None:
    """① 主客队名+北京日期唯一命中 → primary_key。"""
    index = mapping.SidIndex.build(
        mapping.load_universe(
            seed_universe([("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00")])
        )
    )
    res = index.resolve("主队英超", "客队英超", KICKOFF_UTC)
    assert res.sid == "2790001"
    assert res.method == mapping.METHOD_PRIMARY_KEY


def test_sid_index_kickoff_fallback_for_name_variant() -> None:
    """② 客队名变体不中 ① → kickoff 精确+主队精确兜底命中。"""
    index = mapping.SidIndex.build(
        mapping.load_universe(
            seed_universe(
                [("2790002", "主队英超", "客队英超变体", "2026-09-13 03:00:00")]
            )
        )
    )
    res = index.resolve("主队英超", "客队英超", KICKOFF_UTC)
    assert res.sid == "2790002"
    assert res.method == mapping.METHOD_KICKOFF_EXACT


def test_resolve_variants_links_naming_variant_fixture(db) -> None:
    """变体组解析：竞彩/源T 系统性命名变体（赫塔费/赫塔菲型）经别名接通。

    真树点测 2026-09-27 发现的单名两步法死锁：① 主队名变体不中、
    ② 又要求主队精确——注入 srct 别名组后仍是精确串匹配，确定性保持。
    """
    index = mapping.SidIndex.build(
        mapping.load_universe(
            seed_universe([("2790003", "赫塔菲", "马拉加", "2026-09-20 20:00:00")])
        )
    )
    # 无别名：两步全不中
    res = index.resolve("赫塔费", "马拉加", "2026-09-20T12:00:00+00:00")
    assert res.sid is None
    assert res.candidates == ()
    # 注入变体组（canonical + srct 别名）→ primary_key 命中并记 provenance
    res_v = index.resolve_variants(
        ("赫塔费", "赫塔菲"), ("马拉加",), "2026-09-20T12:00:00+00:00"
    )
    assert res_v.sid == "2790003"
    assert res_v.method == mapping.METHOD_PRIMARY_KEY
    assert res_v.home_variant == "赫塔菲"


def test_sync_uses_injected_team_aliases(db) -> None:
    """同步消费注入别名组：变体场落链 + meta 记变体路径（bootstrap 闭环）。"""
    fixture_id = seed_fixture(db, home="赫塔费", away="马拉加")
    duck_con = seed_universe([("2790003", "赫塔菲", "马拉加", "2026-09-13 03:00:00")])
    home_row = db.execute(
        "SELECT home_team_id FROM fixtures WHERE id = ?", (fixture_id,)
    ).fetchone()
    aliases = {int(home_row["home_team_id"]): {"赫塔菲"}}
    stats = mapping.sync_fixture_links(db, duck_con, team_aliases=aliases)
    assert stats.linked == 1
    assert {alias for _, alias in stats.srct_alias_pairs} == {"赫塔菲", "马拉加"}
    row = db.execute(
        "SELECT meta FROM source_match_links WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    assert json.loads(row["meta"])["home_variant"] == "赫塔菲"


def test_sid_index_ambiguous_and_none() -> None:
    """多候选且②步无唯一解=歧义（候选清单）；零候选=无。禁自信合并。

    注：① 步多候选但 fixture 开球与其中一行精确相等时，② 步确定性
    消歧为 kickoff_exact——同名同日不同开球是强区分键，不算歧义。
    """
    index = mapping.SidIndex.build(
        mapping.load_universe(
            seed_universe(
                [
                    ("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00"),
                    ("2790009", "主队英超", "客队英超", "2026-09-13 21:00:00"),
                ]
            )
        )
    )
    # fixture 开球 09-13 10:00Z=北京 18:00，与两行都不等 → 两步均无唯一解
    res = index.resolve("主队英超", "客队英超", "2026-09-13T10:00:00+00:00")
    assert res.sid is None
    assert res.candidates == ("2790001", "2790009")
    none = index.resolve("无关队", "另一队", KICKOFF_UTC)
    assert none.sid is None
    assert none.candidates == ()


# --- sync_fixture_links：物化 + kickoff canonical + 别名对 ---


def test_sync_links_and_alias_pairs(db) -> None:
    """命中落链（method=两步法路径）+ srct 别名对产出。"""
    fixture_id = seed_fixture(db)
    duck_con = seed_universe(
        [("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00")]
    )
    stats = mapping.sync_fixture_links(db, duck_con)
    assert stats.fixtures == 1
    assert stats.linked == 1
    assert stats.unmapped == 0
    assert {alias for _, alias in stats.srct_alias_pairs} == {"主队英超", "客队英超"}
    row = db.execute(
        "SELECT * FROM source_match_links WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    assert row["source_match_id"] == "2790001"
    assert row["method"] == mapping.METHOD_PRIMARY_KEY
    assert row["status"] == mapping.STATUS_LINKED
    assert mapping.srct_sid_for_fixture(db, fixture_id) == "2790001"


def test_sync_kickoff_aligned_to_universe(db) -> None:
    """kickoff canonical=源T：≤12h 漂移自动校准并留前值档案。"""
    fixture_id = seed_fixture(db, kickoff="2026-09-12T19:10:00+00:00")
    duck_con = seed_universe(
        [("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00")]
    )
    stats = mapping.sync_fixture_links(db, duck_con)
    assert stats.kickoff_aligned == 1
    assert fx_store.get_fixture(db, fixture_id)["kickoff_utc"] == (
        "2026-09-12T19:00:00+00:00"
    )
    row = db.execute(
        "SELECT meta FROM source_match_links WHERE fixture_id = ?", (fixture_id,)
    ).fetchone()
    meta = json.loads(row["meta"])
    assert meta["kickoff_previous"] == "2026-09-12T19:10:00+00:00"
    assert meta["kickoff_drift_seconds"] == -600


def test_sync_kickoff_large_drift_flagged_not_aligned(db) -> None:
    """超 12h 漂移只标记不硬改（改期/错链人工队列）。"""
    fixture_id = seed_fixture(db, kickoff=KICKOFF_UTC)
    duck_con = seed_universe(
        [("2790001", "主队英超", "客队英超", "2026-09-13 23:00:00")]  # 漂 20h
    )
    stats = mapping.sync_fixture_links(db, duck_con)
    assert stats.kickoff_aligned == 0
    assert stats.kickoff_conflicts == 1
    assert fx_store.get_fixture(db, fixture_id)["kickoff_utc"] == KICKOFF_UTC


def test_sync_ambiguous_records_candidates(db) -> None:
    """歧义落 ambiguous 行 + 候选进 meta（人工队列输入）。"""
    fixture_id = seed_fixture(db, kickoff="2026-09-13T10:00:00+00:00")
    duck_con = seed_universe(
        [
            ("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00"),
            ("2790009", "主队英超", "客队英超", "2026-09-13 21:00:00"),
        ]
    )
    stats = mapping.sync_fixture_links(db, duck_con)
    assert stats.ambiguous == 1
    assert mapping.srct_sid_for_fixture(db, fixture_id) is None
    row = db.execute(
        "SELECT status, meta FROM source_match_links WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()
    assert row["status"] == mapping.STATUS_AMBIGUOUS
    assert json.loads(row["meta"])["candidates"] == ["2790001", "2790009"]


def test_sync_manual_link_preserved(db) -> None:
    """manual 人工链永不被自动同步覆写。"""
    fixture_id = seed_fixture(db, home="人工链队")
    db.execute(
        """
        INSERT INTO source_match_links
            (fixture_id, source, source_match_id, method, status, meta, mapped_at)
        VALUES (?, 'srct', '9999999', 'manual', 'linked', '{}', '2026-09-27T00:00:00Z')
        """,
        (fixture_id,),
    )
    duck_con = seed_universe(
        [("2790001", "人工链队", "客队英超", "2026-09-13 03:00:00")]
    )
    mapping.sync_fixture_links(db, duck_con)
    row = db.execute(
        "SELECT source_match_id, method FROM source_match_links WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()
    assert row["source_match_id"] == "9999999"
    assert row["method"] == mapping.METHOD_MANUAL


def test_sync_manual_link_still_aligns_kickoff(db) -> None:
    """
    manual 链不豁免 kickoff canonical（评审修正：人工裁决的是身份，开球仍以源T 为准）。
    """
    fixture_id = seed_fixture(db, home="人工链队", kickoff="2026-09-12T19:10:00+00:00")
    db.execute(
        """
        INSERT INTO source_match_links
            (fixture_id, source, source_match_id, method, status, meta, mapped_at)
        VALUES (?, 'srct', '2790001', 'manual', 'linked', '{}', '2026-09-27T00:00:00Z')
        """,
        (fixture_id,),
    )
    duck_con = seed_universe(
        [("2790001", "人工链队", "客队英超", "2026-09-13 03:00:00")]
    )
    stats = mapping.sync_fixture_links(db, duck_con)
    assert stats.linked == 1
    assert stats.kickoff_aligned == 1
    assert fx_store.get_fixture(db, fixture_id)["kickoff_utc"] == (
        "2026-09-12T19:00:00+00:00"
    )
    # manual 行本体不被覆写（mapped_at/meta 原样）
    row = db.execute(
        "SELECT meta, mapped_at FROM source_match_links WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()
    assert row["meta"] == "{}"


def test_sync_unmapped_clears_stale_non_manual(db) -> None:
    """不再解析的场撤历史非 manual 链（镜像真值）。"""
    fixture_id = seed_fixture(db, home="消失队")
    duck_con = seed_universe([("2790001", "消失队", "客队英超", "2026-09-13 03:00:00")])
    mapping.sync_fixture_links(db, duck_con)
    assert mapping.srct_sid_for_fixture(db, fixture_id) == "2790001"
    empty = seed_universe([])  # silver 重建后该场不在 universe
    stats = mapping.sync_fixture_links(db, empty)
    assert stats.unmapped == 1
    assert mapping.srct_sid_for_fixture(db, fixture_id) is None


def test_sync_degraded_without_duck(db) -> None:
    """语料桥缺席 → 降级零动作（stats.degraded 置位）。"""
    seed_fixture(db)
    stats = mapping.sync_fixture_links(db, None)
    assert stats.degraded is not None
    assert stats.fixtures == 0
    assert (
        db.execute("SELECT COUNT(*) AS n FROM source_match_links").fetchone()["n"] == 0
    )


# --- kickoff overrides（jc_silver 冗余列改走映射的供给侧） ---


def test_srct_kickoff_overrides(db) -> None:
    """match_codes(jc matchId) × 物化链 × universe → matchId→源T 开球 ms。"""
    fixture_id = seed_fixture(db)
    duck_con = seed_universe(
        [("2790001", "主队英超", "客队英超", "2026-09-13 03:00:00")]
    )
    mapping.sync_fixture_links(db, duck_con)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date="2026-09-12",
            code="周六001",
            source_match_id="123456",
        ),
    )
    db.commit()
    overrides = mapping.srct_kickoff_overrides(db, duck_con)
    expected_ms = int(datetime(2026, 9, 12, 19, 0, tzinfo=UTC).timestamp() * 1000)
    assert overrides == {"123456": expected_ms}
    assert mapping.srct_kickoff_overrides(db, None) == {}


# --- audit ---


def test_audit_buckets_and_gate(db) -> None:
    """分桶未映射率 + tier1 起步门（<2%）+ 歧义人工队列。"""
    seed_fixture(db, home="甲队", away="乙队")
    seed_fixture(db, home="丙队", away="丁队", league="西甲")
    seed_fixture(db, home="戊队", away="己队", league="西甲")
    duck_con = seed_universe([("2790001", "甲队", "乙队", "2026-09-13 03:00:00")])
    mapping.sync_fixture_links(db, duck_con)
    report = mapping.audit_mapping(db)
    assert report["fixtures"] == 3
    assert report["linked"] == 1
    assert report["unmapped"] == 2
    assert report["per_competition"]["英超"]["unmapped_rate"] == 0.0
    assert report["per_competition"]["西甲"]["unmapped_rate"] == 1.0
    assert report["ambiguous_queue"] == []
    assert report["core_league_gate"]["pass"] is False


def test_audit_sync_pending_when_never_ran(db) -> None:
    """表空=同步未跑（sync_pending 置位，不伪装通过）。"""
    seed_fixture(db)
    report = mapping.audit_mapping(db)
    assert report["sync_pending"] is True
    assert report["core_league_gate"]["pass"] is False


def test_audit_ambiguous_queue_listing(db) -> None:
    """歧义队列逐条列出候选（人工裁决工作面）。"""
    seed_fixture(
        db, home="歧义队", away="客队英超", kickoff="2026-09-13T10:00:00+00:00"
    )
    duck_con = seed_universe(
        [
            ("2790001", "歧义队", "客队英超", "2026-09-13 03:00:00"),
            ("2790009", "歧义队", "客队英超", "2026-09-13 21:00:00"),
        ]
    )
    mapping.sync_fixture_links(db, duck_con)
    queue = mapping.audit_mapping(db)["ambiguous_queue"]
    assert len(queue) == 1
    assert queue[0]["candidates"] == ["2790001", "2790009"]


# --- clubelo 别名桥 + 点时查询（票 77 验收） ---


def _seed_elo(db, rows: list[dict[str, Any]]) -> None:
    written = clubelo.upsert_ratings(db, rows)
    assert written == len(rows)
    db.commit()


def test_clubelo_alias_bridge_and_point_in_time(db) -> None:
    """英文别名桥接通 + 点时 Elo 按中文 canonical 队名走通。"""
    home = fx_store.upsert_team(db, "曼城")
    team_align.record_odds_api_alias(db, home, "Manchester City")
    db.commit()
    _seed_elo(
        db,
        [
            {
                "club": "Manchester City",
                "country": "ENG",
                "level": 1,
                "elo": 1920.0,
                "valid_from": "2026-01-01",
                "valid_to": "2026-07-31",
            },
            {
                "club": "Manchester City",
                "country": "ENG",
                "level": 1,
                "elo": 1950.0,
                "valid_from": "2026-08-01",
                "valid_to": None,
            },
        ],
    )
    report = team_align.sync_clubelo_aliases(db, clubelo.distinct_clubs(db))
    assert report.matched == 1
    assert report.added == 1
    alias = team_align.clubelo_alias_for_team(db, home)
    assert alias == "Manchester City"
    # 点时语义：区间行覆盖查询日；开区间行覆盖至今
    assert clubelo.rating_at(db, "Manchester City", date(2026, 6, 1)) == 1920.0
    assert clubelo.rating_at(db, "Manchester City", date(2026, 9, 1)) == 1950.0
    assert clubelo.rating_at(db, "Manchester City", date(2025, 1, 1)) is None


def test_clubelo_alias_unmatched_reported(db) -> None:
    """别名未覆盖的俱乐部进未命中清单，不硬配。"""
    report = team_align.sync_clubelo_aliases(db, ["Nowhere FC"])
    assert report.clubs_total == 1
    assert report.matched == 0
    assert report.unmatched == ["Nowhere FC"]


# --- CLV 消费端切换：物化链快路（基线两步法语义不变） ---


def _duck_with_event() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fixture_universe"
        " (sid VARCHAR, home VARCHAR, away VARCHAR, kickoff TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO fixture_universe VALUES"
        " ('2790099', '源T主队名', '源T客队名', TIMESTAMP '2026-09-13 03:00:00')"
    )
    con.execute(
        "CREATE TABLE odds_change_event"
        " (sid VARCHAR, bookmaker_id VARCHAR, market VARCHAR,"
        " published_at TIMESTAMPTZ, odds_home DOUBLE, odds_draw DOUBLE,"
        " odds_away DOUBLE)"
    )
    con.execute(
        "INSERT INTO odds_change_event VALUES"
        " ('2790099', ?, '1x2', TIMESTAMPTZ '2026-09-12 05:00:00+08:00',"
        " 2.0, 3.5, 3.5)",
        [quote_evidence.SRCT_PINNACLE_BOOK],
    )
    return con


def test_clv_fast_path_via_link_sid(db) -> None:
    """manual 链 sid 直查收盘（队名与源T 全异、模糊两步必不中的场）。"""
    duck_con = _duck_with_event()
    fixture_id = seed_fixture(db)  # 主队英超/客队英超 ≠ 源T 名
    db.execute(
        """
        INSERT INTO source_match_links
            (fixture_id, source, source_match_id, method, status, meta, mapped_at)
        VALUES (?, 'srct', '2790099', 'manual', 'linked', '{}', '2026-09-27T00:00:00Z')
        """,
        (fixture_id,),
    )
    db.commit()
    sid = mapping.srct_sid_for_fixture(db, fixture_id)
    close = clv.srct_closing_prob(
        duck_con, "主队英超", "客队英超", KICKOFF_UTC, "h", sid=sid
    )
    assert close is not None
    prob, source, basis = close
    assert source == "srct_1x2_closing"
    assert basis == "srct_pinnacle"
    assert prob == pytest.approx(om.shin_implied((2.0, 3.5, 3.5))[0])


def test_clv_fuzzy_fallback_unchanged_without_link(db) -> None:
    """无链时兜底两步法语义不变（票 75 基线行为）。"""
    duck_con = _duck_with_event()
    close = clv.srct_closing_prob(duck_con, "源T主队名", "源T客队名", KICKOFF_UTC, "h")
    assert close is not None
    assert close[2] == "srct_pinnacle"
    no_match = clv.srct_closing_prob(duck_con, "主队英超", "客队英超", KICKOFF_UTC, "h")
    assert no_match is None
