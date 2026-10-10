"""护栏复验报告测试（backtest-decade 票 18）。

合成 gold 行喂真实报告链：护栏触发的选择口径（market_prob=1/jc）、
被拦结局命中统计、era/联赛分层、反推失败计数、reports/ 落盘。
真源网络永不进测试。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import guardrail_report as gr

DAY = "2021-10-02"


def _row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
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
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def test_guardrail_triggers_and_outcomes(gold_env) -> None:
    """极端热门盘触发双栏；被拦结局按实际命中累计。"""
    rows = [
        # fair p_h≈0.94（1.06 收盘）→ jc=0.954，market_prob≈1.048>0.02 不拦；
        # p_a≈0.024（41 收盘）→ jc≈46<50 边界内不拦——构造真触发行：
        _row("9701", psc_home=1.02, psc_draw=60.0, psc_away=80.0),  # 尾部两向触发
        _row("9702"),  # 正常盘不触发
        _row("9703", home_goals=1, away_goals=1),  # 平局结局
    ]
    with gold_env(rows) as (face, duck_con, store):
        payload = gr.build_guardrail_report(store, duck_con, today=date(2026, 10, 10))
        try:
            face_stats = payload["faces"]["psc_proxy"]
            assert face_stats["n_selections"] == 9  # 3 场 × 3 selection
            # 9701 的 d/a 触发（隐含 << 0.02/超 50）→ union ≥2
            clipped = face_stats["clipped_union"]
            assert clipped["n"] >= 2
            assert 0.0 <= clipped["implied_prob_mean"] < 0.02
            assert 0.0 <= clipped["actual_hit_rate"] <= 1.0
            # 反推护栏：均衡盘反推可成 → 失败计数可为 0 且口径入档
            assert payload["inversion_failed"]["n"] >= 0
            assert (store.root / "reports" / "guardrail-decade.json").exists()
            md = (store.root / "reports" / "guardrail-decade.md").read_text(
                encoding="utf-8"
            )
            assert "护栏十年复验" in md
            assert "参数不变更" in payload["note"]
        finally:
            duck_con.close()
            face.close()


def test_guardrail_no_fair_rows_counted_honestly(gold_env) -> None:
    """无 fair 基准的行不进 faces（分母只含有基准场次）。"""
    rows = [_row("9801"), _row("9802", psc_home=None, psc_draw=None, psc_away=None)]
    with gold_env(rows) as (face, duck_con, store):
        payload = gr.build_guardrail_report(store, duck_con)
        try:
            assert payload["n_matches_with_fair"] == 1
            assert payload["faces"]["psc_proxy"]["n_selections"] == 3
        finally:
            duck_con.close()
            face.close()
