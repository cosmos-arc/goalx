"""
进球类玩法（ttg/crs）概率推导（票 wb-04，ADR-0006 canonical 矩阵）。

口径（与词典 model-prob 词条一致，转写不简化）：

- 概率来源：比分矩阵（canonical 10×10）推导的玩法边际视图——ttg 八档
  （0..7+，「7+」归并尾部）与 crs 31 档（28 精确比分 + 胜/平/负其他）。
  同一场比赛的 ttg 与 crs 出自同一矩阵，隐含概率必然一致（这正是
  ADR-0006 选矩阵为 canonical 的理由）；单市场独立建模会破坏一致性，
  不允许。
- EV 口径：**模型概率 × 竞彩价 − 1**（"模型×竞彩价"）。进球类无欧赔
  共识可对照（The Odds API 足球仅 h2h，无 totals 精确盘），因此这里
  的 EV 与 had 玩法页的"欧共识 EV"**不同源**：had 信市场共识，进球类
  只有自家模型可依。模型前瞻 skill 尚未过线（见词典 skill 词条），
  该 EV 是研究对照的诊断量，不是机会信号。
- 模型覆盖：仅映射到 fd 历史底座的联赛（五大）在售场次有 Forecast；
  无 Forecast 的场次概率/EV 诚实置空，不伪造。
"""

from __future__ import annotations

from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.markets import CRS_SELECTIONS, GOALS_MARKETS, TTG_SELECTIONS
from goalx_backend.modelling.score_matrix import ScoreMatrix

__all__ = [
    "GOALS_MARKETS",
    "goals_probabilities",
    "goals_selection_grid",
    "goals_selection_rows",
]


def goals_selection_grid(market_code: str) -> tuple[str, ...]:
    """进球类玩法的官方选项网格（有序；ttg 八档 / crs 31 档）。"""
    if market_code == "ttg":
        return TTG_SELECTIONS
    if market_code == "crs":
        return CRS_SELECTIONS
    raise ValueError(f"非进球类玩法: {market_code}")


def goals_probabilities(matrix: ScoreMatrix, market_code: str) -> dict[str, float]:
    """比分矩阵 → 进球类玩法边际概率（ttg/crs 视图即时计算，不落库）。"""
    if market_code == "ttg":
        return matrix.ttg()
    if market_code == "crs":
        return matrix.crs()
    raise ValueError(f"非进球类玩法: {market_code}")


def goals_selection_rows(
    matrix: ScoreMatrix,
    market_code: str,
    odds: dict[str, float],
) -> list[dict[str, Any]]:
    """
    一个进球玩法在一个 as-of 的选项行列表（票 wb-04 端点载荷的领域核心）。

    每行 ``{"code", "odds", "probability", "ev"}``：竞彩价缺失的选项照常
    返回（odds/ev 为 None，页面诚实显示无报价）；矩阵概率恒在（无矩阵时
    调用方不应进入本函数——无 Forecast 的场次整行置空，见模块 docstring）。
    EV = 概率 × 竞彩价 − 1（模型×竞彩价口径，区别于 had 的欧共识口径）。
    """
    grid = goals_selection_grid(market_code)
    probs = goals_probabilities(matrix, market_code)
    rows: list[dict[str, Any]] = []
    for code in grid:
        price = odds.get(code)
        prob = probs.get(code, 0.0)
        rows.append(
            {
                "code": code,
                "odds": price,
                "probability": round(prob, 6),
                "ev": (
                    round(om.expected_value(prob, price), 6)
                    if price is not None and price > 1.0
                    else None
                ),
            }
        )
    return rows
