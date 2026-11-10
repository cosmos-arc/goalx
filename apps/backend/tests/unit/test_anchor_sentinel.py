"""断点哨兵（票 34，设计稿=13-sentinel-design）：周级三轴对账合成反例单测。

覆盖设计稿 §七 8 条合成反例（断点当周触发/正常周不触发）+ 采集面窄化
（哨兵 sport 选择、pinnacle 服务端过滤、滚中行写侧防护）。srct 腿经
注入 reader（生产=srct_pinnacle_closing_triplet 同一语义，只读）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.fixtures import Tier
from goalx_backend.db import connect, migrate
from goalx_backend.evaluation import anchor_sentinel
from goalx_backend.models import SnapshotInput, SnapshotPurpose

CST_OFFSET = timedelta(hours=8)


def beijing_to_utc_iso(beijing: datetime) -> str:
    """北京墙钟 naive → UTC ISO 串（fixtures.kickoff_utc 形态）。"""
    return (beijing - CST_OFFSET).replace(tzinfo=UTC).isoformat()


def seed_jc_fixture(
    db: sqlite3.Connection,
    *,
    league: str = "英超",
    kickoff_utc: str,
    home: str = "主队",
    away: str = "客队",
    code: str,
    jc: bool = True,
) -> int:
    """一场（默认 FD 联赛）竞彩 fixture；jc=False 模拟非竞彩场。"""
    tier = Tier.TIER1 if league == "英超" else Tier.TIER2
    comp = fx_store.upsert_competition(db, league, tier=tier)
    home_id = fx_store.upsert_team(db, home)
    away_id = fx_store.upsert_team(db, away)
    fid = fx_store.upsert_fixture(db, comp, kickoff_utc, home_id, away_id)
    if jc:
        db.execute(
            "INSERT INTO match_codes (fixture_id, kind, business_date, code)"
            " VALUES (?, 'jingcai', ?, ?)",
            (fid, kickoff_utc[:10], code),
        )
    db.commit()
    return fid


def seed_triplet(
    db: sqlite3.Connection,
    fid: int,
    source: str,
    triplet: tuple[float, float, float],
    captured_at: str,
) -> None:
    """三向快照入库（had h/d/a）。"""
    for selection, odds in zip(("h", "d", "a"), triplet, strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fid,
                market_code="had",
                selection_code=selection,
                source=source,
                odds=odds,
                captured_at=captured_at,
                purpose=SnapshotPurpose.LIVE_CAPTURE,
            ),
        )
    db.commit()


# 设计稿 §四：配对键=JC fixture（FD 11 联赛 ∩ JC 宇宙），周=ISO 周（北京墙钟）。
def test_week_fixtures_scope_is_jc_fd11_and_beijing_week(
    db: sqlite3.Connection,
) -> None:
    monday = date.fromisocalendar(2026, 45, 1)
    in_week = beijing_to_utc_iso(datetime(monday.year, monday.month, monday.day, 20))
    late_sunday_utc = beijing_to_utc_iso(
        datetime.combine(monday + timedelta(days=6), datetime.min.time())
        + timedelta(hours=1)  # 周一 01:00 北京 = 周日 17:00 UTC → 属于第 45 周
    )
    next_monday = beijing_to_utc_iso(
        datetime.combine(monday + timedelta(days=7), datetime.min.time())
        + timedelta(hours=20)
    )
    fid_in = seed_jc_fixture(db, kickoff_utc=in_week, code="周六001")
    fid_late = seed_jc_fixture(
        db, kickoff_utc=late_sunday_utc, home="甲", away="乙", code="周一001"
    )
    seed_jc_fixture(db, league="欧冠", kickoff_utc=in_week, code="周六002")  # 非 FD 11
    seed_jc_fixture(db, kickoff_utc=in_week, code=None, jc=False)  # 非竞彩
    seed_jc_fixture(db, kickoff_utc=next_monday, code="周六003")  # 下周
    rows = anchor_sentinel.week_fixtures(db, 2026, 45)
    assert sorted(int(r["id"]) for r in rows) == sorted((fid_in, fid_late))


def reader_from_map(
    triplets: dict[int, tuple[float, float, float] | None],
) -> anchor_sentinel.SrctReader:
    """srct 腿注入：fid → 三元组（缺场=None，第一锚无资格收盘）。"""

    def read(row: sqlite3.Row) -> tuple[float, float, float] | None:
        return triplets.get(int(row["id"]))

    return read


def seed_week(
    db: sqlite3.Connection,
    *,
    n: int,
    srct: tuple[float, float, float] | None = (2.5, 3.3, 2.8),
    pl: tuple[float, float, float] | None = (2.5, 3.3, 2.8),
    iso_week: tuple[int, int] = (2026, 45),
) -> dict[int, tuple[float, float, float] | None]:
    """一周 n 场同价双源场；返回 srct reader 注入映射。"""
    monday = date.fromisocalendar(*iso_week, 1)
    triplets: dict[int, tuple[float, float, float] | None] = {}
    for i in range(n):
        day = monday + timedelta(days=i % 6)
        fid = seed_jc_fixture(
            db,
            kickoff_utc=beijing_to_utc_iso(
                datetime(day.year, day.month, day.day, 19, 30)
            ),
            home=f"主{i}",
            away=f"客{i}",
            code=f"周{i:03d}",
        )
        if pl is not None:
            seed_triplet(
                db,
                fid,
                anchor_sentinel.PROPLINE_PINNACLE_SOURCE,
                pl,
                captured_at=beijing_to_utc_iso(
                    datetime(day.year, day.month, day.day, 18)
                ),
            )
        triplets[fid] = srct
    return triplets


def face_by_axis(db: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """读取面：每轴最新行（幂等重跑=最新行胜出）。"""
    return anchor_sentinel.latest_week_face(db)


# 设计稿 §七.4：正常周（合成同价/微差）→ 四轴全 ok
def test_normal_week_all_axes_ok(db: sqlite3.Connection) -> None:
    triplets = seed_week(db, n=12)
    report = anchor_sentinel.compute_week(
        db, 2026, 45, srct_reader=reader_from_map(triplets)
    )
    assert report["n_fixtures"] == 12
    face = face_by_axis(db)
    assert set(face) == set(anchor_sentinel.AXES)
    for axis, row in face.items():
        assert row["status"] == "ok", (axis, dict(row))
    assert face[anchor_sentinel.AXIS_COVERAGE_SECOND]["metrics"]["rate"] == 1.0
    assert face[anchor_sentinel.AXIS_COVERAGE_ANCHOR]["metrics"]["rate"] == 1.0
    assert face[anchor_sentinel.AXIS_DEV_ODDS]["metrics"]["median"] == 0.0
    assert face[anchor_sentinel.AXIS_DEV_PROB]["metrics"]["mean"] == 0.0


# 设计稿 §七.2：采集断流周（propline 缺席）→ 1a <90% alert、2/3 insufficient、1b ok
def test_capture_outage_week_alerts_second_coverage_only(
    db: sqlite3.Connection,
) -> None:
    triplets = seed_week(db, n=12, pl=None)
    anchor_sentinel.compute_week(db, 2026, 45, srct_reader=reader_from_map(triplets))
    face = face_by_axis(db)
    assert face[anchor_sentinel.AXIS_COVERAGE_SECOND]["status"] == "alert"
    assert face[anchor_sentinel.AXIS_COVERAGE_SECOND]["metrics"]["rate"] == 0.0
    assert face[anchor_sentinel.AXIS_COVERAGE_ANCHOR]["status"] == "ok"
    assert face[anchor_sentinel.AXIS_DEV_ODDS]["status"] == "insufficient"
    assert face[anchor_sentinel.AXIS_DEV_PROB]["status"] == "insufficient"


# 设计稿 §七.3：第一锚断供周（cid177 缺席）→ 1b alert、1a 分母塌缩自身
# insufficient（自反性防呆的正测）、2/3 insufficient
def test_anchor_outage_week_alerts_anchor_coverage(
    db: sqlite3.Connection,
) -> None:
    triplets = seed_week(db, n=12, srct=None)
    anchor_sentinel.compute_week(db, 2026, 45, srct_reader=reader_from_map(triplets))
    face = face_by_axis(db)
    assert face[anchor_sentinel.AXIS_COVERAGE_ANCHOR]["status"] == "alert"
    assert face[anchor_sentinel.AXIS_COVERAGE_ANCHOR]["metrics"]["rate"] == 0.0
    assert face[anchor_sentinel.AXIS_COVERAGE_SECOND]["status"] == "insufficient"
    assert face[anchor_sentinel.AXIS_DEV_ODDS]["status"] == "insufficient"
    assert face[anchor_sentinel.AXIS_DEV_PROB]["status"] == "insufficient"


# 设计稿 §七.6：小周 n<10 → 各轴 insufficient、不告警
def test_small_week_is_insufficient_and_never_alerts(
    db: sqlite3.Connection,
) -> None:
    triplets = seed_week(db, n=9, srct=None, pl=None)  # 覆盖轴也空，仍只黄不红
    anchor_sentinel.compute_week(db, 2026, 45, srct_reader=reader_from_map(triplets))
    face = face_by_axis(db)
    for axis, row in face.items():
        assert row["status"] == "insufficient", (axis, dict(row))


# 断点形态价组：srct 整组陈旧漂移（三大向全偏），propline 新鲜
STALE_SRCT = (2.2, 3.6, 3.1)
FRESH_PL = (2.5, 3.3, 2.8)


# 设计稿 §七.1：断点周（srct 价陈旧/propline 新鲜，连续两周）→ 2/3 轴
# alert、1a/1b ok（staleness ≠ absence）
def test_breakpoint_week_alerts_divergence_not_coverage(
    db: sqlite3.Connection,
) -> None:
    anchor_sentinel.compute_week(
        db,
        *anchor_sentinel.previous_iso_week(2026, 45),
        srct_reader=reader_from_map(
            seed_week(
                db,
                n=12,
                srct=STALE_SRCT,
                pl=FRESH_PL,
                iso_week=anchor_sentinel.previous_iso_week(2026, 45),
            )
        ),
    )
    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=12, srct=STALE_SRCT, pl=FRESH_PL)),
    )
    face = face_by_axis(db)
    assert face[anchor_sentinel.AXIS_COVERAGE_SECOND]["status"] == "ok"
    assert face[anchor_sentinel.AXIS_COVERAGE_ANCHOR]["status"] == "ok"
    assert face[anchor_sentinel.AXIS_DEV_ODDS]["status"] == "alert"
    assert face[anchor_sentinel.AXIS_DEV_ODDS]["metrics"]["median"] > 0.01
    assert face[anchor_sentinel.AXIS_DEV_PROB]["status"] == "alert"
    assert face[anchor_sentinel.AXIS_DEV_PROB]["metrics"]["mean"] > 0.010


# 设计稿 §七.5：signed 禁令守护——大 |Δp| 但 signed 均值≈0（三向各自 Shin
# 归一 ⇒ 每场 signed 和恒 0）→ 3 轴仍 alert（判据是 mean|Δp| 而非 signed）
def test_signed_mean_is_structurally_zero_yet_prob_axis_alerts(
    db: sqlite3.Connection,
) -> None:
    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=12, srct=STALE_SRCT, pl=FRESH_PL)),
    )
    face = face_by_axis(db)
    prob = face[anchor_sentinel.AXIS_DEV_PROB]
    assert prob["status"] == "alert"
    assert prob["metrics"]["mean"] > 0.010
    assert abs(prob["metrics"]["signed_mean_note"]) < 1e-9  # 结构零，仅注记


# 设计稿 §七.7：连续 2 周条款——单周 median 1.2% 仅 warn，连续两周 → alert
def test_odds_axis_persistence_two_week_clause(db: sqlite3.Connection) -> None:
    mild = (2.47, 3.26, 2.77)  # 各向相对偏差 ~1.1-1.2%
    week_a = anchor_sentinel.previous_iso_week(2026, 45)
    anchor_sentinel.compute_week(
        db,
        *week_a,
        srct_reader=reader_from_map(
            seed_week(db, n=12, srct=mild, pl=FRESH_PL, iso_week=week_a)
        ),
    )
    face_a = anchor_sentinel.latest_week_face(db)
    assert face_a[anchor_sentinel.AXIS_DEV_ODDS]["status"] == "warn"
    assert face_a[anchor_sentinel.AXIS_DEV_ODDS]["metrics"]["median"] > 0.01
    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=12, srct=mild, pl=FRESH_PL)),
    )
    face_b = anchor_sentinel.latest_week_face(db)
    assert face_b[anchor_sentinel.AXIS_DEV_ODDS]["status"] == "alert"
    assert face_b[anchor_sentinel.AXIS_DEV_ODDS]["metrics"]["persisted"] is True


# 设计稿 §七.8：幂等重跑——同周两算，读取面结果不变（append-only 最新行胜出）
def test_recompute_same_week_is_idempotent_on_read_face(
    db: sqlite3.Connection,
) -> None:
    triplets = seed_week(db, n=11)
    moment = datetime(2026, 11, 9, 0, 50, tzinfo=UTC)
    anchor_sentinel.compute_week(
        db, 2026, 45, srct_reader=reader_from_map(triplets), now=moment
    )
    first = face_by_axis(db)
    anchor_sentinel.compute_week(
        db, 2026, 45, srct_reader=reader_from_map(triplets), now=moment
    )
    second = face_by_axis(db)
    assert first == second
    rows = db.execute("SELECT COUNT(*) AS n FROM anchor_sentinel_weekly").fetchone()
    assert rows["n"] == 8  # 4 轴 × 2 算（追加不覆盖）


# 收盘=开球前最后一拍（探查报告 §三：无历史端点；滚后价被资格线挡掉）
def test_propline_leg_takes_last_pre_kickoff_poll(db: sqlite3.Connection) -> None:
    monday = date.fromisocalendar(2026, 45, 3)
    kickoff = beijing_to_utc_iso(datetime(monday.year, monday.month, monday.day, 19))
    fid = seed_jc_fixture(db, kickoff_utc=kickoff, code="周三001")
    seed_triplet(
        db,
        fid,
        anchor_sentinel.PROPLINE_PINNACLE_SOURCE,
        (2.4, 3.2, 2.9),
        captured_at=beijing_to_utc_iso(
            datetime(monday.year, monday.month, monday.day, 12)
        ),
    )
    # 开球后的滚中价：不入对账
    seed_triplet(
        db,
        fid,
        anchor_sentinel.PROPLINE_PINNACLE_SOURCE,
        (1.5, 4.2, 5.5),
        captured_at=beijing_to_utc_iso(
            datetime(monday.year, monday.month, monday.day, 20)
        ),
    )
    from goalx_backend.data import quote_evidence

    triplet = quote_evidence.propline_pinnacle_closing_triplet(db, fid, kickoff)
    assert triplet == (2.4, 3.2, 2.9)


# 设计稿 §五：周报 md 落语料树 reports/anchor-sentinel/<isoyear>-W<ww>.md
# （人读四轴一行一轴 + 异常 top5）
def test_week_report_md_written_to_corpus_tree(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    from goalx_backend.data.corpus_store import CorpusStore

    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=12, srct=STALE_SRCT, pl=FRESH_PL)),
    )
    report = anchor_sentinel.latest_report(db, iso_year=2026, iso_week=45)
    store = CorpusStore(tmp_path / "corpus")
    try:
        path = anchor_sentinel.write_week_report(store, report)
        assert path == (store.root / "reports" / "anchor-sentinel" / "2026-W45.md")
        text = path.read_text(encoding="utf-8")
    finally:
        store.close()
    assert "2026-W45" in text
    for axis in anchor_sentinel.AXES:
        assert axis in text
    assert "coverage_second=ok" in text
    assert "dev_odds=warn" in text  # 单周 >1%=warn（2 周持久性条款另测）
    assert "dev_prob=alert" in text
    assert text.count("\n") >= 6  # 四轴一行一轴 + 异常段，不是空壳


# ---- 采集面（设计稿 §二）：哨兵 sport 选择 + pinnacle 服务端过滤 + 滚中写侧防护 ----

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def kickoff_from_now(hours: float) -> str:
    """相对 _NOW 的 kickoff ISO 串（采集面测试时钟注入）。"""
    return (_NOW + timedelta(hours=hours)).isoformat()


def test_sentinel_sport_keys_union_linked_and_pending(db: sqlite3.Connection) -> None:
    from goalx_backend.data.ingest import propline

    # 已链 fixture、36h 内 → 其 sport 入选
    fid_linked = seed_jc_fixture(db, kickoff_utc=kickoff_from_now(20), code="今001")
    db.execute(
        "UPDATE fixtures SET propline_event_id = 'e1',"
        " propline_sport_key = 'soccer_epl' WHERE id = ?",
        (fid_linked,),
    )
    # 已链但 36h 外 → 不选（省请求）
    fid_far = seed_jc_fixture(db, kickoff_utc=kickoff_from_now(40), code="今002")
    db.execute("UPDATE fixtures SET propline_event_id = 'e2' WHERE id = ?", (fid_far,))
    # 待链面：JC FD 11 联赛、7d 内、未链 → 入选
    db.execute(
        "UPDATE competitions SET odds_api_sport_key = 'soccer_epl' WHERE name = '英超'"
    )
    seed_jc_fixture(db, kickoff_utc=kickoff_from_now(3 * 24), code="今003")
    # 待链但 7d 外 / 非竞彩 → 不选
    seed_jc_fixture(db, kickoff_utc=kickoff_from_now(9 * 24), code="今004")
    seed_jc_fixture(db, kickoff_utc=kickoff_from_now(3), code="x", jc=False)
    db.commit()
    keys = propline.sentinel_sport_keys(db, _NOW)
    assert keys == ["soccer_epl"]


PINNACLE_ONLY_EVENT = {
    # 服务端 bookmakers=pinnacle 过滤后的响应形状（探查报告 §二：仅 1 书）
    "id": "300001",
    "sport_key": "soccer_epl",
    "commence_time": "2026-11-10T20:00:00Z",  # _NOW 后 8h → 未开球
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "last_update": "2026-11-10T04:00:00Z",
    "bookmakers": [
        {
            "key": "pinnacle",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Arsenal", "price": 130},
                        {"name": "Draw", "price": 260},
                        {"name": "Chelsea", "price": 230},
                    ],
                }
            ],
        }
    ],
}


def _sentinel_capture_client(
    events: list[dict], *, captured_requests: list[str]
) -> httpx.Client:
    """MockTransport 客户端：回放 events、记录请求 URL。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(str(request.url))
        return httpx.Response(200, json=events, headers={"X-Daily-Remaining": "900"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_capture_face_requests_pinnacle_filter_and_stores(
    db: sqlite3.Connection,
) -> None:
    from goalx_backend.config import Settings
    from goalx_backend.data.ingest import propline

    fid = seed_jc_fixture(db, kickoff_utc=kickoff_from_now(8), code="今005")
    db.execute("UPDATE fixtures SET propline_event_id = '300001' WHERE id = ?", (fid,))
    db.execute(
        "UPDATE competitions SET odds_api_sport_key = 'soccer_epl' WHERE name = '英超'"
    )
    db.commit()
    requests: list[str] = []
    client = _sentinel_capture_client([PINNACLE_ONLY_EVENT], captured_requests=requests)
    stats = propline.fetch_and_store_odds(
        db,
        Settings(propline_api_key="k"),
        client,
        sport_keys=["soccer_epl"],
        books=("pinnacle",),
        skip_commenced=True,
        now=_NOW,
    )
    assert stats.requests_used == 1
    assert requests == [
        "https://api.prop-line.com/v1/sports/soccer_epl/odds"
        "?markets=h2h&bookmakers=pinnacle"
    ]
    assert stats.ingest.snapshots == 3  # 仅 pinnacle 一书三向
    sources = {
        str(r["source"])
        for r in db.execute(
            "SELECT DISTINCT source FROM odds_snapshots WHERE fixture_id = ?", (fid,)
        )
    }
    assert sources == {anchor_sentinel.PROPLINE_PINNACLE_SOURCE}


def test_capture_face_skips_commenced_events_from_snapshots(
    db: sqlite3.Connection,
) -> None:
    """滚中行写侧防护：commence_time 已过 → 只入观察（quote_observations）不入对账。"""
    from goalx_backend.config import Settings
    from goalx_backend.data.ingest import propline

    live_event = dict(
        PINNACLE_ONLY_EVENT,
        id="300002",
        commence_time="2026-11-10T10:30:00Z",  # _NOW 前 1.5h → 已开球滚中
    )
    fid = seed_jc_fixture(db, kickoff_utc=kickoff_from_now(-1.5), code="今006")
    db.execute("UPDATE fixtures SET propline_event_id = '300002' WHERE id = ?", (fid,))
    db.execute(
        "UPDATE competitions SET odds_api_sport_key = 'soccer_epl' WHERE name = '英超'"
    )
    db.commit()
    requests: list[str] = []
    client = _sentinel_capture_client([live_event], captured_requests=requests)
    stats = propline.fetch_and_store_odds(
        db,
        Settings(propline_api_key="k"),
        client,
        sport_keys=["soccer_epl"],
        books=("pinnacle",),
        skip_commenced=True,
        now=_NOW,
    )
    assert stats.post_commence == 1  # 观察留痕计数
    assert stats.ingest.snapshots == 0  # 快照不落（不混哨兵面）
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM quote_observations WHERE source = 'propline'"
    ).fetchone()
    assert rows["n"] == 1  # 观察证据层照落（原始响应已存证）


# ---- 任务面（ADR-0005 adapter）与晨检第 18 项 ----


def _isolated_settings(tmp_path: Path) -> Settings:
    """隔离文件库 settings（任务面自开连接，不走 :memory: fixture）。"""
    return Settings(
        db_path=tmp_path / "goalx.db",
        corpus_root=tmp_path / "corpus",
        observations_dir=tmp_path / "observations",
        propline_api_key="k",
    )


def test_capture_task_wires_sentinel_face(tmp_path: Path) -> None:
    from goalx_backend import tasks

    settings = _isolated_settings(tmp_path)
    face = connect(settings.db_path)
    try:
        migrate(face)
        fid = seed_jc_fixture(face, kickoff_utc=kickoff_from_now(8), code="任001")
        face.execute(
            "UPDATE fixtures SET propline_event_id = '300001' WHERE id = ?", (fid,)
        )
        face.execute(
            "UPDATE competitions SET odds_api_sport_key = 'soccer_epl'"
            " WHERE name = '英超'"
        )
        face.commit()
    finally:
        face.close()
    requests: list[str] = []
    client = _sentinel_capture_client([PINNACLE_ONLY_EVENT], captured_requests=requests)
    payload = tasks.anchor_sentinel_capture(settings=settings, client=client, now=_NOW)
    assert payload["snapshots"] == 3
    assert payload["sports"] == ["soccer_epl"]
    assert "bookmakers=pinnacle" in requests[0]


def test_weekly_task_defers_without_corpus_anchor(tmp_path: Path) -> None:
    """语料桥缺席=deferred 不落行（防假 1b alert 留痕；晨检 8 天条款兜底）。"""
    from goalx_backend import tasks

    settings = _isolated_settings(tmp_path)
    face = connect(settings.db_path)
    try:
        migrate(face)
    finally:
        face.close()
    payload = tasks.anchor_sentinel_weekly(
        iso_year=2026, iso_week=45, settings=settings
    )
    assert "deferred" in payload
    assert "corpus" in str(payload["deferred"])
    face = connect(settings.db_path)
    try:
        rows = face.execute(
            "SELECT COUNT(*) AS n FROM anchor_sentinel_weekly"
        ).fetchone()
        assert rows["n"] == 0
    finally:
        face.close()


def test_weekly_task_computes_and_writes_report(tmp_path: Path) -> None:
    from goalx_backend import tasks

    settings = _isolated_settings(tmp_path)
    face = connect(settings.db_path)
    try:
        migrate(face)
        triplets = seed_week(face, n=11)
        face.commit()
    finally:
        face.close()
    payload = tasks.anchor_sentinel_weekly(
        iso_year=2026,
        iso_week=45,
        settings=settings,
        srct_reader=reader_from_map(triplets),
        now=datetime(2026, 11, 9, 0, 50, tzinfo=UTC),
    )
    assert payload["n_fixtures"] == 11
    md = Path(str(payload["md_path"]))
    assert md.exists()
    assert "2026-W45" in md.read_text(encoding="utf-8")


def test_morning_check_item_red_on_alert(db: sqlite3.Connection) -> None:
    from goalx_backend.morning_check import check_anchor_sentinel

    triplets = seed_week(db, n=12, srct=None)  # 第一锚断供 → 1b alert
    anchor_sentinel.compute_week(db, 2026, 45, srct_reader=reader_from_map(triplets))
    item = check_anchor_sentinel(db, now=datetime(2026, 11, 10, 1, 5, tzinfo=UTC))
    assert item.is_fail
    assert "coverage_anchor=alert" in item.detail


def test_morning_check_item_yellow_on_warn_or_stale(db: sqlite3.Connection) -> None:
    from goalx_backend.morning_check import check_anchor_sentinel

    item = check_anchor_sentinel(db, now=datetime(2026, 11, 10, 1, 5, tzinfo=UTC))
    assert item.status == "degraded"  # 未首跑：不绿冒充
    mild = (2.47, 3.26, 2.77)
    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=11, srct=mild)),
        now=datetime(2026, 11, 9, 0, 50, tzinfo=UTC),
    )
    fresh = check_anchor_sentinel(db, now=datetime(2026, 11, 10, 1, 5, tzinfo=UTC))
    assert fresh.status == "degraded"  # dev_odds warn → 黄
    assert "dev_odds=warn" in fresh.detail
    stale = check_anchor_sentinel(db, now=datetime(2026, 11, 25, 1, 5, tzinfo=UTC))
    assert stale.status == "degraded"  # >8 天无新周行 → 黄
    assert "8" in stale.detail


def test_morning_check_item_green_on_ok(db: sqlite3.Connection) -> None:
    from goalx_backend.morning_check import check_anchor_sentinel

    anchor_sentinel.compute_week(
        db,
        2026,
        45,
        srct_reader=reader_from_map(seed_week(db, n=11)),
        now=datetime(2026, 11, 9, 0, 50, tzinfo=UTC),
    )
    item = check_anchor_sentinel(db, now=datetime(2026, 11, 10, 1, 5, tzinfo=UTC))
    assert item.status == "ok"
    assert "coverage_second=ok" in item.detail
    assert "dev_prob=ok" in item.detail
