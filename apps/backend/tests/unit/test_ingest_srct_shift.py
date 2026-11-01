"""源T 当期班测试（票 65）：入口/时间解析纯函数 + 四类拍闭环接缝。

样本为 2026-09-26 实测形态裁剪（BaSID var 值与 MatchTime 串结构照源，
sid 用合成值；联赛名/队名用源页字面量——官方侧与联赛名非书商敏感词）。
端点模板一律 `.test` 占位域（代称红线同 test_ingest_srct）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_shift
from goalx_backend.rate_limit import default_limiter

BEIJING = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（同 test_ingest_srct 先例；防封语义不在此测）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


# BaSID 实测形态：多 var，足球表=逗号串（含空段/重复；合成 sid）
SAMPLE_BASID = b'var Ba_Soccer="3001111,3002222,3003333,3002222,,";var Ba_Basket="999";'


def _odds_js(sid: str, league: str, match_time: str) -> bytes:
    """1x2d 形态样本（meta + game/gameDetail 数组，结构照实测裁剪）。"""
    return (
        '\ufeffvar matchname_cn="' + league + '";\n'
        'var MatchTime="' + match_time + '";\n'
        "var ScheduleID=" + sid + ";\n"
        'var hometeam_cn="\u4e3b\u961f";\nvar guestteam_cn="\u5ba2\u961f";\n'
        'game=Array("1129|146644870|Lottery Official|3.27|3.4|1.89|27.09|26.05|'
        "46.86|88.57|2.95|3.18|2.1|30.01|27.84|42.15|88.52|0.85|0.85|0.93|"
        '2025,10-1,18,10,28,00|");\n'
        'gameDetail=Array("145998374^3.35|3.65|2.19|10-18 19:12|'
        '0.97|0.98|0.97|2025;");\n'
    ).encode()


def _multi_book_page(title_marker: str) -> bytes:
    """亚盘/大小多庄页最小样本（13 格行结构照实测；书商名=代称）。"""
    row = (
        "<tr align=center><td><input type=checkbox></td>"
        "<td>\u4ee3\u79f0\u4e66\u5546</td><td>\u76d81</td>"
        "<td>0.93</td><td>半球</td><td>0.85</td>"
        "<td>0.90</td><td>半球</td><td>0.88</td>"
        "<td>0.95</td><td>半球</td><td>0.83</td>"
        "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td></tr>"
    )
    html = (
        f"<html><head><title>{title_marker}</title></head><body>{title_marker}"
        f"<table>{row}</table></body></html>"
    )
    return html.encode()


# 三场剧本：英超远场（每日拍面）、英超当晚场（临场拍面）、非 scope 场
SID_FAR, SID_NEAR, SID_OUT = "3001111", "3002222", "3003333"
# 开球（UTC）：远场 3 天后；当晚场 21:00 京；非 scope 次日
_KICKOFF_FAR = datetime(2026, 9, 29, 11, 30, tzinfo=UTC)  # 19:30 京
_KICKOFF_NEAR = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)  # 21:00 京
_KICKOFF_OUT = datetime(2026, 9, 27, 18, 0, tzinfo=UTC)

_ODDS_ROUTES: dict[str, bytes] = {
    SID_FAR: _odds_js(SID_FAR, "英超", "2026,09-1,29,11,30,00"),
    SID_NEAR: _odds_js(SID_NEAR, "英超", "2026,09-1,26,13,00,00"),
    SID_OUT: _odds_js(SID_OUT, "墨西联", "2026,09-1,27,18,00,00"),
}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_basid_url="https://srct.test/basid/BaSID.js",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}.aspx",
        srct_overdown_url="https://srct.test/overdown/{sid}.aspx",
    )


def _client(
    odds_routes: dict[str, bytes] | None = None,
    *,
    fail_odds: set[str] | None = None,
) -> httpx.Client:
    """MockTransport：入口 + 三端点路径族；fail_odds 指定场 500。"""
    routes = dict(_ODDS_ROUTES if odds_routes is None else odds_routes)
    fail = fail_odds or set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/basid/BaSID.js":
            return httpx.Response(200, content=SAMPLE_BASID)
        if path.startswith("/odds/"):
            sid = path.removeprefix("/odds/").removesuffix(".js")
            if sid in fail:
                return httpx.Response(500, text="boom")
            if sid in routes:
                return httpx.Response(200, content=routes[sid])
        if path.startswith("/asian/"):
            return httpx.Response(200, content=_multi_book_page("亚指指数"))
        if path.startswith("/overdown/"):
            return httpx.Response(200, content=_multi_book_page("大小指数"))
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run(
    tmp_path: Path,
    client: httpx.Client,
    now_utc: datetime,
    **kwargs: Any,
) -> tuple[srct_shift.ShiftStats, CorpusStore]:
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("jitter", None)
    kwargs.setdefault("rate_limiter", default_limiter())
    settings = kwargs.pop("settings", None) or _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    stats = srct_shift.run_shift(
        store, settings, client, now_fn=lambda: now_utc, **kwargs
    )
    return stats, store


# —— 纯函数接缝 ————————————————————————————————————————————————


def test_parse_basid_order_dedup() -> None:
    assert srct_shift.parse_basid(SAMPLE_BASID) == [
        SID_FAR,
        SID_NEAR,
        SID_OUT,
    ]


def test_parse_basid_missing_var() -> None:
    assert srct_shift.parse_basid(b'var Ba_Basket="9";') == []


def test_parse_match_time_js_date_args() -> None:
    # 'MM-1'=0-based 月编码 → 9 月；UTC 口径（与日页标签差 8h 实证）
    got = srct_shift.parse_match_time("2026,09-1,26,03,00,00")
    assert got == datetime(2026, 9, 26, 3, 0, tzinfo=UTC)


def test_parse_match_time_garbage() -> None:
    assert srct_shift.parse_match_time("") is None
    assert srct_shift.parse_match_time(None) is None
    assert srct_shift.parse_match_time("2026,13-1,26,03,00,00") is None
    assert srct_shift.parse_match_time("2026,09-1,32,03,00,00") is None


def test_closing_lead_early_vs_late() -> None:
    # 凌晨/早场（北京午前开球）12h；晚场 2h
    early = datetime(2026, 9, 27, 1, 30, tzinfo=UTC)  # 09:30 京
    late = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)  # 21:00 京
    assert srct_shift.closing_lead(early) == timedelta(hours=12)
    assert srct_shift.closing_lead(late) == timedelta(hours=2)


# —— 四类拍闭环接缝 ————————————————————————————————————————


def test_first_run_open_and_daily_beats(tmp_path: Path) -> None:
    # 10:30 京：三场全开售拍；两场 scope 场再各吃一拍 am daily
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, store = _run(tmp_path, _client(), now_utc)
    assert stats.stopped is None
    assert stats.discovered == 3
    assert stats.open_beats == 3
    assert stats.daily_beats == 2  # 远场+当晚场（am 槽）；非 scope 不拍
    assert stats.requests == 1 + 3 + 2
    rows = store.shift_matches()
    assert rows[SID_FAR]["in_scope"] == 1
    assert rows[SID_FAR]["league"] == "英超"
    assert rows[SID_OUT]["in_scope"] == 0
    assert rows[SID_NEAR]["kickoff_utc"] == _KICKOFF_NEAR.isoformat()
    # 开售拍落 raw（拍键，全部场）；bronze 只进 scope 场（非 scope=罗丙类不进语料面）
    assert store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, f"{SID_FAR}@open")
    assert store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, f"{SID_OUT}@open")
    bronze = store.read_bronze(srct.SRCT_PROVIDER, srct.ODDS_DATASET)
    assert {str(r["sid"]) for r in bronze} == {SID_FAR, SID_NEAR}


def test_rerun_idempotent_zero_refetch(tmp_path: Path) -> None:
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    _run(tmp_path, _client(), now_utc)
    stats, _ = _run(tmp_path, _client(), now_utc)
    assert stats.requests == 1  # 只剩入口清单
    assert (stats.open_beats, stats.daily_beats, stats.close_beats) == (0, 0, 0)


def test_closing_beats_three_endpoints(tmp_path: Path) -> None:
    # 首跑建档（10:30 京）→ 19:30 京（开球前 1.5h ≤ 2h 窗）临场拍三端点
    _run(tmp_path, _client(), datetime(2026, 9, 26, 2, 30, tzinfo=UTC))
    now_utc = datetime(2026, 9, 26, 11, 30, tzinfo=UTC)  # 19:30 京
    stats, store = _run(tmp_path, _client(), now_utc)
    assert stats.close_beats == 3  # 当晚场三端点；远场不在临场窗
    assert stats.daily_beats == 0  # 19:30 非 daily 槽
    assert store.has(
        srct.SRCT_PROVIDER, srct.ASIANODDS_DATASET, f"{SID_NEAR}@close-asian_odds"
    )
    assert store.has(
        srct.SRCT_PROVIDER, srct.OVERDOWN_DATASET, f"{SID_NEAR}@close-over_down"
    )
    ah_bronze = store.read_bronze(srct.SRCT_PROVIDER, srct.ASIANODDS_DATASET)
    assert {str(r["sid"]) for r in ah_bronze} == {SID_NEAR}
    assert ah_bronze[0]["payload"]["books"][0]["cid"] == "8"
    # 再跑同窗：零重抓
    stats2, _ = _run(tmp_path, _client(), now_utc + timedelta(minutes=10))
    assert stats2.requests == 1
    assert stats2.close_beats == 0


def test_budget_stops_mid_run(tmp_path: Path) -> None:
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _ = _run(
        tmp_path, _client(), now_utc, budget=srct.NightBudget(request_cap=3)
    )
    assert stats.stopped == "budget"
    # 入口 + 首场开售拍发出；第 3 次 charge 触顶不发（夜班同语义）
    assert stats.requests == 2
    assert stats.open_beats == 1


def test_night_window_closed(tmp_path: Path) -> None:
    now_utc = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)  # 26 日 02:00 京
    stats, _ = _run(tmp_path, _client(), now_utc)
    assert stats.stopped == "window_closed"
    assert stats.requests == 0


def test_final_collected_skip(tmp_path: Path) -> None:
    # 夜班已收口（裸 sid raw 在）→ 整场跳过不建档
    store = CorpusStore(_settings(tmp_path).corpus_root)
    store.ingest_raw(
        srct.SRCT_PROVIDER, srct.ODDS_DATASET, SID_FAR, b"collected", ext=".js"
    )
    store.close()
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, store = _run(tmp_path, _client(), now_utc)
    assert stats.final_collected == 1
    assert SID_FAR not in store.shift_matches()
    assert stats.open_beats == 2  # 其余两场照常


def test_kicked_off_match_no_beats(tmp_path: Path) -> None:
    # 已开球场：建档行在但开球已过 → 零拍（收口归夜班）
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    _run(tmp_path, _client(), now_utc)
    later = _KICKOFF_NEAR + timedelta(minutes=30)
    stats, _ = _run(tmp_path, _client(), later)
    assert stats.requests == 1
    assert stats.close_beats == 0


def test_failed_open_beat_retries_next_run(tmp_path: Path) -> None:
    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _ = _run(tmp_path, _client(fail_odds={SID_FAR}), now_utc)
    assert stats.open_beats == 2
    assert f"{SID_FAR}@open" in stats.failed
    # 坏拍不落键：下轮同场重试成功
    stats2, store = _run(tmp_path, _client(), now_utc + timedelta(minutes=30))
    assert stats2.open_beats == 1
    assert store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, f"{SID_FAR}@open")


# —— 票 03 缺账：BaSID 200 有体但清单变量消失（反爬变形候选） ——
def test_basid_body_without_soccer_var_face_accounted(tmp_path: Path) -> None:
    """清单空：缺账进 failed['_entry']，行为不变（零 sid 走完、JC 相位照常）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/basid/BaSID.js":
            # 有体但足球清单变量被反爬变形吃掉
            return httpx.Response(200, content=b'var Ba_Basket="999";')
        return httpx.Response(200, content=_multi_book_page("换页"))

    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _store = _run(
        tmp_path, httpx.Client(transport=httpx.MockTransport(handler)), now_utc
    )
    assert stats.discovered == 0
    assert "BaSID 有响应但无清单变量" in stats.failed["_entry"]
    assert stats.stopped is None  # 不早退：照常走完


def test_basid_legal_empty_on_sale_not_face_miss(tmp_path: Path) -> None:
    """correctness F2：var Ba_Soccer="" 合法零在售——不算缺账（无 failed）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/basid/BaSID.js":
            return httpx.Response(200, content=b'var Ba_Soccer="";var Ba_Basket="999";')
        return httpx.Response(200, content=_multi_book_page("换页"))

    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _store = _run(
        tmp_path, httpx.Client(transport=httpx.MockTransport(handler)), now_utc
    )
    assert stats.discovered == 0
    assert "_entry" not in stats.failed  # 合法零在售不算缺账（JC 相位 mock 噪声另计）


# —— 票 05 发现面补缺：bfdata 编号直连 ——
# bfdata 行实测形态裁剪（字段位随行漂移，编号/开球参数串正则非位提取）
BFDATA_JS = (
    'var A=Array(2);var matchdate="09\u670826\u65e5";'
    'A[1]="4004444^#FF6699^\u8377\u7532^\u8377\u7532^^\u683c\u7f57\u5b81\u6839^\u9ad8\u5be7\u6839^^'
    "\u9e7f\u7279\u4e39\u65af\u5df4\u8fbe^\u9e7f\u7279\u4e39\u65af\u5df4\u9054^^20:00^2026,8,26,20,00,00^"
    "0^0^0^^^0^0^0^0^8^14^1^2^^^True^0.75^^^^^^^^^09-26^236^273^^8^0^^2026^1^16^3.25^^"
    '\u5468\u516d105^295^0^0^1^1^1^^^^0";'
    'A[2]="4005555^#006665^\u8461\u8d85^\u8461\u8d85^^\u7ef4\u585e\u4e4c^\u7ef4\u585e\u70cf^^'
    "\u91cc\u5965\u963f\u7ef4^\u91cc\u5965\u963f\u7ef4^^21:00^2026,8,26,21,00,00^"
    '0^0^0^^^^0^0^^^True^0.5^^^^^^^09-26^1805^476^^6^0^^2026^1^23^\u5468\u516d106^14^322^0^0^1^1^0^^^^0"'
).encode()


def test_parse_bfdata_code_join_projection() -> None:
    coded = srct_shift.parse_bfdata(BFDATA_JS)
    # 编号直连：code → (sid, 北京 HH:MM)；JS 日期参数月 0-based（08=9月）
    assert coded["周六105"] == srct_shift.BfGapRow(sid="4004444", hhmm="20:00")
    assert coded["周六106"] == srct_shift.BfGapRow(sid="4005555", hhmm="21:00")


def _seed_jc_row(store: CorpusStore, *, code: str, kickoff_utc: str) -> None:
    store.upsert_jc_shift_match(
        {
            "match_id": "2041" + code[-3:],
            "league": "荷甲",
            "home": "格罗宁根",
            "away": "鹿特丹斯巴达",
            "kickoff_utc": kickoff_utc,
            "beats": 0,
            "finalized": 0,
            "code": code,
        }
    )


def test_gap_fill_discovers_sid_not_in_basid(tmp_path: Path) -> None:
    """JC 在售等源T → bfdata 编号直连补 sid → 并入主班建档（开球 HH:MM 门）。"""
    settings = _settings(tmp_path).model_copy(
        update={"srct_bfdata_url": "https://srct.test/bfdata_ut.js"}
    )
    store = CorpusStore(settings.corpus_root)
    _seed_jc_row(
        store, code="周六105", kickoff_utc="2026-09-26T12:00:00+00:00"
    )  # 北京 20:00
    store.close()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/basid/BaSID.js":
            return httpx.Response(200, content=SAMPLE_BASID)
        if path == "/bfdata_ut.js":
            return httpx.Response(200, content=BFDATA_JS)
        if path.startswith("/odds/"):
            sid = path.removeprefix("/odds/").removesuffix(".js")
            if sid == "4004444":
                return httpx.Response(
                    200, content=_odds_js("4004444", "荷甲", "2026,8,26,12,00,00")
                )
            return httpx.Response(200, content=_ODDS_ROUTES.get(sid, b""))
        if path.startswith(("/asian/", "/overdown/")):
            return httpx.Response(200, content=_multi_book_page("指数页"))
        return httpx.Response(500, text="no-route")

    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, store = _run(
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        now_utc,
        settings=settings,
    )
    assert stats.gap_waiting == 1
    assert stats.gap_filled == 1
    assert "4004444" in store.shift_matches()  # 建档落表（复用主班链路）


def test_gap_fill_time_mismatch_skips(tmp_path: Path) -> None:
    """编号相同开球不齐（改期/回收残行）：宁缺勿错不补。"""
    settings = _settings(tmp_path).model_copy(
        update={"srct_bfdata_url": "https://srct.test/bfdata_ut.js"}
    )
    store = CorpusStore(settings.corpus_root)
    _seed_jc_row(
        store, code="周六105", kickoff_utc="2026-09-26T13:00:00+00:00"
    )  # 北京 21:00 ≠ bf 20:00
    store.close()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/basid/BaSID.js":
            return httpx.Response(200, content=SAMPLE_BASID)
        if request.url.path == "/bfdata_ut.js":
            return httpx.Response(200, content=BFDATA_JS)
        return httpx.Response(200, content=_multi_book_page("换页"))

    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _ = _run(
        tmp_path,
        httpx.Client(transport=httpx.MockTransport(handler)),
        now_utc,
        settings=settings,
    )
    assert stats.gap_filled == 0


def test_gap_fill_no_waiting_no_fetch(tmp_path: Path) -> None:
    """无等源T 场：bfdata 零请求（与 _entry 同口径的空耗纪律）。"""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        fetched.append(path)
        if path == "/basid/BaSID.js":
            return httpx.Response(200, content=SAMPLE_BASID)
        if path == "/bfdata_ut.js":
            return httpx.Response(200, content=BFDATA_JS)
        return httpx.Response(200, content=_multi_book_page("换页"))

    now_utc = datetime(2026, 9, 26, 2, 30, tzinfo=UTC)
    stats, _ = _run(
        tmp_path, httpx.Client(transport=httpx.MockTransport(handler)), now_utc
    )
    assert stats.gap_waiting == 0
    assert "/bfdata_ut.js" not in fetched


def test_jc_code_column_migration(tmp_path: Path) -> None:
    """票 05 迁移幂等：老 checkpoint 库（jc 表无 code 列）重开自补列。"""
    import sqlite3

    root = tmp_path / "corpus"
    store = CorpusStore(root)
    store.ensure_tree()  # 建库（checkpoint 表惰性建，先显式落树）
    store.close()
    conn = sqlite3.connect(root / "checkpoint.db")
    conn.execute("DROP TABLE jc_shift_matches")
    conn.execute(
        """
        CREATE TABLE jc_shift_matches (
            match_id TEXT PRIMARY KEY, league TEXT NOT NULL DEFAULT '',
            home TEXT NOT NULL DEFAULT '', away TEXT NOT NULL DEFAULT '',
            kickoff_utc TEXT, first_seen_at TEXT NOT NULL, last_seen_at TEXT,
            beats INTEGER NOT NULL DEFAULT 0, finalized INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()
    reopened = CorpusStore(root)  # 重开：_create_checkpoint_tables 幂等补列
    _seed_jc_row(reopened, code="周六105", kickoff_utc="2026-09-26T12:00:00+00:00")
    rows = reopened.jc_shift_matches()
    assert rows["2041105"]["code"] == "周六105"  # type: ignore[index]
    reopened.close()
