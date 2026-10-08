"""
Gold 特征面测试（backtest-decade B 相票 06，spec User Story 6-12）。

合成语料（MockTransport 攒 bronze → silver 四件套 → duckdb 桥）+ 合成
运行面（hist_matches/understat_matches/admin_match_exclusions 种子行）+
直写 elo_self silver，走真实 build_match_features 全链：era 分层、fdhist
配对与成熟度门、行政判赛标记、xG 一行一源、AH/OU 收盘族、轨迹收盘与
1h/24h 切片、PIT 断言、版本戳/幂等/digest 钉死。代称红线：cid/书名全假。
真源网络永不进测试。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest

from goalx_backend import db
from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, gold
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    elo_silver,
    srct,
    srct_market,
    srct_odds,
    srct_silver,
)
from goalx_backend.data.silver import write_dataset_meta, write_partition

# 报告今天：era2（2024-10）开球+6mo 远过，PSC 成熟度全放行
TODAY = date(2026, 10, 8)
EARLY_TODAY = date(2024, 11, 1)  # 仅 era2（+6mo=2025-04 > 今天）未成熟

# 三场：era1 ×2（其一行政判赛）+ era2 ×1（全轨迹时代）
ERA1_DAY, ERA1_SID, ERA1_SCORE = "2021-10-02", "80001", "2-1"
ERA1X_DAY, ERA1X_SID, ERA1X_SCORE = "2021-10-02", "80003", "0-3"
ERA2_DAY, ERA2_SID, ERA2_SCORE = "2024-10-01", "80002", "1-2"
ERA2_KICKOFF = datetime(2024, 10, 1, 20, 0)  # 北京墙钟（开球标签 1日20:00）
# era2b：同季次日、无 fdhist/understat 行——验 srct xG 补位与零配对留空
ERA2B_DAY, ERA2B_SID, ERA2B_SCORE = "2024-10-02", "80004", "3-0"

# 1x2 轨迹（era2）：锚 cid177 双书。24h 前切片价/盘中价/收盘价逐档不同；
# 20:00 恰开球行与 21:30 场内行必须被严格 < 开球滤掉。
_ODDS_ANCHOR_ROWS = (
    "1.60|6.20|9.40|09-30 18:00|0.90|0.90|0.90|2024;",
    "1.50|6.00|9.00|10-01 13:00|0.91|0.91|0.91|2024;",
    "1.45|5.80|8.80|10-01 19:30|0.92|0.90|0.90|2024;",
    "1.40|5.00|8.00|10-01 20:00|0.92|0.90|0.90|2024;",
    "1.30|5.50|8.50|10-01 21:30|0.93|0.97|0.84|2024;",
)
_ODDS_BOOK_ROWS = (
    "1.55|5.90|8.90|10-01 12:00|0.93|0.97|0.84|2024;",
    "1.47|5.90|8.90|10-01 19:00|0.93|0.97|0.84|2024;",
)


def _odds_js() -> bytes:
    game = (
        "177|600001|TestAnchor|1.60|6.20|9.40|90|16|11|92|1.45|5.80|8.80|93|16|12|91|"
        "0.90|0.90|0.90|2024,10-1,19,0,0,00|测试锚*|1|0|0.9|0.9|0.9",
        "9001|600002|TestSharp|1.55|5.90|8.90|93|17|12|94|1.47|5.90|8.90|94|17|11|95|"
        "0.93|0.97|0.84|2024,10-1,19,0,0,00|测试甲*|1|0|0.85|0.95|1.11",
    )
    detail = (
        "600001^" + "".join(_ODDS_ANCHOR_ROWS),
        "600002^" + "".join(_ODDS_BOOK_ROWS),
    )
    return (
        'var matchname_cn="英超";\ngame=Array(\n"'
        + '",\n"'.join(game)
        + '");\ngameDetail=Array(\n"'
        + '",\n"'.join(detail)
        + '");\n'
    ).encode()


# 亚盘/大小球多庄页（books=market_quote 源；changes 矩阵=相位与 AH 锚轨迹源）
def _book_row(
    cid: str,
    name: str,
    multi: str,
    open_q: tuple[str, str, str],
    latest_q: tuple[str, str, str],
    close_q: tuple[str, str, str],
    page: str,
) -> str:
    return (
        "<tr align=center><td><input type=checkbox></td>"
        f"<td>{name}</td><td>{multi}</td>"
        + "".join(
            f"<td>{q[i]}</td>" for q in (open_q, latest_q, close_q) for i in (0, 1, 2)
        )
        + f"<td><a href=/changeDetail/{page}.aspx?id=1&companyID={cid}>详</a></td></tr>"
    )


_MATRIX_COLS = (
    "甲*",
    "乙*",
    "丙*",
    "丁*",
    "戊*",
    "己*",
    "庚*",
    "辛*",
    "壬*",
    "癸*",
    "子*",
)


def _matrix_row(
    col: int, bg: str, line: str, waters: tuple[str, str], score: str, when: str
) -> str:
    cells = [
        f"<td style='background: {bg}'>{line}<br />"
        f"<span class='blue b'>{waters[0]}</span>&nbsp;"
        f"<span class='green'>{waters[1]}</span></td>"
        if i == col
        else "<td></td>"
        for i in range(11)
    ]
    return f"<tr align=center>{''.join(cells)}<td >{score}</td><td>{when}</td></tr>"


def _matrix_table(rows: str) -> str:
    header = (
        "<tr align=center class=thead2>"
        + "".join(f"<th width=80>{n}</th>" for n in _MATRIX_COLS)
        + "<th width=40>比分</th><th width=80>变化时间</th></tr>"
    )
    table = '<table cellspacing=1 cellpadding=0 class="font13 company">'
    return table + header + rows + "</table>"


def _asian_page() -> bytes:
    books = (
        _book_row(
            "1",
            "书商1 封",
            "",
            ("0.93", "受让半球", "0.93"),
            ("0.80", "平手", "1.02"),
            ("0.80", "受让平手/半球", "1.06"),
            "handicap",
        ),
        _book_row(
            "8",
            "书商8 封",
            "",
            ("0.90", "受让半球", "0.95"),
            ("2.65", "平手/半球", "0.27"),
            ("0.80", "受让平手/半球", "1.05"),
            "handicap",
        ),
        # 盘2 行：多盘档位——共识/锚只取盘1
        _book_row(
            "8",
            "",
            "盘2",
            ("1.10", "受让平手/半球", "0.70"),
            ("5.00", "平手/半球", "0.12"),
            ("1.20", "平手", "0.65"),
            "handicap",
        ),
    )
    # 矩阵（行序新→旧）：cid8 列=col2。场内/盘中两拍/24h 前早盘
    matrix = (
        _matrix_row(2, "#eaeaff", "受让半球", ("0.70", "1.20"), "1-2", "10-01 21:48")
        + _matrix_row(2, "#FFFFFF", "半球", ("0.85", "1.05"), "", "10-01 19:00")
        + _matrix_row(2, "#FFFFFF", "半球", ("0.90", "1.00"), "", "10-01 10:00")
        + _matrix_row(2, "#dcfbff", "半球", ("0.95", "0.95"), "", "09-30 18:00")
    )
    return (
        "<html><head><title>甲VS乙-亚指指数-新球体育</title></head><body><table>"
        + "".join(books)
        + "</table>"
        + _matrix_table(matrix)
        + "</body></html>"
    ).encode("utf-8")


def _overdown_page() -> bytes:
    books = (
        _book_row(
            "1",
            "书商1 封",
            "",
            ("0.93", "2.5/3", "0.87"),
            ("1.25", "2.5", "0.50"),
            ("0.80", "2.5", "1.00"),
            "overunder",
        ),
        _book_row(
            "3",
            "书商3 封",
            "",
            ("0.95", "2.5/3", "0.85"),
            ("1.10", "2.5", "0.72"),
            ("0.85", "2.5/3", "0.95"),
            "overunder",
        ),
    )
    matrix = _matrix_row(0, "#FFFFFF", "2.5", ("0.90", "1.00"), "", "10-01 18:00") + (
        _matrix_row(0, "#dcfbff", "2.5/3", ("0.95", "0.95"), "", "09-30 12:00")
    )
    return (
        "<html><head><title>甲VS乙-大小指数-新球体育</title></head><body><table>"
        + "".join(books)
        + "</table>"
        + _matrix_table(matrix)
        + "</body></html>"
    ).encode("utf-8")


DETAIL_BYTES = (
    "<html><head><title>甲VS乙-现场分析-新球体育</title></head><body>"
    "<ul><li class='lists'><div class='data'><span >3</span><span>角球</span>"
    "<span >3</span></div></li></ul>"
    "</body></html>"
).encode()
ANALYSIS_BYTES = (
    "<html><head><title>甲VS乙-数据分析-新球体育</title></head><body>"
    "<script>var h_data =[['24-05-10',36,'英超',52,'主队甲',60,'客队乙']];</script>"
    "</body></html>"
).encode()
STATS_HTML = (
    '<html><body><script>var jsonData = {"techStat":{"itemList":['
    '{"home":{"value":0.71},"away":{"value":1.68},"name":"预期进球",'
    '"kind":"EXPECTED_GOALS"}]},"info":{}};</script></body></html>'
).encode()


def _day_page(day: str, entries: list[tuple[str, str]]) -> bytes:
    """一日日页（label=当日 20:00；一比赛行一场——同日多场共存）。"""
    rows = []
    for sid, score in entries:
        home, away = (int(g) for g in score.split("-"))
        rows.append(
            "<tr height=18 align=center>"
            f"<td><span>英超</span></td><td>{int(day[-2:])}日20:00</td>"
            "<td class=style1>完</td>"
            "<td align=right>主队甲</td>"
            f"<td class=style1><font color=blue>{home}</font>"
            f"-<font color=red>{away}</font></td>"
            "<td align=left>客队乙</td>"
            f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
        )
    body = "".join(rows)
    return f"<html><body><table>{body}</table></body></html>".encode("gb18030")


FIXTURES: list[tuple[str, str, str]] = [
    (ERA1_DAY, ERA1_SID, ERA1_SCORE),
    (ERA2_DAY, ERA2_SID, ERA2_SCORE),
    (ERA1X_DAY, ERA1X_SID, ERA1X_SCORE),
    (ERA2B_DAY, ERA2B_SID, ERA2B_SCORE),
]


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


# fdhist 种子：era1 两行（其一入排除窗）+ era2 一行；tuple 序=列序（日/主/客/
# gh/ga/ftr/psc 三元组），psh 起的其余列 SQL 字面量同值批量填
_HIST_INSERT = """
    INSERT INTO hist_matches
        (competition, season, match_date, home_team, away_team, fthg, ftag, ftr,
         psc_home, psc_draw, psc_away, psh_home, psh_draw, psh_away,
         avgc_home, avgc_draw, avgc_away,
         avg_ou_over, avg_ou_under, avgc_ou_over, avgc_ou_under,
         ah_line, avg_ah_home, avg_ah_away, ahc_line, avgc_ah_home, avgc_ah_away,
         hthg, htag, htr, referee, shots_home, shots_away, shots_on_target_home,
         shots_on_target_away, corners_home, corners_away, fouls_home, fouls_away,
         yellow_home, yellow_away, red_home, red_away)
    VALUES ('E0', '2122', ?, ?, ?, ?, ?, ?, ?, ?, ?,
            2.05, 3.30, 3.10, 1.95, 3.55, 3.95,
            1.95, 1.85, 1.90, 1.80, -0.5, 1.95, 1.95, -0.25, 1.90, 1.92,
            1, 0, 'H', 'RefX', 12, 8, 5, 3, 6, 4, 11, 9, 2, 2, 0, 0)
"""
_HIST_SEED: list[tuple[str, str, str, int, int, str, float, float, float]] = [
    (ERA1_DAY, "HomeA", "AwayA", 2, 1, "H", 2.10, 3.40, 3.20),
    (ERA2_DAY, "HomeB", "AwayB", 1, 2, "A", 1.90, 3.60, 4.10),
    (ERA1X_DAY, "HomeC", "AwayC", 0, 3, "A", 8.50, 5.20, 1.30),
]


def _seed_running_face(db_path: Path) -> None:
    conn = db.connect(db_path)
    db.migrate(conn)
    conn.executemany(_HIST_INSERT, _HIST_SEED)
    # understat xG 主源行（英超=epl；era2 场 1-2）——与 srct xG 并存验主源优先
    conn.execute(
        """
        INSERT INTO understat_matches
            (match_id, league, season, datetime_utc, home_team_id, home_team,
             away_team_id, away_team, goals_home, goals_away,
             xg_home, xg_away, first_seen_at, observed_at)
        VALUES ('u1', 'epl', '2024', '2024-10-01T12:00:00', 'h1', 'H1',
                'a1', 'A1', 1, 2, 0.75, 1.60, '2024-10-02', '2024-10-02')
        """
    )
    # 行政判赛排除窗：era1b 场（比分是真的、比赛是假的）
    conn.execute(
        """
        INSERT INTO admin_match_exclusions
            (competition, team, date_start, date_end, reason, created_at)
        VALUES ('E0', 'HomeC', '2021-10-01', '2021-10-03',
                '测试排除窗', '2026-10-08T00:00:00')
        """
    )
    conn.commit()
    conn.close()


def _seed_elo_silver(store: CorpusStore) -> None:
    """直写 elo_self silver（上游契约面；era2 一行测 sid 直连）。"""
    rows = [
        {
            "sid": ERA2_SID,
            "league": "英超",
            "kickoff": ERA2_KICKOFF,
            "home": "主队甲",
            "away": "客队乙",
            "elo_home_pre": 1500.0,
            "elo_away_pre": 1450.0,
            "version": "test",
        }
    ]
    root = store.silver_path(elo_silver.PROVIDER, elo_silver.DATASET)
    write_partition(root / "all", rows, elo_silver._SCHEMA)
    write_dataset_meta(root, {"silver_version": "test", "rows": len(rows)})


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


Built = tuple[CorpusStore, Settings, gold.GoldBuildReport]


def _build_gold(
    store: CorpusStore, settings: Settings, *, today: date
) -> gold.GoldBuildReport:
    duck_con = corpus_duckdb.connect(settings)
    face = db.connect(settings.db_path)
    try:
        return gold.build_match_features(store, face, duck_con, today=today)
    finally:
        face.close()
        duck_con.close()


@pytest.fixture
def built(tmp_path: Path) -> Built:
    """全链：MockTransport 采集 → silver 四件套 → duckdb 桥 → gold 构建。"""
    routes: dict[str, httpx.Response] = {}
    by_day: dict[str, list[tuple[str, str]]] = {}
    for day, sid, score in FIXTURES:
        by_day.setdefault(day, []).append((sid, score))
        routes[f"odds:{sid}"] = httpx.Response(200, content=_odds_js())
        routes[f"ah:{sid}"] = httpx.Response(200, content=_asian_page())
        routes[f"ou:{sid}"] = httpx.Response(200, content=_overdown_page())
        routes[f"dt:{sid}"] = httpx.Response(200, content=DETAIL_BYTES)
        routes[f"ay:{sid}"] = httpx.Response(200, content=ANALYSIS_BYTES)
        routes[f"stats:{sid}"] = httpx.Response(200, content=STATS_HTML)
    for day, entries in by_day.items():
        routes[f"day:{day.replace('-', '')}"] = httpx.Response(
            200, content=_day_page(day, entries)
        )
    settings = _settings(tmp_path)
    _seed_running_face(settings.db_path)
    store = CorpusStore(settings.corpus_root)
    for day in by_day:
        srct.collect_day(
            store, settings, _client(routes), date=day, sleeper=lambda _s: None
        )
    srct_silver.build_fixture_universe(store)
    srct_silver.build_xg_observations(store)
    srct_odds.build_bookmakers(store)
    srct_odds.build_odds_change_events(store)
    srct_market.build_market_quotes(store)
    srct_market.build_detail_faces(store)
    srct_market.build_analysis_faces(store)
    _seed_elo_silver(store)
    corpus_duckdb.build_corpus_duckdb(store)
    report = _build_gold(store, settings, today=TODAY)
    corpus_duckdb.build_corpus_duckdb(store)  # gold 视图注册（CLI 同序）
    return store, settings, report


def _gold_rows(settings: Settings) -> dict[str, dict[str, Any]]:
    con = corpus_duckdb.connect(settings)
    try:
        result = con.execute("SELECT * FROM match_features")
        names = [d[0] for d in result.description]
        rows = result.fetchall()
    finally:
        con.close()
    return {str(r[names.index("sid")]): dict(zip(names, r, strict=True)) for r in rows}


def test_era_layering_and_report(built: Built) -> None:
    """era 字段=正典声明；报告 era 计数与覆盖面计数如实。"""
    _store, settings, report = built
    rows = _gold_rows(settings)
    assert set(rows) == {ERA1_SID, ERA2_SID, ERA1X_SID, ERA2B_SID}
    assert rows[ERA1_SID]["era"] == gold.ERA_PSC_PROXY
    assert rows[ERA1X_SID]["era"] == gold.ERA_PSC_PROXY
    assert rows[ERA2_SID]["era"] == gold.ERA_TRAJECTORY
    assert report.era_psc_proxy_rows == 2
    assert report.era_trajectory_rows == 2
    assert report.rows == 4
    assert report.fd_paired == 2  # era1 + era2（era1b 排除、era2b 无 hist 行）
    assert report.admin_flagged == 1
    assert report.xg_understat == 1
    assert report.xg_srct == 1  # era2b 走 srct 补位；era1 两场季门内留空
    assert report.elo_rows == 1
    assert report.close1x2_rows == 2
    assert report.market_ah_rows == 4
    assert report.market_ou_rows == 4
    assert report.pit_rows == 4  # 双锚切片全时代（AH 锚轨迹 2016 起在档）
    assert report.face_inputs == {
        "hist_matches_rows": 3,
        "understat_rows": 1,
        "admin_exclusions": 4,
    }


def test_fdhist_pairing_maturity_and_admin(built: Built) -> None:
    """fdhist 配对特征：成熟放行/行政判赛整族不消费/未成熟整族置空。"""
    store, settings, report = built
    rows = _gold_rows(settings)
    era1 = rows[ERA1_SID]
    assert era1["psc_home"] == pytest.approx(2.10)
    assert era1["avgc_away"] == pytest.approx(3.95)
    assert era1["fd_avg_ou_over"] == pytest.approx(1.95)
    assert era1["fd_ah_line"] == pytest.approx(-0.5)
    assert era1["fd_shots_home"] == 12  # 标签侧技统不过成熟度门
    assert era1["fd_htr"] == "H"
    assert era1["fd_referee"] == "RefX"
    # era2b 无 hist 行：fd 族全空（空≠无，行仍在）
    assert rows[ERA2B_SID]["psc_home"] is None
    assert rows[ERA2B_SID]["fd_shots_home"] is None
    # era2 也配到 fdhist（并存；era 字段声明轨迹族为正典）
    assert rows[ERA2_SID]["psc_home"] == pytest.approx(1.90)
    # 行政判赛：标记 + 整族不消费（odds 族与技统都不取）
    banned = rows[ERA1X_SID]
    assert banned["admin_excluded"] is True
    assert banned["psc_home"] is None
    assert banned["fd_avg_ou_over"] is None
    assert banned["fd_shots_home"] is None
    assert rows[ERA1_SID]["admin_excluded"] is False
    assert report.fd_immature == 0

    # 未成熟重放（仅 era2 未成熟）：era2 odds 族置空、技统保留；era1 照旧
    early = _build_gold(store, settings, today=EARLY_TODAY)
    assert early.fd_immature == 1
    early_rows = _gold_rows(settings)
    assert early_rows[ERA2_SID]["psc_home"] is None
    assert early_rows[ERA2_SID]["fd_avg_ou_over"] is None
    assert early_rows[ERA2_SID]["fd_shots_home"] == 12
    assert early_rows[ERA1_SID]["psc_home"] == pytest.approx(2.10)


def test_xg_source_discipline(built: Built) -> None:
    """xG 一行一源：understat 主源优先（配对胜出），srct 补位。"""
    _store, settings, _report = built
    rows = _gold_rows(settings)
    assert rows[ERA2_SID]["xg_source"] == "understat"
    assert rows[ERA2_SID]["xg_home"] == pytest.approx(0.75)
    assert rows[ERA2_SID]["xg_away"] == pytest.approx(1.60)
    # era1（2021）在源T stats 季门（2024/25+）外：诚实留空不补
    assert rows[ERA1_SID]["xg_source"] is None
    assert rows[ERA1_SID]["xg_home"] is None
    # era2b 无 understat 行 → srct 补位（一行一源）
    assert rows[ERA2B_SID]["xg_source"] == "srct"
    assert rows[ERA2B_SID]["xg_home"] == pytest.approx(0.71)
    assert rows[ERA1_SID]["elo_home_pre"] is None  # elo 只种了 era2 行
    assert rows[ERA2_SID]["elo_home_pre"] == pytest.approx(1500.0)


def test_trajectory_close_and_slices(built: Built) -> None:
    """轨迹收盘=严格 < 开球的末价；1h/24h 切片=时点末价；PIT 时间戳断言面。"""
    _store, settings, _report = built
    row = _gold_rows(settings)[ERA2_SID]
    # 收盘锚：19:30 行（20:00 恰开球与 21:30 场内行都被滤掉）
    assert row["close1x2_h"] == pytest.approx(1.45)
    assert row["close1x2_d"] == pytest.approx(5.80)
    assert row["close1x2_a"] == pytest.approx(8.80)
    assert row["close1x2_books"] == 2
    # 共识 = 双书中位数 1/odds 归一
    med_h, med_d, med_a = 1.46, 5.85, 8.85
    inv = 1 / med_h + 1 / med_d + 1 / med_a
    assert row["close1x2_cons_h"] == pytest.approx((1 / med_h) / inv)
    assert row["close1x2_cons_d"] == pytest.approx((1 / med_d) / inv)
    assert row["close1x2_cons_a"] == pytest.approx((1 / med_a) / inv)
    # 1h/24h 切片（1x2 锚）：≤19:00 末价 = 13:00 行；≤09-30 20:00 = 09-30 行
    assert row["q1h1x2_h"] == pytest.approx(1.50)
    assert row["q24h1x2_h"] == pytest.approx(1.60)
    assert row["q24h1x2_a"] == pytest.approx(9.40)
    # AH 锚切片来自多庄页矩阵 cid8 列（半球=0.5）
    assert row["q1hah_line"] == pytest.approx(0.5)
    assert row["q1hah_home_water"] == pytest.approx(0.85)
    assert row["q24hah_line"] == pytest.approx(0.5)
    assert row["q24hah_home_water"] == pytest.approx(0.95)
    # PIT：所用最大 published_at = 收盘 19:30（北京）< 开球 20:00
    assert row["pit_max_ms"] == srct_odds.beijing_ms(datetime(2024, 10, 1, 19, 30))
    # era1（2021）：1x2 轨迹不存在（切片为空），AH 锚切片全时代在档。
    # 开球 10-02 20:00 → 1h 界=10-02 19:00/24h 界=10-01 20:00——切片纪律
    # 纯时间 PIT（页内 status 不参与）：矩阵四行时间全在 1h 窗内，末价=
    # 10-01 21:48 行（受让半球 -0.5）；24h 末价=19:00 行（半球）
    era1 = _gold_rows(settings)[ERA1_SID]
    assert era1["q1h1x2_h"] is None
    assert era1["q1hah_line"] == pytest.approx(-0.5)
    assert era1["q1hah_home_water"] == pytest.approx(0.70)
    assert era1["q24hah_line"] == pytest.approx(0.5)
    assert era1["q24hah_home_water"] == pytest.approx(0.85)
    assert era1["pit_max_ms"] == srct_odds.beijing_ms(datetime(2021, 10, 1, 21, 48))


def test_market_family_and_phases(built: Built) -> None:
    """AH/OU 收盘族：盘1 共识中位数/漂移/分歧度/cid8 锚；相位计数。"""
    _store, settings, _report = built
    row = _gold_rows(settings)[ERA2_SID]
    # 亚盘：盘1 两书（盘2 不进共识）——线 -0.5→-0.25（受让半球→受让平手/半球）
    assert row["ah_n_books"] == 2
    assert row["ah_open_line_med"] == pytest.approx(-0.5)
    assert row["ah_close_line_med"] == pytest.approx(-0.25)
    assert row["ah_line_drift"] == pytest.approx(0.25)
    assert row["ah_close_dispersion"] == pytest.approx(0.0)
    assert row["ah_close_home_water_med"] == pytest.approx(0.80)
    assert row["ah_close_away_water_med"] == pytest.approx((1.06 + 1.05) / 2)
    assert row["ah_anchor_open_line"] == pytest.approx(-0.5)
    assert row["ah_anchor_close_line"] == pytest.approx(-0.25)
    assert row["ah_anchor_drift"] == pytest.approx(0.25)
    assert row["ah_anchor_close_home_water"] == pytest.approx(0.80)
    # 大小球：两书 close 线 2.5 与 2.75 → 中位 2.625；open 2.75 → 漂移 -0.125
    assert row["ou_n_books"] == 2
    assert row["ou_close_line_med"] == pytest.approx(2.625)
    assert row["ou_open_line_med"] == pytest.approx(2.75)
    assert row["ou_line_drift"] == pytest.approx(-0.125)
    # 相位计数：AH 矩阵 cid8 列=1 早盘+2 盘前+1 场内；OU=1 早盘+1 盘前
    assert row["ah_n_early"] == 1
    assert row["ah_n_pre"] == 2
    assert row["ah_n_inplay"] == 1
    assert row["ou_n_early"] == 1
    assert row["ou_n_pre"] == 1
    assert row["ou_n_inplay"] == 0


def test_version_stamp_idempotent_and_digest(built: Built) -> None:
    """_meta 版本戳+输入 digest 钉死；同输入重建 parquet 字节级一致。"""
    store, settings, first = built
    root = store.gold_path(gold.GOLD_PROVIDER, gold.GOLD_DATASET)
    meta = json.loads((root / "_meta.json").read_text())
    assert meta["gold_version"] == gold.GOLD_VERSION
    assert meta["rows"] == 4
    assert meta["maturity_today"] == TODAY.isoformat()
    assert meta["face_inputs"] == {
        "hist_matches_rows": 3,
        "understat_rows": 1,
        "admin_exclusions": 4,
    }
    assert set(meta["input_digests"]) == {
        "srct/fixture_universe",
        "srct/market_quote",
        "srct/odds_change_event",
        "srct/xg_observation",
        "elo/elo_self",
    }
    assert all(len(v) == 16 for v in meta["input_digests"].values())

    before = (root / "all" / "data.parquet").read_bytes()
    second = _build_gold(store, settings, today=TODAY)
    assert (root / "all" / "data.parquet").read_bytes() == before
    assert second.input_digests == first.input_digests

    def strip(r: gold.GoldBuildReport) -> dict[str, Any]:
        return {k: v for k, v in asdict(r).items() if k != "built_at"}

    assert strip(second) == strip(first)
    # 版本列随行落盘
    table = pq.read_table(root / "all" / "data.parquet")
    assert set(table.column("version").to_pylist()) == {gold.GOLD_VERSION}
    assert set(table.column("era").to_pylist()) == {
        gold.ERA_PSC_PROXY,
        gold.ERA_TRAJECTORY,
    }


def test_pit_assertion_fires() -> None:
    """无前视断言：pit ≥ 开球（含恰等）即构建失败；None 行放行。"""
    kickoff = srct_odds.beijing_ms(ERA2_KICKOFF)
    gold._assert_no_lookahead([(kickoff, {"sid": "s", "pit_max_ms": kickoff - 1})])
    gold._assert_no_lookahead([(kickoff, {"sid": "s", "pit_max_ms": None})])
    for bad_ms in (kickoff, kickoff + 1):
        with pytest.raises(AssertionError, match="PIT violation"):
            gold._assert_no_lookahead([(kickoff, {"sid": "s", "pit_max_ms": bad_ms})])


def test_empty_corpus_degrades(tmp_path: Path) -> None:
    """空语料：降级报告 + _meta 仍落（建库即审计），不炸。"""
    settings = _settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    conn.close()
    store = CorpusStore(settings.corpus_root)
    srct_silver.build_fixture_universe(store)  # 空 bronze → 零 parquet → 无视图
    corpus_duckdb.build_corpus_duckdb(store)
    report = _build_gold(store, settings, today=TODAY)
    store.close()
    assert report.degraded is not None
    assert "fixture_universe" in report.degraded
    meta = json.loads(
        (
            store.gold_path(gold.GOLD_PROVIDER, gold.GOLD_DATASET) / "_meta.json"
        ).read_text()
    )
    assert meta["degraded"] == report.degraded


def test_cli_gold_build(built: Built, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI gold-build：migrate→gold 重建→duckdb 桥刷新（gold 视图可见）。"""
    import argparse

    from goalx_backend.cli import _cmd_gold_build

    store, settings, _report = built
    store.close()
    _cmd_gold_build(argparse.Namespace(), settings=settings)
    payload = json.loads(capsys.readouterr().out)
    assert payload["gold_version"] == gold.GOLD_VERSION
    assert payload["rows"] == 4
    assert payload["degraded"] is None
    con = corpus_duckdb.connect(settings)
    try:
        (n,) = con.execute("SELECT count(*) FROM match_features").fetchone()
    finally:
        con.close()
    assert n == 4
