"""源B变化时序采集测试（票 49 采集先行）：解析纯函数 + 幂等采集链路。

样本取自 2026-09-21 实测变化页（mid=1348380, pid=50）裁剪。
"""

from __future__ import annotations

import sqlite3

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.ingest import srcb
from goalx_backend.models import MatchCodeInput

SAMPLE_CHANGE = """
<html><head><meta charset="GBK"><title>欧指-数据变化列表</title></head><body>
<table>
<tr><td valign="top" class="changeMenu">
<a pid="24" href="/match/change.php?mid=1348380&pid=24&Type=Odds&c=1">9***均</a>
<a pid="50" class="changeNav selected" href="#">平**</a>
</td></tr>
<tr><td><span >1.07</span><span class="fontblue">8.93</span>
<span class="fontblue">21.28</span></td>
<td class="timetd jsChangeContent" time="09-21 14:56">赛前4分钟</td></tr>
<tr><td><span >1.07</span><span class="fontblue">9.00</span>
<span class="fontblue">21.48</span></td>
<td class="timetd jsChangeContent" time="09-21 14:55">赛前5分钟</td></tr>
<tr><td><span >1.07</span><span >7.97</span><span >16.15</span></td>
<td class="timetd jsChangeContent" time="09-21 10:09">赛前4小时51分</td></tr>
<tr><td><span >2.10</span><span >3.20</span><span >3.30</span></td>
<td class="timetd jsChangeContent" time="09-18 09:00">赛前1天5小时</td></tr>
</table></body></html>
"""


def test_minutes_before_from_label() -> None:
    assert srcb.minutes_before_from_label("赛前4分钟") == 4
    assert srcb.minutes_before_from_label("赛前4小时51分") == 291
    assert srcb.minutes_before_from_label("赛前4小时") == 240
    assert srcb.minutes_before_from_label("赛前1天5小时") == 29 * 60
    assert srcb.minutes_before_from_label("初盘") is None
    assert srcb.minutes_before_from_label("赛后复盘") is None


def test_parse_change_rows_real_shape() -> None:
    rows = srcb.parse_change_rows(SAMPLE_CHANGE)
    assert len(rows) == 4
    first = rows[0]
    assert (first.odds_h, first.odds_d, first.odds_a) == (1.07, 8.93, 21.28)
    assert first.minutes_before == 4
    assert first.time_label == "09-21 14:56"
    # 导航行（changeMenu）不产数据行；最深行混合单位解析正确
    assert rows[3].minutes_before == 29 * 60


def _client(pages: dict[tuple[str, str], str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text="home")
        if path == "/match/change.php":
            key = (request.url.params["mid"], request.url.params["pid"])
            if key not in pages:
                return httpx.Response(404, text="missing")
            return httpx.Response(200, content=pages[key].encode("gb18030"))
        return httpx.Response(200, text="ok")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings() -> Settings:
    return Settings(srcb_mobile_base="https://srcb.test")


def test_collect_idempotent_and_run_row(db) -> None:
    fixed = "2026-09-21T07:00:00+00:00"
    from datetime import UTC, datetime

    now = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    stats = srcb.collect_srcb_changes(
        db,
        _settings(),
        _client({("1348380", "50"): SAMPLE_CHANGE}),
        mids=["1348380"],
        pids=("50",),
        now=now,
        sleep_seconds=0,
    )
    assert stats.requests == 1
    assert stats.rows_added == 4
    assert stats.rows_absorbed == 0
    # 同页重拉：UNIQUE 幂等吸收，first_seen 不变
    again = srcb.collect_srcb_changes(
        db,
        _settings(),
        _client({("1348380", "50"): SAMPLE_CHANGE}),
        mids=["1348380"],
        pids=("50",),
        now=datetime(2026, 9, 21, 8, 0, tzinfo=UTC),
        sleep_seconds=0,
    )
    assert again.rows_added == 0
    assert again.rows_absorbed == 4
    row = db.execute(
        "SELECT * FROM srcb_change_rows WHERE mid='1348380' AND pid='50'"
        " AND minutes_before=4"
    ).fetchone()
    assert row["first_seen_at"] == fixed
    # 行不可变：吸收行 observed_at 保持首见值（无"最近看到"语义）
    assert row["observed_at"] == fixed
    # 页内新变化出现 → 只追加新行
    grown = SAMPLE_CHANGE.replace(
        "赛前4分钟</td></tr>",
        "赛前4分钟</td></tr>\n<tr><td><span >1.08</span><span >9.10</span>"
        '<span >22.00</span></td><td class="timetd jsChangeContent" '
        'time="09-21 14:58">赛前2分钟</td></tr>',
    )
    third = srcb.collect_srcb_changes(
        db,
        _settings(),
        _client({("1348380", "50"): grown}),
        mids=["1348380"],
        pids=("50",),
        now=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        sleep_seconds=0,
    )
    assert third.rows_added == 1
    run = srcb.latest_run(db)
    assert run["rows_added"] == 1
    assert run["requests"] == 1


def test_collect_single_failure_does_not_abort(db) -> None:
    from datetime import UTC, datetime

    stats = srcb.collect_srcb_changes(
        db,
        _settings(),
        _client({("1", "50"): SAMPLE_CHANGE}),  # ("2","50") 缺页 → 404
        mids=["1", "2"],
        pids=("50",),
        now=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
        sleep_seconds=0,
    )
    assert stats.rows_added == 4  # 成功侧不受影响
    assert stats.failed["2:50"].startswith("Client error '404")


def test_upcoming_pool_mids_window(db) -> None:
    competition = fx_store.upsert_competition(db, "胜负彩十四场")
    home = fx_store.upsert_competition  # noqa: F841 — 占位避免误用
    team_a = fx_store.upsert_team(db, "队A")
    team_b = fx_store.upsert_team(db, "队B")
    fixture = fx_store.upsert_fixture(
        db, competition, "2026-09-22T18:45:00+00:00", team_a, team_b
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="pool",
            business_date="2026-09-22",
            code="1",
            source_match_id="99001",
        ),
    )
    # pool_matches 无直接 upsert 助手——用 SQL 种子（表归 data 域，测试直写可接受先例）
    db.execute(
        """
        INSERT INTO pool_periods (market_code, period_no) VALUES ('ttt14', '26140')
        """
    )
    period_id = db.execute(
        "SELECT id FROM pool_periods WHERE period_no='26140'"
    ).fetchone()["id"]
    db.execute(
        """
        INSERT INTO pool_matches
            (pool_period_id, match_seq, source_match_id, kickoff_utc,
             home_team, away_team)
        VALUES (?, 1, '99001', '2026-09-22T18:45:00+00:00', '队A', '队B')
        """,
        (period_id,),
    )
    db.commit()
    rows = srcb.upcoming_pool_mids(db, now_utc="2026-09-21T07:00:00+00:00")
    assert [r["mid"] for r in rows] == ["99001"]
    empty = srcb.upcoming_pool_mids(db, now_utc="2026-09-23T07:00:00+00:00")
    assert empty == []


def test_empty_mids_zero_requests(db) -> None:
    from datetime import UTC, datetime

    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("无 mid 不应发请求")

    stats = srcb.collect_srcb_changes(
        db,
        _settings(),
        httpx.Client(transport=httpx.MockTransport(handler)),
        mids=[],
        now=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
        sleep_seconds=0,
    )
    assert stats.requests == 0
    assert srcb.latest_run(db) is not None


def test_rows_survive_append_only_guard(db) -> None:
    from datetime import UTC, datetime

    srcb.collect_srcb_changes(
        db,
        _settings(),
        _client({("1", "50"): SAMPLE_CHANGE}),
        mids=["1"],
        pids=("50",),
        now=datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
        sleep_seconds=0,
    )
    with db:
        try:
            db.execute("DELETE FROM srcb_change_runs")
            raise AssertionError("run 行应 append-only")
        except sqlite3.IntegrityError:
            pass
