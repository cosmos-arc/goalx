"""源T silver 票 64 测试：market_quote/detail/analysis 形状 + 幂等 + 字典吸收。

合成 bronze（collect_day 攒三日常规场）+ 直写多庄/详情/分析 bronze，走
真实 builder/parquet/duckdb 全链。代称红线：书商名合成打码。真源网络
永不进测试。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import duckdb
import httpx
import pyarrow.parquet as pq
import pytest

from goalx_backend.cli import _cmd_srct_market
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_market, srct_odds


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（noop sleeper 下 20/min 忙转，同族先例）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


def _day_page(sid: str, label: str) -> bytes:
    row = (
        "<tr height=18 align=center>"
        f"<td><span>英超</span></td><td>{label}</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


ODDS_JS = ('var matchname_cn="英超";\ngame=Array();\ngameDetail=Array();\n').encode()
STATS_HTML = (
    b'<html><body><script>var jsonData = {"techStat":{"itemList":[]},"info":{}};'
    b"</script></body></html>"
)
ASIANODDS_HTML = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商8 封</td><td></td>"
    '<td title="2025-10-01 08:00">0.90</td>'
    '<td title="2025-10-01 08:00">受让半球</td>'
    '<td title="2025-10-01 08:00">0.95</td>'
    "<td>2.65</td><td>平手/半球</td><td>0.27</td>"
    "<td>0.80</td><td>受让平手/半球</td><td>1.05</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td>"
    "</tr><tr><td></td><td></td><td>盘2</td>"
    "<td>1.10</td><td>受让平手/半球</td><td>0.70</td>"
    "<td>5.00</td><td>平手/半球</td><td>0.12</td>"
    "<td>1.20</td><td>平手</td><td>0.65</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td>"
    "</tr></table></body></html>"
).encode()
OVERDOWN_HTML = (
    "<html><head><title>甲VS乙-大小指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商1 封</td><td></td>"
    '<td title="2025-10-01 09:30">0.93</td>'
    '<td title="2025-10-01 09:30">2.5/3</td>'
    '<td title="2025-10-01 09:30">0.87</td>'
    "<td>1.25</td><td>2.5</td><td>0.50</td>"
    "<td>0.80</td><td>2.5</td><td>1.00</td>"
    "<td><a href=/changeDetail/overunder.aspx?id=1&companyID=1>详</a></td>"
    "</tr></table></body></html>"
).encode()
DETAIL_HTML = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body>"
    "<script>var homeTeamName = '主队甲';var guestTeamName = '客队乙';"
    "var strTime = '2025-10-01 20:00';</script>"
    "<div>VS 场地： 测试球场 天气：晴 温度：20℃</div>"
    "<ul><li class='lists'><div class='data'><span >1.2</span>"
    "<span>预期进球</span><span >1.8</span></div></li></ul>"
    "</body></html>"
).encode()
ANALYSIS_HTML = (
    "<html><head><title>甲VS乙-数据分析-新球体育</title></head><body>"
    "<script>var hometeam = '主队甲';var guestteam = '客队乙';"
    "var h_data =[['25-05-10',36,'英超',52,'主队甲',60,'客队乙']];</script>"
    "<div>未来五场</div><table>"
    "<tr><td>主队甲</td></tr>"
    "<tr><td>05-25</td><td>英超</td><td>他队 - 主队甲</td><td>分析</td>"
    "<td>3 天</td></tr>"
    "</table></body></html>"
).encode()
# 旧模板两页（v3 分发面，裁剪版）：91003 场走 v3 路径进 bronze→silver 全链
OLD_DETAIL_HTML = (
    "<html><head><title>主队甲 VS 客队乙 详细事件-新球体育</title></head><body>"
    "<script>var strTime='2018-11-10 23:00';</script>"
    '<div id="home"><span class="name">主队甲</span></div>'
    '<div id="guest"><span class="name">客队乙</span></div>'
    "场地：测试球场 天气：小雨 温度：9℃～12℃<br />"
    '<table><tr><th colspan="5">本场技术统计</th></tr>'
    "<tr><td></td><td>6</td><td>角球</td><td>4</td><td></td></tr>"
    "<tr><td></td><td>52%</td><td>控球率</td><td>48%</td><td></td></tr></table>"
    '<div class="icon"><img src="/images/bf_img/3.png" />黄牌</div>'
    "<table><tr><th>详细事件</th></tr>"
    "<tr><td></td><td></td><td>20'</td>"
    "<td><img src='/images/bf_img/3.png' title='黄牌' /></td><td>客将A</td></tr>"
    "</table>"
    '<div class="teamNames"><div class="home">主队甲 4-2-3-1</div>'
    '<div class="guest">客队乙 4-2-3-1</div></div>'
    '<div class="plays"><div class="home five">'
    "<div class='play'><span><div></div><div class='name'>"
    "<a href='//info.srct.test/cn/team/player/901/1001.html'>1 主首A</a>"
    "</div></span></div></div>"
    '<div class="guest five"></div></div>'
    '<div class="backupPlay"><div class="home"></div>'
    '<div class="guest"></div></div>'
    "</body></html>"
).encode()
OLD_ANALYSIS_HTML = (
    "<html><head><title>主队甲 VS 客队乙,分析,足球分析</title></head><body>"
    '<script>var hometeam = "主队甲";var guestteam = "客队乙";'
    "var h_data = [['20-12-13',34,'意甲',154,'他队甲',176,'主队甲']];"
    "var v_data = [['20-07-02',34,'意甲',176,'主队甲',2960,'客队乙',1,3,'0-2']];"
    "</script>"
    '<div>未来五场</div><table cellspacing="0"><tbody><tr>'
    '<td valign="top" width="50%"><TABLE>'
    "<tr><td>主队甲</td></tr>"
    "<tr><td>时间</td><td>赛事</td><td>对阵</td><td>分析</td><td>直播</td>"
    "<td>相隔</td></tr>"
    "<tr><td>12-19</td><td>意甲</td><td>主队甲 - 他队甲</td><td>分析</td>"
    "<td></td><td>3 天</td></tr></TABLE></td>"
    '<td valign="top" width="50%"><TABLE>'
    "<tr><td>客队乙</td></tr>"
    "<tr><td>12-23</td><td>意甲</td><td>他队乙 - 客队乙</td><td>分析</td>"
    "<td></td><td>6 天</td></tr></TABLE></td>"
    "</tr></tbody></table></body></html>"
).encode()

DATES = ["2025-10-01", "2025-10-02", "2025-10-03"]
SIDS = ["91001", "91002", "91003"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}",
        srct_overdown_url="https://srct.test/overdown/{sid}",
        srct_detail_url="https://srct.test/detail/{sid}cn.htm",
        srct_analysis_url="https://srct.test/analysis/{sid}cn.htm",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
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
        elif path.startswith("/analysis/"):
            key = f"ay:{path.removeprefix('/analysis/').removesuffix('cn.htm')}"
        else:
            key = f"stats:{path.removeprefix('/shijian/').removesuffix('.htm')}"
        if key not in routes:
            return httpx.Response(500, text="boom")
        return routes[key]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _build_corpus(tmp_path: Path) -> CorpusStore:
    routes: dict[str, httpx.Response] = {}
    for day, sid in zip(DATES, SIDS, strict=True):
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid, label=f"{int(day[-2:])}日20:00")
        )
        routes[f"odds:{sid}"] = httpx.Response(200, content=ODDS_JS)
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_HTML)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_HTML)
        routes[f"dt:{sid}"] = httpx.Response(
            200,
            content=(
                OLD_DETAIL_HTML if sid == SIDS[-1] else DETAIL_HTML
            ),  # 末场走 v3 旧模板路径
        )
        routes[f"ay:{sid}"] = httpx.Response(
            200, content=(OLD_ANALYSIS_HTML if sid == SIDS[-1] else ANALYSIS_HTML)
        )
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_HTML)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    for day in DATES:
        srct.collect_day(
            store, settings, _client(routes), date=day, sleeper=lambda _s: None
        )
    return store


def _read_table(store: CorpusStore, dataset: str) -> list[dict[str, object]]:
    parts = list((store.root / "silver" / "srct" / dataset).glob("**/data.parquet"))
    rows: list[dict[str, object]] = []
    for part in parts:
        rows.extend(pq.read_table(part).to_pylist())
    return rows


def test_market_quote_shape_and_semantics(tmp_path: Path) -> None:
    store = _build_corpus(tmp_path)
    try:
        report = srct_market.build_market_quotes(store)
    finally:
        store.close()
    rows = _read_table(store, srct_market.MARKET_DATASET)
    # 三场 ×（ah 2 逐盘行 + ou 1 行）
    assert (report.quotes_ah, report.quotes_ou) == (6, 3)
    assert len(rows) == 9
    assert report.bad_waters == 0
    ah1 = next(
        r
        for r in rows
        if r["market"] == "ah" and r["sid"] == "91001" and r["multi"] == "盘1"
    )
    assert ah1["bookmaker_id"] == "srct:ah:8"
    assert ah1["open_line"] == -0.5  # 受让半球 → 主队视角负
    assert ah1["close_line"] == -0.25  # 受让平手/半球
    assert ah1["close_home_water"] == 0.80
    assert ah1["close_line_raw"] == "受让平手/半球"
    assert ah1["latest_home_water"] == 2.65  # 场内末价组
    # v2：初盘时刻（页 title 北京钟面）+ latest 相位（抓取晚于开球=inplay）
    assert int(ah1["open_at"].timestamp() * 1000) == srct_odds.beijing_ms(
        datetime(2025, 10, 1, 8, 0)
    )
    assert ah1["latest_phase"] == "inplay"
    ah2 = next(
        r
        for r in rows
        if r["market"] == "ah" and r["sid"] == "91001" and r["multi"] == "盘2"
    )
    assert ah2["open_at"] is None  # 无 title 行贴源 None
    ou1 = next(r for r in rows if r["market"] == "ou" and r["sid"] == "91001")
    assert ou1["bookmaker_id"] == "srct:ou:1"
    assert ou1["open_line_raw"] == "2.5/3"  # 大小球线数值化（四分位中值）
    assert ou1["open_line"] == 2.75
    assert ou1["close_line"] == 2.5
    assert int(ou1["open_at"].timestamp() * 1000) == srct_odds.beijing_ms(
        datetime(2025, 10, 1, 9, 30)
    )
    assert report.initial_at_missing == 3  # 三场 ah 盘2 行有初值无时刻
    assert report.bad_line_values == 0  # ah 盘口词/ou 数值线全可归一
    assert ah1["kickoff"] is not None  # fixture 冗余列
    # 视图可查
    path = corpus_duckdb.build_corpus_duckdb(store)
    con = duckdb.connect(str(path), read_only=True)
    try:
        cnt = con.execute(
            "SELECT count(*) FROM market_quote WHERE market='ah' AND close_line=-0.25"
        ).fetchone()[0]
        assert cnt == 3
    finally:
        con.close()


def test_detail_analysis_face_shapes(tmp_path: Path) -> None:
    store = _build_corpus(tmp_path)
    try:
        detail = srct_market.build_detail_faces(store)
        analysis = srct_market.build_analysis_faces(store)
    finally:
        store.close()
    assert detail.rows == 3
    d1 = next(r for r in _read_table(store, "match_detail") if r["sid"] == "91001")
    assert (d1["home"], d1["away"], d1["venue"], d1["weather"]) == (
        "主队甲",
        "客队乙",
        "测试球场",
        "晴",
    )
    assert d1["has_xg"] is True
    assert (d1["xg_home"], d1["xg_away"]) == (1.2, 1.8)
    assert analysis.rows == 3
    a1 = next(r for r in _read_table(store, "match_analysis") if r["sid"] == "91001")
    assert a1["recent_home"] == 1
    assert a1["future_home"] == 1
    assert a1["next_home_gap_days"] == 3  # 密度特征：相隔 3 天
    assert a1["next_away_gap_days"] is None  # 客队块缺=空≠无
    # v3 旧模板场（91003 经分发入口进 bronze→silver，与 v2 行同表同构）
    d3 = next(r for r in _read_table(store, "match_detail") if r["sid"] == "91003")
    assert (d3["home"], d3["away"], d3["venue"], d3["weather"]) == (
        "主队甲",
        "客队乙",
        "测试球场",
        "小雨",
    )
    assert (d3["tech_rows"], d3["events"], d3["home_starters"]) == (2, 1, 1)
    assert (d3["has_xg"], d3["xg_home"], d3["home_formation"]) == (
        False,
        None,
        "4-2-3-1",
    )
    a3 = next(r for r in _read_table(store, "match_analysis") if r["sid"] == "91003")
    assert (a3["recent_home"], a3["h2h"], a3["standings_home"]) == (1, 1, 0)
    assert (a3["future_home"], a3["next_home_gap_days"]) == (1, 3)
    assert a3["next_away_gap_days"] == 6


def test_idempotent_rebuild_byte_identical(tmp_path: Path) -> None:
    store = _build_corpus(tmp_path)
    root = store.root / "silver" / "srct"
    try:
        first = srct_market.build_market_quotes(store)
        srct_market.build_detail_faces(store)
        srct_market.build_analysis_faces(store)
        digests = {
            ds: _tree_digest(root / ds)
            for ds in ("market_quote", "match_detail", "match_analysis")
        }
        second = srct_market.build_market_quotes(store)
        srct_market.build_detail_faces(store)
        srct_market.build_analysis_faces(store)
    finally:
        store.close()
    assert digests == {
        ds: _tree_digest(root / ds)
        for ds in ("market_quote", "match_detail", "match_analysis")
    }
    # 记账零漂移：built_at 是活钟戳，两次重建跨秒即差 1s（负载下偶发），剔除后全比
    first_fields = {k: v for k, v in asdict(first).items() if k != "built_at"}
    second_fields = {k: v for k, v in asdict(second).items() if k != "built_at"}
    assert first_fields == second_fields


def _tree_digest(root: Path) -> str:
    sha = hashlib.sha256()
    for part in sorted(root.glob("**/data.parquet")):
        sha.update(part.relative_to(root).as_posix().encode())
        sha.update(part.read_bytes())
    return sha.hexdigest()


def test_cli_srct_market_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _build_corpus(tmp_path)
    store.close()
    _cmd_srct_market(argparse.Namespace(), settings=_settings(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"market", "detail", "analysis", "duckdb"}
    assert payload["market"]["quotes_ah"] == 6
    assert payload["detail"]["rows"] == 3
    assert payload["analysis"]["rows"] == 3
    assert Path(payload["duckdb"]).exists()


def test_bookmaker_absorbs_market_identities(tmp_path: Path) -> None:
    """票 64：cid=8 有代称化身份（多庄页遮罩名），ou 空间入字典。"""
    store = _build_corpus(tmp_path)
    try:
        report = srct_odds.build_bookmakers(store)
    finally:
        store.close()
    rows = _read_table(store, srct_odds.BOOKMAKER_DATASET)
    by_id = {r["bookmaker_id"]: r for r in rows}
    anchor = by_id["srct:ah:8"]
    assert anchor["name_zh_masked"] == "书商8"  # 站点遮罩短名（代称化天然合规）
    assert anchor["match_count"] == 3  # 三场各计一次（盘2 去重）
    assert by_id["srct:ou:1"]["name_zh_masked"] == "书商1"
    assert report.rows == 2  # 空 1x2 语料：仅两多庄条目
