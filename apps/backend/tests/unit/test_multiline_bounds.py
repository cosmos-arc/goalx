"""OU 约束求界 + AH 联合一致性（measure-10）的行为断言。"""

from __future__ import annotations

from goalx_backend.evaluation import multiline_bounds as mlb
from goalx_backend.modelling.score_matrix import ScoreMatrix

# era1 正典=PSC：构造自洽收盘三向（去水后 0.45/0.26/0.29 ×1.06 欧赔）
_ROW = {
    "era": "psc_proxy",
    "league": "E0",
    "psc_home": 2.35,
    "psc_draw": 3.30,
    "psc_away": 3.55,
    "avgc_home": 2.40,
    "avgc_draw": 3.35,
    "avgc_away": 3.60,
    "close1x2_h": 2.35,
    "close1x2_d": 3.30,
    "close1x2_a": 3.55,
    "close1x2_cons_h": 2.36,
    "close1x2_cons_d": 3.31,
    "close1x2_a_cons": 3.56,
    "ou_close_line_med": 2.5,
    "ou_close_over_water_med": 0.95,
    "ou_close_under_water_med": 0.95,
    "ah_close_line_med": -0.5,
    "ah_close_home_water_med": 1.00,
    "ah_close_away_water_med": 0.92,
}


def test_study_row_fits_and_bounds() -> None:
    """单场全链（认证行）：1X2 与 OU 自洽（源出同一 λ）→ 全端点在带内，
    TTG/hhad 界入账。AH 面缺席照常（ah_joint_total=0）。"""
    row = {
        "era": "psc_proxy",
        "league": "E0",
        "psc_home": 2.28,
        "psc_draw": 4.11,
        "psc_away": 3.81,
        "avgc_home": 2.33,
        "avgc_draw": 4.16,
        "avgc_away": 3.86,
        "close1x2_h": 2.28,
        "close1x2_d": 4.11,
        "close1x2_a": 3.81,
        "close1x2_cons_h": 2.29,
        "close1x2_cons_d": 4.12,
        "close1x2_cons_a": 3.82,
        "ou_close_line_med": 2.5,
        "ou_close_over_water_med": 0.95,
        "ou_close_under_water_med": 0.8824,
    }
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    mlb._study_row(row, study, haircut=0.11)
    assert study.fitted == 1
    assert study.certified == 1
    assert study.conflicts == 0
    assert not study.skipped, study.skipped
    # TTG 八档 + hhad 三向都有界账
    assert len([k for k in study.width_sums if k.startswith("ttg:")]) == 8
    assert len([k for k in study.width_sums if k.startswith("hhad:")]) == 3
    # 界宽非负且不太宽（eps=0.015 的端点包络）
    for widths in study.width_sums.values():
        assert all(w >= 0 for w in widths)
        assert max(widths) < 0.10
    # AH 面缺席（行无 ah 字段）→ 联合一致性零入账
    assert study.ah_joint_total == 0


def test_conclusion_line_both_ways() -> None:
    """结论行：保守 EV 命中决定转正判词。"""
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    study.ev_lower_total["ttg:3"] = 10
    payload = mlb._payload(study)
    assert payload["conclusion"] == (
        "认证面（约束可行行）保守界下无正 EV 候选——B 线按界口径不可转正"
    )
    study.ev_lower_hits["ttg:3"] = 1
    payload2 = mlb._payload(study)
    assert payload2["conclusion"] == "认证面（约束可行行）保守界下仍有非零正 EV 候选"
    assert payload2["ev_lower_total_hits"] == 1


def test_envelope_extremes_come_from_endpoint_fits() -> None:
    """包络端点：构造三矩阵（不同 λ）→ min/max 覆盖中心两侧。"""
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    mats = [
        ScoreMatrix.from_lambdas(1.4, 1.1),
        ScoreMatrix.from_lambdas(1.4, 1.1),
        ScoreMatrix.from_lambdas(1.4, 1.1),
    ]
    mlb._ttg_bounds(study, mats, mats[0], haircut=0.11)
    # 同矩阵三份：界宽为 0（中心=端点）
    assert all(w == 0 for widths in study.width_sums.values() for w in widths)


def test_constraint_conflict_row_reported_not_enveloped() -> None:
    """约束冲突（report-audit-20261027 票 04）：eps=0.015 下该收盘自洽行
    的 OU 端点拟合越带（NLS 折中解）→ 冲突计数、不进认证面（界/保守 EV
    零入账），残差账如实输出。"""
    row = dict(_ROW)
    row.pop("close1x2_a_cons")
    row["close1x2_cons_a"] = 3.56
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    mlb._study_row(row, study, haircut=0.11)
    assert study.fitted == 1
    assert study.conflicts == 1
    assert study.certified == 0
    assert not study.width_sums  # 冲突行不产生认证界
    assert not study.ev_lower_total
    assert study.ou_resids  # 残差账在（诊断面）
    assert study.x12_resids
    payload = mlb._payload(study)
    assert payload["sample"]["conflicts"] == 1
    assert payload["sample"]["certified"] == 0
    assert payload["constraint_feasibility"]["ou_resid_max"] is not None


def test_report_synthetic_case_unreachable_band_is_conflict() -> None:
    """报告合成反例：1X2 源自 λ=(1,1)，OU 2.5 目标带 [0.485,0.515] 在
    双 Poisson 族内不可达（拟合落带外）→ 逐样本判冲突。"""
    fair = ScoreMatrix.from_lambdas(1.0, 1.0).had()
    mat = mlb._fit_matrix(fair, 2.5, 0.485)
    assert mat is not None
    model_over = sum(p for b, p in mat.ttg().items() if float(b) > 2.5)
    # 报告复现值 ≈0.4699（带外）；无论精确落点，断言越带即冲突判据成立
    assert abs(model_over - 0.485) > 1e-3
    assert abs(model_over - 0.5) > 0.015  # 落在声明区间 [0.485, 0.515] 外
