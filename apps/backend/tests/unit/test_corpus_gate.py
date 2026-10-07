"""Phase1 五门报告测试（票 55/56 切片 17）。

合成语料（MockTransport 攒 bronze→四件套 silver→duckdb）+ 合成运行面
goalx.db（hist_matches/understat_matches 种子行，列对齐真实 schema 子集）
走真实报告全链：三对账匹配锚（联赛+比分+日期±1）、门②锚价偏差、门③
xG 异常清单、门④恒等式、门⑤键覆盖、JSON/MD 两视图与 CLI 接线。
代称红线：cid/书名全假。真源网络永不进测试。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.cli import _cmd_srct_gate
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, corpus_gate
from goalx_backend.data.corpus_gate import FixtureRow, HistRow, match_fixtures
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds, srct_silver

GATE_TODAY = date(2025, 10, 4)  # 报告今天（Phase1 窗口内，进度远未完）

# 三日三场：联赛=英超、比分各异（避免 ±1 窗跨日同比分歧义）；锚书商
# cid177 轨迹赛前末可见 1.40/5.00/8.00（13:00 变价，21:30 为赛中价）
DAYS: list[tuple[str, str, str]] = [
    ("2025-10-01", "90001", "1-2"),
    ("2025-10-02", "90002", "2-0"),
    ("2025-10-03", "90003", "0-3"),
]
ODDS_JS = (
    'var matchname_cn="英超";\ngame=Array(\n'
    '"177|600001|TestAnchor|1.50|6.00|9.00|60|20|13|90|1.40|5.00|8.00|61|20|14|89|'
    '0.90|0.90|0.90|2025,10-1,13,0,0,00|测试锚*|1|0|0.9|0.9|0.9",\n'
    '"9001|600002|TestSharp|1.30|5.50|8.50|71|17|12|93|1.30|5.50|8.50|72|17|11|94|'
    '0.93|0.97|0.84|2025,10-1,10,0,0,00|测试甲*|1|0|0.85|0.95|1.11");\n'
    "gameDetail=Array(\n"
    '"600001^1.40|5.00|8.00|10-01 21:30|0.92|0.90|0.90|2025;'
    "1.40|5.00|8.00|10-01 13:00|0.91|0.91|0.91|2025;"
    '1.50|6.00|9.00|10-01 10:00|0.90|0.90|0.90|2025;",\n'
    '"600002^1.30|5.50|8.50|10-01 10:00|0.93|0.97|0.84|2025;");\n'
).encode()
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
# 详情页（票 61）：与旧 stats 并存（独立数据集）；tech 一行即有效页
DETAIL_BYTES = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body>"
    "<ul><li class='lists'><div class='data'><span >3</span><span>角球</span>"
    "<span >3</span></div></li></ul>"
    "</body></html>"
).encode()
# 分析页（票 62）：数组层一行即正证据（特征面 bronze-only）
ANALYSIS_BYTES = (
    "<html><head><title>甲VS乙-数据分析-新球体育</title></head><body>"
    "<script>var h_data =[['25-05-10',36,'英超',52,'主队甲',60,'客队乙']];</script>"
    "</body></html>"
).encode()
STATS_HTML = (
    '<html><body><script>var jsonData = {"techStat":{"itemList":['
    '{"home":{"value":0.71},"away":{"value":1.68},"name":"预期进球",'
    '"kind":"EXPECTED_GOALS"}]},"info":{}};</script></body></html>'
).encode()


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


def _day_page(sid: str, label: str, score: str) -> bytes:
    home, away = (int(g) for g in score.split("-"))
    row = (
        "<tr height=18 align=center>"
        f"<td><span>英超</span></td><td>{label}</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        f"<td class=style1><font color=blue>{home}</font>"
        f"-<font color=red>{away}</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


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


def _seed_running_face(db_path: Path) -> None:
    """合成运行面：hist_matches（含一条缺口行）+ understat_matches（含异常 npxg）。"""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE hist_matches (
            competition TEXT, season TEXT, match_date TEXT,
            home_team TEXT, away_team TEXT,
            fthg INTEGER, ftag INTEGER, ftr TEXT,
            psc_home REAL, psc_draw REAL, psc_away REAL,
            psh_home REAL, psh_draw REAL, psh_away REAL,
            avgc_home REAL, avgc_draw REAL, avgc_away REAL
        );
        CREATE TABLE understat_matches (
            match_id TEXT PRIMARY KEY, league TEXT, season INTEGER,
            datetime_utc TEXT, home_team_id TEXT, away_team_id TEXT,
            goals_home INTEGER, goals_away INTEGER,
            npxg_home REAL, npxg_away REAL,
            forecast_w REAL, forecast_d REAL, forecast_l REAL
        );
        """
    )
    psc_by_day = {
        "2025-10-01": (1.40, 5.00, 8.00),  # 与锚价全等（偏差 0）
        "2025-10-02": (1.42, 5.05, 8.08),  # ~1% 内
        "2025-10-03": (1.39, 4.97, 7.95),  # ~0.7%
    }
    hist_rows = [
        (
            "E0",
            "2526",
            day,
            "HomeX",
            "AwayX",
            int(score[0]),
            int(score[2]),
            "H",
            *psc_by_day[day],
            None,
            None,
            None,
            None,
            None,
            None,
        )
        for (day, _sid, score) in DAYS
    ]
    # 缺口行：同日比分无语料对应（10-01 英超 3-3）
    hist_rows.append(
        (
            "E0",
            "2526",
            "2025-10-01",
            "HomeGap",
            "AwayGap",
            3,
            3,
            "D",
            3.0,
            3.0,
            2.0,
            None,
            None,
            None,
            None,
            None,
            None,
        )
    )
    conn.executemany(
        "INSERT INTO hist_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        hist_rows,
    )
    npxg_by_day = {
        "2025-10-01": (0.71, 1.68),  # 全等
        "2025-10-02": (0.72, 1.70),  # 微差
        "2025-10-03": (1.90, 0.60),  # 异常（|差|>1.0 ×2）
    }
    under_rows = [
        (
            f"u{i}",
            "epl",
            2025,
            f"{day}T12:00:00",
            "h1",
            "a1",
            int(score[0]),
            int(score[2]),
            *npxg_by_day[day],
            None,
            None,
            None,
        )
        for i, (day, _sid, score) in enumerate(DAYS)
    ]
    conn.executemany(
        "INSERT INTO understat_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        under_rows,
    )
    conn.commit()
    conn.close()


def _build_corpus(tmp_path: Path) -> tuple[CorpusStore, Settings]:
    routes: dict[str, httpx.Response] = {}
    for day, sid, score in DAYS:
        label = f"{int(day[-2:])}日20:00"
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(sid, label, score)
        )
        routes[f"odds:{sid}"] = httpx.Response(200, content=ODDS_JS)
        routes[f"ah:{sid}"] = httpx.Response(200, content=ASIANODDS_BYTES)
        routes[f"ou:{sid}"] = httpx.Response(200, content=OVERDOWN_BYTES)
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_BYTES)
        routes[f"ay:{sid}"] = httpx.Response(200, content=ANALYSIS_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_HTML)
    settings = _settings(tmp_path)
    _seed_running_face(settings.db_path)
    store = CorpusStore(settings.corpus_root)
    for day, _sid, _score in DAYS:
        srct.collect_day(
            store, settings, _client(routes), date=day, sleeper=lambda _s: None
        )
        store.set_day_status(day, "done")
    srct_silver.build_fixture_universe(store)
    srct_silver.build_xg_observations(store)
    srct_odds.build_bookmakers(store)
    srct_odds.build_odds_change_events(store)
    corpus_duckdb.build_corpus_duckdb(store)
    return store, settings


def test_match_fixtures_ambiguity_and_nearby_window() -> None:
    """两步匹配：精确日唯一先配；同日撞车→ambiguous；窗外→gap；邻日单候选→配。"""
    fixtures = [
        FixtureRow("sid1", "英超", "2025-10-02", 1, 1),
        FixtureRow("sid2", "英超", "2025-10-03", 1, 1),
    ]
    hists = [
        HistRow("E0", "2025-10-02", "H", "A", 1, 1, None, None, None),
        HistRow("E0", "2025-10-05", "H", "B", 1, 1, None, None, None),
    ]
    matched = match_fixtures(fixtures, hists)
    # 步 1：10-02 精确日唯一候选直接配；步 2：10-05 窗外无候选
    assert len(matched.pairs) == 1
    assert matched.pairs[0][0].sid == "sid1"
    assert len(matched.ambiguous) == 0
    assert len(matched.gaps) == 1
    assert len(matched.extra_fixtures) == 1

    single = match_fixtures(
        [FixtureRow("sid1", "英超", "2025-10-01", 2, 0)],
        [HistRow("E0", "2025-10-02", "H", "A", 2, 0, None, None, None)],
    )
    assert len(single.pairs) == 1  # 精确日缺、邻日单候选仍可配（日期基准差吸收）
    assert single.rate == 1.0

    # 同日同比分撞车：精确日多候选、邻日无新增 → ambiguous 不硬配
    collide = match_fixtures(
        [
            FixtureRow("sid1", "英超", "2025-10-02", 1, 1),
            FixtureRow("sid2", "英超", "2025-10-02", 1, 1),
        ],
        [HistRow("E0", "2025-10-02", "H", "A", 1, 1, None, None, None)],
    )
    assert collide.pairs == []
    assert len(collide.ambiguous) == 1
    assert len(collide.extra_fixtures) == 2

    # 交错语义既知权衡（correctness-review 2026-10-07 钉死）：乱序输入下
    # 漂移行（04-10 先处理）±1 窗见两候选 → 歧义，且已遮蔽 04-09 的精确
    # 日配对。真树输入按日期定序（SQL ORDER BY）无此忧，且十年实测交错
    # 比两遍结构净多 528 对——语义终裁见 _score_date_round docstring。
    interleaved = match_fixtures(
        [
            FixtureRow("f1", "英超", "2025-04-09", 1, 0),
            FixtureRow("f2", "英超", "2025-04-11", 1, 0),
        ],
        [
            HistRow("E0", "2025-04-10", "H", "A", 1, 0, None, None, None),
            HistRow("E0", "2025-04-09", "H", "B", 1, 0, None, None, None),
        ],
    )
    assert len(interleaved.pairs) == 1  # 04-09 精确配 f1；04-10 漂移行歧义
    assert len(interleaved.ambiguous) == 1
    # 同输入按日期定序（真树口径）：漂移行后判，两行全配
    ordered = match_fixtures(
        [
            FixtureRow("f1", "英超", "2025-04-09", 1, 0),
            FixtureRow("f2", "英超", "2025-04-11", 1, 0),
        ],
        [
            HistRow("E0", "2025-04-09", "H", "B", 1, 0, None, None, None),
            HistRow("E0", "2025-04-10", "H", "A", 1, 0, None, None, None),
        ],
    )
    assert len(ordered.pairs) == 2
    assert ordered.ambiguous == []


def test_match_fixtures_name_round_and_scope() -> None:
    """v3 名字身份轮：学映射解同比分撞车、±3 吸收改期漂移；口径外单列。"""
    fixtures = [
        FixtureRow("f1", "德甲", "2025-04-02", 2, 1, home="甲", away="乙"),
        FixtureRow("f2", "德甲", "2025-04-09", 1, 0, home="甲", away="乙"),
        FixtureRow("f3", "德甲", "2025-04-16", 3, 2, home="丙", away="丁"),
        FixtureRow("f4", "德甲", "2025-04-23", 0, 0, home="丙", away="丁"),
        FixtureRow("f5", "德甲", "2025-04-16", 3, 2, home="戊", away="己"),
        FixtureRow("f6", "德甲", "2025-04-30", 1, 1, home="己", away="戊"),
        FixtureRow("f7", "德甲", "2025-04-08", 4, 4, home="甲", away="乙"),
        FixtureRow("f8", "德甲", "2025-05-07", 2, 2, home="丙", away="丁"),
    ]
    hists = [
        # 甲/乙 两票学映射（不同日不同比分，各精确日唯一）
        HistRow("D1", "2025-04-02", "TeamA", "TeamB", 2, 1, None, None, None),
        HistRow("D1", "2025-04-09", "TeamA", "TeamB", 1, 0, None, None, None),
        # 丙/丁：04-16 与戊/己同比分撞车（学习轮不投票），04-23/05-07 补票
        HistRow("D1", "2025-04-16", "TeamC", "TeamD", 3, 2, None, None, None),
        HistRow("D1", "2025-04-23", "TeamC", "TeamD", 0, 0, None, None, None),
        HistRow("D1", "2025-05-07", "TeamC", "TeamD", 2, 2, None, None, None),
        # 戊/己：一票撞车一票反向 → 无映射，走比分+日期兜底
        HistRow("D1", "2025-04-16", "TeamE", "TeamF", 3, 2, None, None, None),
        HistRow("D1", "2025-04-30", "TeamF", "TeamE", 1, 1, None, None, None),
        # 改期漂移 3 天（±1 窗外）——名字轮吸收
        HistRow("D1", "2025-04-05", "TeamA", "TeamB", 4, 4, None, None, None),
        # 比甲 playoff 期：CorpusScope 口径外单列
        HistRow("B1", "2025-05-10", "TeamS", "TeamT", 1, 1, None, None, None),
    ]
    matched = match_fixtures(fixtures, hists)
    assert len(matched.pairs) == 8  # 全配齐（含撞车与漂移）
    assert matched.matched_via_name == 6  # 甲乙×3 + 丙丁×3；戊己走兜底轮
    assert matched.name_links_learned == 4  # A→甲 B→乙 C→丙 D→丁
    assert matched.ambiguous == []
    assert matched.gaps == []
    assert len(matched.scope_excluded) == 1
    assert matched.scope_excluded[0].home == "TeamS"
    assert matched.rate == 1.0
    assert matched.extra_fixtures == []


def _assert_gate1_v3(gate1: dict[str, Any]) -> None:
    """门① v3 断言（名字轮/映射/口径外/缺口归因）。"""
    assert gate1["matched"] == 3
    assert gate1["matched_via_name"] == 3  # 名字身份轮全量配走
    assert gate1["name_links_learned"] == 2  # HomeX→主队甲 / AwayX→客队乙
    assert gate1["gaps"] == 1  # 3-3 缺口行
    assert gate1["rate"] == 0.75  # 3/(3+1)
    assert gate1["verdict"] == "fail"  # 低于 99% 线——机制如实判
    assert gate1["gap_attribution"][0]["match"] == "HomeGap vs AwayGap"
    assert gate1["per_competition"]["英超"]["fdhist"] == 4
    assert gate1["scope_excluded"] == 0


def _assert_gate2_v2(gate2: dict[str, Any]) -> None:
    """门② v2 断言（中位数裁决/赛季分层/异常清单）。"""
    assert gate2["usable"] == 3
    assert gate2["mean_relative_deviation"] is not None
    assert gate2["mean_relative_deviation"] < 0.01
    assert gate2["median_relative_deviation"] is not None
    assert gate2["median_relative_deviation"] < 0.01
    assert gate2["verdict_metric"] == "median"
    assert len(gate2["per_season"]) == 1  # 单赛季分层可见
    assert gate2["outliers"] == []
    assert gate2["verdict"] == "pass"


def test_gate5_all_template_miss_is_red_not_empty(tmp_path: Path) -> None:
    """全量模板外=站点再改版红灯（空≠无）：verdict fail 而非 insufficient 跳过。"""
    store = CorpusStore(tmp_path)
    try:
        store.ensure_tree()
        store.ingest_raw(
            srct.SRCT_PROVIDER, srct.ASIANODDS_DATASET, "a", b"<html>redesign</html>"
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER, srct.ASIANODDS_DATASET, "b", b"<html>redesign</html>"
        )
        gate5 = corpus_gate.gate5_pipeline_health(
            store, GATE_TODAY, settled={"2025-10-03"}
        )
        asian = gate5["per_dataset_key_coverage"][srct.ASIANODDS_DATASET]
        assert asian["raw_files"] == 2
        assert asian["parser_template_files"] == 0
        assert asian["key_coverage"] == 0.0  # 红不是 None
        assert gate5["worst_key_coverage"] == 0.0
        assert gate5["verdict"] == "fail"
    finally:
        store.close()


def test_gate5_v3_multi_marker_old_template_pages(tmp_path: Path) -> None:
    """门⑤ 多标记（v3 分发起）：两代模板页都入解析分母，仅再改版页单列。"""
    store = CorpusStore(tmp_path)
    try:
        store.ensure_tree()
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.DETAIL_DATASET,
            "91001",
            "<html><body>新模板页 现场分析</body></html>".encode(),
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.DETAIL_DATASET,
            "91002",
            "<html><head><title>甲VS乙 详细事件</title></head><body></body>"
            "</html>".encode(),
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ANALYSIS_DATASET,
            "91003",
            b"<html><body><script>var h_data = [[]];</script></body></html>",
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.DETAIL_DATASET,
            "91004",
            "<html><body>再改版页</body></html>".encode(),
        )
        gate5 = corpus_gate.gate5_pipeline_health(
            store, GATE_TODAY, settled={"2025-10-03"}
        )
        detail = gate5["per_dataset_key_coverage"][srct.DETAIL_DATASET]
        assert detail["raw_files"] == 3
        assert detail["parser_template_files"] == 2  # 新+旧模板页同入分母
        assert detail["template_mismatch"] == 1  # 仅再改版页单列
        analysis = gate5["per_dataset_key_coverage"][srct.ANALYSIS_DATASET]
        assert analysis["parser_template_files"] == 1  # 旧模板 var h_data 命中
        assert analysis["template_mismatch"] == 0
    finally:
        store.close()


def _assert_gate5_template(gate5: dict[str, Any]) -> None:
    """门⑤断言（模板分母修正/单列/韧性/进度面）。"""
    assert gate5["worst_key_coverage"] == 1.0
    assert gate5["verdict"] == "pass"
    assert gate5["template_mismatch_total"] == 1  # 手植旧模板页单列
    assert gate5["pseudo_200_total"] == 1  # 手植伪 200 页单列
    assert gate5["unreadable_total"] == 1  # 手植撕裂 gzip 单列（.part 不计）
    asian = gate5["per_dataset_key_coverage"][srct.ASIANODDS_DATASET]
    assert asian["raw_files"] == 6  # 3 有效 + 旧模板 + 伪 200 + 撕裂（.part 跳过）
    assert asian["parser_template_files"] == 3
    assert asian["template_mismatch"] == 1
    assert asian["pseudo_200"] == 1
    assert asian["unreadable"] == 1
    assert asian["key_coverage"] == 1.0  # 分母剔除后覆盖满格
    assert gate5["phase1_progress"]["done_dates"] == 3
    assert gate5["phase1_progress"]["progress"] < 1.0


def test_five_gates_report_on_synthetic_tree(tmp_path: Path) -> None:
    store, settings = _build_corpus(tmp_path)
    try:
        # 模板外 raw 页 ×3：旧模板（无标记）、伪 200（404 图）、撕裂 gzip
        # ——都出分母单列，单页坏不炸报告（correctness-review 回归）
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ASIANODDS_DATASET,
            "99000",
            "<html><body>旧模板无标记页</body></html>".encode(),
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ASIANODDS_DATASET,
            "99001",
            b"<html><body><img src='error_404.gif'></body></html>",
        )
        raw_dir = store.raw_dir(srct.SRCT_PROVIDER, srct.ASIANODDS_DATASET)
        (raw_dir / "99002.html.gz").write_bytes(b"\x1f\x8b truncated garbage")
        (raw_dir / "99003.html.gz.part").write_bytes(b"\x1f\x8b leftover temp")
        report = corpus_gate.build_phase1_gate_report(store, settings, today=GATE_TODAY)
    finally:
        store.close()
    gates = report["gates"]
    _assert_gate1_v3(gates["1_fixture_reconciliation"])
    _assert_gate2_v2(gates["2_psc_cid177"])

    gate3 = gates["3_xg_understat"]
    assert gate3["pairs"] == 3
    assert gate3["mae"] is not None
    assert gate3["mae"] > 0
    assert gate3["pearson_r"] is not None
    assert len(gate3["anomalies"]) == 2  # 10-03 主客双侧
    assert gate3["verdict"] == "report_only"

    gate4 = gates["4_conversion"]
    assert gate4["unexplained_gap"] == 0
    assert gate4["verdict"] == "pass"

    gate5 = gates["5_pipeline_health"]
    _assert_gate5_template(gate5)

    assert report["overall"] == "provisional"  # 进度未完不放行
    assert report["coverage"]["matrix"]["英超"]

    reports = store.root / corpus_gate.REPORTS_DIR
    json_path = reports / f"{corpus_gate.REPORT_BASENAME}.json"
    md_path = reports / f"{corpus_gate.REPORT_BASENAME}.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["gates"]
    markdown = md_path.read_text(encoding="utf-8")
    assert "门① 场次对账" in markdown
    assert "HomeGap vs AwayGap" in markdown  # 缺口归因可见


def test_cli_srct_gate_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, settings = _build_corpus(tmp_path)
    store.close()
    _cmd_srct_gate(argparse.Namespace(), settings=settings)
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["gates"]) == {
        "1_fixture_reconciliation",
        "2_psc_cid177",
        "3_xg_understat",
        "4_conversion",
        "5_pipeline_health",
    }
    assert payload["overall"] == "provisional"
    assert payload["md_path"].endswith("phase1-gate.md")
