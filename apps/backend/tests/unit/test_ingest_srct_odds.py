"""源T silver odds_change_event + bookmaker 字典测试（票 55/56 切片 15）。

合成页（结构对齐实测裁剪样本，cid/书名全假——代称红线）走 MockTransport
攒 bronze，再走真实 builder/parquet/duckdb 全链：去重语义（心跳丢/A→B→A
留/首末规则）、source_order 同分钟并列、亚盘年份推断与线值归一、门④记账
恒等、LAST 重建（赛中价不顶替赛前价）、幂等重建、CLI 接线。真源网络
永不进测试。端点模板一律 `.test` 占位域。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import httpx
import pyarrow.parquet as pq
import pytest

from goalx_backend.cli import _cmd_srct_odds
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds, srct_silver


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（noop sleeper 下 20/min 会忙转到真实时间翻窗）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


def _day_page(sid: str, league: str = "英超", label: str = "1日20:00") -> bytes:
    row = (
        "<tr height=18 align=center>"
        f"<td><span>{league}</span></td><td>{label}</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


# 两书商：9001（轨迹含心跳/同分钟对/A→B→A）、1129（初盘截断信号）；
# gameDetail 的 700099 不在 game 数组（未映射记账）。时间全北京钟面，
# 2025-10-01 20:00 开球（day 页 label 派生）。
_ODDS_GAMES = [
    "9001|700001|TestSharp|1.30|5.50|8.50|71|17|12|93|1.30|5.50|8.50|72|17|11|94|"
    "0.93|0.97|0.84|2025,10-1,19,0,0,00|测试甲*|1|0|0.85|0.95|1.11",
    "1129|700002|TestLottery|3.20|3.40|2.10|27|26|47|88|2.90|3.10|2.20|30|27|43|88|"
    "0.85|0.85|0.93|2025,10-1,19,0,0,00|测试官*|1|0|0.8|0.9|1.0",
]

# 源页序=新→旧：13:00 回摆 A；12:45 同分钟异价对（计冲突）；12:10；
# 11:48 同分钟同值心跳对（不计冲突，去重照丢）；10:00 首报（=game 初盘）
_ODDS_DETAILS = [
    "700001^1.40|5.00|8.00|10-01 13:00|0.92|0.90|0.90|2025;"
    "1.52|4.75|7.60|10-01 12:45|0.90|0.90|0.90|2025;"
    "1.50|4.80|7.50|10-01 12:45|0.90|0.90|0.90|2025;"
    "1.40|5.00|8.00|10-01 12:10|0.92|0.90|0.90|2025;"
    "1.36|5.06|8.62|10-01 11:48|0.98|0.89|0.91|2025;"
    "1.36|5.06|8.62|10-01 11:48|0.98|0.89|0.91|2025;"
    "1.30|5.50|8.50|10-01 10:00|0.93|0.97|0.84|2025;",
    "700002^3.10|3.30|2.20|10-01 12:00|0.85|0.85|0.93|2025;",
    "700099^1.40|5.00|8.00|10-01 09:00|1.00|1.00|1.00|2025;",
]

ODDS_JS = (
    'var matchname_cn="英超";\n'
    'game=Array(\n"' + '",\n"'.join(_ODDS_GAMES) + '");\n'
    "gameDetail=Array(\n" + ",\n".join(f'"{d}"' for d in _ODDS_DETAILS) + ");\n"
).encode()

ODDS_JS_EMPTY = (
    'var matchname_cn="英超";\ngame=Array();\ngameDetail=Array();\n'
).encode()


# 撤采留档（票 59）：changeDetail 单书亚盘轨迹不再有采集面——silver ah 面
# 的输入 bronze 行在测试里直写（结构与撤除前解析器产出逐字段一致）。
# 两常规场（2025-10-01 20:00 开球）：行序=源页序新→旧。09:00 与 09-30 同值
# 连续→心跳；12:00 球半、13:00 回摆受让半球（A→B→A）；19:30 临场早段；
# 21:46+ 为赛中（不得顶替赛前末价）；21:50 封盘。
def _hdp_row(
    minute: str | None,
    score: str | None,
    home_water: str | None,
    line: str | None,
    away_water: str | None,
    change_time: str,
    status: str | None,
) -> dict[str, object]:
    """撤除前 parse_handicap_page 的行形状（贴源串/缺列 None）。"""
    return {
        "minute": minute,
        "score": score,
        "home_water": home_water,
        "line": line,
        "away_water": away_water,
        "change_time": change_time,
        "status": status,
    }


_HDP_ROWS = [
    _hdp_row("90", "3-1", None, None, None, "10-01 21:50", "封"),
    _hdp_row("88", "3-1", "0.45", "平手", "1.80", "10-01 21:48", None),
    _hdp_row("86", "2-1", "0.42", "平手", "1.90", "10-01 21:46", None),
    _hdp_row(None, None, "0.80", "平手/半球", "1.05", "10-01 19:30", "即"),
    _hdp_row(None, None, "0.90", "受让半球", "0.92", "10-01 13:00", "即"),
    _hdp_row(None, None, "0.95", "球半", "0.90", "10-01 12:00", "即"),
    _hdp_row(None, None, "0.90", "受让半球", "0.92", "10-01 09:00", "即"),
    _hdp_row(None, None, "0.90", "受让半球", "0.92", "09-30 22:00", "即"),
]
# 跨年场（2026-01-02 开球）：亚盘行 12-30 无年份→推断 2025
_HDP_ROWS_CROSS_YEAR = [
    _hdp_row(None, None, "0.85", "半球", "0.95", "12-30 23:00", "即"),
]

# 亚盘多庄页（票 59 新采集面；silver 消费在票 64，这里只让采集闭环干净）
ASIANODDS_HTML = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商8 封</td><td></td>"
    "<td>0.90</td><td>受让半球</td><td>0.95</td>"
    "<td>2.65</td><td>平手/半球</td><td>0.27</td>"
    "<td>0.80</td><td>受让平手/半球</td><td>1.05</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td>"
    "</tr></table></body></html>"
).encode()
# 大小球多庄页（票 60）：与亚盘多庄同构，线=进球数盘口线
OVERDOWN_HTML = (
    "<html><head><title>甲VS乙-大小指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商1 封</td><td></td>"
    "<td>0.93</td><td>2.5/3</td><td>0.87</td>"
    "<td>1.25</td><td>2.5</td><td>0.50</td>"
    "<td>0.80</td><td>2.5</td><td>1.00</td>"
    "<td><a href=/changeDetail/overunder.aspx?id=1&companyID=1>详</a></td>"
    "</tr></table></body></html>"
).encode()
# 详情页（票 61）：与旧 stats 并存（独立数据集）；tech 一行即有效页
DETAIL_HTML = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body>"
    "<ul><li class='lists'><div class='data'><span >3</span><span>角球</span>"
    "<span >3</span></div></li></ul>"
    "</body></html>"
).encode()
# 分析页（票 62）：数组层一行即正证据（特征面 bronze-only）
ANALYSIS_HTML = (
    "<html><head><title>甲VS乙-数据分析-新球体育</title></head><body>"
    "<script>var h_data =[['25-05-10',36,'英超',52,'主队甲',60,'客队乙']];</script>"
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


def _odds_routes(sid: str, *, empty_odds: bool = False) -> dict[str, httpx.Response]:
    return {
        f"odds:{sid}": httpx.Response(
            200, content=ODDS_JS_EMPTY if empty_odds else ODDS_JS
        ),
        f"ah:{sid}": httpx.Response(200, content=ASIANODDS_HTML),
        f"ou:{sid}": httpx.Response(200, content=OVERDOWN_HTML),
        f"dt:{sid}": httpx.Response(200, content=DETAIL_HTML),
        f"ay:{sid}": httpx.Response(200, content=ANALYSIS_HTML),
        f"stats:{sid}": httpx.Response(200, content=STATS_HTML),
    }


def _append_hdp_bronze(
    store: CorpusStore, rows: list[dict[str, object]], sids: list[str]
) -> None:
    """直写撤采留档数据集的 bronze 行（采集面已停，silver 仍消费该形状）。"""
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct.HANDICAP_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct.HANDICAP_DATASET,
                "sid": sid,
                "fetched_at": "2026-09-25T00:00:00+00:00",
                "parser_version": srct.BRONZE_VERSIONS[srct.HANDICAP_DATASET],
                "raw_sha": "f" * 64,
                "payload": {"rows": rows},
            }
            for sid in sids
        ],
    )


def _collect_bronze(tmp_path: Path) -> tuple[CorpusStore, Settings]:
    """两日常规场（2025-10-01/02）+ 一跨年场（2026-01-02）攒 bronze。"""
    routes: dict[str, httpx.Response] = {}
    for day, sid in (("2025-10-01", "90001"), ("2025-10-02", "90002")):
        label = f"{int(day[-2:])}日20:00"
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid, label=label)
        )
        routes.update(_odds_routes(sid))
    routes["day:20260102"] = httpx.Response(
        200, content=_day_page("90011", label="2日20:00")
    )
    routes["odds:90011"] = httpx.Response(200, content=ODDS_JS_EMPTY)
    routes["ah:90011"] = httpx.Response(200, content=ASIANODDS_HTML)
    routes["ou:90011"] = httpx.Response(200, content=OVERDOWN_HTML)
    routes["dt:90011"] = httpx.Response(200, content=DETAIL_HTML)
    routes["ay:90011"] = httpx.Response(200, content=ANALYSIS_HTML)
    routes["stats:90011"] = httpx.Response(200, content=STATS_HTML)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    for day in ("2025-10-01", "2025-10-02", "2026-01-02"):
        srct.collect_day(
            store, settings, _client(routes), date=day, sleeper=lambda _s: None
        )
    # 亚盘轨迹 bronze 直写（票 59 起采集面只产 asian_odds 多庄页）
    _append_hdp_bronze(store, _HDP_ROWS, ["90001", "90002"])
    _append_hdp_bronze(store, _HDP_ROWS_CROSS_YEAR, ["90011"])
    return store, settings


def _odds_rows(store: CorpusStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for part in sorted(
        (store.root / "silver" / "srct" / srct_odds.ODDS_DATASET).glob(
            "season=*/*/data.parquet"
        )
    ):
        rows.extend(pq.read_table(part).to_pylist())
    return rows


def _book_rows(store: CorpusStore) -> list[dict[str, Any]]:
    table = pq.read_table(
        store.root / "silver" / "srct" / srct_odds.BOOKMAKER_DATASET / "data.parquet"
    )
    return table.to_pylist()


def test_bookmaker_dictionary_shape_and_coverage(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    try:
        report = srct_odds.build_bookmakers(store)
    finally:
        store.close()
    rows = _book_rows(store)
    assert report.rows == 4  # 两 1x2 书 + 亚盘多庄锚 + 大小球多庄家（票 64）
    assert report.ah_anchor is True
    assert report.matches == 3
    fields = {
        f.name
        for f in pq.read_schema(
            store.root
            / "silver"
            / "srct"
            / srct_odds.BOOKMAKER_DATASET
            / "data.parquet"
        )
    }
    assert "tier" not in fields  # 设计红线：无分层列
    assert "is_core" not in fields
    by_id = {r["bookmaker_id"]: r for r in rows}
    assert set(by_id) == {"srct:1x2:9001", "srct:1x2:1129", "srct:ah:8", "srct:ou:1"}
    sharp = by_id["srct:1x2:9001"]
    assert sharp["space"] == "1x2"
    assert sharp["cid"] == "9001"
    assert sharp["name_en"] == "TestSharp"
    assert sharp["name_zh_masked"] == "测试甲*"
    assert sharp["match_count"] == 2  # 跨年场 odds 为空 game
    assert sharp["first_seen"] <= sharp["last_seen"]
    anchor = by_id["srct:ah:8"]
    assert anchor["space"] == "ah"
    assert anchor["name_en"] is None  # 多庄页无 en 名
    assert anchor["name_zh_masked"] == "书商8"  # 站点遮罩短名（票 64 代称化）
    assert anchor["match_count"] == 6  # 多庄页 3 场 + 留档轨迹 3 场并集
    ou_book = by_id["srct:ou:1"]
    assert ou_book["space"] == "ou"
    assert ou_book["name_zh_masked"] == "书商1"
    assert ou_book["match_count"] == 3  # 三场大小球页各一书


def test_odds_events_semantics_and_gate_accounting(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    try:
        report = srct_odds.build_odds_change_events(store, chunk_rows=2)
    finally:
        store.close()
    rows = _odds_rows(store)
    # 每场记一次（两常规场同构 + 跨年场只亚盘 1 行）
    assert report.unexplained_gap == 0  # 门④：转换完整性
    assert report.events_1x2 == 2 * 7  # 9001: 7 保留（心跳 1 丢）；1129: 1
    assert report.events_ah == 2 * 7 + 1
    assert report.heartbeat_dropped == 4  # 每场 1x2 1 + ah 1
    assert report.source_rows_1x2 == 2 * 9
    assert report.source_rows_ah == 2 * 8 + 1
    assert report.unmapped_gameid_rows == 2  # 每场 gameDetail 700099
    assert report.same_minute_conflicts == 2  # 每场 12:45 对
    assert report.open_quote_missing == 2  # 1129 初盘 3.20 不在轨迹首行
    assert report.bad_line_values == 0
    assert report.bad_prices == 0
    row_9001 = [
        r
        for r in rows
        if r["sid"] == "90001"
        and r["market"] == "1x2"
        and r["bookmaker_id"] == "srct:1x2:9001"
    ]
    # A→B→A：1.40 在 12:10 与 13:00 两现，中间隔同分钟对；12:45 两行按
    # 源页序（新→旧）稳定并列：1.52(order3) 在 1.50(order4) 前
    homes = [r["odds_home"] for r in row_9001]
    assert homes == [1.30, 1.36, 1.40, 1.52, 1.50, 1.40]
    published = [r["published_at"] for r in row_9001]
    assert published == sorted(published)  # 书内时间升序
    minute_pair = [r for r in row_9001 if _minute_of(r) == "12:45"]
    assert [r["source_order"] for r in minute_pair] == [1, 2]  # 确定性并列


def _shanghai(value: Any) -> datetime:
    """parquet 读出的 published_at（naive-UTC 或 aware-UTC）→ 上海墙钟。"""
    moment = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    return moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Asia/Shanghai"))


def _minute_of(row: dict[str, Any]) -> str:
    beijing = _shanghai(row["published_at"])
    return f"{beijing.hour:02d}:{beijing.minute:02d}"


def test_ah_line_normalization_and_live_context(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    try:
        srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    rows = [r for r in _odds_rows(store) if r["sid"] == "90001" and r["market"] == "ah"]
    by_line = {r["line_raw"]: r for r in rows}
    assert by_line["受让半球"]["line"] == -0.5  # 主队视角受让为负
    assert by_line["球半"]["line"] == 1.5
    assert by_line["平手/半球"]["line"] == 0.25
    assert by_line["平手"]["line"] == 0.0
    live = by_line["平手"]
    assert live["minute"] == 88  # 上下文列随行
    assert live["score"] == "3-1"
    sealed = [r for r in rows if r["status"] == "封"]
    assert len(sealed) == 1
    assert sealed[0]["minute"] == 90
    # 心跳已丢：受让半球 0.90/0.92 只剩两行（09-30 与 13:00，A→B→A 两端）
    assert sum(1 for r in rows if r["line_raw"] == "受让半球") == 2


def test_ah_cross_year_inference(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    try:
        srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    rows = [r for r in _odds_rows(store) if r["sid"] == "90011" and r["market"] == "ah"]
    assert len(rows) == 1
    assert rows[0]["published_at"].year == 2025  # 12-30 → 锚年 2026 的前一年
    assert _shanghai(rows[0]["published_at"]).strftime("%m-%d %H:%M") == "12-30 23:00"
    part = (
        store.root
        / "silver"
        / "srct"
        / srct_odds.ODDS_DATASET
        / "season=2025-26"
        / "competition=英超"
        / "data.parquet"
    )
    assert part.exists()  # 1 月场仍属 2025-26 季


def test_last_visible_reconstruction_excludes_live(tmp_path: Path) -> None:
    """门内验收查询：first/last_pre_kickoff 可见价，赛中价不顶替。"""
    store, _ = _collect_bronze(tmp_path)
    try:
        srct_odds.build_bookmakers(store)
        srct_odds.build_odds_change_events(store)
        path = corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    con = duckdb.connect(str(path), read_only=True)
    try:
        first_home, last_home = con.execute(
            """
            SELECT first(odds_home ORDER BY published_at, source_order),
                   last(odds_home ORDER BY published_at, source_order)
            FROM odds_change_event
            WHERE sid = '90001' AND market = '1x2'
              AND bookmaker_id = 'srct:1x2:9001' AND published_at < kickoff
            """
        ).fetchone()
        assert first_home == 1.30  # 最早可见完整报价
        assert last_home == 1.40  # 赛前末个可见（13:00 回摆价）
        live_max = con.execute(
            """
            SELECT max(published_at) FROM odds_change_event
            WHERE sid = '90001' AND market = 'ah' AND minute IS NOT NULL
            """
        ).fetchone()[0]
        last_pre = con.execute(
            """
            SELECT line_raw FROM odds_change_event
            WHERE sid = '90001' AND market = 'ah'
              AND published_at < kickoff
            ORDER BY published_at DESC, source_order DESC LIMIT 1
            """
        ).fetchone()[0]
        assert last_pre == "平手/半球"  # 19:30 早段价顶赛前末位
        assert live_max is not None  # 赛中行存在但被排除
        assert live_max.hour >= 13
        books = con.execute(
            "SELECT count(*) FROM bookmaker WHERE space = 'ah'"
        ).fetchone()[0]
        assert books == 1
    finally:
        con.close()


def test_orphan_sid_lands_unknown_partition(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    try:
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "99999",
                    "fetched_at": "2026-09-24T00:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "0" * 64,
                    "payload": {
                        "meta": {},
                        "game": ["9001|700001|TestSharp|1.30|5.50|8.50|"],
                        "game_detail": [
                            "700001^1.30|5.50|8.50|10-01 10:00|0.9|0.9|0.9|2025;"
                        ],
                    },
                }
            ],
        )
        report = srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    assert report.orphan_sids == 1
    part = (
        store.root
        / "silver"
        / "srct"
        / srct_odds.ODDS_DATASET
        / "season=_unknown"
        / "competition=_unknown"
        / "data.parquet"
    )
    assert part.exists()
    rows = pq.read_table(part).to_pylist()
    assert {r["sid"] for r in rows} == {"99999"}
    assert rows[0]["kickoff"] is None
    assert report.unexplained_gap == 0


def test_stale_partition_removed_and_idempotent_rebuild(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    root = store.root / "silver" / "srct" / srct_odds.ODDS_DATASET
    stale = root / "season=2024-25" / "competition=西甲"
    stale.mkdir(parents=True)
    (stale / "data.parquet").write_bytes(b"stale")
    try:
        first = srct_odds.build_odds_change_events(store)
        digest_first = _tree_digest(root)
        second = srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    assert not stale.exists()  # 陈旧分区清理（首次构建即移除）
    assert first.stale_partitions_removed == 1
    assert second.stale_partitions_removed == 0
    assert _tree_digest(root) == digest_first  # 幂等：字节级重建一致


def _tree_digest(root: Path) -> str:
    sha = hashlib.sha256()
    for part in sorted(root.glob("season=*/competition=*/data.parquet")):
        sha.update(part.relative_to(root).as_posix().encode())
        sha.update(part.read_bytes())
    return sha.hexdigest()


def test_cli_srct_odds_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, settings = _collect_bronze(tmp_path)
    store.close()
    import argparse

    _cmd_srct_odds(argparse.Namespace(), settings=settings)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"bookmaker", "odds", "duckdb"}
    assert payload["odds"]["unexplained_gap"] == 0
    assert payload["bookmaker"]["rows"] == 4
    assert payload["bookmaker"]["ah_anchor"] is True
    assert Path(payload["duckdb"]).exists()


def test_no_pre_kickoff_quote_returns_missing(tmp_path: Path) -> None:
    """验收框 4：无赛前行 → 赛前末可见价为 NULL（不取赛中值顶替）。"""
    routes: dict[str, httpx.Response] = {
        "day:20251001": httpx.Response(
            200, content=_day_page("90003", label="1日20:00")
        ),
        "odds:90003": httpx.Response(
            200,
            content=(
                'var matchname_cn="英超";\ngame=Array(\n"'
                + _ODDS_GAMES[0]
                + '");\n'
                + 'gameDetail=Array(\n"700001^1.40|5.00|8.00|10-01 21:00|'
                '0.92|0.90|0.90|2025;");\n'
            ).encode(),
        ),
        "ah:90003": httpx.Response(200, content=ASIANODDS_HTML),
        "stats:90003": httpx.Response(200, content=STATS_HTML),
    }
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    srct.collect_day(
        store, settings, _client(routes), date="2025-10-01", sleeper=lambda _s: None
    )
    try:
        report = srct_odds.build_odds_change_events(store)
        path = corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    assert report.no_pre_kickoff_quote == 1  # 唯一事件 21:00 > 开球 20:00
    con = duckdb.connect(str(path), read_only=True)
    try:
        last_pre = con.execute(
            """
            SELECT last(odds_home ORDER BY published_at, source_order)
            FROM odds_change_event
            WHERE sid = '90003' AND market = '1x2'
              AND published_at < kickoff
            """
        ).fetchone()[0]
        assert last_pre is None  # 缺失即缺失，赛中价不顶替
    finally:
        con.close()


def test_normalize_line_map_edges() -> None:
    assert srct_odds.normalize_line("平手") == 0.0
    assert srct_odds.normalize_line("受让半球/一球") == -0.75
    assert srct_odds.normalize_line("两球半/三球") == 2.75
    assert srct_odds.normalize_line("未识别盘口") is None
    assert srct_odds.normalize_line(None) is None


def test_empty_corpus_builds_meta_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        book_report = srct_odds.build_bookmakers(store)
        odds_report = srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    assert book_report.rows == 0
    assert book_report.ah_anchor is False
    assert odds_report.events_1x2 == 0
    assert odds_report.partitions == 0
    assert (
        store.root / "silver" / "srct" / srct_odds.ODDS_DATASET / "_meta.json"
    ).exists()


# ---- 票 19：分块选轨 + 外排（输入侧内存有界化） ----


def _report_payload(report: object) -> dict[str, Any]:
    return {k: v for k, v in asdict(report).items() if k != "built_at"}


def test_chunking_transparent_byte_identical(tmp_path: Path) -> None:
    """分块透明性：块=大（现状等价单块）与块=极小（逐场多 spill）字节一致。"""
    store, _ = _collect_bronze(tmp_path)
    root = store.root / "silver" / "srct" / srct_odds.ODDS_DATASET
    try:
        single = srct_odds.build_odds_change_events(store, chunk_sids=10**6)
        digest_single = _tree_digest(root)
        tiny = srct_odds.build_odds_change_events(store, chunk_sids=1)
        digest_tiny = _tree_digest(root)
    finally:
        store.close()
    assert digest_tiny == digest_single
    assert _report_payload(tiny) == _report_payload(single)  # 记账也零漂移


def test_spill_dir_cleaned_and_crash_leftover_safe_rerun(tmp_path: Path) -> None:
    store, _ = _collect_bronze(tmp_path)
    root = store.root / "silver" / "srct" / srct_odds.ODDS_DATASET
    spill_root = store.root / "tmp" / "srct_odds_spill"
    try:
        srct_odds.build_odds_change_events(store, chunk_sids=1)
        digest = _tree_digest(root)
        leftover = spill_root / "000000.ndjson"  # 模拟异常退出残留
        leftover.parent.mkdir(parents=True)
        leftover.write_text("garbage\n")
        srct_odds.build_odds_change_events(store, chunk_sids=2)
    finally:
        store.close()
    assert not spill_root.exists()  # 处理完即删；残留先清再跑
    assert _tree_digest(root) == digest  # 重跑幂等不受残留影响


def test_ledger_selects_latest_row_and_filters_old_version(tmp_path: Path) -> None:
    """选轨遍只装信封账本（结构保证：值全 int）；后行胜出+旧版本不可见。"""
    store, _ = _collect_bronze(tmp_path)
    try:
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {  # 同 sid 重抓：700001 轨迹只剩首报一行
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90001",
                    "fetched_at": "2026-09-25T00:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "1" * 64,
                    "payload": {
                        "meta": {},
                        "game": [_ODDS_GAMES[0]],
                        "game_detail": [
                            "700001^1.30|5.50|8.50|10-01 10:00|0.93|0.97|0.84|2025;"
                        ],
                    },
                },
                {  # 旧 parser_version：即使行序在后也不可见
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90002",
                    "fetched_at": "2026-09-26T00:00:00+00:00",
                    "parser_version": "srct_odds_v0",
                    "raw_sha": "2" * 64,
                    "payload": {"meta": {}, "game": [], "game_detail": []},
                },
            ],
        )
        ledger = srct_silver.latest_bronze_ledger(store, srct.ODDS_DATASET)
        report = srct_odds.build_odds_change_events(store)
    finally:
        store.close()
    assert all(isinstance(v, int) for v in ledger.values())  # 零物化账本
    rows_90001 = [
        r
        for r in _odds_rows(store)
        if r["sid"] == "90001" and r["bookmaker_id"] == "srct:1x2:9001"
    ]
    assert len(rows_90001) == 1  # 重抓轨迹（后行）胜出：只余首报
    assert report.unmapped_gameid_rows == 1  # 90001 新轨迹无 700099；90002 旧版不可见
    assert report.unexplained_gap == 0


def test_bookmaker_name_latest_trajectory_wins(tmp_path: Path) -> None:
    """字典名胜出跨重抓仍取最新轨迹（流式化后语义零变化）。"""
    store, _ = _collect_bronze(tmp_path)
    try:
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90001",
                    "fetched_at": "2030-01-01T00:00:00+00:00",  # 必晚于采集真值
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "3" * 64,
                    "payload": {
                        "meta": {},
                        "game": ["9001|700001|TestRenamed|1.30|5.50|8.50|"],
                        "game_detail": [],
                    },
                }
            ],
        )
        srct_odds.build_bookmakers(store)
    finally:
        store.close()
    by_id = {r["bookmaker_id"]: r for r in _book_rows(store)}
    sharp = by_id["srct:1x2:9001"]
    assert sharp["name_en"] == "TestRenamed"  # 最新轨迹英文名胜出
    assert sharp["name_zh_masked"] == "测试甲*"  # 新轨迹无遮罩名：旧值独立保留
    assert sharp["match_count"] == 2  # 每 sid 只计选中轨迹（90002 未重抓）
