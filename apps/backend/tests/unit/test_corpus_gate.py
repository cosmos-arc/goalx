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
HANDICAP_BYTES = (
    "<html><head><title>亚赔变化表</title></head><body><table>"
    "<TR align=center><TD>0.80</TD><TD>平手/半球</TD><TD>1.05</TD>"
    "<TD>10-01 19:30</TD><TD>即</TD></TR></table></body></html>"
).encode("gb18030")
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
        routes[f"hdp:{sid}"] = httpx.Response(200, content=HANDICAP_BYTES)
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
    """±1 窗跨日同比分 → ambiguous 不硬配；窗外 → gap。"""
    fixtures = [
        FixtureRow("sid1", "英超", "2025-10-02", 1, 1),
        FixtureRow("sid2", "英超", "2025-10-03", 1, 1),
    ]
    hists = [
        HistRow("E0", "2025-10-02", "H", "A", 1, 1, None, None, None),
        HistRow("E0", "2025-10-05", "H", "B", 1, 1, None, None, None),
    ]
    matched = match_fixtures(fixtures, hists)
    assert matched.pairs == []
    assert len(matched.ambiguous) == 1  # 两候选不硬配
    assert len(matched.gaps) == 1  # 10-05 窗外无候选
    assert len(matched.extra_fixtures) == 2

    single = match_fixtures(
        [FixtureRow("sid1", "英超", "2025-10-01", 2, 0)],
        [HistRow("E0", "2025-10-02", "H", "A", 2, 0, None, None, None)],
    )
    assert len(single.pairs) == 1  # 邻日仍可配（日期基准差吸收）
    assert single.rate == 1.0


def test_five_gates_report_on_synthetic_tree(tmp_path: Path) -> None:
    store, settings = _build_corpus(tmp_path)
    try:
        report = corpus_gate.build_phase1_gate_report(store, settings, today=GATE_TODAY)
    finally:
        store.close()
    gates = report["gates"]
    gate1 = gates["1_fixture_reconciliation"]
    assert gate1["matched"] == 3
    assert gate1["gaps"] == 1  # 3-3 缺口行
    assert gate1["rate"] == 0.75  # 3/(3+1)
    assert gate1["verdict"] == "fail"  # 低于 99% 线——机制如实判
    assert gate1["gap_attribution"][0]["match"] == "HomeGap vs AwayGap"
    assert gate1["per_competition"]["英超"]["fdhist"] == 4

    gate2 = gates["2_psc_cid177"]
    assert gate2["usable"] == 3
    assert gate2["mean_relative_deviation"] is not None
    assert gate2["mean_relative_deviation"] < 0.01
    assert gate2["verdict"] == "pass"

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
    assert gate5["worst_key_coverage"] == 1.0
    assert gate5["verdict"] == "pass"
    assert gate5["phase1_progress"]["done_dates"] == 3
    assert gate5["phase1_progress"]["progress"] < 1.0

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
