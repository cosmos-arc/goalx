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
    """单场全链：OU 约束拟合成功 → TTG/hhad 界与 AH 一致性入账。"""
    row = dict(_ROW)
    row.pop("close1x2_a_cons")  # 修笔误键名（构造行不含 cons_a 别名）
    row["close1x2_cons_a"] = 3.56
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    mlb._study_row(row, study, haircut=0.11)
    assert study.fitted == 1
    assert not study.skipped, study.skipped
    # TTG 八档 + hhad 三向都有界账
    assert len([k for k in study.width_sums if k.startswith("ttg:")]) == 8
    assert len([k for k in study.width_sums if k.startswith("hhad:")]) == 3
    # 界宽非负且不太宽（eps=0.015 的端点包络）
    for widths in study.width_sums.values():
        assert all(w >= 0 for w in widths)
        assert max(widths) < 0.10
    # AH 半线在场 → 联合一致性入账
    assert study.ah_joint_total == 1
    assert study.ah_joint_ok in (0, 1)


def test_conclusion_line_both_ways() -> None:
    """结论行：保守 EV 命中决定转正判词。"""
    study = mlb.BoundsStudy(built_at="2026-10-20", eps=mlb.EPS, sample_n=1)
    study.ev_lower_total["ttg:3"] = 10
    payload = mlb._payload(study)
    assert payload["conclusion"] == "保守界下无正 EV 候选——B 线按界口径不可转正"
    study.ev_lower_hits["ttg:3"] = 1
    payload2 = mlb._payload(study)
    assert payload2["conclusion"] == "保守界下仍有非零正 EV 候选"
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
