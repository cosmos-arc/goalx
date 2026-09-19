"""票 09 情报采集单测：内部推导落库/幂等/诚实零行/append-only 触发器。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.data import pool as pool_store
from goalx_backend.llm.collect import collect_fdhist_intel, collect_pool_intel
from goalx_backend.llm.store import (
    IntelDraft,
    insert_intel_observation,
    intel_for_fixture,
    raw_hash,
)

_KICKOFF = "2026-09-26T19:00:00+00:00"


def _seed_fixture(
    db: sqlite3.Connection, *, sport_key: str | None, home: str, away: str
) -> int:
    """种子一场竞彩 fixture（英超中文队名 + odds_api 英文别名）。"""
    cur = db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES ('英超', 'tier1', ?, '2026-09-01T00:00:00+00:00')",
        (sport_key,),
    )
    comp_id = cur.lastrowid
    ids = {}
    for name, alias in (
        (home, home.replace("阿森纳", "Arsenal").replace("切尔西", "Chelsea")),
        (away, away.replace("阿森纳", "Arsenal").replace("切尔西", "Chelsea")),
    ):
        cur = db.execute(
            """
            INSERT INTO teams (canonical_name, created_at)
            VALUES (?, '2026-09-01T00:00:00+00:00')
            """,
            (name,),
        )
        team_id = cur.lastrowid
        ids[name] = team_id
        if alias != name:
            db.execute(
                """
                INSERT INTO team_aliases (team_id, source, alias)
                VALUES (?, 'odds_api', ?)
                """,
                (team_id, alias),
            )
    cur = db.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id, away_team_id)"
        " VALUES (?, ?, ?, ?)",
        (comp_id, _KICKOFF, ids[home], ids[away]),
    )
    fixture_id = int(cur.lastrowid)
    # 池桥接的候选集要求 fixture 带竞彩销售编号（fixtures_for_business_dates 契约）
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code, is_single)"
        " VALUES (?, 'jingcai', '2026-09-26', '周六001', 1)",
        (fixture_id,),
    )
    return fixture_id


def _seed_hist(db: sqlite3.Connection) -> None:
    """种子 E0 2627：阿森纳/切尔西近期各 2 场 + 交锋 1 场。"""
    rows = [
        ("E0", "2627", "2026-09-20", "Arsenal", "Everton", 2, 0, "H"),
        ("E0", "2627", "2026-09-13", "Brighton", "Arsenal", 1, 1, "D"),
        ("E0", "2627", "2026-09-19", "Chelsea", "Fulham", 1, 0, "H"),
        ("E0", "2627", "2026-09-12", "West Ham", "Chelsea", 0, 2, "A"),
        ("E0", "2526", "2026-05-02", "Arsenal", "Chelsea", 3, 1, "H"),
    ]
    db.executemany(
        """
        INSERT INTO hist_matches
            (competition, season, match_date, home_team, away_team, fthg, ftag, ftr)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _seed_pool(
    db: sqlite3.Connection, *, home: str = "阿森纳", away: str = "切尔西"
) -> int:
    """种子一期在售 ttt14（截止未来）单场对阵，返回期次 id。"""
    period_id = pool_store.upsert_pool_period(
        db, "ttt14", "26999", "2026-09-27T21:00:00+00:00"
    )
    pool_store.replace_pool_matches(
        db,
        period_id,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id="1348999",
                league="英超",
                kickoff_utc=_KICKOFF,
                home_team=home,
                away_team=away,
                euro_odds=(1.5, 4.0, 6.0),
            )
        ],
    )
    return period_id


def test_collect_pool_intel_full_path(db: sqlite3.Connection) -> None:
    _seed_hist(db)
    fixture_id = _seed_fixture(db, sport_key="soccer_epl", home="阿森纳", away="切尔西")
    _seed_pool(db)

    stats = collect_pool_intel(db)
    assert stats.periods == 1
    assert stats.matches_seen == 1
    assert stats.with_fixture == 1
    assert stats.inserted == 3  # 主队近况 + 客队近况 + H2H

    rows = intel_for_fixture(db, fixture_id)
    kinds = sorted(str(r["kind"]) for r in rows)
    assert kinds == ["form", "form", "h2h"]
    form = next(r for r in rows if r["kind"] == "form" and "Arsenal" in str(r["text"]))
    assert "近3轮" in str(form["text"])
    assert form["source"] == "fdhist:E0"
    assert form["collector"] == "internal-fdhist"
    assert raw_hash(__import__("json").loads(str(form["raw_payload"]))) == str(
        form["raw_hash"]
    )

    # 幂等：同内容重跑零新行
    again = collect_pool_intel(db)
    assert again.inserted == 0
    assert len(intel_for_fixture(db, fixture_id)) == 3


def test_collect_skips_unmapped_league(db: sqlite3.Connection) -> None:
    _seed_hist(db)
    _seed_fixture(db, sport_key=None, home="阿森纳", away="切尔西")  # 巴西甲等非六联赛
    _seed_pool(db)
    stats = collect_pool_intel(db)
    assert stats.with_fixture == 1
    assert stats.inserted == 0
    assert stats.unmapped_league == 1


def test_collect_skips_no_fixture_bridge(db: sqlite3.Connection) -> None:
    _seed_hist(db)
    _seed_pool(db, home="皇家马德里", away="巴塞罗那")  # 无对应竞彩 fixture
    stats = collect_pool_intel(db)
    assert stats.with_fixture == 0
    assert stats.inserted == 0


def test_collect_fdhist_unmapped_team_zero_rows(db: sqlite3.Connection) -> None:
    # 别名存在但 hist 库无该队（如新升班马）：该侧诚实零行
    _seed_hist(db)
    fixture_id = _seed_fixture(db, sport_key="soccer_epl", home="阿森纳", away="切尔西")
    db.execute(
        "DELETE FROM hist_matches WHERE home_team = 'Chelsea' OR away_team = 'Chelsea'"
    )  # 切尔西无历史
    inserted = collect_fdhist_intel(db, fixture_id)
    assert inserted == 1  # 仅主队近况；无 H2H（客队无映射）


def test_intel_observations_append_only(db: sqlite3.Connection) -> None:
    fixture_id = _seed_fixture(db, sport_key="soccer_epl", home="阿森纳", away="切尔西")
    insert_intel_observation(
        db,
        fixture_id,
        IntelDraft(
            kind="form",
            text="x",
            source="t",
            collected_at="2026-09-19T00:00:00+00:00",
            collector="t",
            raw_payload={"a": 1},
        ),
    )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE intel_observations SET text = 'y'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM intel_observations")


def test_match_intels_dropped(db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE name = 'match_intels'"
    ).fetchone()
    assert int(row["n"]) == 0
