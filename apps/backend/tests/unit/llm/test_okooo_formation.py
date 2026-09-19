"""票 09 定案主力源：澳客阵容页伤停采集单测（真实页面 fixture，零外网）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from goalx_backend.data import pool as pool_store
from goalx_backend.llm.okooo_formation import (
    collect_injury_intel,
    fetch_formation_page,
    parse_formation_injuries,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_FORMATION_HTML = (FIXTURES / "okooo_formation_1328101.html.txt").read_bytes()
_KICKOFF = "2026-09-26T19:00:00+00:00"


def _client() -> httpx.Client:
    """formation 页 MockTransport（返回真实存档页面）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/formation/" in request.url.path:
            return httpx.Response(200, content=_FORMATION_HTML)
        raise AssertionError(f"unexpected url: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed(db: sqlite3.Connection, *, source_match_id: str | None = "1328101") -> None:
    """种子一场可桥接的竞彩 fixture + 在售彩池期次。"""
    cur = db.execute(
        "INSERT INTO competitions (name, tier, odds_api_sport_key, created_at)"
        " VALUES ('意甲', 'tier1', 'soccer_italy_serie_a', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = cur.lastrowid
    ids = {}
    for name in ("佛罗伦萨", "那不勒斯"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    cur = db.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id, away_team_id)"
        " VALUES (?, ?, ?, ?)",
        (comp_id, _KICKOFF, ids["佛罗伦萨"], ids["那不勒斯"]),
    )
    fixture_id = cur.lastrowid
    db.execute(
        "INSERT INTO match_codes (fixture_id, kind, business_date, code, is_single)"
        " VALUES (?, 'jingcai', '2026-09-26', '周六001', 1)",
        (fixture_id,),
    )
    period_id = pool_store.upsert_pool_period(
        db, "ttt14", "26999", "2026-09-27T21:00:00+00:00"
    )
    pool_store.replace_pool_matches(
        db,
        period_id,
        [
            pool_store.PoolMatchInput(
                match_seq=1,
                source_match_id=source_match_id,
                league="意甲",
                kickoff_utc=_KICKOFF,
                home_team="佛罗伦萨",
                away_team="那不勒斯",
                euro_odds=(2.5, 3.2, 2.6),
            )
        ],
    )


def test_parse_real_page() -> None:
    html = _FORMATION_HTML.decode("gbk", errors="replace")
    r = parse_formation_injuries(html)
    assert r is not None
    assert r.home_total == "600万"
    assert r.away_total == "1.46亿"
    assert [(p.state, p.position, p.name, p.price) for p in r.home] == [
        ("伤", "【卫】", "法比安", "600万")
    ]
    away = {p.name: p for p in r.away}
    assert away["梅雷特"].position == "【门】"
    assert away["麦克托米奈"].price == "4500万"
    assert "出场:2" in away["麦克托米奈"].detail


def test_parse_page_without_section() -> None:
    assert parse_formation_injuries("<html>无伤停段的页面</html>") is None


def test_collect_full_path_and_idempotent(db: sqlite3.Connection) -> None:
    _seed(db)
    with _client() as client:
        stats = collect_injury_intel(db, client, now="2026-09-19T12:00:00+00:00")
    assert stats.matches_seen == 1
    assert stats.with_source_id == 1
    assert stats.inserted == 1
    row = db.execute(
        "SELECT kind, text, raw_payload FROM intel_observations"
        " WHERE collector = 'okooo-formation'"
    ).fetchone()
    assert row["kind"] == "伤停"
    assert "主队伤停（影响600万）" in str(row["text"])
    assert "麦克托米奈" in str(row["text"])
    assert "1.46亿" in str(row["text"])

    with _client() as client:
        again = collect_injury_intel(db, client, now="2026-09-19T12:00:00+00:00")
    assert again.inserted == 0
    assert again.skipped_known == 1


def test_collect_without_source_id_zero_rows(db: sqlite3.Connection) -> None:
    _seed(db, source_match_id=None)
    with _client() as client:
        stats = collect_injury_intel(db, client, now="2026-09-19T12:00:00+00:00")
    assert stats.with_source_id == 0
    assert stats.inserted == 0


def test_collect_http_failure_counted(db: sqlite3.Connection) -> None:
    _seed(db)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = collect_injury_intel(db, client, now="2026-09-19T12:00:00+00:00")
    assert stats.fetch_failed == 1
    assert stats.inserted == 0


def test_fetch_formation_page_decodes_gbk(db: sqlite3.Connection) -> None:
    with _client() as client:
        html = fetch_formation_page(client, "1328101")
    assert "主队伤停" in html
