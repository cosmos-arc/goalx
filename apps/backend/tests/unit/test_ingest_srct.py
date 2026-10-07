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
from goalx_backend.data.ingest import shell, srct


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶：端点集扩到 5 后单日 21 请求会顶到 20/min 忙转真窗口。

    防封语义本身在 jitter/预算测试里钉死（JITTER_RANGE/请求计数），这里
    只测采集闭环（同 test_ingest_srct_night 先例）。
    """
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


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
    "<td{t1}>{h1}</td><td{t1}>{l1}</td><td{t1}>{a1}</td>"
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
# 变价矩阵实测裁剪（同页下方独立表；列=固定 11 家模板，见 srct._MATRIX_
# TEMPLATE_CIDS）。三行新→旧：21:48 走地（col1→模板 cid3）、10:00 即时
# （col0→cid1）、09:00 早餐盘同值（与 10:00 成心跳对，silver 侧丢）
_MATRIX_COLS = (
    "澳*",
    "Crow*",
    "36*",
    "易胜*",
    "伟*",
    "明*",
    "10*",
    "12*",
    "利*",
    "盈*",
    "18*",
)

# (cid, 名+状态, 盘序, 初盘 title 属性串——空=无时刻行, 初, 即时, 终)
_AH_BOOK_ROWS = [
    (
        "1",
        "书商1 封",
        "",
        ' title="2025-10-17 08:00"',
        ("0.93", "受让半球", "0.93"),
        ("0.80", "平手", "1.02"),
        ("0.80", "受让平手/半球", "1.06"),
    ),
    (
        "1",
        "",
        "盘2",
        ' title="2025-10-17 20:30"',
        ("1.23", "受让平手/半球", "0.63"),
        ("1.18", "平手", "0.68"),
        ("1.18", "平手", "0.68"),
    ),
    (
        "3",
        "书商3 封",
        "",
        ' title="2025-10-17 09:15"',
        ("0.82", "受让半球", "1.06"),
        ("7.14", "平手/半球", "0.03"),
        ("0.81", "受让平手/半球", "1.07"),
    ),
    (
        "8",
        "书商8 封",
        "",
        ' title="2025-10-17 07:30"',
        ("0.90", "受让半球", "0.95"),
        ("2.65", "平手/半球", "0.27"),
        ("0.80", "受让平手/半球", "1.05"),
    ),
    (
        "8",
        "",
        "盘2",
        "",
        ("1.10", "受让平手/半球", "0.70"),
        ("5.00", "平手/半球", "0.12"),
        ("1.20", "平手", "0.65"),
    ),
]
_OU_BOOK_ROWS = [
    (
        "1",
        "书商1 封",
        "",
        ' title="2025-10-17 08:30"',
        ("0.93", "2.5/3", "0.87"),
        ("1.25", "2.5", "0.50"),
        ("0.80", "2.5", "1.00"),
    ),
    (
        "1",
        "",
        "盘2",
        "",
        ("1.13", "3", "0.67"),
        ("1.00", "2.5/3", "0.80"),
        ("1.00", "2.5/3", "0.80"),
    ),
    (
        "3",
        "书商3 封",
        "",
        ' title="2025-10-17 10:00"',
        ("0.95", "2.5/3", "0.85"),
        ("1.10", "2.5", "0.72"),
        ("0.85", "2.5/3", "0.95"),
    ),
]


def _matrix_row(filled: dict[int, str], score: str, time_raw: str) -> str:
    body = "".join(
        f"<td style='background: {filled[i]}'>"
        + (
            '平手/半球<br /><span class="blue b">0.82</span>&nbsp;'
            '<span class="green">1.11</span>'
            if i == 1
            else '半球<br /><span class="blue b">0.95</span>&nbsp;'
            '<span class="green">0.95</span>'
        )
        + "</td>"
        if i in filled
        else "<td></td>"
        for i in range(11)
    )
    return f"<tr align=center>{body}<td >{score}</td><td>{time_raw}</td></tr>"


_AH_MATRIX_HTML = (
    '<table cellspacing=1 cellpadding=0 class="font13 company">'
    "<tr align=center class=thead2>"
    + "".join(f"<th width=80>{n}</th>" for n in _MATRIX_COLS)
    + "<th width=40>比分</th><th width=80>变化时间</th></tr>"
    + _matrix_row({1: "#eaeaff"}, "3-1", "10-18 21:48")
    + _matrix_row({0: "#FFFFFF"}, "", "10-18 10:00")
    + _matrix_row({0: "#dcfbff"}, "", "10-17 09:00")
    + "</table>"
)
SAMPLE_ASIANODDS_HTML = (
    "<html><head><title>诺丁汉森林VS切尔西(2025-2026赛季英超)-亚指指数"
    "-新球体育-球探体育</title></head><body><table>"
    + "".join(
        _AH_ROW.format(
            cid=cid,
            name=name,
            multi=multi,
            t1=t1,
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
        for cid, name, multi, t1, g1, g2, g3 in _AH_BOOK_ROWS
    )
    + "</table>"
    # 页下方变价矩阵（2026-10-01 实测裁剪：固定 11 家模板列+比分+变化时间；
    # 行序新→旧，空格=该书商此刻无变化；格底色=相位图例）
    + _AH_MATRIX_HTML
    + "</body></html>"
)
SAMPLE_ASIANODDS_BYTES = SAMPLE_ASIANODDS_HTML.encode("utf-8")
# 空表（页题在、零书商行）：老场无报价=合法空（定则 4 空≠无）
SAMPLE_ASIANODDS_EMPTY_HTML = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head>"
    "<body><table></table></body></html>"
)

# 大小球多庄页实测裁剪（2025-10-18 场 2789205；与亚盘多庄同构，线=进球数
# 盘口线；书商名打码=代称红线）。2026-09-25 实测：16 家历史页取 2 家 3 行。
SAMPLE_OVERDOWN_HTML = (
    "<html><head><title>诺丁汉森林VS切尔西(2025-2026赛季英超)-大小指数"
    "-新球体育-球探体育</title></head><body><table>"
    + "".join(
        _AH_ROW.format(
            cid=cid,
            name=name,
            multi=multi,
            t1=t1,
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
        for cid, name, multi, t1, g1, g2, g3 in _OU_BOOK_ROWS
    )
    + "</table></body></html>"
)
SAMPLE_OVERDOWN_BYTES = SAMPLE_OVERDOWN_HTML.encode("utf-8")

# 详情页实测裁剪（2025-05-20 场 2591261，结构对齐；球员/队名非敏感可留，
# 数量裁剪到每侧 2 首发 1 替补）。技统条 li.lists 三 span；事件 li 清洗串；
# 阵容 home/guest 容器标记主客（容器双引号、play 单引号=真页原样）。
DETAIL_HEAD = (
    "<html><head><title>主队甲 VS 客队乙(2024-2025赛季英超)"
    "-现场分析-新球体育</title></head><body>"
    "<script>var homeTeamName = '主队甲';var guestTeamName = '客队乙';"
    "var strTime = '2025-05-20 03:00';</script>"
    "<div>VS 场地： 浙江测试球场 天气：多云 温度：15℃～16℃</div>"
    "<div class='title'> 首发阵容 <div class=\"homeN\"><a>主队甲</a> 4-2-3-1"
    "<a class='coach'>(主教练: 教练甲)</a></div>"
    "<div class=\"guestN\"><a class='coach'>(主教练: 教练乙)</a>"
    "<a>客队乙</a> 4-2-3-1</div></div>"
)
DETAIL_PLAY = (
    "<div class='play' onmouseover=\"setImgUrl({pid})\">"
    '{captain}<span><em class="num">{num} </em>'
    "<div class='name'><a>{name}</a></div></span></div>"
)
DETAIL_BENCH = (
    "<div class='play' onmouseover=\"setImgUrl({pid})\">"
    "<span><div class='name'><i>{num} </i><a>{name}</a></div></span></div>"
)
DETAIL_TECH = (
    "<ul>"
    "<li class='lists'><div class='data'><span >3</span><span>角球</span>"
    "<span >3</span></div></li>"
    "<li class='lists'><div class='data'><span >49%</span><span>控球率</span>"
    "<span class='red'>51%</span></div></li>{extra}</ul>"
)
_XG_ROW = (
    "<li class='lists'><div class='data'><span >1.20</span><span>预期进球</span>"
    "<span >1.65</span></div></li>"
)
DETAIL_EVENTS = (
    "<div class='content eventtable'><ul>"
    "<li><div class='data'><span></span><span>9'</span><span><img title='入球'/>"
    "<a>客将A</a> ( 助攻:客将B )</span></div></li>"
    "<li><div class='data'><span>63'</span><span><img title='换人'/>"
    "<a>主将C</a> <a>主将D</a><span></span></div></li>"
    "</ul></div>"
)


def _detail_page(*, xg: bool = False) -> bytes:
    plays = (
        '<div class="plays"><div class="home">'
        + DETAIL_PLAY.format(pid="1001", num="1", name="主首A", captain="")
        + DETAIL_PLAY.format(
            pid="1002", num="4", name="主首B", captain='<div class="captain"></div>'
        )
        + '</div><div class="guest">'
        + DETAIL_PLAY.format(pid="2001", num="7", name="客首A", captain="")
        + DETAIL_PLAY.format(pid="2002", num="11", name="客首B", captain="")
        + "</div></div>"
        + '<div class="home">'
        + DETAIL_BENCH.format(pid="1003", num="12", name="主替A")
        + '</div><div class="guest">'
        + DETAIL_BENCH.format(pid="2003", num="13", name="客替A")
        + "</div>"
    )
    return (
        DETAIL_HEAD
        + plays
        + DETAIL_TECH.format(extra=_XG_ROW if xg else "")
        + DETAIL_EVENTS
        + "</body></html>"
    ).encode("utf-8")


SAMPLE_DETAIL_BYTES = _detail_page()

# 分析页实测裁剪（2025-05-20 场 2591261；数据层 JS 数组 + 未来五场表，
# 行数裁剪；队名/球员非敏感可留）。键贴源 var 名（票 62 bronze-only）。
ANALYSIS_HTML = (
    "<html><head><title>主队甲 VS 客队乙(2024-2025赛季英超)"
    "-数据分析-新球体育</title></head><body><script>"
    "var hometeam = '主队甲';var guestteam = '客队乙';"
    "var strTime = '2025-05-20 03:00';\n"
    "var h_data =[['25-05-10',36,'英超','#FF3333',52,'狼队',60,'主队甲']];\n"
    "var a_data =[['25-05-11',36,'英超','#FF3333',25,'客队乙',19,'他队']];\n"
    "var Vs_eOdds =[[630465,18,'5.01','3.55','1.67','5.90','4.03','1.51',7]];"
    "</script><div>未来五场</div><table>"
    "<tr><td>主队甲</td></tr>"
    "<tr><td>时间</td><td>赛事</td><td>对阵</td><td>分析</td><td>直播</td>"
    "<td>相隔</td><td>05-25</td><td>英超</td><td>他队 - 主队甲</td>"
    "<td>分析</td><td>6 天</td></tr>"
    "<tr><td>客队乙</td></tr>"
    "<tr><td>06-01</td><td>英超</td><td>客队乙 - 他队</td>"
    "<td>分析</td><td>6 天</td></tr>"
    "</table></body></html>"
)
SAMPLE_ANALYSIS_BYTES = ANALYSIS_HTML.encode("utf-8")

# —— 旧模板两页实测裁剪（解析器 v3，backtest-decade A组票 01/02）——
# 结构对齐真树（2017-11 场 1418016 / 2016-04 场 1131142 / 2020-12 场
# 1915380），队名/球员打码，数量裁剪。2017 版：引号属性+title 图标；
# 2016 版：th「技术统计」变体+无 title 图标+未引用属性+无教练/场地。
OLD_DETAIL_HTML = (
    "<html><head><title>主队甲 VS 客队乙 详细事件-新球体育</title></head><body>"
    "<script>var scheduleID=99001;var state=-1;"
    "var strTime='2018-11-10 23:00';</script>"
    '<div id="home"><a href="//info.srct.test/cn/team/Summary/901.html"'
    ' target="_blank"><span class="name">主队甲</span></a></div>'
    '<span class="b">开赛时间：2018-11-10 23:00</span><br />'
    "场地：测试球场 天气：小雨 温度：9℃～12℃<br />"
    '<div id="guest"><a href="//info.srct.test/cn/team/Summary/902.html"'
    ' target="_blank"><span class="name">客队乙</span></a></div>'
    '<table><tr><th colspan="5">本场技术统计</th></tr>'
    "<tr><td><div class='barBg2'><div class='info' style='width:60%;'></td>"
    "<td>6</td><td>角球</td><td>4</td><td><div class='barBg'></div></td></tr>"
    "<tr><td class='bg1'></td><td class='bg1'>52%</td><td class='bg3'>控球率</td>"
    "<td class='bg1'>48%</td><td class='bg1'></td></tr></table>"
    '<div class="icons">'
    '<div class="icon"><img src="/images/bf_img/1.png" />入球</div>'
    '<div class="icon"><img src="/images/bf_img/3.png" />黄牌</div>'
    '<div class="icon"><img src="/images/bf_img/11.png" />换人</div>'
    '<div class="icon"><img src="/images/bf_img/4.png" />换入</div>'
    '<div class="icon"><img src="/images/bf_img/5.png" />换出</div></div>'
    '<table><tr><th colspan="5" class="bg1">详细事件</th></tr>'
    '<tr bgcolor="#FCEAAB"><td colspan="2"><span class="b t15">0</span></td>'
    '<td align="center">时间</td><td colspan="2">'
    '<span class="b t15">2</span></td></tr>'
    "<tr align=\"center\"><td class='bg2'></td><td class='bg2'></td>"
    "<td class='bg4'>20'</td>"
    "<td class='bg2'><img src='/images/bf_img/3.png' title='黄牌' /></td>"
    "<td class='bg2'><a href='//info.srct.test/cn/team/player/902/7001.html' "
    "title='客将A'>客将A</a></td></tr>"
    '<tr align="center">'
    "<td class='bg1'><img src='/images/bf_img/4.png' align='absmiddle'/>"
    "<a href='//info.srct.test/cn/team/player/901/7002.html' "
    "title='主将C'>主将C</a>"
    "<img src='/images/bf_img/5.png' align='absmiddle'/>"
    "<a href='//info.srct.test/cn/team/player/901/7003.html' "
    "title='主将D'>主将D</a></td>"
    "<td class='bg1'><img src='/images/bf_img/11.png' title='换人' /></td>"
    "<td class='bg3'>44'</td><td class='bg1'>&nbsp;</td><td class='bg1'>&nbsp;</td>"
    "</tr></table>"
    '<div id="matchBox2"><div class="teamNames">'
    '<div class="home"><a href="//info.srct.test/cn/team/Summary/901.html">'
    "主队甲</a> 4-2-3-1</div>"
    '<div class="guest"><a href="//info.srct.test/cn/team/Summary/902.html">'
    "客队乙</a> 4-2-3-1</div>"
    "首发阵容</div>"
    '<div class="plays">'
    '<div class="home five">'
    "<div class=\"playBox\"><div class='play'><span><div></div>"
    "<div class='name'><a href='//info.srct.test/cn/team/player/901/1001.html' "
    "title='主首A'>1 主首A</a></div></span></div></div>"
    "<div class=\"playBox\"><div class='play'><span><div></div>"
    "<div class='name'><a href='//info.srct.test/cn/team/player/901/1002.html' "
    "title='主首B'>4 主首B</a></div></span></div></div></div>"
    '<div class="guest five">'
    "<div class=\"playBox\"><div class='play'><span><div></div>"
    "<div class='name'><a href='//info.srct.test/cn/team/player/902/2001.html' "
    "title='客首A'>7 客首A</a></div></span></div></div></div>"
    "</div>"
    '<div class="backupPlay">'
    "<div class=\"home\"><div class='play'><span><div></div>"
    "<div class='name'><a href='//info.srct.test/cn/team/player/901/1004.html' "
    "title='主替A'>12 主替A</a></div></span></div></div>"
    '<div class="bu_txt">替<br />补</div>'
    "<div class=\"guest\"><div class='play'><span><div></div>"
    "<div class='name'><a href='//info.srct.test/cn/team/player/902/2004.html' "
    "title='客替A'>13 客替A</a></div></span></div></div>"
    "</div>"
    '<div class="hurtPlay" style=\'display:none;\'><div class="home">'
    '</div><div class="guest"></div></div>'
    "</div></body></html>"
)
OLD_DETAIL_BYTES = OLD_DETAIL_HTML.encode("utf-8")

# 2016 变体：th「技术统计」（无本场）、图标无 title、未引用属性、
# 无场地/教练/首发言、play 双引号形态、替补空块
OLD_DETAIL_2016_BYTES = (
    "<html><head><title>主队丙 VS 客队丁 详细事件-新球体育</title></head><body>"
    "<script>var strTime='2016-04-06 02:45';</script>"
    '<div id="home"><span class="name">主队丙</span></div>'
    '<div id="guest"><span class="name">客队丁</span></div>'
    "<table><tr><th>技术统计</th></tr>"
    "<tr><td><div class='barBg2'></div></td><td>0</td><td>角球</td>"
    "<td>3</td><td></td></tr>"
    "<tr><td></td><td>&nbsp;</td><td>黄牌</td><td>1</td><td></td></tr></table>"
    '<div class="icon"><img src="/images/bf_img/3.png" />黄牌</div>'
    '<div class="icon"><img src="/images/bf_img/11.png" />换人</div>'
    "<table><tr><th>详细事件</th></tr>"
    "<tr align=center><td width=320 class='bg2'>丁将A</td>"
    "<td width=30 class='bg2'><img src=/images/bf_img/3.png align=absmiddle>"
    "</td><td width=100 class='bg4'>81'</td><td width=30 class='bg2'>&nbsp;"
    "</td><td width=320 class='bg2'>&nbsp;</td></tr></table>"
    '<div id="matchBox"><div class="teamNames">'
    '<div class="home">主队丙 4-2-3-1</div>'
    '<div class="guest">客队丁 4-3-3</div></div>'
    '<div class="plays"><div class="home five">'
    '<div class="playBox"><div class="play"><span><div></div>'
    '<div class="name"><a href="//info.srct.test/cn/team/player/903/3001.html"'
    " title='丙首A' target=_blank>5 丙首A</a></div></span></div></div></div>"
    '<div class="guest five"></div></div>'
    '<div class="backupPlay"><div class="home"></div>'
    '<div class="guest"></div></div>'
    "</div></body></html>"
).encode()

# 旧模板分析页：七 var 数组面（等号带空格）+ 无 homeScoreStr/guestScoreStr
# /strTime + 未来五场两 50% 半区（主左客右各含内层 TABLE）
OLD_ANALYSIS_HTML = (
    "<html><head><title>主队甲 VS 客队乙,分析,篮球分析,足球分析，赛前分析"
    "</title></head><body><script>"
    'var hometeam = "主队甲";\r\nvar guestteam = "客队乙";\r\n'
    "var h_data = [['20-12-13',34,'意甲','#0088FF',154,"
    "'<span title=\"他队甲  排名:9\">他队甲</span>',176,"
    "'<span title=\"主队甲  排名:17\">主队甲</span>',3,0,'1-0','0.75',-1,-1,1,"
    "99001,'11','2','//zq.srct.test/cn/league.aspx?sclassid=34',-1]];\r\n"
    "var a_data = [['20-12-08',34,'意甲','#0088FF',176,"
    "'<span title=\"主队甲  排名:17\">主队甲</span>',552,"
    "'<span title=\"他队乙  排名:19\">他队乙</span>',1,1,'0-0','0.75',0,-1,-1,"
    "99002,'5','1','//zq.srct.test/cn/league.aspx?sclassid=34',0]];\r\n"
    "var v_data = [['20-07-02',34,'意甲','#0088FF',176,"
    "'<span title=\"主队甲  排名:13\">主队甲</span>',2960,"
    "'<span title=\"客队乙  排名:12\">客队乙</span>',1,3,'0-2']];\r\n"
    "var Vs_hOdds = [[99001,8,'0.80','平手/半球','1.06']];\r\n"
    "var Vs_eOdds = [[99001,18,'1.48','4.50','5.81','1.83','3.61','4.41',7]];\r\n"
    "</script>"
    '<div class="porletP"><h2 class="fx_title2">未来五场</h2>'
    '<table cellspacing="0"><tbody><tr>'
    '<td valign="top" width="50%"><TABLE width=\'100%\'>'
    "<tr><td>主队甲</td></tr>"
    "<tr align=center class=red_t1><td>时间</td><td>赛事</td><td>对阵</td>"
    "<td>分析</td><td>直播</td><td>相隔</td></tr>"
    "<tr><td>12-19</td><td>意甲</td><td>主队甲 - 他队甲</td><td>分析</td>"
    "<td></td><td>3 天</td></tr>"
    "</TABLE></td>"
    '<td valign="top" width="50%"><TABLE width=\'100%\'>'
    "<tr><td>客队乙</td></tr>"
    "<tr align=center class=red_t1><td>时间</td><td>赛事</td><td>对阵</td>"
    "<td>分析</td><td>直播</td><td>相隔</td></tr>"
    "<tr><td>12-23</td><td>意甲</td><td>他队乙 - 客队乙</td><td>分析</td>"
    "<td></td><td>6 天</td></tr>"
    "</TABLE></td>"
    "</tr></tbody></table></div>"
    "</body></html>"
)
OLD_ANALYSIS_BYTES = OLD_ANALYSIS_HTML.encode()

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
        srct_overdown_url="https://srct.test/overdown/{sid}",
        srct_detail_url="https://srct.test/detail/{sid}cn.htm",
        srct_analysis_url="https://srct.test/analysis/{sid}cn.htm",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _sid_of(req: httpx.Request) -> str:
    """请求路径尾提取 sid（.js/.htm/cn 后缀全剥，odds 与 detail/analysis 共用）。"""
    tail = req.url.path.rsplit("/", 1)[-1]
    return tail.removesuffix(".js").removesuffix(".htm").removesuffix("cn")


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
        routes[f"ou:{sid}"] = httpx.Response(200, content=SAMPLE_OVERDOWN_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=SAMPLE_DETAIL_BYTES)
        routes[f"ay:{sid}"] = httpx.Response(200, content=SAMPLE_ANALYSIS_BYTES)
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
        elif path.startswith("/overdown/"):
            key = f"ou:{path.removeprefix('/overdown/')}"
        elif path.startswith("/detail/"):
            key = f"dt:{path.removeprefix('/detail/').removesuffix('cn.htm')}"
        elif path.startswith("/analysis/"):
            key = f"ay:{path.removeprefix('/analysis/').removesuffix('cn.htm')}"
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
    assert first["initial_at"] == "2025-10-17 08:00"  # 初盘组 title 开盘时刻（v2）
    assert books[4]["initial_at"] is None  # 无 title 行（空盘）贴源 None
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


def test_parse_asianodds_page_change_matrix() -> None:
    """v2 变价矩阵：列=固定模板 cid（非当页汇总序）、相位底色、空=无变化。"""
    payload = srct.parse_asianodds_page(SAMPLE_ASIANODDS_BYTES)
    assert payload["columns"] == [
        "澳*",
        "Crow*",
        "36*",
        "易胜*",
        "伟*",
        "明*",
        "10*",
        "12*",
        "利*",
        "盈*",
        "18*",
    ]
    changes = payload["changes"]
    assert len(changes) == 3
    # 行序贴源新→旧；col1=Crow* → 模板 cid3（与汇总表 cid 集无关）
    assert changes[0] == {
        "cid": "3",
        "name_raw": "Crow*",
        "time": "10-18 21:48",
        "score": "3-1",
        "line": "平手/半球",
        "home_water": "0.82",
        "away_water": "1.11",
        "bg": "#eaeaff",  # 走地（场内）
    }
    # col0=澳* → 模板 cid1；比分空=盘前
    assert changes[1]["cid"] == "1"
    assert (changes[1]["score"], changes[1]["bg"]) == (None, "#FFFFFF")  # 即时盘
    assert changes[2]["bg"] == "#dcfbff"  # 早餐盘（与上行同值 → silver 心跳丢）
    # 大小球同构（无矩阵页 changes 空、初盘 title 照取）
    ou = srct.parse_overdown_page(SAMPLE_OVERDOWN_BYTES)
    assert ou["changes"] == []
    assert ou["columns"] == []
    assert ou["books"][0]["initial_at"] == "2025-10-17 08:30"
    assert ou["books"][1]["initial_at"] is None


def test_parse_asianodds_page_empty_table_valid() -> None:
    """老场无报价=合法空表（空≠无，定则 4）；坏响应另测。"""
    payload = srct.parse_asianodds_page(SAMPLE_ASIANODDS_EMPTY_HTML.encode("utf-8"))
    assert payload["books"] == []


def test_parse_asianodds_page_not_ah_page_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="非多庄对比页"):
        srct.parse_asianodds_page("<html><body>乱码</body></html>".encode())


def test_parse_overdown_page_shares_multi_book_shape() -> None:
    """大小球多庄与亚盘多庄同构：line=进球数盘口线，水=大/小水位。"""
    payload = srct.parse_overdown_page(SAMPLE_OVERDOWN_BYTES)
    books = payload["books"]
    assert len(books) == 3
    assert (books[0]["cid"], books[0]["multi"]) == ("1", "盘1")
    assert books[0]["initial"] == {
        "home_water": "0.93",
        "line": "2.5/3",
        "away_water": "0.87",
    }
    assert books[0]["close"] == {
        "home_water": "0.80",
        "line": "2.5",
        "away_water": "1.00",
    }
    assert books[1]["multi"] == "盘2"
    assert books[2]["cid"] == "3"


def test_parse_overdown_page_marker_discriminates() -> None:
    """页题标记区分两多庄页：亚盘页喂大小球解析器=坏响应。"""
    with pytest.raises(srct.SrctContentError, match="overdown"):
        srct.parse_overdown_page(SAMPLE_ASIANODDS_BYTES)
    empty = (
        "<html><head><title>甲VS乙-大小指数-新球体育</title></head>"
        "<body><table></table></body></html>"
    )
    assert srct.parse_overdown_page(empty.encode())["books"] == []


def test_parse_detail_page_sections() -> None:
    """详情页四分区：meta（场地/天气/温度/阵型/教练）/技统/事件/阵容。"""
    payload = srct.parse_detail_page(SAMPLE_DETAIL_BYTES)
    meta = payload["meta"]
    assert (meta["home"], meta["away"], meta["kickoff"]) == (
        "主队甲",
        "客队乙",
        "2025-05-20 03:00",
    )
    assert (meta["venue"], meta["weather"], meta["temperature"]) == (
        "浙江测试球场",
        "多云",
        "15℃～16℃",
    )
    assert meta["home_formation"] == "4-2-3-1"
    assert (meta["home_coach"], meta["away_coach"]) == ("教练甲", "教练乙")
    assert meta["referee"] is None  # 老页真无（空≠无，定则 4）
    tech = payload["tech"]
    assert tech[0] == {"home": "3", "name": "角球", "away": "3"}
    assert tech[1] == {"home": "49%", "name": "控球率", "away": "51%"}
    assert payload["has_xg"] is False  # 2025-05 历史页真无 xG
    events = payload["events"]
    assert any("9'" in e and "助攻" in e for e in events)
    assert any("63'" in e for e in events)
    lineup = payload["lineup"]
    assert [p["name"] for p in lineup["home_starters"]] == ["主首A", "主首B"]
    assert lineup["home_starters"][1]["captain"] is True  # 队长标在 play 块内
    assert lineup["home_starters"][0]["pid"] == "1001"
    assert [p["name"] for p in lineup["away_starters"]] == ["客首A", "客首B"]
    assert [p["name"] for p in lineup["home_bench"]] == ["主替A"]
    assert [p["name"] for p in lineup["away_bench"]] == ["客替A"]


def test_parse_detail_page_xg_row_flag() -> None:
    payload = srct.parse_detail_page(_detail_page(xg=True))
    assert payload["has_xg"] is True
    xg = next(r for r in payload["tech"] if r["name"] == "预期进球")
    assert (xg["home"], xg["away"]) == ("1.20", "1.65")


def test_parse_detail_page_not_detail_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="非详情分析页"):
        srct.parse_detail_page("<html><body>乱码</body></html>".encode())
    # 空分区合法（仅页题在的极简页：分区全空，非坏页）
    minimal = (
        "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body></body></html>"
    ).encode()
    payload = srct.parse_detail_page(minimal)
    assert payload["tech"] == []
    assert payload["events"] == []
    assert payload["lineup"]["home_starters"] == []


def test_parse_analysis_page_sections() -> None:
    """分析页：meta + 数组层贴源（键=源 var 名）+ 未来五场主客块。"""
    payload = srct.parse_analysis_page(SAMPLE_ANALYSIS_BYTES)
    assert payload["meta"] == {
        "home": "主队甲",
        "away": "客队乙",
        "kickoff": "2025-05-20 03:00",
    }
    arrays = payload["arrays"]
    assert arrays["h_data"] == [
        "['25-05-10',36,'英超','#FF3333',52,'狼队',60,'主队甲']"
    ]
    assert arrays["a_data"][0].startswith("['25-05-11'")
    assert len(arrays["Vs_eOdds"]) == 1  # 盘路/欧赔对比逐书行（行首 sid+cid）
    assert arrays["homeScoreStr"] == []  # fixture 未含该 var=合法缺
    future = payload["future_fixtures"]
    assert future["home"] == [["05-25", "英超", "他队 - 主队甲", "分析", "6 天"]]
    assert future["away"] == [["06-01", "英超", "客队乙 - 他队", "分析", "6 天"]]


def test_parse_analysis_page_not_analysis_raises() -> None:
    with pytest.raises(srct.SrctContentError, match="非数据分析页"):
        srct.parse_analysis_page("<html><body>乱码</body></html>".encode())
    minimal = (
        "<html><head><title>甲VS乙-数据分析-新球体育</title></head><body></body></html>"
    ).encode()
    payload = srct.parse_analysis_page(minimal)
    assert payload["arrays"]["h_data"] == []
    assert payload["future_fixtures"] == {"home": [], "away": []}


# —— 解析器 v3（旧模板两页，backtest-decade A组票 01/02）——


def test_parse_detail_page_v3_sections() -> None:
    """旧模板 detail（2017 版）：meta/技统/图例事件/阵容与 v2 payload 同构。"""
    payload = srct.parse_detail_page_v3(OLD_DETAIL_BYTES)
    meta = payload["meta"]
    assert (meta["home"], meta["away"], meta["kickoff"]) == (
        "主队甲",
        "客队乙",
        "2018-11-10 23:00",
    )
    assert (meta["venue"], meta["weather"], meta["temperature"]) == (
        "测试球场",
        "小雨",
        "9℃～12℃",
    )
    assert meta["home_formation"] == "4-2-3-1"
    assert meta["referee"] is None  # 旧模板真无（空≠无，定则 4）
    assert (meta["home_coach"], meta["away_coach"]) == (None, None)
    assert payload["tech"] == [
        {"home": "6", "name": "角球", "away": "4"},
        {"home": "52%", "name": "控球率", "away": "48%"},
    ]
    assert payload["has_xg"] is False
    # 半场比分行（colspan）跳过；事件串=时刻+图例标签+侧文本
    assert payload["events"] == ["20' 黄牌 客将A", "44' 换人 主将C 主将D"]
    lineup = payload["lineup"]
    assert [p["name"] for p in lineup["home_starters"]] == ["主首A", "主首B"]
    assert lineup["home_starters"][0] == {
        "pid": "1001",
        "captain": False,  # 旧模板无队长标
        "num": "1",
        "name": "主首A",
    }
    assert [p["name"] for p in lineup["away_starters"]] == ["客首A"]
    assert [p["num"] for p in lineup["home_bench"]] == ["12"]
    assert [p["pid"] for p in lineup["away_bench"]] == ["2004"]


def test_parse_detail_page_v3_2016_flavor() -> None:
    """2016 变体：th「技术统计」头、无 title 图标（语义在图例）、未引用属性。"""
    payload = srct.parse_detail_page_v3(OLD_DETAIL_2016_BYTES)
    meta = payload["meta"]
    assert (meta["home"], meta["away"], meta["kickoff"]) == (
        "主队丙",
        "客队丁",
        "2016-04-06 02:45",
    )
    assert meta["venue"] is None  # 2016 页真无场地行
    assert (meta["home_formation"], meta["away_formation"]) == ("4-2-3-1", "4-3-3")
    assert payload["tech"][1] == {"home": None, "name": "黄牌", "away": "1"}  # &nbsp;
    assert payload["events"] == ["81' 黄牌 丁将A"]  # 图例给 2016 无 title 图标
    starter = payload["lineup"]["home_starters"][0]
    assert (starter["pid"], starter["num"], starter["name"]) == (
        "3001",
        "5",
        "丙首A",
    )


def test_parse_detail_dispatch_routes_both_templates() -> None:
    """分发：新模板→v2，旧模板→v3，两者 payload 同构、乱页抛内容错误。"""
    new_via_dispatch = srct.parse_detail_dispatch(SAMPLE_DETAIL_BYTES)
    assert new_via_dispatch == srct.parse_detail_page(SAMPLE_DETAIL_BYTES)
    old_via_dispatch = srct.parse_detail_dispatch(OLD_DETAIL_BYTES)
    assert old_via_dispatch == srct.parse_detail_page_v3(OLD_DETAIL_BYTES)
    # 新模板页亦含「详细事件」串——分发先查新标记，不受串复用干扰
    assert new_via_dispatch["meta"]["home"] == "主队甲"
    with pytest.raises(srct.SrctContentError, match="detail v3"):
        srct.parse_detail_dispatch("<html><body>乱码</body></html>".encode())
    with pytest.raises(srct.SrctContentError, match="detail v3"):
        srct.parse_detail_dispatch(b"<html><img src='error_404.gif'></html>")


def test_parse_detail_page_v3_correctness_regressions() -> None:
    """correctness-review 四反例回归：加时分钟/无 span 首发块/仅场地行。"""
    extra_time = (
        "<html><head><title>甲 VS 乙 详细事件</title></head><body>"
        '<div class="icon"><img src="/images/bf_img/1.png" />入球</div>'
        "<table><tr><th>详细事件</th></tr>"
        "<tr><td>主将E</td><td><img src='/images/bf_img/1.png' /></td>"
        "<td>118'</td><td></td><td></td></tr>"
        '<tr><td></td><td></td><td>"121\'"</td>'
        "<td><img src='/images/bf_img/1.png' /></td><td>客将E</td></tr>"
        "</table></body></html>"
    ).encode()
    events = srct.parse_detail_page_v3(extra_time)["events"]
    assert events == ["118' 入球 主将E", "121' 入球 客将E"]  # 三位分钟+引号剥除
    # 无 span 包裹的 play 块（真树 sid 1549846 形态）：切片到下一 play 开标签
    no_span = (
        "<html><head><title>甲 VS 乙 详细事件</title></head><body>"
        '<div class="teamNames"><div class="home">甲 4-4-2</div>'
        '<div class="guest">乙 4-4-2</div></div>'
        '<div class="plays"><div class="home">'
        "<div class='play'><div class='name'>"
        "<a href='//info.srct.test/cn/team/player/1/11.html'>1 主首A</a>"
        "</div><div class='img'></div></div>"
        "<div class='play'><div class='name'>"
        "<a href='//info.srct.test/cn/team/player/1/12.html'>4 主首B</a>"
        "</div><div class='img'></div></div>"
        '</div><div class="guest"></div></div></body></html>'
    ).encode()
    lineup = srct.parse_detail_page_v3(no_span)["lineup"]
    assert [p["num"] for p in lineup["home_starters"]] == ["1", "4"]
    assert [p["name"] for p in lineup["home_starters"]] == ["主首A", "主首B"]
    # 2016 仅场地行（无天气/温度）：各段独立降级，venue 不连坐
    venue_only = (
        "<html><head><title>甲 VS 乙 详细事件</title></head><body>"
        '<span class="b">开赛时间：2016-01-25 01:00</span><br />'
        '场地：测试球场甲  \n<div class="leng"><img /></div>'
        "</body></html>"
    ).encode()
    meta = srct.parse_detail_page_v3(venue_only)["meta"]
    assert (meta["venue"], meta["weather"], meta["temperature"]) == (
        "测试球场甲",
        None,
        None,
    )


def test_parse_analysis_page_v3_correctness_regressions() -> None:
    """correctness-review 反例回归：表头行缺 </tr> 并块时首条赛程不丢。"""
    merged_header = (
        "<html><head><title>甲 VS 乙,分析</title></head><body><script>"
        "var h_data = [[]];</script>"
        '<div>未来五场</div><table cellspacing="0"><tbody><tr>'
        '<td valign="top" width="50%"><TABLE>'
        "<tr><td>主队甲</td></tr>"
        "<tr><td>时间</td><td>赛事</td><td>对阵</td><td>分析</td><td>直播</td>"
        "<td>相隔</td><td>12-19</td><td>意甲</td><td>主队甲 - 他队甲</td>"
        "<td>分析</td><td>3 天</td></tr>"
        "</TABLE></td>"
        '<td valign="top" width="50%"><TABLE>'
        "<tr><td>客队乙</td></tr>"
        "<tr><td>12-23</td><td>意甲</td><td>他队乙 - 客队乙</td><td>分析</td>"
        "<td>6 天</td></tr>"
        "</TABLE></td>"
        "</tr></tbody></table></body></html>"
    ).encode()
    payload = srct.parse_analysis_page_v3(merged_header)
    assert payload["future_fixtures"]["home"] == [
        ["12-19", "意甲", "主队甲 - 他队甲", "分析", "3 天"]
    ]
    assert payload["future_fixtures"]["away"] == [
        ["12-23", "意甲", "他队乙 - 客队乙", "分析", "6 天"]
    ]


def test_parse_analysis_page_v3_arrays_isomorphic() -> None:
    """旧模板 analysis：七 var 数组面同构、积分榜 var 缺=空、strTime 缺。"""
    payload = srct.parse_analysis_page_v3(OLD_ANALYSIS_BYTES)
    assert payload["meta"] == {
        "home": "主队甲",
        "away": "客队乙",
        "kickoff": None,  # 旧模板无 strTime（silver 经 fixture join 补）
    }
    arrays = payload["arrays"]
    assert arrays["h_data"][0].startswith("['20-12-13',34,'意甲'")
    assert "<span title=" in arrays["h_data"][0]  # 行内 HTML 贴源保留
    assert len(arrays["a_data"]) == 1
    assert len(arrays["v_data"]) == 1
    assert arrays["Vs_hOdds"][0].startswith("[99001,8,")
    assert arrays["homeScoreStr"] == []  # 两积分榜 var 缺=空≠无
    assert arrays["guestScoreStr"] == []
    assert arrays["h2_data"] == []
    # 未来五场：两 50% 半区（主左客右），队名/表头行不入
    assert payload["future_fixtures"] == {
        "home": [["12-19", "意甲", "主队甲 - 他队甲", "分析", "3 天"]],
        "away": [["12-23", "意甲", "他队乙 - 客队乙", "分析", "6 天"]],
    }


def test_parse_analysis_dispatch_routes_both_templates() -> None:
    new_via_dispatch = srct.parse_analysis_dispatch(SAMPLE_ANALYSIS_BYTES)
    assert new_via_dispatch == srct.parse_analysis_page(SAMPLE_ANALYSIS_BYTES)
    old_via_dispatch = srct.parse_analysis_dispatch(OLD_ANALYSIS_BYTES)
    assert old_via_dispatch == srct.parse_analysis_page_v3(OLD_ANALYSIS_BYTES)
    with pytest.raises(srct.SrctContentError, match="analysis v3"):
        srct.parse_analysis_dispatch("<html><body>乱码</body></html>".encode())


def test_bronze_versions_v3_and_marker_registry() -> None:
    """版本隔离与门⑤标记组钉死：detail/analysis=v3 双标记，v2 函数未动。"""
    assert srct.BRONZE_VERSIONS[srct.DETAIL_DATASET] == "srct_detail_v3"
    assert srct.BRONZE_VERSIONS[srct.ANALYSIS_DATASET] == "srct_analysis_v3"
    assert srct.TEMPLATE_MARKERS[srct.DETAIL_DATASET] == ("现场分析", "详细事件")
    assert srct.TEMPLATE_MARKERS[srct.ANALYSIS_DATASET] == (
        "数据分析",
        "var h_data",
    )
    # v2 函数仍只认新模板（版本函数隔离）
    with pytest.raises(srct.SrctContentError):
        srct.parse_detail_page(OLD_DETAIL_BYTES)
    with pytest.raises(srct.SrctContentError):
        srct.parse_analysis_page(OLD_ANALYSIS_BYTES)


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


def test_parse_stats_page_pseudo_200_raises() -> None:
    """404 图守卫（2026-10-07 补齐，与其余四端点同契；门⑤伪 200 分桶同源）。"""
    dual = b"<html><img src='error_404.gif'><script>var jsonData = {};</script></html>"
    with pytest.raises(srct.SrctContentError, match="404"):
        srct.parse_stats_page(dual)


def test_content_404_discrimination() -> None:
    assert srct.is_content_404(srct.decode_day_page(SAMPLE_404_BYTES))
    assert not srct.is_content_404(srct.decode_day_page(SAMPLE_OVER_BYTES))


def test_collect_day_full_loop(tmp_path: Path) -> None:
    seen, routes = _transport_spy()
    stats, store = _collect(tmp_path, seen, routes, jitter=None)
    assert stats.requests == 25  # 1 日页 + 4 场×6 端点
    assert stats.raw_new == 24
    assert stats.parsed_ok == 25  # 24 端点 + 1 日页 bronze 行（切片 14）
    assert stats.xg_matches == 3  # 挪超场无 xG（老键集），其余三家有
    assert stats.asian_odds_nonempty == 4  # 老季深度探针证据面（票 59 起接管）
    assert stats.asian_odds_books == 20  # 4 场 × 5 逐盘行
    assert stats.over_down_nonempty == 4
    assert stats.over_down_books == 12  # 4 场 × 3 逐盘行
    assert stats.detail_nonempty == 4  # 票 61 探针证据面
    assert stats.analysis_nonempty == 4  # 票 62 特征面
    assert stats.scope_sids == SCOPE_SIDS
    assert stats.failed == {}
    assert stats.parse_failed == {}
    # raw 落盘：内容逐字节还原、sha 对账通过、三端点 checkpoint 可查
    assert store.read_raw("srct", "day_page", DATE, ext=".htm") == SAMPLE_OVER_BYTES
    assert store.verify_raw("srct", "day_page", DATE, ext=".htm")
    for sid in SCOPE_SIDS:
        assert store.verify_raw("srct", "odds_1x2d", sid, ext=".js")
        assert store.verify_raw("srct", "asian_odds", sid, ext=".html")
        assert store.verify_raw("srct", "over_down", sid, ext=".html")
        assert store.verify_raw("srct", "match_detail", sid, ext=".html")
        assert store.verify_raw("srct", "match_analysis", sid, ext=".html")
        assert store.verify_raw("srct", "match_stats", sid, ext=".html")
    # bronze：信封字段齐全、raw_sha 回溯到 checkpoint 的 raw 件、按数据集分文件
    for dataset, count in (
        ("odds_1x2d", 4),
        ("asian_odds", 4),
        ("over_down", 4),
        ("match_detail", 4),
        ("match_analysis", 4),
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
    # 防封：固定 UA；odds/asian/overdown/analysis 带 odds Referer（vip/zq 域
    # 2026-09-26 起强制校验），day/detail 仍仅 UA
    day_req = seen[0]
    assert day_req.headers["User-Agent"] == shell.DESKTOP_UA
    for req in seen[1:]:
        assert req.headers["User-Agent"] == shell.DESKTOP_UA
        sid = _sid_of(req)
        if req.url.path.startswith(("/odds/", "/asian/", "/overdown/", "/analysis/")):
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
    for dataset in (
        "odds_1x2d",
        "asian_odds",
        "over_down",
        "match_detail",
        "match_analysis",
        "match_stats",
    ):
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
    assert stats.requests == 1 + (srct.MAX_RETRIES + 1) + 23
    assert "500" in stats.failed["2789205:odds_1x2d"]
    # 同场另五端点与其余场不受牵连
    assert stats.raw_new == 23
    assert stats.parsed_ok == 24  # 端点 + 日页 bronze 行
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
    assert stats.requests == 25
    assert sleeps == []
    assert "game 数组" in stats.parse_failed["2789205:odds_1x2d"]
    assert stats.failed == {}
    assert stats.parsed_ok == 24  # 端点 + 日页 bronze 行
    assert store.verify_raw("srct", "odds_1x2d", "2789205", ext=".js")  # raw 留档
    assert len(store.read_bronze("srct", "odds_1x2d")) == 3  # 失败场无 bronze 行


def test_asianodds_content_error_counted(tmp_path: Path) -> None:
    """亚盘多庄页伪 200（页题缺）同样走解析失败计数。"""
    seen, routes = _transport_spy()
    routes["ah:2861202"] = httpx.Response(200, content=b"<html>garbage</html>")
    stats, _ = _collect(tmp_path, seen, routes, jitter=None)
    assert "非多庄对比页" in stats.parse_failed["2861202:asian_odds"]


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
    assert stats.raw_new == 24
    assert len(sleeps) == 24  # 每次线上请求后一次礼貌间隔
    assert all(srct.JITTER_RANGE[0] <= s <= srct.JITTER_RANGE[1] for s in sleeps)
    assert srct.JITTER_RANGE == (1.4, 2.6)  # 2.0s±0.6s 防封参数（2026-09-30 再提速）


def test_unconfigured_endpoints_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本地 .env 可能已配端点（第一夜就绪）——测试须隔离 env 源
    for var in (
        "GOALX_SRCT_DAY_URL",
        "GOALX_SRCT_ODDS_URL",
        "GOALX_SRCT_ODDS_REFERER",
        "GOALX_SRCT_ASIANODDS_URL",
        "GOALX_SRCT_OVERDOWN_URL",
        "GOALX_SRCT_DETAIL_URL",
        "GOALX_SRCT_ANALYSIS_URL",
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
    assert seen[0].headers["User-Agent"] == shell.DESKTOP_UA  # 防封参数 CLI 路径同生效


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
    assert payload["requests"] == 7  # 日页 + 六端点
    assert payload["raw_new"] == 6
    assert payload["parsed_ok"] == 7  # 6 端点 + 1 日页
    assert payload["parse_failed"] == {}
    assert payload["xg_matches"] == 1  # 英超场统计页含 xG
    assert payload["parse_success_rate"] == 1.0
    store = CorpusStore(_settings(tmp_path).corpus_root)
    for dataset in (
        "odds_1x2d",
        "asian_odds",
        "over_down",
        "match_detail",
        "match_analysis",
        "match_stats",
    ):
        rows = store.read_bronze("srct", dataset)
        assert len(rows) == 1
        assert rows[0]["raw_sha"] == store.raw_sha("srct", dataset, "2789205")
    ah = store.read_bronze("srct", "asian_odds")[0]["payload"]["books"]
    assert len(ah) == 5  # 3 家 5 逐盘行（fixture 面）
    assert {b["cid"] for b in ah} == {"1", "3", "8"}
    ou = store.read_bronze("srct", "over_down")[0]["payload"]["books"]
    assert len(ou) == 3
    assert ou[0]["close"] == {
        "home_water": "0.80",
        "line": "2.5",
        "away_water": "1.00",
    }


def test_bronze_sids_index_write_path_and_lazy_backfill(tmp_path) -> None:
    """bronze sid 索引表：append 同步 upsert / 存量懒回填 / 版本过滤 / 缓存增量。"""
    from goalx_backend.data.corpus_store import CorpusStore

    store = CorpusStore(_settings(tmp_path).corpus_root)
    store.ensure_tree()
    v1, v2 = "stats_v1", "stats_v2"
    # 存量形态：手工落 bronze 文件（不经 append，模拟旧数据）
    import gzip as _gzip
    import json as _json

    legacy = [
        {"sid": "1001", "parser_version": v1},
        {"sid": "1002", "parser_version": v1},
        {"sid": "1003", "parser_version": v2},
    ]
    path = store.bronze_path("srct", "match_stats")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _gzip.open(path, "wt", encoding="utf-8") as fh:
        for row in legacy:
            fh.write(_json.dumps(row) + "\n")
    # 懒回填 + 版本过滤
    assert store.bronze_sids("srct", "match_stats", v1) == {"1001", "1002"}
    assert store.bronze_sids("srct", "match_stats", v2) == {"1003"}
    # 写入路径：append 后缓存集合同步增长（跨日 collect_day 复用的核心保障）
    store.append_bronze("srct", "match_stats", [{"sid": "1004", "parser_version": v1}])
    assert store.bronze_sids("srct", "match_stats", v1) == {"1001", "1002", "1004"}
    fresh = CorpusStore(_settings(tmp_path).corpus_root)  # 新实例=查表不复用缓存
    assert fresh.bronze_sids("srct", "match_stats", v1) == {"1001", "1002", "1004"}


def test_ingest_raw_race_tolerant(tmp_path, monkeypatch) -> None:
    """双班竞态:对端先 replace 同名 .part → 本端 FileNotFoundError 当成功。"""
    from pathlib import Path

    from goalx_backend.data.corpus_store import CorpusStore

    store = CorpusStore(_settings(tmp_path).corpus_root)
    ref = store.ingest_raw("srct", "match_stats", "1", b"first")
    path = Path(ref.path)

    real_replace = Path.replace

    def racing_replace(self, target):
        if str(self).endswith(".part"):
            (path.parent / "1.gz.part").unlink(missing_ok=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(path.read_bytes() if path.exists() else b"")
            raise FileNotFoundError(str(self))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", racing_replace)
    store.ingest_raw("srct", "match_stats", "1", b"again")  # 不炸=过
