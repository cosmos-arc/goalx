"""新浪 AI 网关伤停采集测试（票 09 补六落地面）：解析/映射/幂等/故障隔离。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from goalx_backend.db import connect, migrate
from goalx_backend.llm.sina_intel import (
    collect_sina_injury_intel,
    parse_injuries,
    parse_on_sell,
    sina_stats_dict,
)

_KICKOFF = "2026-09-20T08:00:00+00:00"  # 16:00 北京


def _seed_jc_fixture(conn: sqlite3.Connection, code: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('日职联', 'tier2', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = conn.execute(
        "SELECT id FROM competitions WHERE name = '日职联'"
    ).fetchone()["id"]
    ids = {}
    for name in ("队甲", "队乙"):
        cur = conn.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    fixture_id = conn.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id,"
        " away_team_id) VALUES (?, ?, ?, ?)",
        (comp_id, _KICKOFF, ids["队甲"], ids["队乙"]),
    ).lastrowid
    conn.execute(
        "INSERT INTO match_codes (fixture_id, kind, code, business_date)"
        " VALUES (?, 'jingcai', ?, '2026-09-20')",
        (fixture_id, code),
    )
    return int(fixture_id)


def _on_sell_payload(match_no: str = "周日002", match_id: str = "3751658") -> dict:
    return {
        "result": {
            "data": [
                {
                    "matchNo": match_no,
                    "matchId": match_id,
                    "team1": "町田泽维",
                    "team2": "柏太阳神",
                    "matchTime": "1789891200",  # 北京 16:00 = UTC 08:00
                },
                {"matchNo": "", "matchId": "999"},  # 无编号——剔除
            ]
        }
    }


def _injury_payload() -> dict:
    return {
        "result": {
            "data": {
                "team1": [
                    {
                        "playerName": "冈村大八",
                        "positionCn": "后卫",
                        "typeCn": "出战成疑",
                        "reason": "无法出战",
                        "missedMatches": "0",
                        "startTime": "0",
                        "endTime": "0",
                    },
                    {
                        "playerName": "伤员乙",
                        "positionCn": "前锋",
                        "typeCn": "受伤",
                        "reason": "腿筋",
                        "missedMatches": "4",
                        "startTime": "1772726400",
                        "endTime": "0",
                    },
                ],
                "team2": [],
            }
        }
    }


def _client_with(on_sell: dict | None, injury: dict | None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "jczqOnSellMatches" in url:
            if on_sell is None:
                return httpx.Response(500)
            return httpx.Response(200, json=on_sell)
        if "footballMatchTeamInjury" in url:
            if injury is None:
                return httpx.Response(500)
            return httpx.Response(200, json=injury)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(tmp_path / "sina-test.db")
    migrate(conn)
    yield conn
    conn.close()


def test_parse_on_sell_filters_and_converts_kickoff() -> None:
    rows = parse_on_sell(_on_sell_payload())
    assert len(rows) == 1
    row = rows[0]
    assert row["matchNo"] == "周日002"
    assert row["kickoff"] == "2026-09-20T08:00:00+00:00"


def test_parse_injuries_fields() -> None:
    injuries = parse_injuries(_injury_payload())
    assert len(injuries.team1) == 2
    assert injuries.team1[0].name == "冈村大八"
    assert injuries.team1[0].type_cn == "出战成疑"
    assert injuries.team1[1].missed_matches == "4"
    assert injuries.team2 == []
    # data 缺失 → 空结构（不虚构）
    assert parse_injuries({"result": {}}).is_empty()


def test_collect_maps_match_no_and_inserts(db: sqlite3.Connection) -> None:
    fixture_id = _seed_jc_fixture(db, "周日002")
    client = _client_with(_on_sell_payload(), _injury_payload())
    stats = collect_sina_injury_intel(db, client)
    assert stats.on_sell == 1
    assert stats.mapped == 1
    assert stats.inserted == 1
    row = db.execute(
        "SELECT kind, text, source, collector FROM intel_observations"
        " WHERE fixture_id = ?",
        (fixture_id,),
    ).fetchone()
    assert row["collector"] == "sina-injury"
    assert row["source"] == "sina.com/gateway"
    assert "冈村大八" in row["text"]
    assert "缺4场" in row["text"]
    # 幂等：同内容重跑零新行
    stats2 = collect_sina_injury_intel(
        db, _client_with(_on_sell_payload(), _injury_payload())
    )
    assert stats2.inserted == 0
    assert stats2.skipped_known == 1
    assert sina_stats_dict(stats2)["skipped_known"] == 1


def test_collect_skips_unmapped_and_keeps_going_on_fetch_failure(
    db: sqlite3.Connection,
) -> None:
    """在售列表 500 → 整体失败计数；单场伤停 500 → 跳过不阻塞（两轮验证）。"""
    fixture_id = _seed_jc_fixture(db, "周日002")
    # 列表挂 → on_sell=0 记 fetch_failed，零落库不炸
    stats = collect_sina_injury_intel(db, _client_with(None, _injury_payload()))
    assert stats.on_sell == 0
    assert stats.fetch_failed == 1
    assert db.execute("SELECT COUNT(*) c FROM intel_observations").fetchone()["c"] == 0
    # 单场伤停挂 → mapped 计数保留、inserted=0
    stats2 = collect_sina_injury_intel(db, _client_with(_on_sell_payload(), None))
    assert stats2.mapped == 1
    assert stats2.inserted == 0
    assert stats2.fetch_failed == 1
    assert db.execute("SELECT COUNT(*) c FROM intel_observations").fetchone()["c"] == 0
    assert fixture_id  # 映射存在但无数据落库（诚实零行）


def test_collect_no_injury_data_is_zero_rows(db: sqlite3.Connection) -> None:
    """网关无伤停数据（data 空）——不产情报（宁缺毋假）。"""
    _seed_jc_fixture(db, "周日002")
    empty = {"result": {"data": {"team1": [], "team2": []}}}
    stats = collect_sina_injury_intel(db, _client_with(_on_sell_payload(), empty))
    assert stats.mapped == 1
    assert stats.inserted == 0
    assert stats.fetch_failed == 0
