"""换帅事件派生面测试（autoresearch 票 18：§5-1 落地序①判据底座）。

派生语义：tenures × hist_matches 逐场指派——每场分配「date_from ≤ 当日且
区间覆盖、date_from 最晚」的任职行（重叠期代理主帅优先）；事件=相邻两场
指派从 A 换到 B 的边界（事件日=B 段首战日）；样本=新帅段头 1-3 场。
"""

from __future__ import annotations

import sqlite3

from goalx_backend.data import coach_changes
from goalx_backend.data.ingest import tm_coach


def _insert_hist(
    db: sqlite3.Connection,
    rows: list[tuple[str, str, str, str, str]],
) -> None:
    """(competition, season, match_date, home, away) → hist_matches 行。"""
    for comp, season, day, home, away in rows:
        db.execute(
            """
            INSERT INTO hist_matches
                (competition, season, match_date, home_team, away_team,
                 fthg, ftag, ftr)
            VALUES (?, ?, ?, ?, ?, 1, 0, 'H')
            """,
            (comp, season, day, home, away),
        )
    db.commit()


def _tenure(
    club_tm_id: int,
    club_name: str,
    coach: str,
    date_from: str,
    date_to: str | None,
    *,
    role: str = "manager",
) -> tm_coach.TmTenure:
    return tm_coach.TmTenure(
        club_tm_id=club_tm_id,
        club_name=club_name,
        role=role,
        coach_tm_id=None,
        coach_name=coach,
        date_from=date_from,
        date_to=date_to,
        games=None,
    )


def _seed_club(db: sqlite3.Connection, club_tm_id: int, name: str, league: str) -> None:
    db.execute(
        """
        INSERT INTO tm_club_seasons
            (league_tm_code, season, club_tm_id, club_name,
             first_seen_at, observed_at)
        VALUES (?, '2019', ?, ?, ?, ?)
        """,
        (league, club_tm_id, name, NOW, NOW),
    )
    db.commit()


NOW = "2026-11-10T00:00:00+00:00"


def _events(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT * FROM coach_change_events
        ORDER BY club_tm_id, event_date
        """
    ).fetchall()


def _samples(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT s.game_no, m.match_date, m.home_team, m.away_team, e.incoming_coach
        FROM coach_change_samples s
        JOIN coach_change_events e ON e.id = s.event_id
        JOIN hist_matches m ON m.id = s.hist_match_id
        ORDER BY e.id, s.game_no
        """
    ).fetchall()


def test_basic_succession_event_and_three_game_samples(db) -> None:
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Unai Emery", "2018-07-01", "2019-11-29"),
            _tenure(11, "Arsenal", "Mikel Arteta", "2019-12-22", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-11-09", "Arsenal", "Southampton"),
            ("E0", "1920", "2019-11-24", "Arsenal", "Norwich"),
            ("E0", "1920", "2019-12-26", "Arsenal", "Chelsea"),  # 新帅首战
            ("E0", "1920", "2019-12-29", "Arsenal", "Man United"),
            ("E0", "1920", "2020-01-01", "Arsenal", "Leeds"),
            ("E0", "1920", "2020-01-04", "Arsenal", "Crystal Palace"),  # 第 4 场不入样
        ],
    )
    stats = coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert len(events) == 1
    assert events[0]["outgoing_coach"] == "Unai Emery"
    assert events[0]["incoming_coach"] == "Mikel Arteta"
    assert events[0]["event_date"] == "2019-12-26"
    assert events[0]["hist_competition"] == "E0"
    assert events[0]["hist_season"] == "1920"
    assert events[0]["incoming_is_caretaker"] == 0
    samples = _samples(db)
    assert [s["game_no"] for s in samples] == [1, 2, 3]
    assert samples[0]["match_date"] == "2019-12-26"
    assert samples[2]["match_date"] == "2020-01-01"  # 第 4 场被窗口截断
    assert stats["events"] == 1
    assert stats["samples"] == 3
    assert stats["unresolved_clubs"] == []


def test_caretaker_interim_and_return_of_manager(db) -> None:
    """代理段：Emery 终 → Ljungberg(代理) → Arteta，两事件三段。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Unai Emery", "2018-07-01", "2019-11-29"),
            _tenure(
                11,
                "Arsenal",
                "Freddie Ljungberg",
                "2019-11-29",
                "2019-12-21",
                role="caretaker",
            ),
            _tenure(11, "Arsenal", "Mikel Arteta", "2019-12-22", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-11-09", "Arsenal", "Southampton"),  # Emery
            ("E0", "1920", "2019-11-30", "Norwich", "Arsenal"),  # 代理首战
            ("E0", "1920", "2019-12-21", "Arsenal", "Everton"),  # 代理末战
            ("E0", "1920", "2019-12-26", "Arsenal", "Chelsea"),  # Arteta 首战
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert len(events) == 2
    first, second = events
    assert first["incoming_coach"] == "Freddie Ljungberg"
    assert first["incoming_is_caretaker"] == 1
    assert first["event_date"] == "2019-11-30"
    assert second["outgoing_coach"] == "Freddie Ljungberg"
    assert second["incoming_coach"] == "Mikel Arteta"
    assert second["event_date"] == "2019-12-26"


def test_overlapping_tenures_latest_start_wins(db) -> None:
    """主帅病假未结段（开区间）+ 代理：date_from 更晚者接管指派。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Boss A", "2019-07-01", None),  # 开区间未结
            _tenure(
                11,
                "Arsenal",
                "Caretaker C",
                "2019-11-01",
                "2019-12-15",
                role="caretaker",
            ),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-10-01", "Arsenal", "X"),  # A
            ("E0", "1920", "2019-11-02", "Arsenal", "Y"),  # 代理首战
            ("E0", "1920", "2019-11-20", "Arsenal", "Z"),  # 代理
            ("E0", "1920", "2019-12-20", "Arsenal", "W"),  # A 回归
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert [(e["incoming_coach"], e["outgoing_coach"]) for e in events] == [
        ("Caretaker C", "Boss A"),
        ("Boss A", "Caretaker C"),
    ]


def test_gap_between_tenures_is_not_a_boundary(db) -> None:
    """两段之间无任职覆盖的空窗场：无指派；再交接仍按相邻指派算边界。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Boss A", "2019-07-01", "2019-10-01"),
            _tenure(11, "Arsenal", "Boss B", "2019-12-01", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-09-01", "Arsenal", "X"),  # A
            ("E0", "1920", "2019-11-15", "Arsenal", "G"),  # 空窗（无指派）
            ("E0", "1920", "2019-12-05", "Arsenal", "Y"),  # B 首战
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert len(events) == 1
    assert events[0]["outgoing_coach"] == "Boss A"
    assert events[0]["incoming_coach"] == "Boss B"


def test_first_tenure_without_prior_assignment_is_not_event(db) -> None:
    """窗首上任（此前无已指派场）：非换帅事件。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [_tenure(11, "Arsenal", "Boss A", "2016-07-01", None)],
        observed_at=NOW,
    )
    _insert_hist(db, [("E0", "1617", "2016-08-01", "Arsenal", "X")])
    coach_changes.derive_coach_changes(db, now=NOW)
    assert _events(db) == []
    assert _samples(db) == []


def test_away_matches_count_in_stream(db) -> None:
    """客场比赛同样进入该队比赛流（主场过滤不得漏客场）。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Boss A", "2019-07-01", "2019-11-01"),
            _tenure(11, "Arsenal", "Boss B", "2019-11-15", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-10-01", "Arsenal", "X"),
            ("E0", "1920", "2019-11-16", "Y", "Arsenal"),  # 客场新帅首战
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert len(events) == 1
    assert events[0]["event_date"] == "2019-11-16"


def test_identity_uses_english_side_aliases(db) -> None:
    """TM 英文名 → hist 队名三级匹配；未解析俱乐部诚实计数不炸。"""
    _seed_club(db, 11, "Arsenal FC", "GB1")
    _seed_club(db, 999, "Nowhere Town", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal FC", "Boss A", "2019-07-01", "2019-11-01"),
            _tenure(11, "Arsenal FC", "Boss B", "2019-11-15", None),
            _tenure(999, "Nowhere Town", "Boss C", "2019-07-01", "2019-11-01"),
            _tenure(999, "Nowhere Town", "Boss D", "2019-11-15", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-10-01", "Arsenal", "Southampton"),
            ("E0", "1920", "2019-11-16", "Everton", "Arsenal"),
            ("E0", "1920", "2019-10-02", "Watford", "Southampton"),
            ("E0", "1920", "2019-11-17", "Watford", "Burnley"),
        ],
    )
    stats = coach_changes.derive_coach_changes(db, now=NOW)
    assert stats["unresolved_clubs"] == ["Nowhere Town"]
    events = _events(db)
    assert len(events) == 1
    assert events[0]["club_name"] == "Arsenal FC"


def test_event_team_id_resolved_via_aliases(db) -> None:
    """事件行 canonical team_id：hist 队名经 team_aliases 英文侧解析。"""
    db.execute(
        "INSERT INTO teams (canonical_name, created_at) VALUES ('阿森纳', ?)",
        (NOW,),
    )
    db.execute(
        "INSERT INTO team_aliases (team_id, source, alias)"
        " VALUES (1, 'odds_api', 'Arsenal')"
    )
    db.commit()
    _seed_club(db, 11, "Arsenal FC", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal FC", "Boss A", "2019-07-01", "2019-11-01"),
            _tenure(11, "Arsenal FC", "Boss B", "2019-11-15", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-10-01", "Arsenal", "X"),
            ("E0", "1920", "2019-11-16", "Y", "Arsenal"),
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert events[0]["team_id"] == 1


def test_rebuild_is_idempotent(db) -> None:
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Boss A", "2019-07-01", "2019-11-01"),
            _tenure(11, "Arsenal", "Boss B", "2019-11-15", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-10-01", "Arsenal", "X"),
            ("E0", "1920", "2019-11-16", "Arsenal", "Y"),
            ("E0", "1920", "2019-11-20", "Arsenal", "Z"),
        ],
    )
    coach_changes.derive_coach_changes(db, now=NOW)
    first_events = [dict(r) for r in _events(db)]
    first_samples = [dict(r) for r in _samples(db)]
    stats = coach_changes.derive_coach_changes(db, now=NOW)  # 重建（同 now）
    second_events = [dict(r) for r in _events(db)]
    second_samples = [dict(r) for r in _samples(db)]
    assert first_events == second_events
    assert first_samples == second_samples
    assert stats["events"] == 1


def test_same_day_two_matches_boundary_counts_first(db) -> None:
    """同日两场：按 (date, id) 稳定排序，边界取当日首场。"""
    _seed_club(db, 11, "Arsenal", "GB1")
    tm_coach.upsert_tenures(
        db,
        [
            _tenure(11, "Arsenal", "Boss A", "2019-07-01", "2019-11-01"),
            _tenure(11, "Arsenal", "Boss B", "2019-11-02", None),
        ],
        observed_at=NOW,
    )
    _insert_hist(
        db,
        [
            ("E0", "1920", "2019-11-02", "Arsenal", "X"),  # id 更小 → 新帅首战
            ("E0", "1920", "2019-11-02", "Arsenal", "Y"),
        ],
    )
    _insert_hist(db, [("E0", "1920", "2019-10-01", "Arsenal", "Old")])
    coach_changes.derive_coach_changes(db, now=NOW)
    events = _events(db)
    assert len(events) == 1
    assert events[0]["event_date"] == "2019-11-02"


def test_coverage_report_league_by_season(db) -> None:
    db.execute(
        """
        INSERT INTO tm_club_seasons
            (league_tm_code, season, club_tm_id, club_name, first_seen_at, observed_at)
        VALUES ('GB1', '2016', 11, 'Arsenal FC', ?, ?),
               ('GB1', '2016', 281, 'Manchester City', ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    tm_coach.upsert_tenures(
        db,
        [_tenure(11, "Arsenal FC", "Boss A", "2016-07-01", None)],
        observed_at=NOW,
    )
    from goalx_backend.data.reconcile import upsert_source_coverage

    upsert_source_coverage(
        db,
        source="transfermarkt",
        coverage_date="2016",
        match_count=2,
        coverage_status="covered",
        observed_at=NOW,
        league_key="GB1",
    )
    db.commit()
    report = coach_changes.coverage_report(db)
    row = next(r for r in report["leagues"] if r["season"] == "2016")
    assert row["tm_code"] == "GB1"
    assert row["clubs"] == 2
    assert row["clubs_with_tenures"] == 1
    assert row["status"] == "covered"
