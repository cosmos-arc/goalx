"""球队名对齐层测试（票 25 验收：覆盖率报告、人工覆盖即时生效、幂等）。"""

from __future__ import annotations

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.modelling import team_align as ta


def seed_teams_with_aliases(db, entries: list[tuple[str, list[tuple[str, str]]]]):
    """建队 + 别名 (source, alias)；返回 canonical name -> team_id。"""
    ids = {}
    for canonical, aliases in entries:
        team_id = fx_store.upsert_team(db, canonical)
        ids[canonical] = team_id
        for source, alias in aliases:
            db.execute(
                "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
                " VALUES (?, ?, ?)",
                (team_id, source, alias),
            )
    db.commit()
    return ids


def test_normalize_strips_accents_legal_tokens_and_digits() -> None:
    assert ta.normalize_team_name("Borussia Mönchengladbach") == (
        "borussia monchengladbach"
    )
    assert ta.normalize_team_name("FC Koln") == "koln"
    assert ta.normalize_team_name("Mainz 05") == "mainz"
    assert ta.normalize_team_name("AS Roma") == "roma"
    assert ta.normalize_team_name("Tottenham Hotspur") == "tottenham hotspur"


def test_normalize_expands_fd_abbreviations() -> None:
    assert ta.normalize_team_name("Man United") == "manchester united"
    assert ta.normalize_team_name("Man City") == "manchester city"
    assert ta.normalize_team_name("Nott'm Forest") == "nottingham forest"
    assert ta.normalize_team_name("Wolves") == "wolverhampton"
    assert ta.normalize_team_name("Ath Madrid") == "atletico madrid"
    assert ta.normalize_team_name("Paris SG") == "paris saint germain"
    assert ta.normalize_team_name("Ein Frankfurt") == "eintracht frankfurt"


def test_index_exact_and_subset_matching() -> None:
    index = ta.NameIndex.build(
        {
            "Manchester United": 1,
            "Manchester City": 2,
            "Real Madrid": 3,
            "Inter Milan": 4,
            "AC Milan": 5,
        }
    )
    # 精确/规范化等价
    assert index.resolve("Manchester United") == 1
    assert index.resolve("Man United") == 1
    # 词元子集唯一命中：Valladolid ⊂ Real Valladolid 不在池中时无命中
    assert index.resolve("Valladolid") is None
    # Milan：exact 命中 AC Milan（不被 Inter Milan 抢走）
    assert index.resolve("Milan") == 5
    # Inter：子集唯一命中 Inter Milan
    assert index.resolve("Inter") == 4
    # Real：Real Madrid 与（假想的）另一 Real X 歧义 → None
    ambiguous = ta.NameIndex.build({"Real Madrid": 3, "Real Betis": 6})
    assert ambiguous.resolve("Real") is None


def test_index_subset_direction_both_ways() -> None:
    index = ta.NameIndex.build({"Wolverhampton Wanderers": 9})
    assert index.resolve("Wolverhampton") == 9
    # 子集匹配双向：索引侧是缩写（已展开为 wolverhampton）也能命中全名查询
    reverse = ta.NameIndex.build({"Wolves": 9})
    assert reverse.resolve("Wolverhampton Wanderers") == 9
    # 词元完全不交则无命中
    assert index.resolve("Aston Villa") is None


def test_alias_index_resolve_and_manual_override(db) -> None:
    ids = seed_teams_with_aliases(
        db,
        [
            ("曼联", [("odds_api", "Manchester United")]),
            ("曼城", [("odds_api", "Manchester City")]),
        ],
    )
    index = ta.alias_index(db)
    assert index.resolve("Man United") == ids["曼联"]
    assert index.resolve("Man City") == ids["曼城"]
    # 无别名球队解析不到
    assert index.resolve("Liverpool") is None
    # 人工覆盖即时生效：直接插 manual 别名
    db.execute(
        "INSERT INTO team_aliases (team_id, source, alias) VALUES (?, 'manual', ?)",
        (ids["曼联"], "Man Utd"),
    )
    db.commit()
    assert ta.alias_index(db).resolve("Man Utd") == ids["曼联"]


def test_match_model_team_reverse_mapping() -> None:
    model_teams = ["Man United", "Liverpool", "Everton", "Aston Villa"]
    assert ta.match_model_team(model_teams, ["Manchester United"]) == "Man United"
    assert ta.match_model_team(model_teams, ["利物浦", "Manchester United"]) == (
        "Man United"
    )
    assert ta.match_model_team(model_teams, ["Chelsea", "Arsenal"]) is None


def test_alignment_report_coverage_and_unmatched(db) -> None:
    seed_teams_with_aliases(
        db,
        [
            ("曼联", [("odds_api", "Manchester United")]),
            ("利物浦", [("odds_api", "Liverpool")]),
        ],
    )
    rs_store.upsert_hist_matches(
        db,
        [
            {
                "competition": "E0",
                "season": "2526",
                "match_date": "2025-08-16",
                "home_team": "Man United",
                "away_team": "Liverpool",
                "fthg": 1,
                "ftag": 0,
                "ftr": "H",
                "psc_home": 2.1,
                "psc_draw": 3.4,
                "psc_away": 3.6,
                "avgc_home": 2.0,
                "avgc_draw": 3.5,
                "avgc_away": 3.7,
            },
            {
                "competition": "E0",
                "season": "2526",
                "match_date": "2025-08-23",
                "home_team": "Man United",
                "away_team": "Everton",
                "fthg": 2,
                "ftag": 2,
                "ftr": "D",
                "psc_home": 1.9,
                "psc_draw": 3.6,
                "psc_away": 4.0,
                "avgc_home": None,
                "avgc_draw": None,
                "avgc_away": None,
            },
        ],
    )
    report = ta.build_alignment_report(db)
    assert report.per_competition["E0"] == {"total": 3, "matched": 2}
    assert report.coverage == pytest.approx(2 / 3)
    # identity 票 02：覆盖率守门——2/3 < 0.95 → degraded
    assert report.status == "degraded"
    assert {"competition": "E0", "team": "Everton"} in report.unmatched
    # 幂等：重跑同报告
    again = ta.build_alignment_report(db)
    assert again.per_competition == report.per_competition


def test_alignment_report_status_ok_when_covered(db) -> None:
    """identity 票 02：全部命中 → ok；空报告（无 hist 数据）→ degraded。"""
    seed_teams_with_aliases(db, [("曼联", [("odds_api", "Man United")])])
    rs_store.upsert_hist_matches(
        db,
        [
            {
                "competition": "E0",
                "season": "2526",
                "match_date": "2025-08-16",
                "home_team": "Man United",
                "away_team": "Liverpool",
                "fthg": 1,
                "ftag": 0,
                "ftr": "H",
                "psc_home": 2.1,
                "psc_draw": 3.4,
                "psc_away": 3.6,
                "avgc_home": 2.0,
                "avgc_draw": 3.5,
                "avgc_away": 3.7,
            },
        ],
    )
    seed_teams_with_aliases(db, [("利物浦", [("odds_api", "Liverpool")])])
    full = ta.build_alignment_report(db)
    assert full.coverage == pytest.approx(1.0)
    assert full.status == "ok"
    assert ta.AlignmentReport().status == "degraded"  # 空分母=覆盖率 0，不冒充达标


def test_backfill_aliases_from_events(db) -> None:
    comp = fx_store.upsert_competition(db, "英超", tier=fx_store.Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fixture = fx_store.upsert_fixture(db, comp, "2026-09-19T19:00:00+00:00", home, away)
    fx_store.set_odds_api_join(db, fixture, "evt1", "soccer_epl", "time_window")

    events = [("evt1", "Arsenal", "Chelsea")]
    added = ta.backfill_aliases_from_events(db, events)
    assert added == 2
    assert ta.alias_index(db).resolve("Arsenal") == home
    # 幂等：重复回填不新增
    assert ta.backfill_aliases_from_events(db, events) == 0
    assert ta.backfill_aliases_from_events(db, []) == 0
    # 未 join 的 event 不落别名
    assert ta.backfill_aliases_from_events(db, [("evtX", "X", "Y")]) == 0


def test_record_srct_en_aliases_idempotent_and_conflict(db) -> None:
    """英文活水落库（identity-alias 票 01）：幂等 + 同名异队不静默。"""
    arsenal = fx_store.upsert_team(db, "阿森纳")
    liverpool = fx_store.upsert_team(db, "利物浦")
    added, conflicts = ta.record_srct_en_aliases(
        db, [(arsenal, "Arsenal"), (liverpool, "Liverpool")]
    )
    assert (added, conflicts) == (2, [])
    # 幂等：重复对子零新增
    assert ta.record_srct_en_aliases(db, [(arsenal, "Arsenal")]) == (0, [])
    # 同名异队：后到者被 UNIQUE 拦下且显式进冲突清单（原归属不动）
    assert ta.record_srct_en_aliases(db, [(liverpool, "Arsenal")]) == (
        0,
        [("Arsenal", arsenal, liverpool)],
    )
    assert ta.srct_en_team_ids(db) == {arsenal, liverpool}


def test_english_alias_census_jc_universe(db) -> None:
    """普查分母=竞彩 universe 队；未解析清单全列（守门输入）。"""
    from goalx_backend.data import fixtures as fixtures_store

    comp = fixtures_store.upsert_competition(db, "英超", tier=fixtures_store.Tier.TIER1)
    home = fixtures_store.upsert_team(db, "阿森纳")
    away = fixtures_store.upsert_team(db, "切尔西")
    other = fixtures_store.upsert_team(db, "上海海港")
    for home_id, away_id, code in (
        (home, away, "周六001"),
        (other, home, "周六002"),
    ):
        fid = fixtures_store.upsert_fixture(
            db, comp, "2026-11-07T19:00:00+00:00", home_id, away_id
        )
        db.execute(
            "INSERT INTO match_codes (fixture_id, kind, business_date, code)"
            " VALUES (?, 'jingcai', '2026-11-07', ?)",
            (fid, code),
        )
    db.commit()
    ta.record_srct_en_aliases(db, [(home, "Arsenal")])
    census = ta.english_alias_census(db)
    assert census["jc_teams"] == 3
    assert census["english_resolvable"] == 1
    assert census["unresolved"] == ["上海海港", "切尔西"]


def test_clubelo_bridge_anchors_on_srct_en(db) -> None:
    """桥解饿（identity-alias 票 01）：srct_en 锚即可接通 clubelo 名。"""
    home = fx_store.upsert_team(db, "利物浦")
    ta.record_srct_en_aliases(db, [(home, "Liverpool")])
    report = ta.sync_clubelo_aliases(db, ["Liverpool", "Nowhere FC"])
    assert report.matched == 1
    assert report.added == 1
    assert report.unmatched == ["Nowhere FC"]
    assert ta.clubelo_alias_for_team(db, home) == "Liverpool"


def test_record_srct_en_aliases_cross_source_conflict(db) -> None:
    """跨源同名异队同样拦下（correctness F3）：变体队错绑比漏登记更危险。"""
    real = fx_store.upsert_team(db, "利物浦")
    variant = fx_store.upsert_team(db, "利物浦变体")
    db.execute(
        "INSERT INTO team_aliases (team_id, source, alias) VALUES (?, 'manual', ?)",
        (real, "Liverpool"),
    )
    db.commit()
    # 变体队带来同名英文名 → 跨源冲突，不落行
    assert ta.record_srct_en_aliases(db, [(variant, "Liverpool")]) == (
        0,
        [("Liverpool", real, variant)],
    )
    # 同队同名跨源共存互证（ADR-0013）：manual 已有，srct_en 照落不冲突
    assert ta.record_srct_en_aliases(db, [(real, "Liverpool")]) == (1, [])


def test_english_alias_census_counts_manual_english_only(db) -> None:
    """普查分子含 manual 英文别名；manual 中文别名不计（correctness F4）。"""
    from goalx_backend.data import fixtures as fixtures_store

    comp = fixtures_store.upsert_competition(db, "英超", tier=fixtures_store.Tier.TIER1)
    home = fixtures_store.upsert_team(db, "阿森纳")
    away = fixtures_store.upsert_team(db, "切尔西")
    fid = fixtures_store.upsert_fixture(
        db, comp, "2026-11-07T19:00:00+00:00", home, away
    )
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code)"
        " VALUES (?, 'jingcai', '2026-11-07', '周六001')",
        (fid,),
    )
    db.execute(
        "INSERT INTO team_aliases (team_id, source, alias) VALUES"
        " (?, 'manual', 'Arsenal'), (?, 'manual', '切尔西')",
        (home, away),
    )
    db.commit()
    census = ta.english_alias_census(db)
    assert census["jc_teams"] == 2
    assert census["english_resolvable"] == 1
    assert census["unresolved"] == ["切尔西"]
