"""
uniform 官方赛果采集测试（票 44）：真实载荷 fixture 解析 + 注入 client
的同步对账链路。

fixture 为 2026-09-13..20 实测载荷（138 场，五种枚举形态齐）：
'2'+Payout（正常派彩）、'2'+空（完场未派彩）、'2'+Refund（无效场次）、
'0'（取消）、'1'+Close（未完场）。HTTP 层用 httpx.MockTransport 仿分页。
测试不打真实外网。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.fixtures import (
    upsert_competition,
    upsert_fixture,
    upsert_match_code,
    upsert_team,
)
from goalx_backend.data.ingest import uniform
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import DrawResultInput, MatchCodeInput

FIXTURES = Path(__file__).parent.parent / "fixtures"
PAYLOAD = json.loads(
    (FIXTURES / "uniform_2026-09-13_20.json").read_text(encoding="utf-8")
)
ROWS = PAYLOAD["value"]["matchResult"]
NOW = datetime(2026, 9, 21, 0, 30, tzinfo=UTC)  # 窗口次日晨（08:30 对账拍）


def _settings() -> Settings:
    return Settings(
        sporttery_uniform_url="https://uniform.test/getUniformMatchResultV1.qry"
    )


def _client(payload: dict) -> httpx.Client:
    """仿分页：按 pageNo 切片返回 fixture 行（pageSize=30 同产线）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("pageNo", "1"))
        batch = ROWS[(page - 1) * 30 : page * 30]
        value = dict(PAYLOAD["value"], matchResult=batch)
        return httpx.Response(
            200, json={"success": True, "errorCode": "0", "value": value}
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _row(match_id: int) -> dict:
    return next(r for r in ROWS if r["matchId"] == match_id)


def _seed(db, match_id: int, code: str, business_date: str, kickoff: str) -> int:
    """按官方 matchId 建竞彩场次（source_match_id 一跳 join 的键）。"""
    competition = upsert_competition(db, "测试联赛")
    home = upsert_team(db, f"主{match_id}")
    away = upsert_team(db, f"客{match_id}")
    fid = upsert_fixture(db, competition, kickoff, home, away)
    upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid,
            kind="jingcai",
            business_date=business_date,
            code=code,
            source_match_id=str(match_id),
        ),
    )
    return fid


# --- 解析（纯函数，锚定实测锚点行） ---


def test_parse_payout_finished() -> None:
    obs = uniform.parse_uniform_match(_row(2041586))  # 周日001 亚运男足 0:0
    assert obs.final is True
    assert obs.void is False
    assert obs.reject_reason is None
    assert (obs.home_goals, obs.away_goals) == (0, 0)
    assert (obs.half_home_goals, obs.half_away_goals) == (0, 0)
    assert obs.win_flag == "D"
    assert obs.match_num_str == "周日001"
    assert obs.league_id == 83


def test_parse_refund_is_void() -> None:
    obs = uniform.parse_uniform_match(_row(2041505))  # 周三014 西甲 无效场次
    assert obs.void is True
    assert obs.void_reason == "官方无效场次(Refund)"
    assert obs.final is False  # 比分为"无效场次"非数字
    assert obs.reject_reason is None


def test_parse_cancelled_is_void() -> None:
    obs = uniform.parse_uniform_match(_row(2041478))  # 周一013 亚运女足 取消
    assert obs.void is True
    assert obs.void_reason == "官方取消"


def test_parse_close_not_final_not_void() -> None:
    obs = uniform.parse_uniform_match(_row(2041588))  # 周日003 日职 未完场
    assert obs.final is False
    assert obs.void is False
    assert obs.reject_reason is None  # 未终态跳过，不算拒因


def test_parse_pool_status_empty_still_final() -> None:
    obs = uniform.parse_uniform_match(_row(2041584))  # 周六029 葡超 2:2 未派彩
    assert obs.final is True
    assert obs.pool_status == ""
    assert (obs.home_goals, obs.away_goals) == (2, 2)


def test_parse_rejects_inconsistent_win_flag() -> None:
    raw = dict(_row(2041586), winFlag="H")  # 0:0 但标主胜
    assert uniform.parse_uniform_match(raw).reject_reason == "win_flag_inconsistent"


def test_parse_rejects_unknown_status() -> None:
    raw = dict(_row(2041586), matchResultStatus="9")
    obs = uniform.parse_uniform_match(raw)
    assert obs.reject_reason is not None
    assert obs.reject_reason.startswith("unknown_result_status")


def test_parse_rejects_unparseable_final_score() -> None:
    raw = dict(_row(2041586), sectionsNo999="腰斩", matchResultStatus="2")
    obs = uniform.parse_uniform_match(raw)
    assert obs.reject_reason is not None
    assert obs.reject_reason.startswith("unparseable_score")


def test_fetch_paginates_all_rows() -> None:
    with _client(PAYLOAD) as client:
        rows = uniform.fetch_uniform_results(
            client, _settings(), "2026-09-13", "2026-09-20"
        )
    assert len(rows) == len(ROWS) == 138


# --- 同步链路（join + 观测落库 + 对账 + coverage） ---


def test_sync_reconciles_against_stored_results(db) -> None:
    fid_payout = _seed(
        db, 2041586, "周日001", "2026-09-20", "2026-09-20T11:00:00+00:00"
    )
    fid_empty_pool = _seed(
        db, 2041584, "周六029", "2026-09-19", "2026-09-19T20:00:00+00:00"
    )
    fid_refund = _seed(
        db, 2041505, "周三014", "2026-09-16", "2026-09-16T18:00:00+00:00"
    )
    _seed(db, 2041478, "周一013", "2026-09-14", "2026-09-14T10:00:00+00:00")
    fid_close = _seed(db, 2041588, "周日003", "2026-09-20", "2026-09-20T09:00:00+00:00")
    # 周六029 2:2 已由源D 落库 → 一致；周三014 已录比分但官方判无效 → void 冲突
    import_draw_results(
        db,
        [
            DrawResultInput(
                fixture_id=fid_empty_pool, home_goals=2, away_goals=2, source="500.com"
            ),
            DrawResultInput(
                fixture_id=fid_refund, home_goals=1, away_goals=0, source="manual"
            ),
        ],
    )
    with _client(PAYLOAD) as client:
        sync_stats, rec_stats = uniform.sync_uniform_results(
            db,
            _settings(),
            client,
            business_dates=["2026-09-14", "2026-09-16", "2026-09-19", "2026-09-20"],
            now=NOW,
        )

    assert sync_stats.fetched == 138
    assert sync_stats.observed_rows == 138
    assert sync_stats.unmatched == 133  # 138 - 5 个已建场次
    # 对账：2:2 一致；无效场次 vs 已录比分 → void_mismatch；
    # 周日001/周一013 官方终态但库内无 → missing；周日003 未完场不比
    assert rec_stats.compared == 4
    assert rec_stats.consistent == 1
    assert rec_stats.void_mismatch == 1
    assert rec_stats.missing_result == 2
    reasons = {e["reason"] for e in rec_stats.pending_manual}
    assert reasons == {
        "stored_result_vs_reference_void",
        "reference_final_missing_fact",
        "reference_void_missing_fact",
    }
    # 并行对账不改事实源：库内无新赛果（周日001 仍无记录）
    assert rs_store.get_draw_result(db, fid_payout) is None
    assert rs_store.get_draw_result(db, fid_close) is None
    # 观测行落库且 void 标记正确
    refund_obs = db.execute(
        "SELECT void_flag, void_reason FROM uniform_result_observations"
        " WHERE match_id = 2041505"
    ).fetchone()
    assert int(refund_obs["void_flag"]) == 1
    assert refund_obs["void_reason"] == "官方无效场次(Refund)"


def test_sync_observation_idempotent_same_run(db) -> None:
    _seed(db, 2041586, "周日001", "2026-09-20", "2026-09-20T11:00:00+00:00")
    with _client(PAYLOAD) as client:
        first, _ = uniform.sync_uniform_results(
            db, _settings(), client, business_dates=["2026-09-20"], now=NOW
        )
        second, _ = uniform.sync_uniform_results(
            db, _settings(), client, business_dates=["2026-09-20"], now=NOW
        )
    assert first.observed_rows == 138
    assert second.observed_rows == 0  # 同 observed_at 幂等
    n = db.execute("SELECT COUNT(*) FROM uniform_result_observations").fetchone()[0]
    assert n == 138


def test_sync_observations_append_only(db) -> None:
    _seed(db, 2041586, "周日001", "2026-09-20", "2026-09-20T11:00:00+00:00")
    with _client(PAYLOAD) as client:
        uniform.sync_uniform_results(
            db, _settings(), client, business_dates=["2026-09-20"], now=NOW
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM uniform_result_observations")


def test_sync_records_coverage_per_day_and_league(db) -> None:
    _seed(db, 2041586, "周日001", "2026-09-20", "2026-09-20T11:00:00+00:00")
    with _client(PAYLOAD) as client:
        uniform.sync_uniform_results(
            db,
            _settings(),
            client,
            business_dates=["2026-09-20", "2026-09-12"],
            now=NOW,
        )
    rows = db.execute(
        "SELECT coverage_date, league_key, match_count, coverage_status"
        " FROM source_coverage WHERE source = 'sporttery.cn' ORDER BY coverage_date"
    ).fetchall()
    by_date = {r["coverage_date"]: r for r in rows}
    # 09-20 亚运男足（league 83）有行 → covered
    assert by_date["2026-09-20"]["coverage_status"] == "covered"
    # 09-12 在请求区间内但无任何场次 → fetched_empty（空≠无）
    assert by_date["2026-09-12"]["match_count"] == 0
    assert by_date["2026-09-12"]["coverage_status"] == "fetched_empty"


def test_sync_widens_match_date_range_and_propagates_unmatched(db) -> None:
    # 采集区间按 matchDate 放宽 ±1（晚场归属前业务日）；unmatched 传播到对账 run 行
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["from"] = str(request.url.params.get("matchBeginDate"))
        seen["to"] = str(request.url.params.get("matchEndDate"))
        value = dict(PAYLOAD["value"], matchResult=ROWS[:1], pages=1)
        return httpx.Response(
            200, json={"success": True, "errorCode": "0", "value": value}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, rec_stats = uniform.sync_uniform_results(
            db, _settings(), client, business_dates=["2026-09-20"], now=NOW
        )
    assert seen == {"from": "2026-09-19", "to": "2026-09-21"}
    row = db.execute(
        "SELECT unmatched, parse_version FROM draw_reconciliation_runs ORDER BY id DESC"
    ).fetchone()
    assert int(row["unmatched"]) == rec_stats.unmatched == 1
    assert row["parse_version"] == "uniform_v1"


def test_sync_zero_cost_skip_still_records_run(db) -> None:
    # 无候选业务日 → 不发请求，仍留 compared=0 的对账运行证据
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("zero-cost skip 不应发请求")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sync_stats, rec_stats = uniform.sync_uniform_results(
            db, _settings(), client, now=NOW
        )
    assert sync_stats.fetched == 0
    assert rec_stats.compared == 0
    row = db.execute(
        "SELECT compared, source FROM draw_reconciliation_runs ORDER BY id DESC"
    ).fetchone()
    assert row["source"] == "sporttery.cn"
    assert int(row["compared"]) == 0
