"""
Understat xG 采集测试（票 45）：解析/防前视累计/幂等/join 三态/coverage。

不打真实外网（httpx.MockTransport 注入载荷）；防前视是本票红线，
attach_prior 的同刻不可见与缺值跳过各有专测。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.fixtures import (
    upsert_competition,
    upsert_fixture,
    upsert_team,
)
from goalx_backend.data.ingest import understat as us
from goalx_backend.modelling.team_align import alias_index, record_odds_api_alias

NOW = datetime(2026, 9, 21, 1, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(understat_base_url="https://us.test")


def _match(
    mid: str,
    dt: str,
    home_id: str,
    home: str,
    away_id: str,
    away: str,
    *,
    is_result: bool = True,
    npxg_home: float | None = 1.0,
    npxg_away: float | None = 1.0,
    goals: tuple[int, int] | None = (2, 1),
) -> us.UnderstatMatch:
    return us.UnderstatMatch(
        match_id=mid,
        league="epl",
        season="2026",
        datetime_utc=dt,
        home_team_id=home_id,
        home_team=home,
        away_team_id=away_id,
        away_team=away,
        is_result=is_result,
        goals_home=goals[0] if goals else None,
        goals_away=goals[1] if goals else None,
        xg_home=float(npxg_home) if npxg_home is not None else None,
        xg_away=float(npxg_away) if npxg_away is not None else None,
        npxg_home=npxg_home,
        npxg_away=npxg_away,
        forecast_w=0.5 if is_result else None,
        forecast_d=0.3 if is_result else None,
        forecast_l=0.2 if is_result else None,
    )


def _doc() -> dict[str, Any]:
    """实测载荷形态缩影：字符串数值、未赛场次无 forecast、history 带 npxG。"""
    return {
        "teams": {
            "71": {
                "id": "71",
                "title": "Arsenal",
                "history": [
                    {"npxG": 1.5, "npxGA": 0.5, "date": "2026-08-21 19:00:00"},
                    {"npxG": 1.2, "npxGA": 0.8, "date": "2026-08-23 13:00:00"},
                ],
            },
            "83": {
                "id": "83",
                "title": "Coventry",
                "history": [{"npxG": 0.5, "npxGA": 1.8, "date": "2026-08-21 19:00:00"}],
            },
            "90": {
                "id": "90",
                "title": "Aston Villa",
                "history": [{"npxG": 0.9, "npxGA": 1.2, "date": "2026-08-23 13:00:00"}],
            },
            "88": {"id": "88", "title": "Manchester City", "history": []},
        },
        "dates": [
            {
                "id": "31180",
                "isResult": True,
                "h": {"id": "71", "title": "Arsenal"},
                "a": {"id": "83", "title": "Coventry"},
                "goals": {"h": "3", "a": "0"},
                "xG": {"h": "1.85424", "a": "0.558336"},
                "datetime": "2026-08-21 19:00:00",
                "forecast": {"w": "0.7194", "d": "0.2156", "l": "0.065"},
            },
            {
                "id": "31181",
                "isResult": True,
                "h": {"id": "90", "title": "Aston Villa"},
                "a": {"id": "71", "title": "Arsenal"},
                "goals": {"h": "1", "a": "1"},
                "xG": {"h": "0.9", "a": "1.2"},
                "datetime": "2026-08-23 13:00:00",
                "forecast": {"w": "0.3", "d": "0.4", "l": "0.3"},
            },
            {
                "id": "31222",
                "isResult": False,
                "h": {"id": "88", "title": "Manchester City"},
                "a": {"id": "71", "title": "Arsenal"},
                "goals": {"h": None, "a": None},
                "xG": {"h": None, "a": None},
                "datetime": "2026-09-20 13:00:00",
            },
        ],
    }


def _client(files: dict[str, object], base_ok: bool = True) -> httpx.Client:
    """files：'{league}/{season}' → 载荷。

    载荷形态：dict=正常 JSON、404=未覆盖、str=200 非 JSON 体、Exception=非 2xx。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200 if base_ok else 500, text="home")
        name = request.url.path.removeprefix("/getLeagueData/").removesuffix("")
        key = "/".join(name.split("/")[-2:])
        if key not in files:
            return httpx.Response(404, text="no file")
        payload = files[key]
        if payload == 404:
            return httpx.Response(404, text="not covered")
        if isinstance(payload, str):
            return httpx.Response(200, text=payload)
        if isinstance(payload, Exception):
            return httpx.Response(500, text=str(payload))
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_teams(db, *names: str) -> dict[str, int]:
    """建 canonical 队 + odds_api 英文别名（同一俱乐部跨 fixture 共享 id）。"""
    ids: dict[str, int] = {}
    for name in names:
        team_id = upsert_team(db, f"队{name}")
        record_odds_api_alias(db, team_id, name)
        ids[name] = team_id
    return ids


# --- 纯函数 ---


def test_season_start_year_july_boundary() -> None:
    assert us.season_start_year(date(2026, 6, 30)) == "2025"
    assert us.season_start_year(date(2026, 7, 1)) == "2026"
    assert us.season_start_year(date(2026, 9, 20)) == "2026"


def test_parse_league_types_and_npxg_join() -> None:
    matches = us.parse_league(_doc(), league="epl", season="2026")
    by_id = {m.match_id: m for m in matches}
    played = by_id["31180"]
    assert played.is_result is True
    assert (played.goals_home, played.goals_away) == (3, 0)
    assert played.xg_home == pytest.approx(1.85424)
    assert played.npxg_home == pytest.approx(1.5)  # history 对齐（去点球口径）
    assert played.npxg_away == pytest.approx(0.5)
    assert played.forecast_w == pytest.approx(0.7194)
    assert played.datetime_utc == "2026-08-21T19:00:00"
    unplayed = by_id["31222"]
    assert unplayed.is_result is False
    assert unplayed.goals_home is None
    assert unplayed.xg_home is None
    assert unplayed.forecast_w is None  # 未赛场次不携带 forecast


def test_attach_prior_excludes_same_day_and_future() -> None:
    """红线（定则 2）：prior 只含开球日严格早于本场的已完场累计。

    同日错峰早场（m2 13:00）在晚场（m3 20:00）决策时点常未完场——其
    赛后定值的最终 npxG 不得泄入晚场 prior（评审 BLOCKER 修正用例）。
    """
    matches = [
        _match(
            "m1",
            "2026-08-21T19:00:00",
            "71",
            "Arsenal",
            "83",
            "Coventry",
            npxg_home=1.5,
            npxg_away=0.5,
        ),
        _match(
            "m2",
            "2026-08-23T13:00:00",
            "90",
            "Aston Villa",
            "71",
            "Arsenal",
            npxg_home=0.8,
            npxg_away=1.2,
        ),
        # 同日晚场：prior 不含同日 m2（即便 m2 已完场且时刻更早）
        _match("m3", "2026-08-23T20:00:00", "71", "Arsenal", "88", "Manchester City"),
        # 次日：prior 应含 m1+m2+m3
        _match("m4", "2026-08-24T15:00:00", "71", "Arsenal", "83", "Coventry"),
        # 未完场：不进任何后续累计
        _match(
            "m5",
            "2026-09-06T15:00:00",
            "83",
            "Coventry",
            "88",
            "Manchester City",
            is_result=False,
            goals=None,
        ),
        _match("m6", "2026-09-13T15:00:00", "71", "Arsenal", "90", "Aston Villa"),
    ]
    us.attach_prior(matches)
    by_id = {m.match_id: m for m in matches}
    m1 = by_id["m1"]
    assert m1.prior_npxg_home == 0.0
    assert m1.prior_matches_home == 0
    m3 = by_id["m3"]
    # 只含前日的 m1（1.5 进攻 / 0.5 失球），同日 m2 不可见
    assert (m3.prior_npxg_home, m3.prior_npxga_home, m3.prior_matches_home) == (
        1.5,
        0.5,
        1,
    )
    m4 = by_id["m4"]
    # 含开球日严格早于它的 m1+m2+m3：进攻 1.5+1.2+1.0、失球 0.5+0.8+1.0
    assert (m4.prior_npxg_home, m4.prior_npxga_home, m4.prior_matches_home) == (
        3.7,
        2.3,
        3,
    )
    m6 = by_id["m6"]
    # m5 未完场不计入：Arsenal 计数停在 4
    assert m6.prior_matches_home == 4


def test_attach_prior_skips_missing_npxg() -> None:
    """缺 npxG 的完场场跳过（不按 0 计），后续 prior 计数不涨。"""
    matches = [
        _match(
            "a",
            "2026-08-21T19:00:00",
            "71",
            "Arsenal",
            "83",
            "Coventry",
            npxg_home=None,
            npxg_away=0.5,
        ),
        _match("b", "2026-08-28T15:00:00", "71", "Arsenal", "83", "Coventry"),
    ]
    us.attach_prior(matches)
    assert matches[1].prior_npxg_home == 0.0
    assert matches[1].prior_matches_home == 0


# --- 同步链路（join + 幂等 + coverage + run 行） ---


def test_sync_joins_unique_fixture_only(db) -> None:
    competition = upsert_competition(db, "英超")
    ids = _seed_teams(db, "Arsenal", "Coventry", "Manchester City")
    # 同对阵 ±1 日两 fixture → 该窗口歧义，禁硬配
    upsert_fixture(
        db, competition, "2026-08-21T19:00:00+00:00", ids["Arsenal"], ids["Coventry"]
    )
    upsert_fixture(
        db, competition, "2026-08-22T19:00:00+00:00", ids["Arsenal"], ids["Coventry"]
    )
    fid_unique = upsert_fixture(
        db,
        competition,
        "2026-09-20T14:00:00+00:00",
        ids["Manchester City"],
        ids["Arsenal"],
    )
    with _client({"epl/2026": _doc()}) as client:
        stats = us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl",),
            seasons=("2026",),
            now=NOW,
        )
    assert stats.matches == 3
    rows = {
        str(r["match_id"]): r
        for r in db.execute("SELECT * FROM understat_matches").fetchall()
    }
    # 31180 Arsenal/Coventry：±1 窗口两个 fixture 候选 → NULL（歧义不硬配）
    assert rows["31180"]["fixture_id"] is None
    # 31181 主客 = Villa/Arsenal：Villa 无别名无 fixture → NULL
    assert rows["31181"]["fixture_id"] is None
    # 31222 = Man City/Arsenal 唯一命中
    assert rows["31222"]["fixture_id"] == fid_unique
    assert stats.joined == 1
    assert stats.unmatched == 2
    run = us.latest_sync_run(db)
    assert run["matches"] == 3
    assert run["results"] == 2
    assert run["joined"] == 1
    coverage = {
        str(r["league_key"]): str(r["coverage_status"])
        for r in db.execute(
            "SELECT * FROM source_coverage WHERE source = 'understat'"
        ).fetchall()
    }
    assert coverage == {"epl": "covered"}


def test_sync_idempotent_keeps_first_seen_and_no_result_retraction(db) -> None:
    competition = upsert_competition(db, "英超")
    ids = _seed_teams(db, "Arsenal", "Coventry")
    upsert_fixture(
        db, competition, "2026-08-21T19:00:00+00:00", ids["Arsenal"], ids["Coventry"]
    )
    with _client({"epl/2026": _doc()}) as client:
        first = us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl",),
            seasons=("2026",),
            now=NOW,
        )
        # 第二跑：31180 改判未完场（异常载荷）——完场态与数值不回撤
        doc2 = _doc()
        doc2["dates"][0]["isResult"] = False
        doc2["dates"][0]["xG"] = {"h": None, "a": None}
        second = us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl",),
            seasons=("2026",),
            now=datetime(2026, 9, 22, 1, 0, tzinfo=UTC),
        )
    assert first.observed_at != second.observed_at
    row = db.execute(
        "SELECT * FROM understat_matches WHERE match_id = '31180'"
    ).fetchone()
    assert row["is_result"] == 1
    assert row["xg_home"] == pytest.approx(1.85424)
    assert row["first_seen_at"] == NOW.isoformat(timespec="seconds")
    assert row["observed_at"] == "2026-09-22T01:00:00+00:00"
    assert (
        db.execute("SELECT COUNT(*) AS n FROM understat_matches").fetchone()["n"] == 3
    )
    runs = db.execute("SELECT COUNT(*) AS n FROM understat_sync_runs").fetchone()["n"]
    assert runs == 2


def test_sync_coverage_not_covered_and_failed(db) -> None:
    with _client({"epl/2026": _doc(), "la_liga/2026": 404}) as client:
        stats = us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl", "la_liga"),
            seasons=("2026",),
            now=NOW,
        )
    by_league = {e["league"]: e["status"] for e in stats.leagues}
    assert by_league == {"epl": "ok", "la_liga": "not_covered"}
    coverage = {
        (str(r["league_key"])): str(r["coverage_status"])
        for r in db.execute(
            "SELECT * FROM source_coverage WHERE source = 'understat'"
        ).fetchall()
    }
    assert coverage == {"epl": "covered", "la_liga": "not_covered"}
    # 非首页暖会话失败 → 整跑抛异常（fail-closed，不静默半跑）
    with pytest.raises(httpx.HTTPError):
        with _client({"epl/2026": _doc()}, base_ok=False) as client:
            us.sync_understat(
                db,
                _settings(),
                client,
                alias=alias_index(db),
                leagues=("epl",),
                seasons=("2026",),
                now=NOW,
            )


def test_sync_garbage_body_marks_failed_not_crash(db) -> None:
    """200 垃圾体（非 JSON）单联赛 failed 不炸整跑（docstring 契约）。"""
    with _client({"epl/2026": _doc(), "la_liga/2026": "<html>err</html>"}) as client:
        stats = us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl", "la_liga"),
            seasons=("2026",),
            now=NOW,
        )
    by_league = {e["league"]: e["status"] for e in stats.leagues}
    assert by_league == {"epl": "ok", "la_liga": "failed"}
    run = us.latest_sync_run(db)
    assert run["failed"] == 1
    assert run["matches"] == 3  # 只有 epl 落库
    coverage = {
        str(r["league_key"]): str(r["coverage_status"])
        for r in db.execute(
            "SELECT * FROM source_coverage WHERE source = 'understat'"
        ).fetchall()
    }
    assert coverage == {"epl": "covered", "la_liga": "fetch_failed"}


def test_join_ambiguous_day_poisons_window(db) -> None:
    """邻日唯一候选不能"救回"歧义日：±1 窗口含歧义键整体放弃（评审修正）。"""
    competition = upsert_competition(db, "英超")
    ids = _seed_teams(db, "Manchester City", "Arsenal")
    upsert_fixture(
        db,
        competition,
        "2026-09-20T14:00:00+00:00",
        ids["Manchester City"],
        ids["Arsenal"],
    )
    # 次日同对阵两 fixture → 09-21 键歧义
    upsert_fixture(
        db,
        competition,
        "2026-09-21T14:00:00+00:00",
        ids["Manchester City"],
        ids["Arsenal"],
    )
    upsert_fixture(
        db,
        competition,
        "2026-09-21T19:00:00+00:00",
        ids["Manchester City"],
        ids["Arsenal"],
    )
    with _client({"epl/2026": _doc()}) as client:
        us.sync_understat(
            db,
            _settings(),
            client,
            alias=alias_index(db),
            leagues=("epl",),
            seasons=("2026",),
            now=NOW,
        )
    row = db.execute(
        "SELECT fixture_id FROM understat_matches WHERE match_id = '31222'"
    ).fetchone()
    assert row["fixture_id"] is None
