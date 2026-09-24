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
from goalx_backend.data.ingest import srct, srct_odds


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

# 亚盘（2025-10-01 20:00 开球）：源页序新→旧。09:00 与 09-30 同值连续→
# 心跳；12:00 球半、13:00 回摆受让半球（A→B→A）；19:30 临场早段；
# 21:46+ 为赛中（不得顶替赛前末价）；21:50 封盘。
HANDICAP_HTML = (
    "<html><head><title>亚赔变化表</title></head><body><table>"
    "<TR align=center><TD>90</TD><TD>3-1</TD><TD>封</TD><TD>10-01 21:50</TD></TR>"
    "<TR align=center><TD>88</TD><TD>3-1</TD><TD>0.45</TD><TD>平手</TD>"
    "<TD>1.80</TD><TD>10-01 21:48</TD></TR>"
    "<TR align=center><TD>86</TD><TD>2-1</TD><TD>0.42</TD><TD>平手</TD>"
    "<TD>1.90</TD><TD>10-01 21:46</TD></TR>"
    "<TR align=center><TD>0.80</TD><TD>平手/半球</TD><TD>1.05</TD>"
    "<TD>10-01 19:30</TD><TD>即</TD></TR>"
    "<TR align=center><TD>0.90</TD><TD>受让半球</TD><TD>0.92</TD>"
    "<TD>10-01 13:00</TD><TD>即</TD></TR>"
    "<TR align=center><TD>0.95</TD><TD>球半</TD><TD>0.90</TD>"
    "<TD>10-01 12:00</TD><TD>即</TD></TR>"
    "<TR align=center><TD>0.90</TD><TD>受让半球</TD><TD>0.92</TD>"
    "<TD>10-01 09:00</TD><TD>即</TD></TR>"
    "<TR align=center><TD>0.90</TD><TD>受让半球</TD><TD>0.92</TD>"
    "<TD>09-30 22:00</TD><TD>即</TD></TR>"
    "</table></body></html>"
).encode("gb18030")

# 跨年场（2026-01-02 开球）：亚盘行 12-30 无年份→推断 2025
ODDS_JS_EMPTY = (
    'var matchname_cn="英超";\ngame=Array();\ngameDetail=Array();\n'
).encode()
HANDICAP_CROSS_YEAR = (
    "<html><head><title>亚赔变化表</title></head><body><table>"
    "<TR align=center><TD>0.85</TD><TD>半球</TD><TD>0.95</TD>"
    "<TD>12-30 23:00</TD><TD>即</TD></TR>"
    "</table></body></html>"
).encode("gb18030")

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
        srct_handicap_url="https://srct.test/handicap/{sid}",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/over/"):
            key = f"day:{path.removeprefix('/over/').removesuffix('.htm')}"
        elif path.startswith("/odds/"):
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        elif path.startswith("/handicap/"):
            key = f"hdp:{path.removeprefix('/handicap/')}"
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
        f"hdp:{sid}": httpx.Response(200, content=HANDICAP_HTML),
        f"stats:{sid}": httpx.Response(200, content=STATS_HTML),
    }


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
    routes["hdp:90011"] = httpx.Response(200, content=HANDICAP_CROSS_YEAR)
    routes["stats:90011"] = httpx.Response(200, content=STATS_HTML)
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    for day in ("2025-10-01", "2025-10-02", "2026-01-02"):
        srct.collect_day(
            store, settings, _client(routes), date=day, sleeper=lambda _s: None
        )
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
    assert report.rows == 3  # 两 1x2 书 + 亚盘锚
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
    assert set(by_id) == {"srct:1x2:9001", "srct:1x2:1129", "srct:ah:8"}
    sharp = by_id["srct:1x2:9001"]
    assert sharp["space"] == "1x2"
    assert sharp["cid"] == "9001"
    assert sharp["name_en"] == "TestSharp"
    assert sharp["name_zh_masked"] == "测试甲*"
    assert sharp["match_count"] == 2  # 跨年场 odds 为空 game
    assert sharp["first_seen"] <= sharp["last_seen"]
    anchor = by_id["srct:ah:8"]
    assert anchor["space"] == "ah"
    assert anchor["name_en"] is None  # 页面无名
    assert anchor["match_count"] == 3  # 三场亚盘均有报价轨迹


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


def test_cli_srct_odds_payload(tmp_path: Path, capsys: pytest.CaptureFilter) -> None:
    store, settings = _collect_bronze(tmp_path)
    store.close()
    import argparse

    _cmd_srct_odds(argparse.Namespace(), settings=settings)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"bookmaker", "odds", "duckdb"}
    assert payload["odds"]["unexplained_gap"] == 0
    assert payload["bookmaker"]["rows"] == 3
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
        "hdp:90003": httpx.Response(200, content=b"<html></html>"),
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
