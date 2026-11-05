"""票 13 M3 评测报告单测：配对统计/两档达标/情报质量（离线种子）。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.results import DrawResultInput, upsert_draw_result
from goalx_backend.evaluation.forward_validation import (
    ForwardCoverage,
    ForwardGroupMetrics,
    ForwardSkillReport,
)
from goalx_backend.llm.m3_report import (
    TIER_A_MIN_PAIRS,
    PairedFusedVsMl,
    _ece_clean,
    _mean_ece,
    _tier_a,
    _tier_b,
    intel_quality,
    m3_protocol_report,
    paired_fused_vs_ml,
    report_summary,
)
from goalx_backend.llm.review import record_blind_review
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.modelling.forecast import insert_forecast
from goalx_backend.models import MatchCodeInput, SnapshotInput

_KICKOFF = "2026-09-20T19:00:00+00:00"


def _seed_one(
    db: sqlite3.Connection,
    fixture_id: int,
    *,
    ml: tuple[float, float, float],
    llm: tuple[float, float, float],
    fused: tuple[float, float, float] | None,
    home_goals: int,
    away_goals: int,
    intel: bool = False,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO competitions (name, tier, created_at)"
        " VALUES ('英超', 'tier1', '2026-09-01T00:00:00+00:00')"
    )
    comp_id = db.execute("SELECT id FROM competitions WHERE name = '英超'").fetchone()[
        "id"
    ]
    ids = {}
    for name in (f"主{fixture_id}", f"客{fixture_id}"):
        cur = db.execute(
            "INSERT INTO teams (canonical_name, created_at)"
            " VALUES (?, '2026-09-01T00:00:00+00:00')",
            (name,),
        )
        ids[name] = cur.lastrowid
    db.execute(
        """
        INSERT INTO fixtures
            (id, competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (fixture_id, comp_id, _KICKOFF, ids[f"主{fixture_id}"], ids[f"客{fixture_id}"]),
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="ml",
        model_version="dc:1",
        content_hash=f"ml-{fixture_id}",
        payload={
            "matrix": [[0, 0, ml[2]], [0, ml[1], 0], [ml[0], 0, 0]],
            "lambda_home": 1.0,
            "lambda_away": 1.0,
        },
        issued_at="2026-09-20T10:00:00+00:00",
    )
    insert_forecast(
        db,
        fixture_id=fixture_id,
        track="llm",
        model_version="glm:1",
        content_hash=f"llm-{fixture_id}",
        payload={
            "h": llm[0],
            "d": llm[1],
            "a": llm[2],
            "intel_ids": [],
            "intel_count": 0,
        },
        issued_at="2026-09-20T10:50:00+00:00",
    )
    if fused is not None:
        insert_forecast(
            db,
            fixture_id=fixture_id,
            track="fused",
            model_version="leap:w=0.5",
            content_hash=f"fu-{fixture_id}",
            payload={"h": fused[0], "d": fused[1], "a": fused[2], "method": "log_pool"},
            issued_at="2026-09-20T10:55:00+00:00",
        )
    upsert_draw_result(
        db,
        DrawResultInput(
            fixture_id=fixture_id,
            home_goals=home_goals,
            away_goals=away_goals,
            source="test",
            published_at="2026-09-20T22:00:00+00:00",
        ),
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date="2026-09-20",
            code=f"周日{fixture_id:03d}",
        ),
    )
    for sel, odds in zip(("h", "d", "a"), (2.4, 3.3, 2.9), strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture_id,
                market_code="had",
                selection_code=sel,
                source="odds_api:pin",
                odds=odds,
                captured_at="2026-09-20T09:00:00+00:00",
                observed_at="2026-09-20T09:00:00+00:00",
            ),
        )
    if intel:
        insert_intel_observation(
            db,
            fixture_id,
            IntelDraft(
                kind="伤停",
                text="主队伤停：x",
                source="okooo.com/formation",
                collected_at="2026-09-20T09:00:00+00:00",
                collector="okooo-formation",
                raw_payload={"a": 1},
            ),
        )


def test_paired_stats_and_tiers_small_sample(db: sqlite3.Connection) -> None:
    # fused 更准的一场 + ml 更准的一场
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=(0.45, 0.3, 0.25),
        home_goals=2,
        away_goals=0,
    )
    _seed_one(
        db,
        2,
        ml=(0.2, 0.3, 0.5),
        llm=(0.4, 0.3, 0.3),
        fused=(0.15, 0.25, 0.60),
        home_goals=0,
        away_goals=1,
    )
    paired = paired_fused_vs_ml(db).model_dump()
    assert paired["pairs"] == 2
    assert paired["fused_win_rate"] == 0.5  # 一胜一负
    report_model = m3_protocol_report(db)
    report = report_model.model_dump()
    assert report["tier_a"]["verdict"] == "未证明（样本不足）"  # 2 < 200
    assert report["tier_b"]["verdict"] == "未证明（样本不足）"
    assert report["tier_a"]["checks"]["pairs_ge_200"] is False
    assert "配对 2 场" in report_summary(report_model)


def test_tier_threshold_constants_frozen() -> None:
    # 票 05 冻结阈值——常量被意外改动时此测试失败
    assert TIER_A_MIN_PAIRS == 200


def test_intel_quality_columns(db: sqlite3.Connection) -> None:
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=1,
        away_goals=0,
        intel=True,
    )
    _seed_one(
        db,
        2,
        ml=(0.4, 0.3, 0.3),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=0,
        away_goals=1,
        intel=False,
    )
    q = intel_quality(db).model_dump()
    assert q["covered"] == 1
    assert q["coverage"] == 0.5
    assert q["median_hours_to_kickoff"] is not None
    assert q["median_hours_to_kickoff"] > 0
    assert q["avg_sources"] == 1.0


def test_blind_summary_in_report(db: sqlite3.Connection) -> None:
    _seed_one(
        db,
        1,
        ml=(0.5, 0.3, 0.2),
        llm=(0.3, 0.4, 0.3),
        fused=None,
        home_goals=1,
        away_goals=0,
    )
    record_blind_review(db, cycle="W1", fixture_id=1, choice="llm")
    report = m3_protocol_report(db).model_dump()
    assert report["blind_review"]["total"] == 1
    assert report["blind_review"]["llm_share"] == 1.0


# ---- 票 03：ECE 真算 + Tier B 方向核验（判词三态）----


def _coverage() -> ForwardCoverage:
    return ForwardCoverage(
        settled_fixtures=0,
        no_forecast=0,
        post_kickoff_only=0,
        no_market_baseline=0,
        scored=0,
    )


def _paired(
    *,
    pairs: int = 600,
    span_weeks: int = 32,
    dm_p: float = 0.001,
    mean_rps_delta: float = -0.012,
    dm_stat: float = -3.2,
) -> PairedFusedVsMl:
    return PairedFusedVsMl(
        pairs=pairs,
        fused_win_rate=0.55,
        mean_rps_delta=mean_rps_delta,
        dm_stat=dm_stat,
        dm_p=dm_p,
        span_weeks=span_weeks,
    )


def test_tier_b_three_verdicts_by_direction() -> None:
    """票 03 黄金样例：显著更好→达标候选；显著更差→方向反向；不显著→未达标。"""
    # 显著更好（delta<0 与 dm_stat<0 同向）+ 复核净 → 达标候选
    better = _tier_b(_paired(), review_clean=True)
    assert better.checks["fused_better_direction"] is True
    assert better.verdict == "达标候选（须用户显式裁决切换）"
    # 显著更差（DM 双侧只测有差异）→ 方向反向，不再误判达标候选
    worse = _tier_b(
        _paired(mean_rps_delta=0.012, dm_stat=3.2, dm_p=0.001), review_clean=True
    )
    assert worse.checks["dm_p_lt_005"] is True
    assert worse.checks["fused_better_direction"] is False
    assert worse.verdict == "方向反向（融合显著更差）——未达标"
    # 不显著（p=0.4）→ 未达标（方向反向仅在显著时单列）
    flat = _tier_b(
        _paired(dm_p=0.4, mean_rps_delta=-0.001, dm_stat=-0.8), review_clean=True
    )
    assert flat.verdict == "未达标"
    # 样本不足 → 未证明
    small = _tier_b(_paired(pairs=100), review_clean=True)
    assert small.verdict == "未证明（样本不足）"
    # correctness F2 钉行为：delta 缺失（现实不可达）但 dm_stat>0 显著 → 仍方向反向
    no_delta = _tier_b(
        PairedFusedVsMl(
            pairs=600,
            fused_win_rate=0.45,
            mean_rps_delta=None,
            dm_stat=3.2,
            dm_p=0.001,
            span_weeks=32,
        ),
        review_clean=True,
    )
    assert no_delta.verdict == "方向反向（融合显著更差）——未达标"


def test_tier_a_reachable_with_real_ece() -> None:
    """票 03：ECE 真算后 Tier A 判决可达（原桩恒 None → ece 检查恒 False）。"""
    paired = PairedFusedVsMl(
        pairs=250,
        fused_win_rate=0.55,
        mean_rps_delta=-0.01,
        dm_stat=-2.5,
        dm_p=0.01,
        span_weeks=8,
    )
    status = _tier_a(paired, ece_clean=True)
    assert status.verdict == "达标（融合线保留）"
    assert status.checks["ece_no_degradation"] is True


def test_mean_ece_pair_count_weighted() -> None:
    """票 03 人裁决口径：组内三键等权 → 组间按 n_fixtures 加权。"""
    report = ForwardSkillReport(
        rule="r",
        track="llm",
        coverage=_coverage(),
        groups={
            "a": ForwardGroupMetrics(
                n=100,
                n_fixtures=100,
                ece_h=0.1,
                ece_d=0.1,
                ece_a=0.1,
                insufficient_samples=False,
            ),
            "b": ForwardGroupMetrics(
                n=300,
                n_fixtures=300,
                ece_h=0.2,
                ece_d=0.2,
                ece_a=0.2,
                insufficient_samples=False,
            ),
            "zero": ForwardGroupMetrics(n=0, n_fixtures=0),
        },
    )
    # (100×0.1 + 300×0.2) / 400 = 0.175；零样本/样本不足组不计权
    assert _mean_ece(report) == pytest.approx(0.175)
    # correctness F1：两个各 25 场的不足组聚合到 50 场也不作通过依据
    split = ForwardSkillReport(
        rule="r",
        track="llm",
        coverage=_coverage(),
        groups={
            "glm:1": ForwardGroupMetrics(
                n=25, n_fixtures=25, ece_h=0.0, ece_d=0.0, ece_a=0.0
            ),
            "glm:2": ForwardGroupMetrics(
                n=25, n_fixtures=25, ece_h=0.0, ece_d=0.0, ece_a=0.0
            ),
        },
    )
    assert _mean_ece(split) is None
    assert (
        _mean_ece(
            ForwardSkillReport(rule="r", track="llm", coverage=_coverage(), groups={})
        )
        is None
    )


def test_ece_clean_computes_from_seeded_tracks(db: sqlite3.Connection) -> None:
    """票 03 集成：≥30 样本两轨可算（原桩恒 None）——同概率两轨 ECE 相等 → 无劣化。"""
    for fixture_id in range(1, 31):
        _seed_one(
            db,
            fixture_id,
            ml=(0.45, 0.3, 0.25),
            llm=(0.45, 0.3, 0.25),
            fused=None,
            home_goals=fixture_id % 3,  # 混合结果分布
            away_goals=(fixture_id + 1) % 3,
        )
    assert _ece_clean(db) is True


def test_span_weeks_mixed_generation_kickoffs() -> None:
    """审计批D #4：混代 kickoff（legacy naive + v2 aware）此前裸
    fromisoformat 在 max()-min() 直接 TypeError 炸整份报告；统一读口后
    按真时刻算跨度，不可解析样本不进跨度。"""
    from goalx_backend.evaluation.forward_validation import ForwardSample
    from goalx_backend.llm.m3_report import _span_weeks

    def sample(fid: int, kickoff: str) -> ForwardSample:
        return ForwardSample(
            fixture_id=fid,
            kickoff_utc=kickoff,
            model_version="v",
            issued_at="2026-01-01T00:00:00+00:00",
            had_probs={"h": 0.4, "d": 0.3, "a": 0.3},
            fair_probs={"h": 0.4, "d": 0.3, "a": 0.3},
            ftr="h",
            competition="E0",
            season="2526",
        )

    # naive 09-12 20:00 北京 = 12:00Z 与 aware 09-19 12:00Z 相差 7 天
    weeks = _span_weeks(
        [1, 2],
        {1: sample(1, "2026-09-12T20:00:00")},
        {2: sample(2, "2026-09-19T12:00:00+00:00")},
    )
    assert weeks == 1
    # 坏串样本跳过不炸（唯一可解析样本跨度 0）
    assert (
        _span_weeks(
            [1, 2],
            {1: sample(1, "garbage")},
            {2: sample(2, "2026-09-19T12:00:00+00:00")},
        )
        == 0
    )
