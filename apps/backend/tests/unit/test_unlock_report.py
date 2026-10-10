"""hhad/ttg 解锁判定报告测试（backtest-decade 票 15）。

合成 gold 行（含 AH/OU 收盘族）喂真实报告全链：水位去水数学、线约定
（cid8 正=主让）、半线过滤、Brier/校准对照、ttg 桶复检、reports/ 落盘。
真源网络永不进测试。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import unlock_report as ur

# era1 均衡盘（反推矩阵近中性 λ）+ AH/OU 半线与水位
ERA1_DAY = "2021-10-02"


def _row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{ERA1_DAY}T20:00:00"),
        "season": "2021-22",
        "home": "A",
        "away": "B",
        "home_goals": 2,
        "away_goals": 1,
        "stage": "常规轮",
        "era": gold_mod.ERA_PSC_PROXY,
        "admin_excluded": False,
        "psc_home": 2.0,
        "psc_draw": 3.4,
        "psc_away": 3.8,
        "close1x2_h": None,
        "close1x2_d": None,
        "close1x2_a": None,
        "close1x2_cons_h": None,
        "close1x2_cons_d": None,
        "close1x2_cons_a": None,
        "ah_close_line_med": 0.5,  # cid8 半线：主让半一
        "ah_close_home_water_med": 0.95,
        "ah_close_away_water_med": 0.95,
        "ou_close_line_med": 2.5,
        "ou_close_over_water_med": 0.90,
        "ou_close_under_water_med": 1.00,
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def test_malay_conversion_and_two_way_prob() -> None:
    assert ur.malay_decimal(0.95) == pytest.approx(1.95)
    # 等水位 → 0.5；偏水 → 概率随水位降
    assert ur.two_way_prob(0.95, 0.95) == pytest.approx(0.5)
    assert ur.two_way_prob(1.00, 0.90) < 0.5
    assert ur.two_way_prob(0.90, 1.00) > 0.5


def test_line_filters() -> None:
    # OU：x.0/x.5 干净半步长过，四分线不过
    assert ur._clean_half_line(2.5) == 2.5
    assert ur._clean_half_line(3.0) == 3.0
    assert ur._clean_half_line(2.75) is None
    assert ur._clean_half_line(None) is None
    # AH：仅半线（整数线有 push 退场）
    assert ur._half_only(0.5) == 0.5
    assert ur._half_only(1.0) is None
    assert ur._half_only(-0.75) is None


def test_build_report_faces_and_ttg(gold_env) -> None:
    """端到端：faces 对照/线约定/ttg 桶/reports 落盘。"""
    rows = [
        # 主让半一 + 赢盘（净胜 1>0.5）；总进球 3>2.5 → over
        _row("9001", home_goals=2, away_goals=1),
        # 同线不赢盘（1-1，净胜 0 < 0.5）；总进球 2 → under
        _row("9002", home_goals=1, away_goals=1),
        # AH 整数线（push 面）→ 不进 AH 样本；OU 四分线 → 不进 OU 样本
        _row(
            "9003",
            ah_close_line_med=1.0,
            ou_close_line_med=2.75,
        ),
        # 水位越带 → 双面都不进样本
        _row("9004", ah_close_home_water_med=4.0, ou_close_over_water_med=0.1),
        # era2 行（锚收盘，市场侧语义同链）
        _row(
            "9005",
            era=gold_mod.ERA_TRAJECTORY,
            season="2024-25",
            psc_home=None,
            psc_draw=None,
            psc_away=None,
            close1x2_h=1.95,
            close1x2_d=3.4,
            close1x2_a=3.9,
        ),
    ]
    with gold_env(rows) as (face, duck_con, store):
        payload = ur.build_unlock_report(store, duck_con, today=date(2026, 10, 10))
        try:
            assert payload["n_matches"] == 5
            assert payload["skipped"]["ah_no_half_line"] == 2  # 整数线 + 越带
            assert payload["skipped"]["ou_no_clean_line"] == 2  # 四分线 + 越带
            ou = payload["faces"]["ou"]["psc_proxy"]
            # era1 两场进 OU 面（9001 over / 9002 under）
            assert ou["n"] == 2
            assert ou["actual_rate"] == pytest.approx(0.5)
            assert ou["market"]["mean_prob"] != pytest.approx(
                ou["model_1x2_inverted"]["mean_prob"]
            )  # 市场侧与反推侧不同源可分
            ah = payload["faces"]["hhad"]["psc_proxy"]
            assert ah["n"] == 2  # 9001/9002（era1 两场半线健全）
            assert ah["actual_rate"] == pytest.approx(0.5)
            # era2 分层存在且市场侧数值有限（9005：2-1 让半赢盘、3 球大）
            era2_ah = payload["faces"]["hhad"]["trajectory"]
            assert era2_ah["n"] == 1
            assert era2_ah["actual_rate"] == pytest.approx(1.0)
            assert 0.0 <= era2_ah["market"]["mean_prob"] <= 1.0
            era2_ou = payload["faces"]["ou"]["trajectory"]
            assert era2_ou["n"] == 1
            assert era2_ou["actual_rate"] == pytest.approx(1.0)
            # ttg 桶：隐含与实际分布和为 1（era 主 scope）
            for era in ("psc_proxy",):
                assert abs(sum(payload["ttg_actual"][era].values()) - 1.0) < 0.01
                assert abs(sum(payload["ttg_implied"][era].values()) - 1.0) < 0.01
            # reports/ 双视图落盘
            reports = Path(store.root) / "reports"
            assert (reports / "unlock-hhad-ttg.json").exists()
            md = (reports / "unlock-hhad-ttg.md").read_text(encoding="utf-8")
            assert "hhad/ttg 解锁判定报告" in md
            assert "门=报告+用户点头" in md
            json.loads((reports / "unlock-hhad-ttg.json").read_text(encoding="utf-8"))
        finally:
            duck_con.close()
            face.close()


def test_ah_cover_convention_cid8_positive_home_gives(gold_env) -> None:
    """线约定回归：cid8 正线=主让——主让一球半（+1.5）下 1-0 是输盘。"""
    rows = [
        _row("9101", home_goals=1, away_goals=0, ah_close_line_med=1.5),  # 净胜1<1.5 输
        _row("9102", home_goals=3, away_goals=0, ah_close_line_med=1.5),  # 净胜3>1.5 赢
    ]
    with gold_env(rows) as (face, duck_con, store):
        payload = ur.build_unlock_report(store, duck_con)
        try:
            ah = payload["faces"]["hhad"]["psc_proxy"]
            assert ah["n"] == 2
            assert ah["actual_rate"] == pytest.approx(0.5)
            # 反推侧同约定：hhad(-1.5)["h"] < 0.5（让一球半主胜概率低于半）
            assert ah["model_1x2_inverted"]["mean_prob"] < 0.5
        finally:
            duck_con.close()
            face.close()
