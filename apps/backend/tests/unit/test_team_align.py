"""球队名对齐层测试（票 25 验收：覆盖率报告、人工覆盖即时生效、幂等）。"""

from __future__ import annotations

import pytest

from goalx_backend import team_align as ta
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store


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


def test_resolve_hist_team_and_manual_override(db) -> None:
    ids = seed_teams_with_aliases(
        db,
        [
            ("曼联", [("odds_api", "Manchester United")]),
            ("曼城", [("odds_api", "Manchester City")]),
        ],
    )
    assert ta.resolve_hist_team(db, "Man United") == ids["曼联"]
    assert ta.resolve_hist_team(db, "Man City") == ids["曼城"]
    # 无别名球队解析不到
    assert ta.resolve_hist_team(db, "Liverpool") is None
    # 人工覆盖即时生效：直接插 manual 别名
    db.execute(
        "INSERT INTO team_aliases (team_id, source, alias) VALUES (?, 'manual', ?)",
        (ids["曼联"], "Man Utd"),
    )
    db.commit()
    assert ta.resolve_hist_team(db, "Man Utd") == ids["曼联"]


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
    assert {"competition": "E0", "team": "Everton"} in report.unmatched
    # 幂等：重跑同报告
    again = ta.build_alignment_report(db)
    assert again.per_competition == report.per_competition


def test_backfill_aliases_from_events(db) -> None:
    comp = fx_store.upsert_competition(db, "英超", tier=fx_store.Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fixture = fx_store.upsert_fixture(db, comp, "2026-09-19T19:00:00+00:00", home, away)
    fx_store.set_odds_api_join(db, fixture, "evt1", "soccer_epl", "time_window")

    events = [("evt1", "Arsenal", "Chelsea")]
    added = ta.backfill_aliases_from_events(db, events)
    assert added == 2
    assert ta.resolve_hist_team(db, "Arsenal") == home
    # 幂等：重复回填不新增
    assert ta.backfill_aliases_from_events(db, events) == 0
    assert ta.backfill_aliases_from_events(db, []) == 0
    # 未 join 的 event 不落别名
    assert ta.backfill_aliases_from_events(db, [("evtX", "X", "Y")]) == 0
