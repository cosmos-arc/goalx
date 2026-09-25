"""源T轨迹语料采集测试（票 55 切片 11）：日页解析样本 + 采集高位接缝闭环。

样本取自 2026-09-23 实测裁剪：Over 日页 2025-10-18（英超/挪超/西丁三行）、
404 伪 200 页（GB2312，站链已洗）、1x2d 轨迹 2789205。端点模板一律
`.test` 占位域——真值只进本地 .env（代称红线）。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

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
    'var hometeam_cn="诺丁汉森林";\n'
    'var guestteam_cn="切尔西";\n'
    "game=Array("
    '"1129|146644870|Lottery Official|3.27|3.4|1.89|27.09|26.05|46.86|88.57|'
    '2.95|3.18|2.1|30.01|27.84|42.15|88.52|0.85|0.85|0.93|2025,10-1,18,10,28,00|",'
    '"281|146017965|Bet 365|3.3|3.8|1.9|27.74|24.09|48.18|91.53|'
    '3.1|3.5|2.25|30.64|27.14|42.22|94.99|0.90|0.94|1.00|2025,10-1,18,9,58,00|");\n'
    "gameDetail=Array("
    '"145998374^3.35|3.65|2.19|10-18 19:12|0.97|0.98|0.97|2025;'
    '3.4|3.7|2.16|10-18 18:50|0.97|0.98|0.96|2025");\n'
)

# 47 键统计页实测裁剪（键集两代：2025 含 xG / 2018 无 xG）
SAMPLE_STATS_XG_HTML = (
    '<html><head><meta charset="utf-8"></head><body><script>var jsonData ='
    '{"techStat":{"itemList":['
    '{"home":{"value":3,"text":"3"},"away":{"value":8,"text":"8"},"name":"角球","kind":"CORNER"},'
    '{"home":{"value":9,"text":"9"},"away":{"value":13,"text":"13"},"name":"射门","kind":"SHOOT"},'
    '{"home":{"value":0.71,"text":"0.71"},"away":{"value":1.68,"text":"1.68"},"name":"预期进球","kind":"EXPECTED_GOALS"},'
    '{"home":{"value":0.31,"text":"0.31"},"away":{"value":1.37,"text":"1.37"},"name":"运动战预期进球","kind":"XGOPEN_PlAY"},'
    '{"home":{"value":0.02,"text":"0.02"},"away":{"value":0.11,"text":"0.11"},"name":"射正预期进球","kind":"XGOT"},'
    '{"home":{"value":4,"text":"4"},"away":{"value":6,"text":"6"},"name":"禁区射门","kind":"TIOBX"}'
    ']},"info":{}};</script></body></html>'
)
SAMPLE_STATS_NOXG_HTML = (
    '<html><head><meta charset="utf-8"></head><body><script>var jsonData ='
    '{"techStat":{"itemList":['
    '{"home":{"value":3,"text":"3"},"away":{"value":8,"text":"8"},"name":"角球","kind":"CORNER"},'
    '{"home":{"value":9,"text":"9"},"away":{"value":13,"text":"13"},"name":"射门","kind":"SHOOT"},'
    '{"home":{"value":1,"text":"1"},"away":{"value":0,"text":"0"},"name":"中柱","kind":"HIT_WOODWORK"},'
    '{"home":{"value":2,"text":"2"},"away":{"value":0,"text":"0"},"name":"先开球","kind":"KICK_OFF_FIRST"}'
    ']},"info":{}};</script></body></html>'
)

# 亚盘多庄页实测裁剪（2025-10-18 场 2789205，UTF-8；历史页 14 家取 3 家 5
# 行；书商名打码=代称红线）。三组列=初/即时/终——2026-09-25 与存档 cid8
# changeDetail 轨迹交叉验证：close=盘前末行逐值一致、latest=92' 临场行。
_AH_ROW = (
    "<tr align=center>"
    "<td><input type=checkbox></td>"
    "<td>{name}</td>"
    "<td>{multi}</td>"
    "<td>{h1}</td><td>{l1}</td><td>{a1}</td>"
    "<td>{h2}</td><td>{l2}</td><td>{a2}</td>"
    "<td>{h3}</td><td>{l3}</td><td>{a3}</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=2789205&companyID={cid}>详</a></td>"
    "</tr>"
)
_AH_BOOKS = [
    # (cid, 名+状态, 盘序, 初, 即时, 终)——值取自实测页原值
    (
        "1",
        "书商1 封",
        "",
        ("0.93", "受让半球", "0.93"),
        ("0.80", "平手", "1.02"),
        ("0.80", "受让平手/半球", "1.06"),
    ),
    (
        "1",
        "",
        "盘2",
        ("1.23", "受让平手/半球", "0.63"),
        ("1.18", "平手", "0.68"),
        ("1.18", "平手", "0.68"),
    ),
    (
        "3",
        "书商3 封",
        "",
        ("0.82", "受让半球", "1.06"),
        ("7.14", "平手/半球", "0.03"),
        ("0.81", "受让平手/半球", "1.07"),
    ),
    (
        "8",
        "书商8 封",
        "",
        ("0.90", "受让半球", "0.95"),
        ("2.65", "平手/半球", "0.27"),
        ("0.80", "受让平手/半球", "1.05"),
    ),
    (
        "8",
        "",
        "盘2",
        ("1.10", "受让平手/半球", "0.70"),
        ("5.00", "平手/半球", "0.12"),
        ("1.20", "平手", "0.65"),
    ),
]
SAMPLE_ASIANODDS_HTML = (
    "<html><head><title>诺丁汉森林VS切尔西(2025-2026赛季英超)-亚指指数"
    "-新球体育-球探体育</title></head><body><table>"
    + "".join(
        _AH_ROW.format(
            cid=cid,
            name=name,
            multi=multi,
            h1=g1[0],
            l1=g1[1],
            a1=g1[2],
            h2=g2[0],
            l2=g2[1],
            a2=g2[2],
            h3=g3[0],
            l3=g3[1],
            a3=g3[2],
        )
        for cid, name, multi, g1, g2, g3 in _AH_BOOKS
    )
    + "</table></body></html>"
)
SAMPLE_ASIANODDS_BYTES = SAMPLE_ASIANODDS_HTML.encode("utf-8")
# 空表（页题在、零书商行）：老场无报价=合法空（定则 4 空≠无）
SAMPLE_ASIANODDS_EMPTY_HTML = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head>"
    "<body><table></table></body></html>"
)

DATE = "2025-10-18"
SCOPE_SIDS = ["2789205", "2711573", "2861202", "2862060"]
# 统计页 xG 分布：挪超 2711573 用无 xG 样本（老键集），其余三家有 xG
STATS_XG_SIDS = {"2789205", "2861202", "2862060"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _transport_spy() -> tuple[list[httpx.Request], dict[str, httpx.Response]]:
    """请求记录器 + 可编程响应表（day + 每场端点路径族）。"""
    seen: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {
        "day": httpx.Response(200, content=SAMPLE_OVER_BYTES)
    }
    for sid in SCOPE_SIDS:
        routes[f"odds:{sid}"] = httpx.Response(
            200, content=SAMPLE_ODDS_JS.encode("utf-8")
        )
        routes[f"ah:{sid}"] = httpx.Response(200, content=SAMPLE_ASIANODDS_BYTES)
        stats_sample = (
            SAMPLE_STATS_XG_HTML if sid in STATS_XG_SIDS else SAMPLE_STATS_NOXG_HTML
        )
        routes[f"stats:{sid}"] = httpx.Response(
            200, content=stats_sample.encode("utf-8")
        )
    return seen, routes


def _client(
    seen: list[httpx.Request], routes: dict[str, httpx.Response]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/over/20251018.htm":
            key = "day"
        elif path.startswith("/odds/"):
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        elif path.startswith("/asian/"):
            key = f"ah:{path.removeprefix('/asian/')}"
        else:
            key = f"stats:{path.removeprefix('/shijian/').removesuffix('.htm')}"
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


def test_parse_odds_page_meta_and_rows() -> None:
    payload = srct.parse_odds_page(SAMPLE_ODDS_JS.encode("utf-8"))
    assert payload["meta"]["league"] == "英超"
    assert payload["meta"]["home"] == "诺丁汉森林"
    assert payload["meta"]["away"] == "切尔西"
    assert payload["meta"]["schedule_id"] == "2789205"
    assert len(payload["game"]) == 2  # 书商行原串（含竞彩官方 cid1129）
    assert payload["game"][0].startswith("1129|")
    assert "3.35|3.65|2.19|10-18 19:12" in payload["game_detail"][0]


def test_parse_odds_page_missing_game_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="game 数组"):
        srct.parse_odds_page(b"var x=1;")


def test_parse_asianodds_page_groups_and_multi() -> None:
    payload = srct.parse_asianodds_page(SAMPLE_ASIANODDS_BYTES)
    books = payload["books"]
    assert len(books) == 5  # 3 家 × 主盘 + 两家多盘第二行
    assert sorted({b["cid"] for b in books}, key=int) == ["1", "3", "8"]
    first = books[0]
    assert (first["cid"], first["name_raw"], first["multi"]) == ("1", "书商1 封", "盘1")
    assert first["initial"] == {
        "home_water": "0.93",
        "line": "受让半球",
        "away_water": "0.93",
    }
    assert first["latest"] == {
        "home_water": "0.80",
        "line": "平手",
        "away_water": "1.02",
    }
    assert first["close"] == {
        "home_water": "0.80",
        "line": "受让平手/半球",
        "away_water": "1.06",
    }
    # cid8 主盘：close=存档 changeDetail 盘前末行 / latest=92' 临场行（交叉验证）
    b8 = next(b for b in books if b["cid"] == "8" and b["multi"] == "盘1")
    assert b8["close"] == {
        "home_water": "0.80",
        "line": "受让平手/半球",
        "away_water": "1.05",
    }
    assert b8["latest"] == {
        "home_water": "2.65",
        "line": "平手/半球",
        "away_water": "0.27",
    }
    assert b8["initial"]["line"] == "受让半球"
    multi = next(b for b in books if b["cid"] == "8" and b["multi"] == "盘2")
    assert multi["name_raw"] == ""  # 多盘行名格空（贴源）
    assert multi["initial"]["line"] == "受让平手/半球"


def test_parse_asianodds_page_empty_table_valid() -> None:
    """老场无报价=合法空表（空≠无，定则 4）；坏响应另测。"""
    payload = srct.parse_asianodds_page(SAMPLE_ASIANODDS_EMPTY_HTML.encode("utf-8"))
    assert payload["books"] == []


def test_parse_asianodds_page_not_ah_page_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="非亚指多庄页"):
        srct.parse_asianodds_page("<html><body>乱码</body></html>".encode())


def test_parse_stats_page_xg_and_legacy_keysets() -> None:
    xg = srct.parse_stats_page(SAMPLE_STATS_XG_HTML.encode("utf-8"))
    assert xg["has_xg"] is True
    assert len(xg["stats"]) == 6
    kinds = {item["kind"] for item in xg["stats"]}
    assert {
        "EXPECTED_GOALS",
        "XGOPEN_PlAY",
        "XGOT",
    } <= kinds  # 字面量匹配（拼写不规则照收）
    expected = next(item for item in xg["stats"] if item["kind"] == "EXPECTED_GOALS")
    assert (expected["home_value"], expected["away_value"]) == (0.71, 1.68)
    # 老键集（2018）：24 键无 xG——照常解析（缺 xG≠无数据，定则 4）
    legacy = srct.parse_stats_page(SAMPLE_STATS_NOXG_HTML.encode("utf-8"))
    assert legacy["has_xg"] is False
    assert {item["kind"] for item in legacy["stats"]} == {
        "CORNER",
        "SHOOT",
        "HIT_WOODWORK",
        "KICK_OFF_FIRST",
    }


def test_parse_stats_page_missing_block_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="jsonData"):
        srct.parse_stats_page(b"<html>no data</html>")


def test_content_404_discrimination() -> None:
    assert srct.is_content_404(srct.decode_day_page(SAMPLE_404_BYTES))
    assert not srct.is_content_404(srct.decode_day_page(SAMPLE_OVER_BYTES))


def test_collect_day_full_loop(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    stats, store = _collect(tmp_path, seen, routes, jitter=None)
    assert stats.requests == 13  # 1 日页 + 4 场×3 端点
    assert stats.raw_new == 12
    assert stats.parsed_ok == 13  # 12 端点 + 1 日页 bronze 行（切片 14）
    assert stats.xg_matches == 3  # 挪超场无 xG（老键集），其余三家有
    assert stats.asian_odds_nonempty == 4  # 老季深度探针证据面（票 59 起接管）
    assert stats.asian_odds_books == 20  # 4 场 × 5 逐盘行
    assert stats.scope_sids == SCOPE_SIDS
    assert stats.failed == {}
    assert stats.parse_failed == {}
    # raw 落盘：内容逐字节还原、sha 对账通过、三端点 checkpoint 可查
    assert store.read_raw("srct", "day_page", DATE, ext=".htm") == SAMPLE_OVER_BYTES
    assert store.verify_raw("srct", "day_page", DATE, ext=".htm")
    for sid in SCOPE_SIDS:
        assert store.verify_raw("srct", "odds_1x2d", sid, ext=".js")
        assert store.verify_raw("srct", "asian_odds", sid, ext=".html")
        assert store.verify_raw("srct", "match_stats", sid, ext=".html")
    # bronze：信封字段齐全、raw_sha 回溯到 checkpoint 的 raw 件、按数据集分文件
    for dataset, count in (
        ("odds_1x2d", 4),
        ("asian_odds", 4),
        ("match_stats", 4),
    ):
        rows = store.read_bronze("srct", dataset)
        assert len(rows) == count
        for row in rows:
            assert row["provider"] == "srct"
            assert row["dataset"] == dataset
            assert row["sid"] in SCOPE_SIDS
            assert row["parser_version"] == srct.BRONZE_VERSIONS[dataset]
            assert isinstance(row["fetched_at"], str)
    ah_rows = store.read_bronze("srct", "asian_odds")
    assert len(ah_rows[0]["payload"]["books"]) == 5  # 家数/逐盘行 bronze 可查
    assert {b["cid"] for b in ah_rows[0]["payload"]["books"]} == {"1", "3", "8"}
    odds_rows = store.read_bronze("srct", "odds_1x2d")
    first = odds_rows[0]
    sha_in_checkpoint = (
        store._checkpoint()
        .execute(
            """
        SELECT sha256 FROM raw_artifacts
        WHERE provider='srct' AND dataset='odds_1x2d' AND key=?
        """,
            (first["sid"],),
        )
        .fetchone()["sha256"]
    )
    assert first["raw_sha"] == sha_in_checkpoint
    stats_rows = store.read_bronze("srct", "match_stats")
    noxg = next(r for r in stats_rows if r["sid"] == "2711573")
    assert noxg["payload"]["has_xg"] is False  # 缺 xG 不跳页（定则 4）
    # 目录树布局（ADR-0011 决策 1）
    for sub in ("raw", "bronze", "silver", "gold", "duckdb"):
        assert (tmp_path / "corpus" / sub).is_dir()
    assert (tmp_path / "corpus" / "checkpoint.db").is_file()
    # 防封：固定 UA；odds 带对应 Referer，handicap/stats 仅 UA
    day_req = seen[0]
    assert day_req.headers["User-Agent"] == srct.DESKTOP_UA
    for req in seen[1:]:
        assert req.headers["User-Agent"] == srct.DESKTOP_UA
        sid = req.url.path.rsplit("/", 1)[-1].removesuffix(".js").removesuffix(".htm")
        if req.url.path.startswith("/odds/"):
            assert req.headers["Referer"] == f"https://srct.test/oddslist/{sid}.htm"
        else:
            assert "Referer" not in req.headers


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
    assert stats.skipped == 4  # 三端点全缓存的场次
    assert stats.raw_new == 0
    assert stats.bronze_repaired == 0
    # append-only 守卫：重跑零追加（每数据集行数不变，无重复行）
    for dataset in ("odds_1x2d", "asian_odds", "match_stats"):
        assert len(store.read_bronze("srct", dataset)) == 4


def test_collect_day_backoff_retries_then_failed(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    routes["odds:2789205"] = httpx.Response(500, text="boom")  # 该场轨迹端点永久失败
    sleeps: list[float] = []
    stats, _ = _collect(tmp_path, seen, routes, jitter=None, sleeper=sleeps.append)
    # 失败端点重试 MAX_RETRIES 次后记失败；指数退避 2/4/8
    assert (
        sum(1 for r in seen if r.url.path.startswith("/odds/2789205"))
        == srct.MAX_RETRIES + 1
    )
    assert sleeps == [2.0, 4.0, 8.0]
    # requests 数全部线上请求（含重试），非仅成功数——夜班预算记账口径
    assert stats.requests == 1 + (srct.MAX_RETRIES + 1) + 11
    assert "500" in stats.failed["2789205:odds_1x2d"]
    # 同场另两端点与其余场不受牵连
    assert stats.raw_new == 11
    assert stats.parsed_ok == 12  # 端点 + 日页 bronze 行
    store = CorpusStore(_settings(tmp_path).corpus_root)
    assert store.verify_raw("srct", "asian_odds", "2789205", ext=".html")
    assert store.verify_raw("srct", "odds_1x2d", "2711573", ext=".js")


def test_parse_failed_raw_kept_no_bronze_row(tmp_path: Path) -> None:
    """伪 200（缺 game 数组）：raw 100% 留档、计数入摘要、不写 bronze 行。"""
    seen, routes = _transport_spy()
    routes["odds:2789205"] = httpx.Response(200, content=b"var x=1;")
    sleeps: list[float] = []
    stats, store = _collect(tmp_path, seen, routes, jitter=None, sleeper=sleeps.append)
    assert (
        sum(1 for r in seen if r.url.path.startswith("/odds/2789205")) == 1
    )  # 内容坏响应不重试
    assert stats.requests == 13
    assert sleeps == []
    assert "game 数组" in stats.parse_failed["2789205:odds_1x2d"]
    assert stats.failed == {}
    assert stats.parsed_ok == 12  # 端点 + 日页 bronze 行
    assert store.verify_raw("srct", "odds_1x2d", "2789205", ext=".js")  # raw 留档
    assert len(store.read_bronze("srct", "odds_1x2d")) == 3  # 失败场无 bronze 行


def test_asianodds_content_error_counted(tmp_path: Path) -> None:
    """亚盘多庄页伪 200（页题缺）同样走解析失败计数。"""
    seen, routes = _transport_spy()
    routes["ah:2861202"] = httpx.Response(200, content=b"<html>garbage</html>")
    stats, _ = _collect(tmp_path, seen, routes, jitter=None)
    assert "非亚指多庄页" in stats.parse_failed["2861202:asian_odds"]


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
    assert stats.raw_new == 12
    assert len(sleeps) == 12  # 每次线上请求后一次礼貌间隔
    assert all(srct.JITTER_RANGE[0] <= s <= srct.JITTER_RANGE[1] for s in sleeps)
    assert srct.JITTER_RANGE == (2.0, 4.0)  # 3s±1s 防封参数


def test_unconfigured_endpoints_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本地 .env 可能已配端点（第一夜就绪）——测试须隔离 env 源
    for var in (
        "GOALX_SRCT_DAY_URL",
        "GOALX_SRCT_ODDS_URL",
        "GOALX_SRCT_ODDS_REFERER",
        "GOALX_SRCT_ASIANODDS_URL",
        "GOALX_SRCT_STATS_URL",
    ):
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


def test_bronze_repair_after_loss_zero_refetch(tmp_path: Path) -> None:
    """kill 落在 raw 落盘后、bronze 追加前：次跑本地重解析回补，零重抓。"""
    _collect(tmp_path, *_transport_spy(), jitter=None)
    store = CorpusStore(_settings(tmp_path).corpus_root)
    store.bronze_path("srct", "odds_1x2d").unlink()  # 模拟 bronze 丢失

    def no_network(_: httpx.Request) -> httpx.Response:
        raise AssertionError("回补不应重抓")

    stats = srct.collect_day(
        store,
        _settings(tmp_path),
        httpx.Client(transport=httpx.MockTransport(no_network)),
        date=DATE,
        sleeper=lambda _s: None,
        jitter=None,
    )
    assert stats.requests == 0
    assert stats.bronze_repaired == 4
    assert stats.parsed_ok == 4  # 仅回补侧计数
    rows = store.read_bronze("srct", "odds_1x2d")
    assert len(rows) == 4  # 缺口自愈且无重复
    assert stats.skipped == 4  # raw 全缓存，回补不改变跳过语义


def test_cli_three_endpoint_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 高位接缝扩到三端点：一条命令进去，报告数字与 bronze 出来。"""
    from goalx_backend import cli

    seen, routes = _transport_spy()
    # 单 scope 场（英超行）——真抖动下 3 次礼貌间隔 ≈9s，换来 CLI 路径全仿真
    routes["day"] = httpx.Response(
        200,
        content=(
            "<html><head><meta charset='gb2312'></head><body><table>"
            + _ROW_EPL
            + "</table></body></html>"
        ).encode("gb18030"),
    )
    args = cli.build_parser().parse_args(["srct-collect", "--date", DATE])
    cli._cmd_srct_collect(
        args, settings=_settings(tmp_path), client=_client(seen, routes)
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == 4  # 日页 + 三端点
    assert payload["raw_new"] == 3
    assert payload["parsed_ok"] == 4  # 3 端点 + 1 日页
    assert payload["parse_failed"] == {}
    assert payload["xg_matches"] == 1  # 英超场统计页含 xG
    assert payload["parse_success_rate"] == 1.0
    store = CorpusStore(_settings(tmp_path).corpus_root)
    for dataset in ("odds_1x2d", "asian_odds", "match_stats"):
        rows = store.read_bronze("srct", dataset)
        assert len(rows) == 1
        assert rows[0]["raw_sha"] == store.raw_sha("srct", dataset, "2789205")
    ah = store.read_bronze("srct", "asian_odds")[0]["payload"]["books"]
    assert len(ah) == 5  # 3 家 5 逐盘行（fixture 面）
    assert {b["cid"] for b in ah} == {"1", "3", "8"}
