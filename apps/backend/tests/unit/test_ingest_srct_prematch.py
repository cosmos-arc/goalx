"""源T 赛前拍点测试（autoresearch 票 14）：拍窗纯函数 + 快照落库闭环接缝。

样本为 detail 页实测形态裁剪（同 test_ingest_srct 的 v2 新模板；赛前页=
页题标记在而阵容区空）。端点模板一律 `.test` 占位域（代称红线同源）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_prematch
from goalx_backend.rate_limit import default_limiter

KICKOFF = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)  # 北京 11-11 03:00
SID = "4012345"


def _at(minutes_to_kickoff: int) -> datetime:
    return KICKOFF - timedelta(minutes=minutes_to_kickoff)


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（同 test_ingest_srct_shift 先例；防封语义不在此测）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


# —— detail 页样本（v2 新模板形态裁剪；赛前页=阵容区空/齐两种） ——

_PREMATCH_HEAD = (
    "<html><head><title>主队甲 VS 客队乙-现场分析-新球体育</title></head><body>"
    "<script>var homeTeamName = '主队甲';var guestTeamName = '客队乙';"
    "var strTime = '2026-11-11 03:00';</script>"
    "<div>VS 场地： 测试球场 天气：多云 温度：15℃</div>"
)

_PLAY = (
    "<div class='play' onmouseover=\"setImgUrl({pid})\">"
    '<span><em class="num">{num} </em>'
    "<div class='name'><a>{name}</a></div></span></div>"
)


def _detail_page(*, starters_per_side: int = 0) -> bytes:
    """赛前 detail 页：页题标记在；starters_per_side=0 → 预计阵容空态。

    赛前页实测形态（2026-11-10 真树两场 −15.4h 探针，票 14 调研报告）：
    阵容区块标题=「预计阵容」、球员区「暂无数据」——官方公布后才换
    「首发阵容」；现解析器按「首发阵容」锚取阵容，赛前页自然解析为
    lineup 全空（首次公开时刻读数的"空"态），预计区原文随 raw 存档。
    """
    if starters_per_side == 0:
        predicted = (
            '<div class="title"> 预计阵容 <div class="homeN"><a>主队甲</a>'
            "<a class='coach'>(主教练: 教练甲)</a></div>"
            "<div class=\"guestN\"><a class='coach'>(主教练: 教练乙)</a>"
            "<a>客队乙</a></div></div>"
            "<div>替补</div><div>暂无数据</div><div>伤停</div><div>暂无数据</div>"
        )
        return (_PREMATCH_HEAD + predicted + "</body></html>").encode()
    plays = '<div class="title"> 首发阵容 </div><div class="plays"><div class="home">'
    plays += "".join(
        _PLAY.format(pid=str(1000 + i), num=str(i + 1), name=f"主首{i}")
        for i in range(starters_per_side)
    )
    plays += '</div><div class="guest">'
    plays += "".join(
        _PLAY.format(pid=str(2000 + i), num=str(i + 1), name=f"客首{i}")
        for i in range(starters_per_side)
    )
    plays += "</div></div>"
    return (_PREMATCH_HEAD + plays + "</body></html>").encode()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_detail_url="https://srct.test/analysis/{sid}.html",
    )


def _client(page: bytes = _detail_page()) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/analysis/"):
            return httpx.Response(200, content=page)
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run_beats(
    tmp_path: Path,
    client: httpx.Client,
    now: datetime,
    candidates: list[tuple[str, datetime]] | None = None,
    **kwargs: Any,
) -> tuple[srct_prematch.PrematchStats, CorpusStore]:
    settings = kwargs.pop("settings", None) or _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    stats = srct_prematch.run_prematch_beats(
        store,
        settings,
        client,
        candidates or [(SID, KICKOFF)],
        now=now,
        limiter=kwargs.pop("rate_limiter", default_limiter()),
        sleeper=lambda _s: None,
        **kwargs,
    )
    return stats, store


def _bronze_rows(store: CorpusStore, sid: str) -> list[dict[str, object]]:
    return [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, srct.DETAIL_DATASET)
        if str(row["sid"]) == sid
    ]


# —— 拍窗纯函数 ————————————————————————————————————————


def test_due_lead_window_membership() -> None:
    """四档拍窗 (n−30, n]：档内任一时刻恰命中本档；边界不双中。"""
    # −150 档窗 (120, 150]：边界与窗内各取样
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(150)) == 150
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(121)) == 150
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(90)) == 90
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(61)) == 90
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(60)) == 60
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(31)) == 60
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(30)) == 30
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(1)) == 30


def test_due_lead_gap_between_windows_is_none() -> None:
    """档间空档 (90, 120]：不拍（4 档离散拍点，非连续采样）。"""
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(120)) is None
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(105)) is None
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(91)) is None


def test_due_lead_outside_horizon_or_started_is_none() -> None:
    """开球已过 / 超首档窗：不拍（收口归夜班；远场归每日拍）。"""
    assert srct_prematch.due_prematch_lead(KICKOFF, KICKOFF) is None
    assert srct_prematch.due_prematch_lead(KICKOFF, KICKOFF + timedelta(1)) is None
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(151)) is None
    assert srct_prematch.due_prematch_lead(KICKOFF, _at(600)) is None


def test_prematch_key_shape() -> None:
    """拍键 `{sid}@pre-{n}`：与夜班裸键/既有拍键互不占位。"""
    assert srct_prematch.prematch_key("123456", 150) == "123456@pre-150"
    assert srct_prematch.prematch_key("123456", 30) != srct_prematch.prematch_key(
        "123456", 60
    )


# —— 快照落库闭环 ————————————————————————————————————————


def test_prematch_beat_lands_snapshot(tmp_path: Path) -> None:
    """−140 落 −150 档：raw 拍键 + bronze 行（sid 裸键、阵容空、PIT 时刻）。"""
    started = datetime.now(UTC)
    stats, store = _run_beats(tmp_path, _client(), _at(140))
    assert (stats.candidates, stats.due, stats.beats) == (1, 1, 1)
    assert stats.raw_new == 1
    assert stats.parsed_ok == 1
    assert stats.full_xi == 0  # 赛前页阵容空（首次公开读数的"空"态）
    key = srct_prematch.prematch_key(SID, 150)
    assert store.has(srct.SRCT_PROVIDER, srct.DETAIL_DATASET, key)
    rows = _bronze_rows(store, SID)
    assert len(rows) == 1
    row = rows[0]
    assert row["parser_version"] == srct.BRONZE_VERSIONS[srct.DETAIL_DATASET]
    assert row["raw_sha"] == store.raw_sha(srct.SRCT_PROVIDER, srct.DETAIL_DATASET, key)
    lineup = dict(row["payload"])["lineup"]  # type: ignore[arg-type]
    assert lineup["home_starters"] == []  # type: ignore[index]
    # PIT observed_at：抓取时刻（真实钟，秒精度），不是名义档时刻 kickoff−150
    observed = datetime.fromisoformat(str(row["fetched_at"]))
    assert observed.tzinfo is not None
    assert observed >= started.replace(microsecond=0)
    assert observed != _at(150)
    store.close()


def test_same_beat_twice_no_dup(tmp_path: Path) -> None:
    """append-only 幂等：同窗重跑零请求零重复行（拍键即台账）。"""
    now = _at(140)
    stats1, store = _run_beats(tmp_path, _client(), now)
    assert stats1.beats == 1
    stats2, _ = _run_beats(tmp_path, _client(), now + timedelta(minutes=5))
    assert stats2.requests == 0
    assert stats2.beats == 0
    assert len(_bronze_rows(store, SID)) == 1
    store.close()


def test_each_lead_lands_own_key(tmp_path: Path) -> None:
    """四档四键四行：逐档推进各落各拍（窗口空档轮零请求）。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    landed = 0
    for minutes, expect_beat in (
        (140, 150),
        (85, 90),
        (55, 60),
        (25, 30),
        (105, None),  # 档间空档不拍
    ):
        stats, store = _run_beats(
            tmp_path, _client(), _at(minutes), settings=_settings(tmp_path)
        )
        if expect_beat is None:
            assert stats.due == 0
            assert stats.requests == 0
            continue
        assert stats.beats == 1
        assert store.has(
            srct.SRCT_PROVIDER,
            srct.DETAIL_DATASET,
            srct_prematch.prematch_key(SID, expect_beat),
        )
        landed += 1
    assert landed == 4
    assert len(_bronze_rows(store, SID)) == 4
    store.close()


def test_full_xi_snapshot_counted(tmp_path: Path) -> None:
    """两侧 11 齐页：full_xi 计数（首次公开时刻读数的"满"态面）。"""
    stats, store = _run_beats(
        tmp_path, _client(_detail_page(starters_per_side=11)), _at(55)
    )
    assert stats.beats == 1
    assert stats.full_xi == 1
    lineup = dict(_bronze_rows(store, SID)[0]["payload"])[  # type: ignore[arg-type]
        "lineup"
    ]
    assert len(lineup["home_starters"]) == 11  # type: ignore[index]
    store.close()


def test_failed_beat_no_key_retries_next_round(tmp_path: Path) -> None:
    """失败拍不占键：下轮同窗自愈（拍键幂等语义同当期班）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    stats, store = _run_beats(
        tmp_path, httpx.Client(transport=httpx.MockTransport(handler)), _at(140)
    )
    assert stats.beats == 0
    assert srct_prematch.prematch_key(SID, 150) in stats.failed
    assert not store.has(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, srct_prematch.prematch_key(SID, 150)
    )
    stats2, store = _run_beats(tmp_path, _client(), _at(135))
    assert stats2.beats == 1
    store.close()


def test_pseudo_200_page_counts_failed_no_key(tmp_path: Path) -> None:
    """内容判别失败（伪 200 图）不占键不落行——真拍点窗口内可重试。"""
    bad = (_PREMATCH_HEAD + "error_404.gif" + "</body></html>").encode()

    stats, store = _run_beats(tmp_path, _client(bad), _at(140))
    assert stats.beats == 0
    assert stats.raw_new == 0
    assert not store.has(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, srct_prematch.prematch_key(SID, 150)
    )
    store.close()


def test_budget_stop_records_partial_progress(tmp_path: Path) -> None:
    """预算触顶不上抛（collect_day 先例）：stopped 落档、已落拍计数入摘要
    （correctness F1——部分进度丢失会让晨检/运维面对账失真）。"""
    other = "4098888"
    kickoff2 = KICKOFF + timedelta(minutes=5)
    stats, store = _run_beats(
        tmp_path,
        _client(),
        _at(140),
        candidates=[(SID, KICKOFF), (other, kickoff2)],
        budget=srct.NightBudget(
            request_cap=2
        ),  # 首拍 charge=1 落库，次拍 charge=2 触顶
    )
    assert stats.stopped == "budget"
    assert stats.beats == 1  # 首拍已落库并计入摘要
    assert stats.requests == 1  # 触顶那次未发出不计
    assert store.has(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, srct_prematch.prematch_key(SID, 150)
    )
    store.close()


def test_final_collected_and_kicked_off_skip(tmp_path: Path) -> None:
    """夜班已收口（裸键在）/已开球候选：零请求跳过。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    store.ingest_raw(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, SID, b"final", ext=".html"
    )
    store.close()
    stats, store = _run_beats(tmp_path, _client(), _at(140), settings=settings)
    assert stats.skipped_final == 1
    assert stats.requests == 0
    stats2, store2 = _run_beats(tmp_path, _client(), KICKOFF + timedelta(minutes=5))
    assert stats2.due == 0
    assert stats2.requests == 0
    store.close()
    store2.close()


# —— 落库计数面（晨检第 18 项的数源；LIKE 模式与 prematch_key 同源钉死） ———


def test_prematch_snapshot_face_counts(tmp_path: Path) -> None:
    """计数面：只认 @pre- 拍键（裸键/@close 等不进面）；recent 按水线切。"""
    import sqlite3

    stats, store = _run_beats(tmp_path, _client(), _at(140))
    assert stats.beats == 1
    store.close()
    store = CorpusStore(_settings(tmp_path).corpus_root)
    # 同数据集混入非拍键工件：裸键（夜班收口）与 @close 拍键不得进计数
    store.ingest_raw(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, SID, b"final", ext=".html"
    )
    store.ingest_raw(
        srct.SRCT_PROVIDER,
        srct.DETAIL_DATASET,
        f"{SID}@close-{srct.DETAIL_DATASET}",
        b"close",
        ext=".html",
    )
    # 一条老拍键回拨 fetched_at（验证 recent 水线切分）
    store.ingest_raw(
        srct.SRCT_PROVIDER,
        srct.DETAIL_DATASET,
        srct_prematch.prematch_key(SID, 90),
        b"older",
        ext=".html",
    )
    conn = sqlite3.connect(store.checkpoint_path)
    conn.execute(
        "UPDATE raw_artifacts SET fetched_at = '2026-11-01T00:00:00+00:00'"
        " WHERE key = ?",
        (srct_prematch.prematch_key(SID, 90),),
    )
    conn.commit()
    conn.close()
    face = store.prematch_snapshot_face(since_iso="2026-11-10T00:00:00+00:00")
    assert face["total"] == 2  # @pre-150 + @pre-90（裸键/@close 不计）
    assert face["recent"] == 1  # 只有 @pre-150 在水线后
    assert face["latest_fetched_at"] is not None
    assert face["latest_fetched_at"] > "2026-11-10T00:00:00"
    store.close()


def test_prematch_snapshot_face_empty_tree(tmp_path: Path) -> None:
    """空面如实零（未开始语义归晨检项）。"""
    store = CorpusStore(_settings(tmp_path).corpus_root)
    face = store.prematch_snapshot_face(since_iso="2026-11-10T00:00:00+00:00")
    assert face == {"total": 0, "recent": 0, "latest_fetched_at": None}
    store.close()


# —— 当期班相位集成（隔离红线：拍点失败不阻塞主链/JC 相位） —————————————————

_SHIFT_SETTINGS_KW = {
    "srct_basid_url": "https://srct.test/basid/BaSID.js",
    "srct_odds_url": "https://srct.test/odds/{sid}.js",
    "srct_odds_referer": "https://srct.test/oddslist/{sid}.htm",
}

_ODDS_JS = (
    '\ufeffvar matchname_cn="\u82f1\u8d85";\nvar MatchTime="2026,10-1,10,21,30,00";\n'
    "var ScheduleID=4099999;\n"
    'var hometeam_cn="\u4e3b\u961f";\nvar guestteam_cn="\u5ba2\u961f";\n'
    'game=Array("1129|146644870|Lottery Official|3.27|3.4|1.89'
    '|2025,10-1,18,10,28,00|");\n'
    "gameDetail=Array();\n"
).encode()


def _shift_client(
    *, detail_fails: bool = False, basid: bytes = b'var Ba_Soccer="4099999";'
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/basid/BaSID.js":
            return httpx.Response(200, content=basid)
        if path.startswith("/odds/"):
            return httpx.Response(200, content=_ODDS_JS)
        if path.startswith("/analysis/"):
            if detail_fails:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, content=_detail_page())
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _run_shift(
    tmp_path: Path,
    client: httpx.Client,
    now: datetime,
    candidates: list[tuple[str, datetime]],
) -> tuple[Any, CorpusStore]:
    from goalx_backend.data.ingest import srct_shift

    settings = Settings(
        corpus_root=tmp_path / "corpus",
        srct_detail_url="https://srct.test/analysis/{sid}.html",
        **_SHIFT_SETTINGS_KW,
    )
    store = CorpusStore(settings.corpus_root)
    stats = srct_shift.run_shift(
        store,
        settings,
        client,
        now_fn=lambda: now,
        sleeper=lambda _s: None,
        jitter=None,
        rate_limiter=default_limiter(),
        prematch_candidates=candidates,
    )
    return stats, store


def test_run_shift_phase_lands_prematch_beat(tmp_path: Path) -> None:
    """相位挂接：主班循环后、JC 相位前，赛前拍共用预算/滑窗落拍。"""
    stats, store = _run_shift(tmp_path, _shift_client(), _at(140), [(SID, KICKOFF)])
    assert stats.stopped is None
    assert stats.open_beats == 1  # 主班开售拍照常（BaSID 一场）
    assert stats.prematch_candidates == 1
    assert stats.prematch_beats == 1
    assert store.has(
        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, srct_prematch.prematch_key(SID, 150)
    )
    store.close()


def test_run_shift_prematch_failure_isolated(tmp_path: Path) -> None:
    """红线：赛前拍 500 → 只留痕 failed，主班开售拍已落、JC 相位照常走到。"""
    stats, store = _run_shift(
        tmp_path, _shift_client(detail_fails=True), _at(140), [(SID, KICKOFF)]
    )
    assert stats.stopped is None
    assert stats.open_beats == 1  # 主链不受阻
    assert stats.prematch_beats == 0
    assert srct_prematch.prematch_key(SID, 150) in stats.failed
    assert "jc:jc:discovery" in stats.failed  # JC 相位照常执行（未配置降级留痕）
    store.close()


def test_run_shift_prematch_budget_stop(tmp_path: Path) -> None:
    """预算触顶在赛前拍相位：整班收手留痕 + 已落拍计数并入摘要（F1）；
    夜窗外零候选相位移交。"""
    from goalx_backend.data.ingest import srct as _srct
    from goalx_backend.data.ingest import srct_shift

    settings = Settings(
        corpus_root=tmp_path / "corpus",
        srct_detail_url="https://srct.test/analysis/{sid}.html",
        **_SHIFT_SETTINGS_KW,
    )
    store = CorpusStore(settings.corpus_root)
    stats = srct_shift.run_shift(
        store,
        settings,
        _shift_client(),
        now_fn=lambda: _at(140),
        sleeper=lambda _s: None,
        jitter=None,
        rate_limiter=default_limiter(),
        budget=_srct.NightBudget(request_cap=4),  # 入口+开售拍+首档拍=4，次场触顶
        prematch_candidates=[
            (SID, KICKOFF),
            ("4098888", KICKOFF + timedelta(minutes=5)),
        ],
    )
    assert stats.stopped == "budget"
    assert stats.prematch_beats == 1  # 部分进度入摘要（correctness F1）
    assert stats.raw_new == 2  # 开售拍 + 赛前首拍
    store.close()


# —— 候选解析（主库归属函数拼装）+ 任务装配 ————————————————————————


def test_jc_prematch_candidates_resolves_linked_in_window(
    db: sqlite3.Connection,
) -> None:
    """[now, now+150min] 已链场上榜；未链/窗外/已开球排除。"""
    from goalx_backend.data import fixtures as fx_store
    from goalx_backend.models import MatchCodeInput, Tier

    now = datetime(2026, 11, 10, 17, 0, tzinfo=UTC)
    bd = fx_store.beijing_business_date(now)
    comp = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "主队甲")
    away = fx_store.upsert_team(db, "客队乙")
    kickoff_iso = (now + timedelta(minutes=100)).isoformat(timespec="seconds")
    linked = fx_store.upsert_fixture(db, comp, kickoff_iso, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=linked, kind="jingcai", business_date=bd, code="周二001"
        ),
    )
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
        " method, status, mapped_at) VALUES (?, 'srct', '5001111', 'primary_key',"
        " 'linked', ?)",
        (linked, now.isoformat(timespec="seconds")),
    )
    # 未链近场：排除（无 sid 可拍）
    unlinked = fx_store.upsert_fixture(
        db,
        comp,
        (now + timedelta(minutes=90)).isoformat(timespec="seconds"),
        home,
        away,
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=unlinked, kind="jingcai", business_date=bd, code="周二002"
        ),
    )
    # 窗外远场（>150min）：排除
    far = fx_store.upsert_fixture(
        db,
        comp,
        (now + timedelta(minutes=200)).isoformat(timespec="seconds"),
        home,
        away,
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=far, kind="jingcai", business_date=bd, code="周三001"
        ),
    )
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
        " method, status, mapped_at) VALUES (?, 'srct', '5002222', 'primary_key',"
        " 'linked', ?)",
        (far, now.isoformat(timespec="seconds")),
    )
    # 已开球场：排除（from=now）
    played = fx_store.upsert_fixture(
        db,
        comp,
        (now - timedelta(minutes=30)).isoformat(timespec="seconds"),
        home,
        away,
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=played,
            kind="jingcai",
            business_date=fx_store.beijing_business_date(now - timedelta(days=1)),
            code="周一001",
        ),
    )
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
        " method, status, mapped_at) VALUES (?, 'srct', '5003333', 'primary_key',"
        " 'linked', ?)",
        (played, now.isoformat(timespec="seconds")),
    )
    db.commit()
    got = srct_prematch.jc_prematch_candidates(db, now)
    assert got == [("5001111", datetime.fromisoformat(kickoff_iso))]


def test_srct_shift_run_wires_prematch_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """任务装配：主库解析候选注入 run_shift（同一 now 钟面）。"""
    from goalx_backend import tasks
    from goalx_backend.data.ingest import srct_shift

    captured: dict[str, object] = {}
    stub_now: list[datetime] = []

    def fake_run_shift(
        _store: object, _settings: object, _client: object, **kwargs: object
    ) -> srct_shift.ShiftStats:
        captured.update(kwargs)
        return srct_shift.ShiftStats()

    monkeypatch.setattr(tasks.srct_shift, "run_shift", fake_run_shift)
    monkeypatch.setattr(tasks, "mapping_sync", lambda: {"ok": True})
    monkeypatch.setattr(tasks, "srct_remat_run", lambda: {"ok": True})

    def fake_candidates(_conn: object, now: datetime) -> list[tuple[str, datetime]]:
        stub_now.append(now)
        return [("5001111", now + timedelta(minutes=100))]

    monkeypatch.setattr(tasks.srct_prematch, "jc_prematch_candidates", fake_candidates)
    settings = Settings(
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
        research_db_path=tmp_path / "research.db",
    )
    payload = tasks.srct_shift_run(no_window=True, settings=settings, client=_client())
    assert stub_now, "候选解析未执行"
    assert captured["prematch_candidates"] == [
        ("5001111", stub_now[0] + timedelta(minutes=100))
    ]
    assert "prematch_degraded" not in payload


def test_srct_shift_run_prematch_resolution_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主库读失败降级：空候选继续拍班，payload 留 degraded 注记。"""
    from goalx_backend import tasks
    from goalx_backend.data.ingest import srct_shift

    def fake_run_shift(
        _store: object, _settings: object, _client: object, **kwargs: object
    ) -> srct_shift.ShiftStats:
        assert kwargs["prematch_candidates"] == []
        return srct_shift.ShiftStats()

    monkeypatch.setattr(tasks.srct_shift, "run_shift", fake_run_shift)
    monkeypatch.setattr(tasks, "mapping_sync", lambda: {"ok": True})
    monkeypatch.setattr(tasks, "srct_remat_run", lambda: {"ok": True})

    def _boom(_conn: object, _now: datetime) -> list[tuple[str, datetime]]:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(tasks.srct_prematch, "jc_prematch_candidates", _boom)
    settings = Settings(
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
        research_db_path=tmp_path / "research.db",
    )
    payload = tasks.srct_shift_run(no_window=True, settings=settings, client=_client())
    assert "database is locked" in str(payload["prematch_degraded"])
