"""开→收漂移复验票 54 测试：口径/价值组/实测 ROI/确定性。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from goalx_backend.data import results as rs_store
from goalx_backend.evaluation import drift_replay

# 赔率组合经 Shin 实算选定（期望值注释为实算结果）：
# ROW_VALUE 'a' 早 6.0 收 4.0：EV_early=+0.632 / EV_close=−0.021 / ftr=A 命中
# ROW_MISS  'd' 早 5.5 收 4.8：EV_early=+0.070 / ftr=H 未中（实测亏损样本）
# ROW_FLAT  早收同价 → 漂移 0，不入价值组
_ROWS: list[dict[str, Any]] = [
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-10",
        "home_team": "Man United",
        "away_team": "Chelsea",
        "fthg": 0,
        "ftag": 2,
        "ftr": "A",
        "psc_home": 2.20,
        "psc_draw": 3.50,
        "psc_away": 3.60,
        "psh_home": 2.20,
        "psh_draw": 3.50,
        "psh_away": 6.00,
        "avgc_home": 2.15,
        "avgc_draw": 3.60,
        "avgc_away": 3.70,
    },
    {
        "competition": "E0",
        "season": "2425",
        "match_date": "2025-05-11",
        "home_team": "Arsenal",
        "away_team": "Everton",
        "fthg": 2,
        "ftag": 1,
        "ftr": "H",
        "psc_home": 1.80,
        "psc_draw": 3.60,
        "psc_away": 5.00,
        "psh_home": 1.80,
        "psh_draw": 3.60,
        "psh_away": 5.00,
        "avgc_home": 1.75,
        "avgc_draw": 3.70,
        "avgc_away": 5.20,
    },
    {
        "competition": "E0",
        "season": "2324",
        "match_date": "2024-05-19",
        "home_team": "Man City",
        "away_team": "West Ham",
        "fthg": 1,
        "ftag": 0,
        "ftr": "H",
        "psc_home": 1.42,
        "psc_draw": 4.80,
        "psc_away": 7.50,
        "psh_home": 1.35,
        "psh_draw": 5.50,
        "psh_away": 8.50,
        "avgc_home": 1.44,
        "avgc_draw": 4.60,
        "avgc_away": 7.70,
    },
    # PSC 缺（2627 形态）：入 no_psc，不产漂移行
    {
        "competition": "E0",
        "season": "2627",
        "match_date": "2026-09-20",
        "home_team": "Leeds",
        "away_team": "Liverpool",
        "fthg": 0,
        "ftag": 3,
        "ftr": "A",
        "psc_home": None,
        "psc_draw": None,
        "psc_away": None,
        "psh_home": 4.00,
        "psh_draw": 3.80,
        "psh_away": 1.90,
        "avgc_home": 4.20,
        "avgc_draw": 3.90,
        "avgc_away": 1.85,
    },
]


def _seed(db: sqlite3.Connection) -> None:
    assert rs_store.upsert_hist_matches(db, _ROWS) == 4


def test_collect_rows_coverage_and_value_signs(db) -> None:
    _seed(db)
    rows, coverage = drift_replay.collect_drift_rows(
        db, competitions=("E0",), seasons=("2324", "2425", "2627")
    )
    assert coverage == {
        "rows": 4,
        "out_of_scope": 0,
        "no_psc": 1,  # 2627 行无收盘基准
        "no_psh": 0,
        "usable": 3,
    }
    assert len(rows) == 9  # 3 场 × 三向
    value_a = next(r for r in rows if r.odds_early == 6.0)
    assert value_a.ev_early > 0.6  # 早窗价值（收盘公允评早价，实算 +0.632）
    assert -0.05 < value_a.ev_close < 0  # 收盘同书自评 = Shin 水位
    assert value_a.hit is True  # ftr=A → 实测命中


def test_value_cohort_evaporation_and_realized(db) -> None:
    _seed(db)
    report = drift_replay.drift_replay_report(
        db, competitions=("E0",), seasons=("2324", "2425", "2627")
    )
    cohort = report["value_cohorts"]["ev_early_gt_0.05"]
    assert cohort["n"] == 2  # VALUE 'a'(+0.632) + MISS 'd'(+0.070)
    assert cohort["ev_early_mean"] > 0.3
    # 收盘组均值转负 → 蒸发比 >1（全部蒸发并倒贴水位，Buchdahl 方向更强）
    assert cohort["evaporation_ratio"] > 1.0
    assert cohort["steamed_rate"] == 1.0  # 两笔早价都被 steamed
    # 实测：命中 6.0 派彩 + 未中 −1 → ROI_early = (5−1)/2 = +2.0
    assert cohort["roi_early_realized"] == 2.0
    # 等待口径：命中那笔按收价 3.60 → (2.6−1)/2 = +0.8 —— 该样本早锁优于等待
    assert cohort["roi_close_realized"] == 0.8
    # 阈值 0 组多含 MISS 'a'（+0.024）
    assert report["value_cohorts"]["ev_early_gt_0.00"]["n"] == 3


def test_report_shape_and_determinism(db) -> None:
    _seed(db)
    report = drift_replay.drift_replay_report(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    assert set(report) >= {
        "params",
        "caliber",
        "coverage",
        "overall",
        "by_selection",
        "by_season",
        "value_cohorts",
    }
    assert set(report["by_selection"]) == {"h", "d", "a"}
    assert set(report["by_season"]) == {"2324", "2425"}
    assert report["overall"]["steamed_rate"] is not None
    # 全选实测基线：9 注 3 中（VALUE'a'@6.0 + FLAT'h'@1.80 + MISS'h'@1.35）
    assert report["overall"]["roi_early_realized_all"] == round(
        (5 + 0.8 + 0.35 - 6) / 9, 4
    )  # = 0.0167
    again = drift_replay.drift_replay_report(
        db, competitions=("E0",), seasons=("2324", "2425")
    )
    assert json.dumps(again) == json.dumps(report)
