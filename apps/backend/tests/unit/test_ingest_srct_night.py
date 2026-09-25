"""源T夜班调度与请求预算测试（票 55 切片 13）：季窗清单 + 高位夜班接缝。

合成页面（结构对齐 2026-09 实测裁剪样本）走 MockTransport：真源网络
永不进测试——夜班实探才是真验证（spec 测试接缝裁定）。端点模板一律
`.test` 占位域（代称红线）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from limits import RateLimitItemPerMinute

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_night

NOW_IN_WINDOW = datetime(2026, 9, 24, 2, 0, 0)  # 深夜窗口内（01:00-08:00）
TODAY = date(2026, 9, 24)
# 小季窗：3 日 × 每日 1 场（91001/91002/91003），夜班全场景够用
TEST_SEASONS = (srct_night.SeasonWindow("2025/26", "2025-10-01", "2025-10-03"),)
DATES = ["2025-10-01", "2025-10-02", "2025-10-03"]
DATE_TO_SID = dict(zip(DATES, ["91001", "91002", "91003"], strict=True))


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶：noop sleeper 下 20/min 窗会忙转到真实时间翻窗。

    防封语义本身在 test_ingest_srct（真实 sleep 接缝）与本文件
    NightBudget 单测里钉死，这里只测夜班编排。
    """
    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


DAY_404_BYTES = ("<html><body><img src='/image/error_404.gif'></body></html>").encode(
    "gb18030"
)


def _day_page(sid: str) -> bytes:
    row = (
        "<tr height=18 align=center>"
        "<td><span>英超</span></td><td>1日20:00</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


ODDS_JS = (
    'var matchname_cn="英超";var ScheduleID=91001;'
    'game=Array("1129|1|Lottery Official|3.2|3.4|2.1|27|26|47|88|'
    '2.9|3.1|2.2|30|27|43|88|0.85|0.85|0.93|2025,10-1,18,10,28,00|");\n'
    "gameDetail=Array();\n"
).encode()
# 亚盘多庄页（票 59）：一行一书=正证据（结构对齐 2026-09-25 实测，名打码）
ASIANODDS_BYTES = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商8 封</td><td></td>"
    "<td>0.90</td><td>受让半球</td><td>0.95</td>"
    "<td>2.65</td><td>平手/半球</td><td>0.27</td>"
    "<td>0.80</td><td>受让平手/半球</td><td>1.05</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td>"
    "</tr></table></body></html>"
).encode()
# 大小球多庄页（票 60）：与亚盘多庄同构，线=进球数盘口线
OVERDOWN_BYTES = (
    "<html><head><title>甲VS乙-大小指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商1 封</td><td></td>"
    "<td>0.93</td><td>2.5/3</td><td>0.87</td>"
    "<td>1.25</td><td>2.5</td><td>0.50</td>"
    "<td>0.80</td><td>2.5</td><td>1.00</td>"
    "<td><a href=/changeDetail/overunder.aspx?id=1&companyID=1>详</a></td>"
    "</tr></table></body></html>"
).encode()
# 详情页（票 61）：技统条一行=正证据（结构对齐实测，球员名合成）
DETAIL_BYTES = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body>"
    "<div class='title'> 首发阵容</div>"
    "<ul><li class='lists'><div class='data'><span >3</span><span>角球</span>"
    "<span >3</span></div></li></ul>"
    "</body></html>"
).encode()
STATS_HTML = (
    '<html><body><script>var jsonData = {"techStat":{"itemList":['
    '{"home":{"value":0.71},"away":{"value":1.68},"name":"预期进球",'
    '"kind":"EXPECTED_GOALS"}]},"info":{}};</script></body></html>'
).encode()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}",
        srct_overdown_url="https://srct.test/overdown/{sid}",
        srct_detail_url="https://srct.test/detail/{sid}cn.htm",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _transport_spy() -> tuple[list[httpx.Request], dict[str, httpx.Response]]:
    """请求记录器 + 全日期全端点可编程响应表。"""
    seen: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {}
    for day, sid in DATE_TO_SID.items():
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid)
        )
        routes[f"odds:{sid}"] = httpx.Response(200, content=ODDS_JS)
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_HTML)
    return seen, routes


def _client(
    seen: list[httpx.Request], routes: dict[str, httpx.Response]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.startswith("/over/"):
            key = f"day:{path.removeprefix('/over/').removesuffix('.htm')}"
        elif path.startswith("/odds/"):
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        elif path.startswith("/asian/"):
            key = f"ah:{path.removeprefix('/asian/')}"
        elif path.startswith("/overdown/"):
            key = f"ou:{path.removeprefix('/overdown/')}"
        elif path.startswith("/detail/"):
            key = f"dt:{path.removeprefix('/detail/').removesuffix('cn.htm')}"
        else:
            key = f"stats:{path.removeprefix('/shijian/').removesuffix('.htm')}"
        if key not in routes:
            return httpx.Response(500, text="boom")
        return routes[key]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(
    tmp_path: Path,
    seen: list[httpx.Request],
    routes: dict[str, httpx.Response],
    **kwargs: Any,
) -> srct_night.SrctNightSummary:
    kwargs.setdefault("now_fn", lambda: NOW_IN_WINDOW)
    kwargs.setdefault("seasons", TEST_SEASONS)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        return srct_night.run_night(
            store,
            settings,
            _client(seen, routes),
            today=TODAY,
            sleeper=lambda _s: None,
            **kwargs,
        )
    finally:
        store.close()


def _store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(_settings(tmp_path).corpus_root)


def test_night_budget_guards() -> None:
    budget = srct.NightBudget(request_cap=3, failure_streak_cap=2)
    budget.charge(2)
    with pytest.raises(srct.BudgetExhausted):
        budget.charge(1)  # 3 >= 3 触顶
    budget2 = srct.NightBudget(failure_streak_cap=2)
    budget2.note(True)
    budget2.note(False)  # 成功清零
    budget2.note(True)
    with pytest.raises(srct.CircuitOpen):
        budget2.note(True)  # 连败 2 熔断


def test_phase1_task_list_shape() -> None:
    tasks = srct_night.phase1_dates(TODAY)
    labels = [s for s, _ in tasks]
    # 最新季优先 + 季内最新日倒序；当季上界 = 前天（today-2）
    assert labels[0] == "2026/27"
    assert labels[0] == labels[1] == labels[2]
    assert tasks[0][1] == "2026-09-22"  # TODAY-2：完场稳态上界
    assert labels[-1] == "2023/24"
    assert tasks[-1][1] == "2023-08-01"
    assert set(labels) == {"2026/27", "2025/26", "2024/25", "2023/24"}
    days = [d for _, d in tasks]
    assert days == sorted(days, reverse=True)  # 整表单调（全局倒序）


def test_season_windows_are_contiguous_twelve_months() -> None:
    # 季窗 8/1-次年 7/31 连续 12 个月：挪超/瑞超夏季历不吃空洞
    days = srct_night.season_dates(srct_night.PHASE1_SEASONS[1], TODAY)
    assert days[0] == "2025-08-01"
    assert days[-1] == "2026-07-31"
    assert len(days) == 365


def test_pending_excludes_settled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.ensure_tree()
    store.set_day_status("2025-10-02", "done")
    store.set_day_status("2025-10-03", "not_found")
    pending = srct_night.pending_dates(store, TODAY, TEST_SEASONS)
    assert [d for _, d in pending] == ["2025-10-01"]


def test_run_night_completes_and_persists(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    summary = _run(tmp_path, seen, routes)
    assert summary.stop_reason == "completed"
    assert summary.dates_done == 3
    assert summary.requests == 18  # 3 日页 + 3×5 端点
    assert summary.raw_new == 15  # 场次端点页（日页另计，沿切片 11 口径）
    assert summary.parsed_ok == 18  # 15 端点 + 3 日页 bronze 行
    assert summary.xg_matches == 3
    assert summary.failed_count == 0
    assert summary.pending_before == 3
    assert summary.pending_after == 0
    store = _store(tmp_path)
    rows = store.night_summaries()
    assert len(rows) == 1
    assert rows[0]["stop_reason"] == "completed"
    assert rows[0]["requests"] == 18
    assert json.loads(str(rows[0]["failed_json"])) == {}
    for day in DATES:
        assert store.verify_raw("srct", "day_page", day, ext=".htm")
    # 日级台账：done 三日全报
    assert store.day_status_dates("done") == set(DATES)


def test_second_night_zero_requests_heartbeat(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    _run(tmp_path, seen, routes)
    wire_after_night1 = len(seen)
    summary = _run(tmp_path, seen, routes)
    assert len(seen) == wire_after_night1  # 断点续传零重抓
    assert summary.stop_reason == "completed"
    assert summary.requests == 0
    assert summary.dates_attempted == 0
    assert summary.pending_before == 0
    assert len(_store(tmp_path).night_summaries()) == 2  # 心跳也留行


def test_budget_stop_cross_night_resume(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    night1 = _run(tmp_path, seen, routes, request_cap=6)
    # 预算 6：日1 末端点（第 6 请求）计费触顶未发出——日1 无 done，次夜补
    assert night1.stop_reason == "budget"
    assert night1.requests == 6  # 预算口径：第 6 次计费触顶未发出
    assert len(seen) == 5  # 真实线上 = 已发出 5 次
    assert night1.dates_done == 0
    assert night1.pending_after == 3  # 日1 无 done，三日全 pending
    day2_raw = _store(tmp_path).has("srct", "day_page", "2025-10-02")
    assert not day2_raw  # 中断日的日页请求未发出、raw 未落
    night2 = _run(tmp_path, seen, routes)
    assert night2.stop_reason == "completed"
    assert night2.dates_done == 3  # 日1（只补末端点）+ 日2/日3 全量
    assert night2.requests == 13  # 1 + 6 + 6：日1 末端点 + 日2/日3 全量
    # 两夜合计线上请求 = 全集 18，零浪费
    assert len(seen) == 18
    assert _store(tmp_path).day_status_dates("done") == set(DATES)


def test_circuit_breaker_stops_night(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    breaker_seasons = (srct_night.SeasonWindow("2025/26", "2025-10-01", "2025-10-05"),)
    for sid in ("91001", "91002", "91003"):
        routes.pop(f"odds:{sid}")  # 全端点 500 → 每场必败
        routes.pop(f"ah:{sid}")
        routes.pop(f"ou:{sid}")
        routes.pop(f"stats:{sid}")
        routes.pop(f"dt:{sid}")
    # 补出 4/5 两日的日页路由（ breaker 季窗 5 日）
    for day, sid in (("2025-10-04", "91004"), ("2025-10-05", "91005")):
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid)
        )
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        summary = srct_night.run_night(
            store,
            settings,
            _client(seen, routes),
            today=TODAY,
            now_fn=lambda: NOW_IN_WINDOW,
            sleeper=lambda _s: None,
            seasons=breaker_seasons,
        )
    finally:
        store.close()
    assert summary.stop_reason == "circuit"
    # 5 场连败熔断：日页 5 + 每场 5 端点 × 4 次尝试 = 105 请求即收手
    assert summary.requests == 5 + 5 * 5 * (srct.MAX_RETRIES + 1)
    assert summary.failed_count == 25
    assert summary.dates_done == 4  # 前四日干净返回（场败不拦 done）
    assert summary.pending_after == 1  # 熔断日次夜再试
    assert _store(tmp_path).night_summaries()[0]["stop_reason"] == "circuit"


def test_not_found_day_recorded_and_not_refetched(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["day:20251002"] = httpx.Response(200, content=DAY_404_BYTES)
    night1 = _run(tmp_path, seen, routes)
    assert night1.dates_done == 2
    assert night1.dates_not_found == 1
    assert _store(tmp_path).day_status_dates("not_found") == {"2025-10-02"}
    wire_after_night1 = len(seen)
    night2 = _run(tmp_path, seen, routes)
    assert len(seen) == wire_after_night1  # not_found 日不再耗请求
    assert night2.pending_before == 0  # 待办清零（done×2 + not_found×1）
    assert night2.stop_reason == "completed"


def test_window_closed_no_work_no_row(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    summary = _run(tmp_path, seen, routes, now_fn=lambda: datetime(2026, 9, 24, 12, 0))
    assert summary.stop_reason == "window_closed"
    assert seen == []  # 窗口外零请求
    assert _store(tmp_path).night_summaries() == []  # 没干活不留行


def test_day_page_transport_failure_continues(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["day:20251002"] = httpx.Response(502, text="bad gateway")
    summary = _run(tmp_path, seen, routes)
    assert summary.stop_reason == "completed"  # 单日页失败不炸整夜
    assert summary.dates_done == 2
    assert "2025-10-02:day_page" in summary.failed
    assert summary.pending_after == 1  # 失败日无 done，次夜重试
    # 摘要 requests = 预算口径：日页重试 4 次全计入（stats 丢弃路径不丢账）
    assert summary.requests == 6 + (srct.MAX_RETRIES + 1) + 6
    assert sum(1 for r in seen if "20251002" in r.url.path) == srct.MAX_RETRIES + 1


def test_consecutive_fake_200_day_pages_trip_circuit(tmp_path: Path) -> None:
    """软封锁以伪 200 日页呈现：不许绕开熔断整夜烧完/not_found 全表。"""
    seen, routes = _transport_spy()
    for day in DATES:  # 三日全伪 200 + 断点处仍差 2 败 → 用 5 日季窗钉死熔断
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=DAY_404_BYTES
        )
    seasons5 = (srct_night.SeasonWindow("2025/26", "2025-10-01", "2025-10-05"),)
    for day in ("2025-10-04", "2025-10-05"):
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=DAY_404_BYTES
        )
    summary = _run(tmp_path, seen, routes, seasons=seasons5)
    assert summary.stop_reason == "circuit"
    assert summary.dates_not_found == 5  # 第 5 日也先记账再熔断，当晚收手
    assert summary.requests == 5  # 每伪 200 日恰 1 请求（内容错误不重试）
    # 摘要留痕 + 已记的 not_found 不回滚（次夜晨检人工判）
    assert _store(tmp_path).night_summaries()[0]["stop_reason"] == "circuit"


def test_window_rechecked_per_date_mid_night(tmp_path: Path) -> None:
    """长夜越 08:00：逐日复判，越界即收手（已干活的夜照落摘要行）。"""
    seen, routes = _transport_spy()
    ticks = iter(
        [
            datetime(2026, 9, 24, 7, 59),  # 开跑（窗内）
            datetime(2026, 9, 24, 7, 59),  # 日1 前复判（窗内）
            datetime(2026, 9, 24, 8, 0),  # 日2 前复判（越界）
            datetime(2026, 9, 24, 8, 0),  # ended_at
        ]
    )
    summary = _run(tmp_path, seen, routes, now_fn=lambda: next(ticks))
    assert summary.stop_reason == "window_closed"
    assert summary.dates_done == 1  # 日1 完成后越界收手
    assert summary.pending_after == 2
    assert summary.requests == 6
    assert len(_store(tmp_path).night_summaries()) == 1  # 干过活照留行


def test_cli_run_and_list_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from goalx_backend import cli

    seen, routes = _transport_spy()
    args = cli.build_parser().parse_args(["srct-night", "--request-cap", "20"])
    cli._cmd_srct_night(
        args,
        settings=_settings(tmp_path),
        client=_client(seen, routes),
        now_fn=lambda: NOW_IN_WINDOW,
        sleeper=lambda _s: None,
        seasons=TEST_SEASONS,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["stop_reason"] == "completed"
    assert payload["requests"] == 18
    assert payload["dates_done"] == 3
    assert payload["failed"] == {}

    list_args = cli.build_parser().parse_args(["srct-night", "--list"])
    cli._cmd_srct_night(list_args, settings=_settings(tmp_path))
    nights = json.loads(capsys.readouterr().out)["nights"]
    assert len(nights) == 1
    assert nights[0]["stop_reason"] == "completed"
    assert nights[0]["dates_done"] == 3


# ---- 票 18：老季两请求分层（浅深默认 + 探针升全深 + 持久 + 升深补抓） ----

# 老季窗（起始年 ≤2019 分层界）：3 日 × 每日 1 场；pending 序 = 新→旧
OLD_SEASONS = (srct_night.SeasonWindow("2019/20", "2019-08-01", "2019-08-03"),)
OLD_DATES = ["2019-08-01", "2019-08-02", "2019-08-03"]  # sids 91001/91002/91003

# 空内容形态（合法零行解析——探针"零证据"的事实依据；页题在=真页）
DETAIL_EMPTY_BYTES = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body></body></html>"
).encode()
OVERDOWN_EMPTY_BYTES = (
    "<html><head><title>甲VS乙-大小指数-新球体育</title></head>"
    "<body><table></table></body></html>"
).encode()
ASIANODDS_EMPTY_BYTES = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head>"
    "<body><table></table></body></html>"
).encode()
STATS_EMPTY_HTML = (
    b'<html><body><script>var jsonData = {"techStat":{"itemList":[]},"info":{}};'
    b"</script></body></html>"
)


def _old_day_routes(routes: dict[str, httpx.Response]) -> None:
    """老季日页路由（_transport_spy 只注册 2025-10 三日）。"""
    for day, sid in zip(OLD_DATES, DATE_TO_SID.values(), strict=True):
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid)
        )


def _deep_paths(seen: list[httpx.Request]) -> list[str]:
    """wire 上的亚盘多庄/统计端点路径（浅深断言：不应出现）。"""
    return [
        r.url.path
        for r in seen
        if (
            r.url.path.startswith("/asian/")
            or r.url.path.startswith("/overdown/")
            or r.url.path.startswith("/detail/")
            or r.url.path.startswith("/shijian/")
        )
    ]


def test_phase1_window_not_layered(tmp_path: Path) -> None:
    """Phase1 季窗（2023/24 起）不落分层界：全深直跑，零深度行。"""
    seen, routes = _transport_spy()
    summary = _run(tmp_path, seen, routes)
    assert summary.requests == 18  # 全深五端点，与分层前完全一致
    store = _store(tmp_path)
    assert store.season_depths() == {}  # 无判定即无行（表随 ensure_tree 建好）
    assert srct_night._is_layered(TEST_SEASONS[0]) is False


def test_old_season_probe_upgrades_to_full(tmp_path: Path) -> None:
    """探针日统计页非空 → 该季升全深：三日全打三端点。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    summary = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert summary.stop_reason == "completed"
    assert summary.requests == 18  # 探针升全深：与 Phase1 同价（1+5）×3
    assert summary.xg_matches == 3
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_FULL}


def test_old_season_probe_empty_downgrades_shallow(tmp_path: Path) -> None:
    """探针日统计/亚盘多庄页全空 → 降浅深：余日只打 日页+轨迹 两请求。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    for sid in DATE_TO_SID.values():
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_EMPTY_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_EMPTY_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_EMPTY_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_EMPTY_HTML)
    summary = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert summary.stop_reason == "completed"
    assert summary.dates_done == 3
    # 探针日（最新 pending=08-03，sid 91003）全深 6 请求；余两日浅深各 2
    assert summary.requests == 6 + 2 * 2
    # 浅深日零深端点上线；探针日恰一对（老季日均 8/3 ≤ 2×1+1 验收线）
    assert _deep_paths(seen) == [
        "/asian/91003",
        "/overdown/91003",
        "/detail/91003cn.htm",
        "/shijian/91003.htm",
    ]
    assert summary.xg_matches == 0
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_SHALLOW}


def test_shallow_depth_persists_across_nights(tmp_path: Path) -> None:
    """跨夜不重探：夜1 判浅深后，夜2 直读——pending 日全浅深零深端点。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    for sid in DATE_TO_SID.values():
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_EMPTY_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_EMPTY_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_EMPTY_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_EMPTY_HTML)
    # 预算 7：探针日（6 请求）干净跑完即断浅深；次日日页计费触顶停机
    night1 = _run(tmp_path, seen, routes, seasons=OLD_SEASONS, request_cap=7)
    assert night1.stop_reason == "budget"
    assert night1.dates_done == 1
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_SHALLOW}
    wire_after_night1 = len(seen)
    night2 = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert night2.stop_reason == "completed"
    assert night2.requests == 4  # 两日 ×（日页+轨迹）
    assert len(seen) == wire_after_night1 + 4
    assert _deep_paths(seen) == [
        "/asian/91003",
        "/overdown/91003",
        "/detail/91003cn.htm",
        "/shijian/91003.htm",
    ]  # 仅探针日
    assert _store(tmp_path).day_status_dates("done") == set(OLD_DATES)


def test_upgrade_backfill_refetches_only_deep_endpoints(tmp_path: Path) -> None:
    """升深补抓：浅深期 done 日重开端点集——day/odds 缓存命中，只补深端点。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    for sid in DATE_TO_SID.values():
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_EMPTY_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_EMPTY_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_EMPTY_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_EMPTY_HTML)
    _run(tmp_path, seen, routes, seasons=OLD_SEASONS)  # 夜1：全季浅深完成
    wire_after_night1 = len(seen)
    store = _store(tmp_path)
    store.set_season_depth("2019/20", srct.DEPTH_FULL)  # 模拟人工升深
    store.close()
    night2 = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert night2.pending_before == 0  # 无新 pending——纯补抓
    assert night2.dates_done == 2  # 探针日深端点已在（空页有 raw），不补
    assert night2.requests == 8  # 两日 ×（亚盘/大小球/详情/统计）；日页/轨迹全缓存
    assert len(seen) == wire_after_night1 + 8
    backfilled = sorted(p for p in _deep_paths(seen) if "91003" not in p)
    assert backfilled == [
        "/asian/91001",
        "/asian/91002",
        "/detail/91001cn.htm",
        "/detail/91002cn.htm",
        "/overdown/91001",
        "/overdown/91002",
        "/shijian/91001.htm",
        "/shijian/91002.htm",
    ]
    night3 = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert night3.requests == 0  # 补齐后零请求心跳（浅深完成日不再挂账）
    assert night3.dates_attempted == 0


def test_probe_day_not_found_defers_decision(tmp_path: Path) -> None:
    """伪 200 探针日无证据不断案：次日夜下一 pending 日重探。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    routes["day:20190803"] = httpx.Response(200, content=DAY_404_BYTES)
    summary = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert summary.dates_not_found == 1
    assert summary.dates_done == 2
    # 08-03 伪 200（1 请求）不断案；08-02 重探升全深（6）；08-01 全深（6）
    assert summary.requests == 1 + 6 + 6
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_FULL}


def test_probe_endpoint_failure_defers_to_next_day(tmp_path: Path) -> None:
    """宁可错升不错漏：探针日任一端点失败=证据不可信，不断案次日重探。"""
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    routes.pop("stats:91003")  # 探针日统计端点 500（传输失败无 raw）
    summary = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    assert summary.stop_reason == "completed"
    assert summary.dates_done == 3  # 场级失败不拦 done
    # 08-03 探针失败日（1×5+4 重试）不断案；08-02 重探升全深；08-01 全深
    assert summary.requests == (5 + (srct.MAX_RETRIES + 1)) + 6 + 6
    assert "91003:match_stats" in summary.failed
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_FULL}


def test_interrupted_probe_resume_keeps_evidence(tmp_path: Path) -> None:
    """回归（票 18 评审）：探针日采完落库但断案前崩溃——续传从缓存重放证据。

    干净停机（预算/熔断）当夜即断案不受影响；本测模拟的是进程崩溃窗口
    （bronze 已落、无判定行）：次日重探同日全缓存命中，正证据必须从
    缓存重放计账，否则错降浅深=数据永久缺口（宁可错升不错漏）。
    """
    seen, routes = _transport_spy()
    _old_day_routes(routes)
    routes["ah:91003"] = httpx.Response(200, content=ASIANODDS_BYTES)  # 唯一正证据
    for sid in ("91001", "91002", "91003"):
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_EMPTY_HTML)
    for sid in ("91001", "91002"):
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_EMPTY_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_EMPTY_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_EMPTY_BYTES)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    srct.collect_day(  # 崩溃模拟：探针日全深采完落库，断案/报 done 均未发生
        store,
        settings,
        _client(seen, routes),
        date="2019-08-03",
        sleeper=lambda _s: None,
    )
    store.close()
    assert len(seen) == 6  # 日页+五端点已上线落库
    assert _store(tmp_path).season_depths() == {}  # 无判定行（崩溃）
    night = _run(tmp_path, seen, routes, seasons=OLD_SEASONS)
    # 重探同日全缓存命中：亚盘证据从缓存重放 → 升全深；后两日全深
    assert _store(tmp_path).season_depths() == {"2019/20": srct.DEPTH_FULL}
    assert night.dates_done == 3
    assert night.requests == 12  # 续传探针日零线上 + 08-02/08-01 各 6
    assert len(seen) == 6 + 12
