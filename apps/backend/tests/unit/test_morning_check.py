"""09:05 晨检代码化测试（ingest-orchestration 票 01）：逐项红绿判定。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend import morning_check as mc
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data.corpus_store import GOLD_DATASET, GOLD_PROVIDER, CorpusStore
from goalx_backend.models import MatchCodeInput, Tier
from goalx_backend.morning_check import (
    DEGRADED,
    FAIL,
    OK,
    check_bets,
    check_corpus,
    check_gold_freshness,
    check_ledger,
    check_night,
    check_pool_drift,
    check_remat,
    check_serve_log,
    check_today_eu,
    check_wrap,
)

NOW = datetime(2026, 11, 1, 1, 5, tzinfo=UTC)  # 北京 09:05


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path: Path) -> CorpusStore:
    return CorpusStore(tmp_path / "corpus")


def _night_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "night_date": "2026-10-31",
        "started_at": _iso(9),
        "ended_at": _iso(2),
        "stop_reason": "completed",
        "dates_attempted": 1,
        "dates_done": 1,
        "dates_not_found": 0,
        "pending_before": 0,
        "pending_after": 0,
        "requests": 100,
        "raw_new": 50,
        "parsed_ok": 50,
        "bronze_repaired": 0,
        "xg_matches": 0,
        "parse_failed_count": 0,
        "failed_count": 0,
        "failed_json": "[]",
        "budget_cap": 8000,
    }
    base.update(overrides)
    return base


def test_check_night_states(store: CorpusStore) -> None:
    """completed=绿；budget/circuit=黄；过旧/无行=红。"""
    store.record_night_summary(_night_row())
    assert check_night(store, now=NOW).status == OK
    store.record_night_summary(_night_row(stop_reason="budget"))
    assert check_night(store, now=NOW).status == DEGRADED
    store.record_night_summary(_night_row(ended_at=_iso(40)))
    assert check_night(store, now=NOW).status == FAIL


def test_check_remat_and_corpus(store: CorpusStore) -> None:
    """状态行/最新工件新鲜判定（无行=红；26h/36h 界）。"""
    assert check_remat(store, now=NOW).status == FAIL
    store.upsert_remat_state(_iso(3), 100, "{}")
    assert check_remat(store, now=NOW).status == OK
    assert check_corpus(store, now=NOW).status == FAIL
    store.ingest_raw("srct", "day_page", "k1", b"x", ext=".htm")
    # ingest_raw 落 fetched_at=now（真实钟），对 NOW(2026-11-01) 而言必新鲜
    item = check_corpus(store, now=datetime.now(UTC))
    assert item.status == OK


def test_check_gold_freshness_states(store: CorpusStore) -> None:
    """gold _meta built_at：36h 绿/60h 黄/更旧红；meta 缺席=红不冒充新鲜。"""
    assert check_gold_freshness(store, now=NOW).status == FAIL  # 无 meta
    meta_dir = store.gold_path(GOLD_PROVIDER, GOLD_DATASET)
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / "_meta.json"

    def _meta(hours_ago: float | str) -> str:
        value = hours_ago if isinstance(hours_ago, str) else _iso(hours_ago)
        return f'{{"built_at": "{value}"}}'

    meta_path.write_text(_meta(20), encoding="utf-8")
    assert check_gold_freshness(store, now=NOW).status == OK
    meta_path.write_text(_meta(48), encoding="utf-8")
    assert check_gold_freshness(store, now=NOW).status == DEGRADED
    meta_path.write_text(_meta(80), encoding="utf-8")
    assert check_gold_freshness(store, now=NOW).status == FAIL
    meta_path.write_text(_meta("not-a-date"), encoding="utf-8")
    assert check_gold_freshness(store, now=NOW).status == FAIL
    # correctness F4：合法 JSON 非对象体（手工损坏形态）不炸晨检整跑
    meta_path.write_text('["not", "an", "object"]', encoding="utf-8")
    assert check_gold_freshness(store, now=NOW).status == FAIL


def test_check_wrap_and_ledger(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO funnel_days (business_date, as_of, n_on_sale, n_quote_matched,"
        " n_fresh, n_valuation, n_eligible, created_at)"
        " VALUES ('2026-10-31', ?, 1, 1, 1, 1, 0, ?)",
        (_iso(2), _iso(2)),
    )
    db.commit()
    assert check_wrap(db, now=NOW).status == OK
    assert check_ledger(db, now=NOW).status == FAIL  # 空账本=红
    db.execute(
        "INSERT INTO price_deviation_ledger (fixture_id, market_code, selection_code,"
        " jc_odds, observed_at, snapshot_id, created_at)"
        " VALUES (1, 'had', 'h', 2.0, ?, 1, ?)",
        (_iso(3), _iso(3)),
    )
    db.commit()
    assert check_ledger(db, now=NOW).status == OK


def test_check_today_eu_buckets(db: sqlite3.Connection) -> None:
    """有链=绿；有场无链=红（红不许静默跳过）；空板=黄。"""
    business_date = fx_store.beijing_business_date(NOW)
    assert check_today_eu(db, business_date=business_date).status == DEGRADED
    comp = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fixture = fx_store.upsert_fixture(db, comp, "2026-11-01T18:00:00+00:00", home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code="周日001",
        ),
    )
    db.commit()
    assert check_today_eu(db, business_date=business_date).status == FAIL
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id, method,"
        " status, mapped_at) VALUES (?, 'srct', '123', 'primary_key', 'linked', ?)",
        (fixture, _iso(1)),
    )
    db.commit()
    assert check_today_eu(db, business_date=business_date).status == OK


def test_check_alias_coverage_states(db: sqlite3.Connection) -> None:
    """项 16（identity 票 02）：no_mapping>0=红附队名；清零=绿；无账=黄。"""
    assert (
        mc.check_alias_coverage(
            db, business_date=fx_store.beijing_business_date(NOW)
        ).status
        == DEGRADED
    )  # 覆盖账无行
    business_date = fx_store.beijing_business_date(NOW)
    comp = fx_store.upsert_competition(db, "英冠", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "谢周三")
    away = fx_store.upsert_team(db, "普利茅斯")
    fixture = fx_store.upsert_fixture(db, comp, "2026-11-01T18:00:00+00:00", home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code="周日002",
        ),
    )
    db.execute(
        "INSERT INTO forecast_coverage"
        " (business_date, fixture_id, covered, reason, detail, recorded_at)"
        " VALUES (?, ?, 0, 'no_mapping', 'home=None away=None', ?)",
        (business_date, fixture, _iso(1)),
    )
    db.commit()
    item = mc.check_alias_coverage(
        db, business_date=fx_store.beijing_business_date(NOW)
    )
    assert item.status == FAIL
    assert "谢周三" in item.detail
    assert "普利茅斯" in item.detail
    assert "home=None" in item.detail  # 模型侧断名随单报出
    db.execute(
        "UPDATE forecast_coverage SET reason = 'not_fd_league', detail = NULL"
        " WHERE fixture_id = ?",
        (fixture,),
    )
    db.commit()
    assert (
        mc.check_alias_coverage(
            db, business_date=fx_store.beijing_business_date(NOW)
        ).status
        == OK
    )


def test_check_alias_coverage_bad_business_date(db: sqlite3.Connection) -> None:
    """correctness F1：写侧无校验的业务日坏串只降级本项，不炸整检。"""
    comp = fx_store.upsert_competition(db, "英冠", tier=Tier.TIER1)
    fixture = fx_store.upsert_fixture(
        db,
        comp,
        "2026-11-07T18:00:00+00:00",
        fx_store.upsert_team(db, "斯托克城"),
        fx_store.upsert_team(db, "加的夫城"),
    )
    # cli forecast --date 2026-11-7 未校验即入库；'2026-11-7' 文本序还赢当月日期
    db.execute(
        "INSERT INTO forecast_coverage"
        " (business_date, fixture_id, covered, reason, recorded_at)"
        " VALUES ('2026-11-7', ?, 1, NULL, ?)",
        (fixture, _iso(1)),
    )
    db.commit()
    item = mc.check_alias_coverage(db, business_date="2026-11-07")
    assert item.status == DEGRADED
    assert "2026-11-7" in item.detail


def test_check_alias_coverage_stale_account(db: sqlite3.Connection) -> None:
    """陈账的 no_mapping 0 不冒充新鲜：落后 >1 业务日=黄。"""
    comp = fx_store.upsert_competition(db, "英冠", tier=Tier.TIER1)
    fixture = fx_store.upsert_fixture(
        db,
        comp,
        "2026-10-20T18:00:00+00:00",
        fx_store.upsert_team(db, "利兹联"),
        fx_store.upsert_team(db, "米尔顿"),
    )
    db.execute(
        "INSERT INTO forecast_coverage"
        " (business_date, fixture_id, covered, reason, recorded_at)"
        " VALUES ('2026-10-20', ?, 1, NULL, ?)",
        (fixture, _iso(1)),
    )
    db.commit()
    item = mc.check_alias_coverage(
        db, business_date=fx_store.beijing_business_date(NOW)
    )
    assert item.status == DEGRADED
    assert "落后" in item.detail


def test_check_bets_observation(db: sqlite3.Connection) -> None:
    assert check_bets(db).status == OK
    assert "0 注" in check_bets(db).detail


def test_check_paper_account_observation(db: sqlite3.Connection) -> None:
    """项 15（auto-paper 票 04）：空台账=未开始不冒充；行数/现值/锚注记齐。"""
    item = mc.check_paper_account(db)
    assert item.status == OK
    assert "auto-v1 0/人工 0 注" in item.detail
    assert "现值 ¥1000.00" in item.detail
    assert "未开始" in item.detail


def test_check_serve_log(tmp_path: Path) -> None:
    clean = tmp_path / "serve.log"
    clean.write_text("hello\n")
    assert check_serve_log(clean).status == OK
    dirty = tmp_path / "dirty.log"
    dirty.write_text("x\nCRASHED\nTraceback (most recent call last):\n")
    assert check_serve_log(dirty).status == DEGRADED
    assert check_serve_log(tmp_path / "absent.log").status == DEGRADED


def _pool_client(
    *, sfc: str = "26155", jqc: str | None = "26225", fail: bool = False
) -> httpx.Client:
    """官方当期端点 mock：兼容单请求（90,0 带两面）与按 game 双请求两形态。"""

    def _face(draw: str | None) -> dict[str, Any]:
        return {"lotteryDrawNum": draw, "matchList": [{"matchNum": 1}]} if draw else {}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(500, text="boom")
        param = str(request.url.params["param"])
        if param.startswith("94,"):
            value = {"jqcMatch": _face(jqc)}
        else:
            value = {
                "sfcMatch": _face(sfc),
                # 90,0 的 jqc 面在 2026-07 后恒空（官方改按 game 下发）
                "jqcMatch": {},
            }
        return httpx.Response(200, json={"success": True, "value": value})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_check_pool_drift(db: sqlite3.Connection) -> None:
    """当期在库=绿；端点挂=黄。"""
    settings = Settings()
    pool_store.upsert_pool_period(db, "ttt14", "26155", _iso(-10))
    pool_store.upsert_pool_period(db, "goals4", "26225", _iso(-10))
    db.commit()
    assert check_pool_drift(db, settings, _pool_client()).status == OK
    degraded = check_pool_drift(db, settings, _pool_client(fail=True))
    assert degraded.status == DEGRADED


def test_check_pool_drift_detects_lag(db: sqlite3.Connection) -> None:
    """差集分支（票面验收"库内期号落后官方在售→红"）：sfc 库止 26150、
    官方在售 26155 → 红。goals4 的活面差集要待 zucai 双请求修复（票 02）
    合入后才可达，其断供形态在单请求版由 7 天陈旧分支兜住（见下用例）。"""
    settings = Settings()
    pool_store.upsert_pool_period(db, "ttt14", "26150", _iso(-10))
    pool_store.upsert_pool_period(db, "goals4", "26225", _iso(-10))
    db.commit()
    item = check_pool_drift(db, settings, _pool_client(), now=NOW)
    assert item.status == FAIL
    assert "官方在售 26155 > 库内 26150" in item.detail


def test_check_pool_drift_detects_goals4_outage(db: sqlite3.Connection) -> None:
    """goals4 断供形态（票 02 事故）：库内止于 7 月 + 官方当期在售 → 红。"""
    settings = Settings()
    pool_store.upsert_pool_period(db, "ttt14", "26155", _iso(-10))
    pool_store.upsert_pool_period(db, "goals4", "26133", _iso(2400))  # 止于 7 月
    db.commit()
    item = check_pool_drift(db, settings, _pool_client())
    assert item.status == FAIL
    assert "goals4" in item.detail
    # 官方 jqc 面空（过渡态）且库旧 → 仍红（断供复发形态）
    empty_face = check_pool_drift(db, settings, _pool_client(jqc=None))
    assert empty_face.status == FAIL


def test_check_stack_timeout_is_red_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """correctness F1：connect_ex 超时抛 TimeoutError → 红/黄不炸。"""

    class DeadSocket:
        def settimeout(self, _: float) -> None: ...

        def connect_ex(self, _: tuple[str, int]) -> int:
            raise TimeoutError("timed out")

        def close(self) -> None: ...

    monkeypatch.setattr(mc, "socket", type("s", (), {"socket": DeadSocket}))
    item = mc.check_stack()
    assert item.status == FAIL
    assert item.detail.count("down") >= 1


def test_check_pool_drift_bad_body_degrades(db: sqlite3.Connection) -> None:
    """correctness F2：200+HTML 体（WAF 页）→ 黄不崩。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>denied</html>")

    item = check_pool_drift(
        db, Settings(), httpx.Client(transport=httpx.MockTransport(handler)), now=NOW
    )
    assert item.status == DEGRADED


def test_check_serve_log_prefect_crashed_marker(tmp_path: Path) -> None:
    """correctness F3：Prefect 实际拼写 Crashed（title case）必须命中。"""
    log = tmp_path / "serve.log"
    log.write_text("x\nFinished in state Crashed('Flow run process was terminated')\n")
    assert check_serve_log(log).status == DEGRADED


def test_check_jc_waiting_buckets(db: sqlite3.Connection) -> None:
    """票 05 兜底可见性：全链=绿；视界内等=黄；≤2h 临界=红。"""
    from goalx_backend.morning_check import check_jc_waiting

    bd = fx_store.beijing_business_date(NOW)
    bd_next = fx_store.beijing_business_date(NOW + timedelta(days=1))
    comp = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    far = fx_store.upsert_fixture(db, comp, "2026-11-01T21:00:00+00:00", home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=far, kind="jingcai", business_date=bd, code="周日101"
        ),
    )
    db.commit()
    item = check_jc_waiting(db, now=NOW, business_dates=(bd, bd_next))
    assert item.status == DEGRADED  # 开球 +20h：等源T 黄
    critical = fx_store.upsert_fixture(
        db, comp, "2026-11-01T02:05:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=critical, kind="jingcai", business_date=bd, code="周日102"
        ),
    )
    db.commit()
    item = check_jc_waiting(db, now=NOW, business_dates=(bd, bd_next))
    assert item.status == FAIL  # 开球 +1h：临界红
    assert "临界 1" in item.detail
    assert "周日102" in item.detail  # 名单单列（票面验收措辞）
    for fid in (far, critical):
        db.execute(
            "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
            " method, status, mapped_at) VALUES (?, 'srct', '123', 'primary_key',"
            " 'linked', ?)",
            (fid, _iso(1)),
        )
    db.commit()
    item = check_jc_waiting(db, now=NOW, business_dates=(bd, bd_next))
    assert item.status == OK  # 全链绿


def test_check_jc_waiting_overnight_residual_is_red(db: sqlite3.Connection) -> None:
    """correctness F1：深夜场临界窗（23:00-03:00）无观察点——晨检 09:05 的
    最强信号=近 36h 内已开球仍未链（昨夜残差），必须红。"""
    from goalx_backend.morning_check import check_jc_waiting

    bd = fx_store.beijing_business_date(NOW)
    bd_prev = fx_store.beijing_business_date(NOW - timedelta(days=1))
    comp = fx_store.upsert_competition(db, "荷甲", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "格罗宁根")
    away = fx_store.upsert_team(db, "鹿特丹斯巴达")
    # 昨日业务日的深夜场：北京今晨 01:00 已开球 8h、未链
    fixture = fx_store.upsert_fixture(db, comp, "2026-10-31T17:00:00+00:00", home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture, kind="jingcai", business_date=bd_prev, code="周六015"
        ),
    )
    db.commit()
    item = check_jc_waiting(db, now=NOW, business_dates=(bd_prev, bd))
    assert item.status == FAIL
    assert "昨夜残差 1" in item.detail
    assert "周六015" in item.detail


# --- boundary-hardening 票 06：运行指纹项（第 13 项） ---


def test_check_runtime_fingerprint_dirty_is_yellow(tmp_path: Path) -> None:
    """dirty=黄不红(裁决①照跑);healthz 不可读只缺面不算红(栈死归项 1)。"""
    log = tmp_path / "serve.log"

    def clean_probe() -> dict[str, str]:
        return {"git_sha": "a" * 40, "git_dirty": "false"}

    log.write_text(
        "runtime fingerprint: git_sha=aaa git_dirty=false\n", encoding="utf-8"
    )
    item = mc.check_runtime_fingerprint(log, probe=clean_probe)
    assert item.status == mc.OK

    def dirty_probe() -> dict[str, str]:
        return {"git_sha": "a" * 40, "git_dirty": "true"}

    item = mc.check_runtime_fingerprint(log, probe=dirty_probe)
    assert item.status == mc.DEGRADED  # 亮黄不红

    # 调度日志:最近一次启动指纹后带 WORKTREE DIRTY → 黄
    log.write_text(
        "runtime fingerprint: git_sha=old\nWORKTREE DIRTY x\n"
        "restart\nruntime fingerprint: git_sha=bbb\n",
        encoding="utf-8",
    )
    item = mc.check_runtime_fingerprint(log, probe=clean_probe)
    assert item.status == mc.OK  # dirty 行在旧班次,本班次干净

    log.write_text(
        "runtime fingerprint: git_sha=bbb\nWORKTREE DIRTY — 照跑\n",
        encoding="utf-8",
    )
    item = mc.check_runtime_fingerprint(log, probe=clean_probe)
    assert item.status == mc.DEGRADED

    # healthz 不可读:不红(栈活性项 1 覆盖);日志无指纹行:如实注明
    def broken_probe() -> dict[str, str]:
        raise httpx.ConnectError("down")

    empty_log = tmp_path / "empty.log"
    empty_log.write_text("", encoding="utf-8")
    item = mc.check_runtime_fingerprint(empty_log, probe=broken_probe)
    assert item.status == mc.OK
    assert "healthz 不可读" in item.detail
    assert "指纹行缺" in item.detail
