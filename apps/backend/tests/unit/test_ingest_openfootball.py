"""
openfootball 对账源测试（票 44）：score 两形态解析 + 赛季键 + 注入
client/alias 索引的对账链路。

join 定则 1 路径：开球日 ±1 + 双方英文队名经 team_aliases 规范化解析
到同一 fixture（odds_api 别名桥）。测试不打真实外网。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.fixtures import (
    upsert_competition,
    upsert_fixture,
    upsert_match_code,
    upsert_team,
)
from goalx_backend.data.ingest import openfootball as of
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.modelling.team_align import alias_index, record_odds_api_alias
from goalx_backend.models import DrawResultInput, MatchCodeInput

FIXTURES = Path(__file__).parent.parent / "fixtures"
NOW = datetime(2026, 9, 21, 0, 30, tzinfo=UTC)

SEASON_DOC = {
    "name": "English Premier League 2026/27",
    "matches": [
        {
            "round": "Matchday 1",
            "date": "2026-09-20",
            "time": "20:00",
            "team1": "Arsenal FC",
            "team2": "Coventry City FC",
            "score": {"ht": [2, 0], "ft": [3, 0]},
        },
        {
            "round": "Matchday 1",
            "date": "2026-09-20",
            "team1": "Hull City AFC",
            "team2": "Manchester United FC",
            "score": [2, 2],  # list 形态（无半场）
        },
        {
            "round": "Matchday 1",
            "date": "2026-09-19",
            "team1": "Ipswich Town FC",
            "team2": "Sunderland AFC",
            "score": {"ht": [1, 1], "ft": [2, 1]},
        },
        {
            "round": "Matchday 2",
            "date": "2026-09-27",
            "team1": "Arsenal FC",
            "team2": "Coventry City FC",
            # 未回填比分（约一轮滞后常态）
        },
    ],
}


def _settings() -> Settings:
    return Settings(openfootball_base_url="https://of.test/football.json")


def _client(files: dict[str, object]) -> httpx.Client:
    """files：路径后缀（如 en.1）→ 载荷（dict=200、404 标记、Exception=炸）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        name = f"{parts[-2]}/{parts[-1].removesuffix('.json')}"
        if name not in files:
            return httpx.Response(404, text="no fixture")
        payload = files[name]
        if payload == 404:
            return httpx.Response(404, text="not covered")
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed(db, league: str, home: str, away: str, kickoff: str, code: str) -> int:
    competition = upsert_competition(db, league)
    home_id = upsert_team(db, home)
    away_id = upsert_team(db, away)
    record_odds_api_alias(db, home_id, f"{home} FC")
    record_odds_api_alias(db, away_id, f"{away} FC")
    fid = upsert_fixture(db, competition, kickoff, home_id, away_id)
    upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid, kind="jingcai", business_date="2026-09-20", code=code
        ),
    )
    return fid


# --- 解析（纯函数） ---


def test_parse_score_dict_and_list_forms() -> None:
    assert of.parse_score({"ft": [3, 0], "ht": [1, 0]}) == ((3, 0), (1, 0))
    assert of.parse_score([2, 2]) == ((2, 2), None)
    assert of.parse_score({"ft": "3:0"}) == (None, None)
    assert of.parse_score(None) == (None, None)


def test_parse_season_keeps_scored_matches_only() -> None:
    matches = of.parse_season(SEASON_DOC)
    assert len(matches) == 3
    assert (matches[0].ft, matches[0].ht) == ((3, 0), (2, 0))
    assert matches[1].ht is None  # list 形态无半场


def test_season_key_cross_year() -> None:
    assert of.season_key("2026-08-21") == "2026-27"
    assert of.season_key("2027-01-05") == "2026-27"
    assert of.season_key("2026-06-30") == "2025-26"


# --- 对账链路（join + 比对 + coverage） ---


def test_reconcile_consistent_mismatch_and_missing(db) -> None:
    fid_ok = _seed(
        db, "英超", "Arsenal", "Coventry City", "2026-09-20T19:00:00+00:00", "周日001"
    )
    _seed(
        db,
        "英超",
        "Hull City",
        "Manchester United",
        "2026-09-20T12:30:00+00:00",
        "周日002",
    )
    import_draw_results(
        db, [DrawResultInput(fixture_id=fid_ok, home_goals=3, away_goals=0)]
    )  # 源D 已落 3:0；周日002 尚无赛果
    with _client({"2026-27/en.1": SEASON_DOC}) as client:
        stats = of.reconcile_openfootball(
            db, _settings(), client, alias_index=alias_index(db), now=NOW
        )
    assert stats.compared == 2
    assert stats.consistent == 1  # Arsenal 3:0 ↔ 3:0
    assert stats.missing_result == 1  # Hull 2:2 官方无记录（of 已回填）
    assert stats.pending_manual[0]["reason"] == "reference_final_missing_fact"
    # 半场一侧缺失宽让：库内无半场、of 有半场 → 不算不一致
    assert stats.score_mismatch == 0
    # coverage：赛季粒度 covered
    row = db.execute(
        "SELECT coverage_date, league_key, match_count, coverage_status"
        " FROM source_coverage WHERE source = 'openfootball'"
    ).fetchone()
    assert row["coverage_date"] == "2026-27"
    assert row["league_key"] == "en.1"
    assert int(row["match_count"]) == 3
    assert row["coverage_status"] == "covered"


def test_reconcile_score_mismatch_detected(db) -> None:
    fid = _seed(
        db, "英超", "Arsenal", "Coventry City", "2026-09-20T19:00:00+00:00", "周日001"
    )
    import_draw_results(
        db, [DrawResultInput(fixture_id=fid, home_goals=1, away_goals=1)]
    )  # 源D 落 1:1，openfootball 回填 3:0
    with _client({"2026-27/en.1": SEASON_DOC}) as client:
        stats = of.reconcile_openfootball(
            db, _settings(), client, alias_index=alias_index(db), now=NOW
        )
    assert stats.score_mismatch == 1
    assert stats.pending_manual[0]["reason"] == "score_mismatch"


def test_reconcile_unmatched_and_ambiguous(db) -> None:
    # 未映射联赛不请求；未配对（无别名可解析）不报警
    _seed(
        db, "英超", "Arsenal", "Coventry City", "2026-09-20T19:00:00+00:00", "周日001"
    )
    doc = {
        "matches": [
            # 同对阵同窗口两条（日期 19/20 都在 ±1 内）→ 多候选进人工
            {
                "date": "2026-09-19",
                "team1": "Arsenal FC",
                "team2": "Coventry City FC",
                "score": [1, 0],
            },
            {
                "date": "2026-09-20",
                "team1": "Arsenal FC",
                "team2": "Coventry City FC",
                "score": [2, 0],
            },
        ]
    }
    with _client({"2026-27/en.1": doc}) as client:
        stats = of.reconcile_openfootball(
            db, _settings(), client, alias_index=alias_index(db), now=NOW
        )
    assert stats.compared == 0
    assert stats.pending_manual[0]["reason"] == "openfootball_ambiguous_match"


def test_reconcile_unmapped_league_and_404_coverage(db) -> None:
    # 日职（未映射）不请求；映射联赛文件 404 → coverage fetch_failed、跳过
    _seed(
        db, "日职", "Gamba Osaka", "Vissel Kobe", "2026-09-20T09:00:00+00:00", "周日003"
    )
    _seed(
        db, "英超", "Arsenal", "Coventry City", "2026-09-20T19:00:00+00:00", "周日001"
    )
    with _client({"2026-27/en.1": 404}) as client:
        stats = of.reconcile_openfootball(
            db, _settings(), client, alias_index=alias_index(db), now=NOW
        )
    assert stats.compared == 0
    row = db.execute(
        "SELECT coverage_status FROM source_coverage WHERE source = 'openfootball'"
    ).fetchone()
    assert row["coverage_status"] == "not_covered"


def test_reconcile_empty_window_records_run(db) -> None:
    # 窗口内无竞彩场次 → 零请求，仍留 compared=0 运行行
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("空窗口不应发请求")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        stats = of.reconcile_openfootball(
            db, _settings(), client, alias_index=alias_index(db), now=NOW
        )
    assert stats.compared == 0
    assert rs_store.list_draw_results(db) == []
