"""票 09 fdorg 客户端单测：standings 解析（离线样本）/ 免费档码表 / 缺 key。"""

from __future__ import annotations

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.llm.fdorg import (
    FREE_COMPETITIONS,
    FdorgKeyMissing,
    fetch_standings,
    parse_standings,
)

_SAMPLE = {
    "competition": {"code": "PL"},
    "standings": [
        {
            "type": "TOTAL",
            "group": "Regular Season",
            "table": [
                {
                    "position": 1,
                    "team": {"name": "Arsenal"},
                    "points": 12,
                    "playedGames": 5,
                    "goalsFor": 11,
                    "goalsAgainst": 2,
                    "form": "WWDWW",
                },
                {
                    "position": 2,
                    "team": {"name": "Chelsea"},
                    "points": 10,
                    "playedGames": 5,
                    "goalsFor": 9,
                    "goalsAgainst": 4,
                    "form": None,
                },
                # 坏行：缺字段——跳过不炸
                {"position": 3, "team": {}},
            ],
        },
        # 欧冠式分组表：非 Regular Season 全表——跳过
        {
            "type": "TOTAL",
            "group": "Group A",
            "table": [
                {
                    "position": 1,
                    "team": {"name": "X"},
                    "points": 3,
                    "playedGames": 1,
                    "goalsFor": 1,
                    "goalsAgainst": 0,
                }
            ],
        },
    ],
}


def test_free_competition_codes() -> None:
    assert set(FREE_COMPETITIONS) == {
        "PL",
        "BL1",
        "SA",
        "PD",
        "FL1",
        "DED",
        "EDC",
        "PDP",
        "BSA",
        "CL",
        "WC",
        "EC",
    }


def test_parse_standings_regular_season_only() -> None:
    rows = parse_standings(_SAMPLE)  # type: ignore[arg-type]
    assert [r.team for r in rows] == ["Arsenal", "Chelsea"]
    top = rows[0]
    assert (top.position, top.points, top.played, top.goals_for, top.goals_against) == (
        1,
        12,
        5,
        11,
        2,
    )
    assert top.form == "WWDWW"
    assert rows[1].form is None


def test_parse_standings_malformed_empty() -> None:
    assert parse_standings({}) == []  # type: ignore[arg-type]
    assert parse_standings({"standings": []}) == []  # type: ignore[arg-type]


def test_fetch_standings_requires_key() -> None:
    settings = Settings(fdorg_api_key="")
    with httpx.Client() as client, pytest.raises(FdorgKeyMissing):
        fetch_standings(client, settings, "PL")


def test_fetch_standings_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/v4/competitions/PL/standings")
            assert request.headers["X-Auth-Token"] == "k"
            return httpx.Response(200, json=_SAMPLE)  # type: ignore[arg-type]

    settings = Settings(fdorg_api_key="k")
    with httpx.Client(transport=_FakeTransport()) as client:
        rows = fetch_standings(client, settings, "PL")
    assert rows[0].team == "Arsenal"
