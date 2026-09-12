"""sporttery 竞彩采集测试（解析纯函数 + 入库幂等）。"""

from __future__ import annotations

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.ingest import sporttery
from goalx_backend.store import fixtures as fx_store

SAMPLE = {
    "errorCode": "0",
    "value": {
        "matchInfoList": [
            {
                "businessDate": "2026-09-12",
                "subMatchList": [
                    {
                        "matchId": 2041430,
                        "matchNumStr": "周六026",
                        "leagueAbbName": "荷甲",
                        "leagueAllName": "荷兰甲级联赛",
                        "homeTeamAllName": "福图纳锡塔德",
                        "awayTeamAllName": "阿贾克斯",
                        "matchDate": "2026-09-13",
                        "matchTime": "02:00:00",
                        "bettingSingle": 0,
                        "had": {
                            "a": "1.28",
                            "d": "5.10",
                            "h": "6.60",
                            "updateDate": "2026-09-12",
                            "updateTime": "22:29:36",
                        },
                        "hhad": {
                            "a": "1.83",
                            "d": "4.10",
                            "h": "2.95",
                            "goalLine": "+1",
                            "goalLineValue": "+1.00",
                            "updateDate": "2026-09-12",
                            "updateTime": "22:29:27",
                        },
                        "crs": {
                            "s00s00": "32.00",
                            "s01s00": "26.00",
                            "s02s01": "9.00",
                            "s1sh": "40.00",
                            "s1sd": "18.00",
                            "s1sa": "6.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "21:10:00",
                        },
                        "ttg": {
                            "s0": "32.00",
                            "s3": "3.70",
                            "s7": "11.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "17:56:39",
                        },
                        "hafu": {
                            "aa": "1.75",
                            "hh": "12.50",
                            "dh": "18.00",
                            "updateDate": "2026-09-12",
                            "updateTime": "19:49:59",
                        },
                    }
                ],
            }
        ]
    },
}


def test_parse_matches_full_shape() -> None:
    matches = sporttery.parse_matches(SAMPLE)
    assert len(matches) == 1
    m = matches[0]
    assert m.source_match_id == "2041430"
    assert m.code == "周六026"
    assert m.kickoff_utc == "2026-09-12T18:00:00+00:00"  # CST 02:00 → UTC 前一日 18:00
    assert m.is_single is False


def test_parse_market_selections() -> None:
    m = sporttery.parse_matches(SAMPLE)[0]
    by_code = {q.market_code: q for q in m.markets}
    assert by_code["had"].prices == {"h": 6.60, "d": 5.10, "a": 1.28}
    assert by_code["hhad"].goal_line == "+1"
    assert by_code["crs"].prices["0:0"] == 32.0
    assert by_code["crs"].prices["2:1"] == 9.0
    assert by_code["crs"].prices["h_other"] == 40.0
    assert by_code["ttg"].prices["3"] == 3.70
    assert by_code["hafu"].prices["aa"] == 1.75
    # 调盘时点随市场各自保留（CST→UTC）
    assert by_code["had"].captured_at == "2026-09-12T14:29:36+00:00"
    assert by_code["ttg"].captured_at == "2026-09-12T09:56:39+00:00"


def test_store_matches_idempotent(db) -> None:
    matches = sporttery.parse_matches(SAMPLE)
    first = sporttery.store_matches(db, matches)
    second = sporttery.store_matches(db, matches)
    assert first.matches == 1
    assert first.snapshots > 0
    assert second.snapshots == 0  # append-only 去重
    assert second.duplicate_snapshots == first.snapshots
    rows = fx_store.fixtures_for_business_date(db, "2026-09-12")
    assert len(rows) == 1
    assert rows[0]["competition_tier"] == "tier2"  # 荷甲
    history = fx_store.odds_history(db, int(rows[0]["id"]), "had")
    assert len(history) == 3  # h/d/a 各一条，重跑不增


def test_fetch_uses_referer_and_retries_payload(db) -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        assert "poolCode" in str(request.url)
        return httpx.Response(200, json=SAMPLE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = sporttery.fetch_calculator_payload(Settings(), client)
    assert payload["errorCode"] == "0"
    assert seen_headers["referer"] == "https://www.sporttery.cn/"


def test_fetch_raises_on_error_code() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorCode": "E0001"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="E0001"):
        sporttery.fetch_calculator_payload(Settings(), client)
