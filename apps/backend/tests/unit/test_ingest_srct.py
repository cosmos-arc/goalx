"""源T轨迹语料采集测试（票 55 切片 11）：日页解析样本 + 采集高位接缝闭环。

样本取自 2026-09-23 实测裁剪：Over 日页 2025-10-18（英超/挪超/西丁三行）、
404 伪 200 页（GB2312，站链已洗）、1x2d 轨迹 2789205。端点模板一律
`.test` 占位域——真值只进本地 .env（代称红线）。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from limits import RateLimitItemPerSecond

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct

_ROW_EPL = """<tr height=18 align=center bgColor=#FFFDF3 id='tr1_375' name='36,1' infoid='1' sId='2789205'><td bgcolor=#FF3333 style='color:white' id='ls_375'><span>英超</span><span></span></td><td>18日19:30</td><td class=style1>完</td><td align=right><span name='yellow'><img src='/bf_img/yellow2.gif'></span><span name='order'><font color=#888888>[17]</font></span>诺丁汉森林</td><td class=style1 style='cursor:pointer;' onclick='showgoallist(2789205)'><font color=blue>0</font>-<font color=red>3</font></td><td align=left>切尔西<span name='order'><font color=#888888>[7]</font></span> <span name='red'><img src='/bf_img/redcard1.gif'></span> <span name='yellow'><img src='/bf_img/yellow4.gif'></span></td><td><font color=red>0</font>-<font color=red>0</font></td><td id='hdp_375' val='-0.5' style="color:green;">*半球</td><td id='ou_375' val='2.75'>2.5/3</td><td style='word-spacing:-3px' align=left class="icons2"> <a href=javascript: onclick='analysis(2789205)'>析</a><a href=javascript: onclick='AsianOdds(2789205)' style='margin-left:3px;'>亚</a> <a href=javascript: onclick='EuropeOdds(2789205)' style='margin-left:3px;'>欧</a><a href='javascript:advices(2789205)'><img src='/image/fx2.gif' alt='网友情报' style='margin-left:3px;'></a><img src='/image/zd.gif' alt='走地' style='margin-left:3px;'></td></tr>"""  # noqa: E501 实测样本整行

_ROW_NOR = """<tr height=18 align=center bgColor=#F0F0F0 id='tr1_422' name='22,1' infoid='12' sId='2711573'><td bgcolor=#666666 style='color:white' id='ls_422'><span>挪超</span><span></span></td><td>18日20:00</td><td class=style1>完</td><td align=right><span name='order'><font color=#888888>[9]</font></span>萨普斯堡</td><td class=style1 style='cursor:pointer;' onclick='showgoallist(2711573)'><font color=blue>2</font>-<font color=red>5</font></td><td align=left>博德闪耀<span name='order'><font color=#888888>[2]</font></span></td><td><font color=blue>1</font>-<font color=red>2</font></td><td id='hdp_422' val='-1' style="color:green;">*一球</td><td id='ou_422' val='3.5'>3.5</td><td style='word-spacing:-3px' align=left class="icons2"> <a href=javascript: onclick='analysis(2711573)'>析</a><a href=javascript: onclick='AsianOdds(2711573)' style='margin-left:3px;'>亚</a> <a href=javascript: onclick='EuropeOdds(2711573)' style='margin-left:3px;'>欧</a><a href='javascript:advices(2711573)'><img src='/image/fx2.gif' alt='网友情报' style='margin-left:3px;'></a><img src='/image/zd.gif' alt='走地' style='margin-left:3px;'></td></tr>"""  # noqa: E501 实测样本整行

_ROW_XD = """<tr height=18 align=center bgColor=#FFFDF3 id='tr1_137' name='951,0' infoid='3' sId='2878931' style='display: none;'><td bgcolor=#b00900 style='color:white' id='ls_137'><span>西丁</span><span></span></td><td>18日17:00</td><td class=style1>完</td><td align=right><span name='yellow'><img src='/bf_img/yellow5.gif'></span><span name='order'><font color=#888888>[4]</font></span>马德里体育会C队</td><td class=style1 style='cursor:pointer;' onclick='showgoallist(2878931)'><font color=red>2</font>-<font color=blue>1</font></td><td align=left>特里巴尔<span name='order'><font color=#888888>[13]</font></span> <span name='yellow'><img src='/bf_img/yellow3.gif'></span></td><td><font color=red>1</font>-<font color=blue>0</font></td><td id='hdp_137' val='0.5' style="color:#bb0000;">半球</td><td id='ou_137' val='2.5'>2.5</td><td style='word-spacing:-3px' align=left class="icons2"> <a href=javascript: onclick='analysis(2878931)'>析</a><a href=javascript: onclick='AsianOdds(2878931)' style='margin-left:3px;'>亚</a> <a href=javascript: onclick='EuropeOdds(2878931)' style='margin-left:3px;'>欧</a><img src='/image/zd.gif' alt='走地' style='margin-left:3px;'></td></tr>"""  # noqa: E501 实测样本整行

_ROW_CL = """<tr height=18 align=center bgColor=#F0F0F0 id='tr1_228' name='103,1' infoid='53' sId='2861202'><td bgcolor=#f75000 style='color:white' id='ls_228'><span>欧冠杯</span><span></span></td><td>23日00:45</td><td class=style1>完</td><td align=right><span name='order'><font color=#888888>[西甲8]</font></span>毕尔巴鄂竞技</td><td class=style1 style='cursor:pointer;' onclick='showgoallist(2861202)'><font color=red>3</font>-<font color=blue>1</font></td><td align=left>卡拉巴克<span name='order'><font color=#888888>[阿塞超1]</font></span></td><td><font color=red>1</font>-<font color=red>1</font></td><td id='hdp_228' val='1.5' style="color:#bb0000;">球半</td><td id='ou_228' val='2.75'>2.5/3</td><td style='word-spacing:-3px' align=left class="icons2"> <a href=javascript: onclick='analysis(2861202)'>析</a><a href=javascript: onclick='AsianOdds(2861202)' style='margin-left:3px;'>亚</a> <a href=javascript: onclick='EuropeOdds(2861202)' style='margin-left:3px;'>欧</a><a href='javascript:advices(2861202)'><img src='/image/fx2.gif' alt='网友情报' style='margin-left:3px;'></a><img src='/image/zd.gif' alt='走地' style='margin-left:3px;'></td></tr>"""  # noqa: E501 实测样本整行

_ROW_EL = """<tr height=18 align=center bgColor=#FFFDF3 id='tr1_123' name='113,1' infoid='53' sId='2862060'><td bgcolor=#6F00DD style='color:white' id='ls_123'><span>欧罗巴杯</span><span></span></td><td>24日00:45</td><td class=style1>完</td><td align=right><span name='yellow'><img src='/bf_img/yellow2.gif'></span><span name='order'><font color=#888888>[荷甲12]</font></span>前进之鹰</td><td class=style1 style='cursor:pointer;' onclick='showgoallist(2862060)'><font color=red>2</font>-<font color=blue>1</font></td><td align=left>阿斯顿维拉<span name='order'><font color=#888888>[英超11]</font></span></td><td><font color=red>1</font>-<font color=red>1</font></td><td id='hdp_123' val='-1' style="color:#bb0000;">*一球</td><td id='ou_123' val='3'>3</td><td style='word-spacing:-3px' align=left class="icons2"> <a href=javascript: onclick='analysis(2862060)'>析</a><a href=javascript: onclick='AsianOdds(2862060)' style='margin-left:3px;'>亚</a> <a href=javascript: onclick='EuropeOdds(2862060)' style='margin-left:3px;'>欧</a><a href='javascript:advices(2862060)'><img src='/image/fx2.gif' alt='网友情报' style='margin-left:3px;'></a><img src='/image/zd.gif' alt='走地' style='margin-left:3px;'></td></tr>"""  # noqa: E501 实测样本整行

SAMPLE_OVER_HTML = (
    "<html><head><meta charset='gb2312'></head><body><table>"
    + _ROW_EPL
    + _ROW_NOR
    + _ROW_CL
    + _ROW_EL
    + _ROW_XD
    + "</table></body></html>"
)
SAMPLE_OVER_BYTES = SAMPLE_OVER_HTML.encode("gb18030")

# 404 伪 200 实测页裁剪（GB2312，站链已洗，判别标记保留）
SAMPLE_404_HTML = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN">
<html xmlns="//www.w3.org/1999/xhtml">
	<HEAD>
		<TITLE>404</TITLE>
		<META http-equiv="Content-Type" content="text/html; charset=gb2312">
	</HEAD>
	<BODY leftMargin="0" topMargin="0" style="background:none;">
		<br><br><br>
		<div align=center><img src="/image/error_404.gif"></div>
	</BODY>
</html>
"""
SAMPLE_404_BYTES = SAMPLE_404_HTML.encode("gb18030")

# 1x2d 轨迹实测裁剪（UTF-8+BOM；切片 11 只依赖 game 标记，完整解析在切片 12）
SAMPLE_ODDS_JS = (
    '\ufeffvar matchname="English Premier League";\n'
    'var matchname_cn="英超";\n'
    'var MatchTime="2025,10-1,18,11,30,00";\n'
    "var ScheduleID=2789205;\n"
    'var hometeam="Nottingham Forest";\n'
    'var guestteam="Chelsea";\n'
    "game=Array("
    '"1129|146644870|Lottery Official|3.27|3.4|1.89|27.09|26.05|46.86|88.57|'
    '2.95|3.18|2.1|30.01|27.84|42.15|88.52|0.85|0.85|0.93|2025,10-1,18,10,28,00|",'
    '"281|146017965|Bet 365|3.3|3.8|1.9|27.74|24.09|48.18|91.53|'
    '3.1|3.5|2.25|30.64|27.14|42.22|94.99|0.90|0.94|1.00|2025,10-1,18,9,58,00|");\n'
    "gameDetail=Array("
    '"145998374^3.35|3.65|2.19|10-18 19:12|0.97|0.98|0.97|2025;'
    '3.4|3.7|2.16|10-18 18:50|0.97|0.98|0.96|2025");\n'
)

DATE = "2025-10-18"
SCOPE_SIDS = ["2789205", "2711573", "2861202", "2862060"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
    )


def _transport_spy() -> tuple[list[httpx.Request], dict[str, httpx.Response]]:
    """请求记录器 + 可编程响应表（day/odds 两个路径族）。"""
    seen: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {
        "day": httpx.Response(200, content=SAMPLE_OVER_BYTES),
        "2789205": httpx.Response(200, content=SAMPLE_ODDS_JS.encode("utf-8")),
        "2711573": httpx.Response(200, content=SAMPLE_ODDS_JS.encode("utf-8")),
        "2861202": httpx.Response(200, content=SAMPLE_ODDS_JS.encode("utf-8")),
        "2862060": httpx.Response(200, content=SAMPLE_ODDS_JS.encode("utf-8")),
    }
    return seen, routes


def _client(
    seen: list[httpx.Request], routes: dict[str, httpx.Response]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/over/20251018.htm":
            key = "day"
        else:
            key = request.url.path.strip("/").removesuffix(".js")
        if key not in routes:
            return httpx.Response(500, text="boom")
        return routes[key]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _collect(
    tmp_path: Path,
    seen: list[httpx.Request],
    routes: dict[str, httpx.Response],
    **kwargs: Any,
) -> tuple[srct.SrctCollectStats, CorpusStore]:
    kwargs.setdefault("sleeper", lambda _s: None)
    store = CorpusStore(_settings(tmp_path).corpus_root)
    stats = srct.collect_day(
        store, _settings(tmp_path), _client(seen, routes), date=DATE, **kwargs
    )
    return stats, store


def test_parse_over_page_real_shape() -> None:
    matches = srct.parse_over_page(SAMPLE_OVER_HTML)
    assert [m.sid for m in matches] == [
        "2789205",
        "2711573",
        "2861202",
        "2862060",
        "2878931",
    ]
    epl = matches[0]
    assert (epl.league, epl.kickoff_label, epl.home, epl.away, epl.score) == (
        "英超",
        "18日19:30",
        "诺丁汉森林",  # [17] 排名前缀剥掉
        "切尔西",
        "0-3",
    )
    # 欧冠杯/欧罗巴杯是站点字面量（非"欧冠/欧联"）；跨联赛排名 [西甲8] 剥掉
    cl = matches[2]
    assert (cl.league, cl.home, cl.away) == ("欧冠杯", "毕尔巴鄂竞技", "卡拉巴克")
    assert matches[3].league == "欧罗巴杯"
    scope = srct.filter_scope(matches)
    assert [m.sid for m in scope] == SCOPE_SIDS  # 西丁出 CorpusScope


def test_content_404_discrimination() -> None:
    assert srct.is_content_404(srct.decode_day_page(SAMPLE_404_BYTES))
    assert not srct.is_content_404(srct.decode_day_page(SAMPLE_OVER_BYTES))


def test_collect_day_full_loop(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    stats, store = _collect(tmp_path, seen, routes, jitter=None)
    assert stats.requests == 5  # 1 日页 + 4 场轨迹
    assert stats.raw_new == 4
    assert stats.scope_sids == SCOPE_SIDS
    assert stats.failed == {}
    # raw 落盘：内容逐字节还原、sha 对账通过、checkpoint 可查
    assert store.read_raw("srct", "day_page", DATE, ext=".htm") == SAMPLE_OVER_BYTES
    assert store.verify_raw("srct", "day_page", DATE, ext=".htm")
    for sid in SCOPE_SIDS:
        assert store.verify_raw("srct", "odds_1x2d", sid, ext=".js")
    # 目录树布局（ADR-0011 决策 1）
    for sub in ("raw", "bronze", "silver", "gold", "duckdb"):
        assert (tmp_path / "corpus" / sub).is_dir()
    assert (tmp_path / "corpus" / "checkpoint.db").is_file()
    # 防封：固定 UA + 对应 Referer（日页仅 UA）
    day_req, odds_reqs = seen[0], seen[1:]
    assert day_req.headers["User-Agent"] == srct.DESKTOP_UA
    for req in odds_reqs:
        assert req.headers["User-Agent"] == srct.DESKTOP_UA
        sid = req.url.path.strip("/").removesuffix(".js")
        assert req.headers["Referer"] == f"https://srct.test/oddslist/{sid}.htm"


def test_collect_day_resume_zero_refetch(tmp_path: Path) -> None:
    _collect(tmp_path, *_transport_spy(), jitter=None)

    # 第二跑：任何网络请求都视为违规（中断后重跑零重抓）
    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("断点续传不应再发请求")

    store = CorpusStore(_settings(tmp_path).corpus_root)
    stats = srct.collect_day(
        store,
        _settings(tmp_path),
        httpx.Client(transport=httpx.MockTransport(no_network)),
        date=DATE,
        sleeper=lambda _s: None,
        jitter=None,
    )
    assert stats.day_page_cached
    assert stats.requests == 0
    assert stats.skipped == 4
    assert stats.raw_new == 0


def test_collect_day_backoff_retries_then_failed(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["2789205"] = httpx.Response(500, text="boom")  # 永久失败
    sleeps: list[float] = []
    stats, _ = _collect(tmp_path, seen, routes, jitter=None, sleeper=sleeps.append)
    # 失败场重试 MAX_RETRIES 次后记失败；指数退避 2/4/8
    assert sum(1 for r in seen if "2789205" in r.url.path) == srct.MAX_RETRIES + 1
    assert sleeps == [2.0, 4.0, 8.0]
    # requests 数全部线上请求（含重试），非仅成功数——夜班预算记账口径
    assert stats.requests == 1 + (srct.MAX_RETRIES + 1) + 3
    assert "500" in stats.failed["2789205"]
    # 其余场不受牵连
    assert stats.raw_new == 3
    assert CorpusStore(_settings(tmp_path).corpus_root).verify_raw(
        "srct", "odds_1x2d", "2711573", ext=".js"
    )


def test_collect_day_content_error_no_retry(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["2789205"] = httpx.Response(200, content=b"var x=1;")  # 伪 200 缺标记
    sleeps: list[float] = []
    stats, _ = _collect(tmp_path, seen, routes, jitter=None, sleeper=sleeps.append)
    assert sum(1 for r in seen if "2789205" in r.url.path) == 1  # 确定性坏响应不重试
    assert stats.requests == 1 + 1 + 3
    assert sleeps == []
    assert "轨迹标记" in stats.failed["2789205"]


def test_day_page_content_404_fails_run(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["day"] = httpx.Response(200, content=SAMPLE_404_BYTES)
    with pytest.raises(srct.SrctContentError):
        _collect(tmp_path, seen, routes, jitter=None)
    assert len(seen) == 1  # 日页伪 200 不重试


def test_jitter_within_range(tmp_path: Path) -> None:
    sleeps: list[float] = []
    stats, _ = _collect(
        tmp_path,
        *_transport_spy(),
        jitter=srct.JITTER_RANGE,
        sleeper=sleeps.append,
        rng=random.Random(7),
    )
    assert stats.raw_new == 4
    assert len(sleeps) == 4  # 每场轨迹后一次礼貌间隔
    assert all(srct.JITTER_RANGE[0] <= s <= srct.JITTER_RANGE[1] for s in sleeps)
    assert srct.JITTER_RANGE == (2.0, 4.0)  # 3s±1s 防封参数


def test_unconfigured_endpoints_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本地 .env 可能已配端点（第一夜就绪）——测试须隔离 env 源
    for var in ("GOALX_SRCT_DAY_URL", "GOALX_SRCT_ODDS_URL", "GOALX_SRCT_ODDS_REFERER"):
        monkeypatch.delenv(var, raising=False)
    store = CorpusStore(tmp_path / "corpus")
    with pytest.raises(RuntimeError, match="GOALX_SRCT_DAY_URL"):
        srct.collect_day(
            store,
            Settings(_env_file=None, corpus_root=tmp_path / "corpus"),
            _client(*_transport_spy()),
            date=DATE,
            sleeper=lambda _s: None,
        )


def test_corpus_root_default_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOALX_CORPUS_ROOT", raising=False)
    assert Settings(_env_file=None).corpus_root == Path.home() / "goalx-data"
    monkeypatch.setenv("GOALX_CORPUS_ROOT", str(tmp_path / "elsewhere"))
    assert Settings(_env_file=None).corpus_root == tmp_path / "elsewhere"


def test_cli_srct_collect_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 高位接缝：一条命令进去，数据树内容与报告数字出来。"""
    from goalx_backend import cli

    seen, routes = _transport_spy()
    # 零 scope 页：CLI 接缝只验参数接线/JSON 报告/树落地（满环在 collect_day 层钉死，
    # 也避免真 3s 抖动拖慢套件）
    routes["day"] = httpx.Response(
        200, content="<html><body><table></table></body></html>".encode("gb18030")
    )
    args = cli.build_parser().parse_args(["srct-collect", "--date", DATE])
    cli._cmd_srct_collect(
        args, settings=_settings(tmp_path), client=_client(seen, routes)
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["date"] == DATE
    assert payload["requests"] == 1
    assert payload["scope_sids"] == []
    assert payload["parse_version"] == srct.PARSE_VERSION
    store = CorpusStore(_settings(tmp_path).corpus_root)
    assert store.verify_raw("srct", "day_page", DATE, ext=".htm")
    assert seen[0].headers["User-Agent"] == srct.DESKTOP_UA  # 防封参数 CLI 路径同生效


def test_throttle_sliding_window_ceiling() -> None:
    """限流套件语义：滑动窗口满后按 reset 时间等待（MemoryStorage 无外部存储）。"""
    limiter = srct._default_limiter()
    item = RateLimitItemPerSecond(1)  # 1s 窗，组件最小粒度
    waits: list[float] = []

    def sleep_and_record(seconds: float) -> None:
        waits.append(seconds)
        time.sleep(seconds)

    srct._throttle(limiter, item, lambda _s: None)  # 空窗直过，零等待
    assert waits == []
    srct._throttle(limiter, item, sleep_and_record)  # 满窗：等到窗口滑出才放行
    assert 0 < waits[0] <= 1.05
