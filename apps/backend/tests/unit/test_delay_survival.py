"""延迟生存率 Stage 1 到达率审计（report-audit 票 07 预注册）的行为断言。

合成账本行覆盖漏斗每一终态（分母分层互斥性）、主指标身份门
（票 03 白名单）、日分类与停止条件算术；CLI 接线走 --skip-close
（收盘注入缝用桩提供方覆盖，真源网络永不进测试）。
"""

from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from goalx_backend import db
from goalx_backend.cli import _cmd_delay_audit
from goalx_backend.config import Settings
from goalx_backend.data import delay_survival as ds
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.models import SaleStatusInput, Tier

OBS = "2026-10-20T11:00:00+00:00"
KICKOFF = "2026-10-20T18:00:00+00:00"
BACKFILL_TS = "2026-10-21T00:00:00+00:00"

# 桩收盘提供方：按主队名取 (p_close, close_ref)；缺主队名=无收盘
CloseSpec = dict[str, tuple[float, str] | None]


def _provider(spec: CloseSpec):
    def provide(
        home: str, away: str, kickoff_utc: str, selection: str
    ) -> tuple[float, str, str] | None:
        entry = spec.get(home)
        if entry is None:
            return None
        return entry[0], entry[1], "srct_pinnacle"

    return provide


def _seed_fixture(db: sqlite3.Connection, n: int) -> tuple[str, int]:
    """一场比赛（主队名=收盘桩规格键）；返回 (主队名, fixture_id)。"""
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"主{n}")
    away = fx_store.upsert_team(db, f"客{n}")
    fixture = fx_store.upsert_fixture(db, competition, KICKOFF, home, away)
    db.commit()
    return f"主{n}", fixture


_SNAPSHOT_SEQ = itertools.count(1001)


def _ledger_row(
    db: sqlite3.Connection,
    *,
    sel: str = "h",
    odds: float = 3.0,
    observed: str = OBS,
    source_updated: str | None = OBS,
    p_sharp: tuple[float, float, float] | None = (0.5, 0.3, 0.2),
    anchor: str | None = "srct_1x2_asof",
    sale: str | None = "on_sale",
    single: int | None = 1,
    backfilled: bool = False,
) -> None:
    """直写账本一行（append-only 触发器只禁删/改，INSERT 直通）。"""
    snapshot_id = next(_SNAPSHOT_SEQ)
    db.execute(
        """
        INSERT INTO price_deviation_ledger
        (fixture_id, market_code, selection_code, jc_odds, observed_at,
         source_updated_at, snapshot_id, p_sharp_h, p_sharp_d, p_sharp_a,
         anchor_ref, sale_state, single_eligible, reference_backfilled_at,
         created_at)
        VALUES ((SELECT MAX(id) FROM fixtures), 'had', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?)
        """,
        (
            sel,
            odds,
            observed,
            source_updated,
            snapshot_id,
            *(p_sharp if p_sharp else (None, None, None)),
            anchor,
            sale,
            single,
            BACKFILL_TS if backfilled else None,
            observed,
        ),
    )
    db.commit()


def _at(lag_seconds: int) -> str:
    """OBS 往前 lag 秒的 ISO 时刻（构造陈旧度）。"""
    return (datetime.fromisoformat(OBS) - timedelta(seconds=lag_seconds)).isoformat()


def _funnel_rows(db: sqlite3.Connection) -> CloseSpec:
    """覆盖全部终态的 14 行（每行一场比赛，主队名=收盘桩规格键）。"""
    spec: CloseSpec = {}

    def row(
        n: int,
        *,
        odds: float = 3.0,
        source_updated: str | None = OBS,
        p_sharp: tuple[float, float, float] | None = (0.5, 0.3, 0.2),
        anchor: str | None = "srct_1x2_asof",
        sale: str | None = "on_sale",
        single: int | None = 1,
        backfilled: bool = False,
        close: tuple[float, str] | None = (0.5, "srct_1x2_closing"),
    ) -> None:
        home, _fid = _seed_fixture(db, n)
        if close is not None:
            spec[home] = close
        _ledger_row(
            db,
            odds=odds,
            source_updated=source_updated,
            p_sharp=p_sharp,
            anchor=anchor,
            sale=sale,
            single=single,
            backfilled=backfilled,
        )

    row(1, sale="stopped")  # A sale_stopped
    row(2, sale="unknown")  # B sale_unknown
    row(3, single=0)  # C single_ineligible
    row(4, single=None)  # D single_unknown
    row(5, p_sharp=None, anchor=None)  # E no_reference
    row(6, backfilled=True, source_updated=_at(3600))  # F backfilled
    row(7, source_updated=_at(200), close=None)  # G stale@0s、可达@300s
    row(8)  # H survived（同身份收盘正）
    row(9, close=(0.2, "srct_1x2_closing"))  # I faded（收盘转负）
    row(10, anchor=None, close=None)  # J identity_unknown
    row(11, anchor="pinnacle", close=(0.5, "eu_consensus_5"))  # K mismatch
    row(12, close=None)  # L close_missing
    row(13, odds=1.5)  # M no_edge
    row(14, anchor="eu_consensus_5", close=(0.6, "eu_consensus_3"))  # N survived
    return spec


def test_full_funnel_layers_reasons_metric(db: sqlite3.Connection) -> None:
    """14 行全终态：分母逐层、终态互斥、主指标与身份门分层。"""
    spec = _funnel_rows(db)
    report = ds.build_arrival_audit_report(
        db,
        date_from="2026-10-20",
        date_to="2026-10-20",
        close_provider=_provider(spec),
    )
    overall = report["overall"]
    assert overall["l1"] == 14
    assert overall["l2"] == 10  # A-D 出局
    assert overall["l3"] == 7  # E/F/G 出局；H-N 陈旧度=0
    assert overall["survival_ratio"] == pytest.approx(7 / 10)
    assert overall["reasons"] == {
        "sale_stopped": 1,
        "sale_unknown": 1,
        "single_ineligible": 1,
        "single_unknown": 1,
        "no_reference": 1,
        "backfilled": 1,
        "stale": 1,
        "no_edge": 1,
        "identity_unknown": 1,
        "identity_mismatch": 1,
        "close_missing": 1,
        "survived": 2,
        "faded": 1,
    }
    metric = overall["metric"]
    # 分母=L2∩可算（E 无参考出局）；正候选 8；同身份收盘可比 4（F/H/I/N
    # ——回填行属 L2，主指标照算，剔除回填是预指定对照不是主口径）
    assert metric["n_base"] == 9
    assert metric["positive_at_decision"] == 8
    assert metric["close_computable_positive"] == 4
    assert metric["survived"] == 3
    assert metric["survival_share"] == pytest.approx(3 / 9)
    ci = metric["wilson95"]
    assert ci is not None
    assert ci[0] <= 3 / 9 <= ci[1]
    assert metric["positive_excluded"] == {
        "identity_unknown": 1,
        "identity_mismatch": 1,
        "close_missing": 2,  # G 无收盘桩 + L 提供方空手
    }
    # 预指定对照：剔陈旧>300s 后 F(3600s) 出分母、G(200s) 留
    contrast = overall["contrasts"]["excl_stale_gt_300s"]
    assert contrast["n_base"] == 8
    assert contrast["positive_at_decision"] == 7
    assert contrast["survived"] == 2
    # 档位骨架：只判读 d=0；300s 档 G 变可达（照记数、标 pending）
    tiers = {t["tier_s"]: t for t in overall["delay_tiers"]}
    assert tiers[0]["status"] == "reported"
    assert tiers[0]["l3"] == 7
    assert tiers[180]["l3"] == 7
    assert tiers[300]["status"] == "pending"
    assert tiers[300]["l3"] == 8
    assert report["close_face"] == "injected"


def test_stop_condition_arithmetic(db: sqlite3.Connection) -> None:
    """停止条件两臂：回填占比>L1 30% 且剔后 L3 正EV<1/周 → 触发。"""
    for n in range(4):
        _seed_fixture(db, n)
    _ledger_row(db, backfilled=True)
    _ledger_row(db, backfilled=True)
    _ledger_row(db, backfilled=True)
    _ledger_row(db, sale="stopped")  # 非回填但停售（正EV=0）
    report = ds.build_arrival_audit_report(
        db, date_from="2026-10-20", date_to="2026-10-26"
    )
    sc = report["stop_condition"]
    assert sc["l1"] == 4
    assert sc["backfill_share"] == pytest.approx(0.75)
    assert sc["l3_positive_ex_backfill_ex_stale"] == 0
    assert sc["per_week"] == pytest.approx(0.0)
    assert sc["triggered"] is True


def test_day_classification_and_gate(db: sqlite3.Connection) -> None:
    """active/疑似断采/无在售证据/无数据四类 + 严格销售日门槛。"""
    _home, fixture_id = _seed_fixture(db, 1)
    _ledger_row(db)  # 10-20 active
    for day, state in (
        ("2026-10-20", "on_sale"),
        ("2026-10-21", "on_sale"),  # 在售而账本零行 → 疑似断采
        ("2026-10-22", "stopped"),  # 只有停售行 → 无在售证据
    ):
        fx_store.append_sale_status(
            db,
            SaleStatusInput(
                fixture_id=fixture_id,
                market_code="had",
                sale_state=state,
                observed_at=f"{day}T10:00:00+00:00",
            ),
        )
    db.commit()
    report = ds.build_arrival_audit_report(
        db, date_from="2026-10-20", date_to="2026-10-23"
    )
    classes = {d["date"]: d["class"] for d in report["day_classification"]}
    assert classes == {
        "2026-10-20": "active",
        "2026-10-21": "suspected_outage",
        "2026-10-22": "no_sale_evidence",
        "2026-10-23": "outage_no_data",
    }
    assert report["protocol"]["sale_days_strict"] == 2
    assert report["protocol"]["metric_status"] == "provisional"
    assert len(report["by_day"]) == 4
    assert report["by_day"][3]["l1"] == 0  # 零行日照记不丢日


def test_staleness_distribution(db: sqlite3.Connection) -> None:
    """陈旧度分位数/直方桶/不可算单列。"""
    _seed_fixture(db, 1)
    for lag in (10, 100, 200, 400, 5000, 40000):
        _ledger_row(db, source_updated=_at(lag))
    _ledger_row(db, source_updated=None)  # 不可算
    st = ds.build_arrival_audit_report(
        db, date_from="2026-10-20", date_to="2026-10-20"
    )["overall"]["staleness"]
    assert st["n_computable"] == 6
    assert st["n_missing"] == 1
    assert st["gt_300s"] == 3
    assert st["buckets"] == {
        "0-60s": 1,
        "60-180s": 1,
        "180-300s": 1,
        "300-900s": 1,
        ">3600s": 2,  # 5000s 与 40000s 都越过小时界
    }
    assert st["quantiles"]["min"] == pytest.approx(10.0)
    assert st["quantiles"]["max"] == pytest.approx(40000.0)


def test_wilson_ci_basics() -> None:
    """Wilson CI：含点估计；0/n 与 n/n 收边界；n=0 无区间。"""
    ci = ds._wilson_ci(1, 8)
    assert ci is not None
    assert ci[0] <= 0.125 <= ci[1]
    zero = ds._wilson_ci(0, 4)
    assert zero is not None
    assert zero[0] == 0.0
    full = ds._wilson_ci(4, 4)
    assert full is not None
    assert full[1] == pytest.approx(1.0)
    assert ds._wilson_ci(0, 0) is None


def test_close_face_absent(db: sqlite3.Connection) -> None:
    """无收盘提供方：收盘面 absent，正候选落 close_missing 不冒充。"""
    _seed_fixture(db, 1)
    _ledger_row(db)  # 新鲜正候选
    report = ds.build_arrival_audit_report(
        db, date_from="2026-10-20", date_to="2026-10-20"
    )
    assert report["close_face"] == "absent"
    assert report["overall"]["reasons"]["close_missing"] == 1
    assert report["overall"]["metric"]["survived"] == 0


def test_window_validation(db: sqlite3.Connection) -> None:
    """窗参校验：格式错/起始晚于截止 → fail-loud。"""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        ds.build_arrival_audit_report(db, date_from="2026/10/20", date_to="2026-10-21")
    with pytest.raises(ValueError, match="起始日晚于截止日"):
        ds.build_arrival_audit_report(db, date_from="2026-10-22", date_to="2026-10-21")


def test_orphan_ledger_row_fails_loud(db: sqlite3.Connection) -> None:
    """账本行关联不上场次=完整性事故，拒绝出报告（不静默缩分母）。"""
    db.execute(
        """
        INSERT INTO price_deviation_ledger
        (fixture_id, market_code, selection_code, jc_odds, observed_at,
         snapshot_id, created_at)
        VALUES (9999, 'had', 'h', 3.0, ?, 4242, ?)
        """,
        (OBS, OBS),
    )
    db.commit()
    with pytest.raises(RuntimeError, match="无法关联场次"):
        ds.build_arrival_audit_report(db, date_from="2026-10-20", date_to="2026-10-20")


def test_report_writes_two_views_with_fingerprint(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """store 在场：JSON+MD 落语料树 reports/，随运行指纹（票 06 惯例）。"""
    _seed_fixture(db, 1)
    _ledger_row(db)
    store = CorpusStore(tmp_path / "corpus")
    try:
        report = ds.build_arrival_audit_report(
            db,
            date_from="2026-10-20",
            date_to="2026-10-20",
            close_provider=None,
            store=store,
        )
    finally:
        store.close()
    reports = tmp_path / "corpus" / "reports"
    assert (reports / "stage1-arrival-audit.json").is_file()
    md = (reports / "stage1-arrival-audit.md").read_text(encoding="utf-8")
    assert "Stage 1 到达率审计" in md
    assert "失败变体记录" in md
    fingerprint: dict[str, Any] = report["run_fingerprint"]
    assert "price_deviation_ledger" in fingerprint["db"]["tables"]
    assert report["overall"]["l4"] is None  # L4 留空如实记


def _file_db(tmp_path: Path) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "goalx.db")
    db.migrate(conn)
    return conn


def test_cli_delay_audit_skip_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI 接线：--skip-close 口径冒烟，JSON stdout+双视图落盘。"""
    settings = Settings(corpus_root=tmp_path / "corpus", db_path=tmp_path / "goalx.db")
    conn = _file_db(tmp_path)
    try:
        _seed_fixture(conn, 1)
        _ledger_row(conn)
    finally:
        conn.close()
    _cmd_delay_audit(
        argparse.Namespace(
            date_from="2026-10-20", date_to="2026-10-20", skip_close=True
        ),
        settings=settings,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "delay-survival-stage1/1"
    assert payload["close_face"] == "absent"
    assert payload["md_path"].endswith("reports/stage1-arrival-audit.md")
    assert (tmp_path / "corpus" / "reports" / "stage1-arrival-audit.md").is_file()


def test_cli_delay_audit_missing_db_fails_loud(tmp_path: Path) -> None:
    """运行面库缺失 fail-loud（connect 会新建空库，空面假报告更糟）。"""
    settings = Settings(corpus_root=tmp_path / "corpus", db_path=tmp_path / "goalx.db")
    with pytest.raises(FileNotFoundError, match="运行面库不存在"):
        _cmd_delay_audit(
            argparse.Namespace(
                date_from="2026-10-20", date_to="2026-10-20", skip_close=True
            ),
            settings=settings,
        )
