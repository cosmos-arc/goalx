"""票 13 复核/盲评/控制事件单测。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.config import Settings
from goalx_backend.data.results import DrawResultInput, upsert_draw_result
from goalx_backend.llm.protocol import record_control_events
from goalx_backend.llm.review import (
    enqueue_post_settle,
    open_reviews,
    record_blind_review,
    record_verdict,
)
from goalx_backend.modelling.forecast import insert_forecast

_KICKOFF = "2026-09-20T19:00:00+00:00"


def _seed_fixture(db: sqlite3.Connection, fixture_id: int = 1) -> int:
    db.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超', 'tier1', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = db.execute("SELECT id FROM competitions WHERE name = '英超'").fetchone()[
        "id"
    ]
    ids = {}
    for name in (f"主{fixture_id}", f"客{fixture_id}"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    cur = db.execute(
        """
        INSERT INTO fixtures
            (id, competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fixture_id, comp_id, _KICKOFF, ids[f"主{fixture_id}"], ids[f"客{fixture_id}"]),
    )
    return fixture_id


def _seed_tracks(
    db: sqlite3.Connection,
    fixture_id: int,
    ml: tuple[float, float, float],
    llm: tuple[float, float, float],
) -> None:
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload={
            "matrix": [[0, 0, ml[2]], [0, ml[1], 0], [ml[0], 0, 0]],
            "lambda_home": 1.0,
            "lambda_away": 1.0,
        },
        issued_at="2026-09-20T10:00:00+00:00",
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:1",
        content_hash=f"llm-{fixture_id}",
        payload={
            "h": llm[0],
            "d": llm[1],
            "a": llm[2],
            "intel_ids": [],
            "intel_count": 0,
        },
        issued_at="2026-09-20T10:50:00+00:00",
    )


def _settle(db: sqlite3.Connection, fixture_id: int, home: int, away: int) -> None:
    upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=fixture_id,
            home_goals=home,
            away_goals=away,
            source="test",
            published_at="2026-09-20T22:00:00+00:00",
        ),
    )


def test_enqueue_post_settle_one_hit_one_miss(db: sqlite3.Connection) -> None:
    fid = _seed_fixture(db)
    # ML 判主胜(0.6)，LLM 判客胜(0.55)；结果客胜 → ML 错 LLM 对
    _seed_tracks(db, fid, (0.6, 0.2, 0.2), (0.2, 0.25, 0.55))
    _settle(db, fid, 0, 2)
    stats = enqueue_post_settle(db, now="2026-09-20T23:00:00+00:00")
    assert stats.settled_checked == 1
    assert stats.enqueued == 1
    item = db.execute(
        "SELECT * FROM review_items WHERE fixture_id = ?", (fid,)
    ).fetchone()
    assert item["route"] == "post_settle"

    # 同对同错不入队：再结算一场双轨都判主胜且结果主胜
    _seed_tracks(db, 2, (0.6, 0.2, 0.2), (0.5, 0.3, 0.2)) if _seed_fixture(
        db, 2
    ) else None
    _settle(db, 2, 2, 0)
    stats2 = enqueue_post_settle(db, now="2026-09-20T23:10:00+00:00")
    assert stats2.enqueued == 0
    assert stats2.skipped_known == 1  # 第一场 UNIQUE 幂等


def test_record_verdict_lifecycle(db: sqlite3.Connection) -> None:
    fid = _seed_fixture(db)
    _seed_tracks(db, fid, (0.6, 0.2, 0.2), (0.2, 0.25, 0.55))
    _settle(db, fid, 0, 2)
    enqueue_post_settle(db, now="2026-09-20T23:00:00+00:00")
    item = open_reviews(db)[0]
    assert record_verdict(db, int(item["id"]), "key_contribution", note="伤停情报关键")
    done = db.execute(
        "SELECT * FROM review_items WHERE id = ?", (item["id"],)
    ).fetchone()
    assert done["status"] == "done"
    assert done["verdict"] == "key_contribution"
    assert record_verdict(db, int(item["id"]), "misleading") is False  # 已 done
    with pytest.raises(ValueError):
        record_verdict(db, int(item["id"]), "bogus")


def test_blind_review_idempotent(db: sqlite3.Connection) -> None:
    fid = _seed_fixture(db)
    assert record_blind_review(db, cycle="2026W38", fixture_id=fid, choice="llm")
    assert not record_blind_review(db, cycle="2026W38", fixture_id=fid, choice="ml")
    n = db.execute("SELECT COUNT(*) AS n FROM blind_reviews").fetchone()["n"]
    assert n == 1


def test_control_events_once_per_day(db: sqlite3.Connection) -> None:
    settings = Settings(
        glm_api_key="k", m3_analyst_enabled=False, m3_fusion_enabled=False
    )
    events = record_control_events(db, settings)
    assert set(events) == {"analyst_disabled", "fused_disabled"}
    again = record_control_events(db, settings)
    assert again == []  # 同日不重复
