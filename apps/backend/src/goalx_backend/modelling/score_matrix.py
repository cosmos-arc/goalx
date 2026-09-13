"""
比分矩阵（10×10）canonical 概率表示与全玩法推导视图（ADR 0006，票 24）。

矩阵是双线预测的统一输出格式；各玩法概率一律由矩阵推导为视图，而非独立
建模——这保证同一场比赛所有玩法的隐含概率一致（奖池覆盖优化与 LEAP 融合
都需要一致的联合分布）。

- ``matrix_from_lambdas``：Poisson×Poisson + Dixon-Coles 低比分 tau 修正，
  尾部截断后归一化。
- ``ScoreMatrix``：不可变矩阵 + 玩法推导视图（had/hhad/crs/ttg/hafu 与池票
  边际复用）。半全场按 λ 半场拆分（≈0.45λ，Dixon & Robinson 1998 比例
  近似）构造上/下半场两个独立 Poisson 矩阵联合求和，作为附属视图。
- ``combination_probability``：跨场条件独立组合概率（任9 连乘）。

选项编码与 settlements/migrations 的官方网格一致（crs 28 精确比分 + 三档
「其他」，ttg 0..7+ 八档）。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import exp, factorial

from goalx_backend.markets import CRS_EXACT_SCORES

MATRIX_SIZE = 10  # canonical 网格：比分 0..9
HALF_SPLIT = 0.45  # 半场 λ 拆分比例（文献一致区间 0.44-0.46 取中）
Grid = tuple[tuple[float, ...], ...]

_CRS_EXACT_SET = frozenset(CRS_EXACT_SCORES)
_WDL_CODES = ("h", "d", "a")
_POOL_WDL = {"h": "3", "d": "1", "a": "0"}


def poisson_pmf(k: int, lam: float) -> float:
    """Poisson 概率质量 ``P(X=k; lam)``（纯 stdlib，矩阵构建热路径）。"""
    return exp(-lam) * lam**k / factorial(k)


def _dc_tau(h: int, a: int, lam_home: float, lam_away: float, rho: float) -> float:
    """Dixon-Coles 低比分相关性修正（仅作用于 0/1 比分格）。"""
    if h == 0 and a == 0:
        return 1.0 - lam_home * lam_away * rho
    if h == 0 and a == 1:
        return 1.0 + lam_home * rho
    if h == 1 and a == 0:
        return 1.0 + lam_away * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def matrix_from_lambdas(
    lam_home: float,
    lam_away: float,
    *,
    rho: float = 0.0,
    size: int = MATRIX_SIZE,
) -> Grid:
    """
    从 (λ_home, λ_away, ρ) 构造比分概率矩阵（行=主队进球，列=客队进球）。

    DC tau 修正仅调 0-0/1-0/0-1/1-1 四格；尾部按 ``size`` 截断后归一化，
    保证矩阵概率和为 1（票 24 验收）。
    """
    if lam_home <= 0 or lam_away <= 0:
        raise ValueError(f"λ 必须为正: {lam_home}, {lam_away}")
    home_pmf = [poisson_pmf(k, lam_home) for k in range(size)]
    away_pmf = [poisson_pmf(k, lam_away) for k in range(size)]
    grid: list[tuple[float, ...]] = []
    total = 0.0
    for h in range(size):
        row: list[float] = []
        for a in range(size):
            cell = home_pmf[h] * away_pmf[a] * _dc_tau(h, a, lam_home, lam_away, rho)
            if cell < 0.0:
                raise ValueError(
                    f"tau 修正产生负概率(rho={rho} 超出合理范围, 格 {h}:{a})"
                )
            row.append(cell)
            total += cell
        grid.append(tuple(row))
    if total <= 0.0:
        raise ValueError("矩阵全零, 无法归一化")
    return tuple(tuple(cell / total for cell in row) for row in grid)


def _outcome_sign(h: int, a: int) -> str:
    """胜平负符号（接受让球线平移后的比较）。"""
    if h > a:
        return "h"
    if h < a:
        return "a"
    return "d"


class ScoreMatrix:
    """一场比赛的 canonical 10×10 比分概率矩阵与全玩法推导视图。"""

    def __init__(self, grid: Grid, *, lam_home: float, lam_away: float) -> None:
        """Wrap a grid produced by :func:`matrix_from_lambdas`."""
        self.grid = grid
        self.lam_home = lam_home
        self.lam_away = lam_away
        self._size = len(grid)

    @classmethod
    def from_lambdas(
        cls,
        lam_home: float,
        lam_away: float,
        *,
        rho: float = 0.0,
        size: int = MATRIX_SIZE,
    ) -> ScoreMatrix:
        """Construct from Poisson intensities (see :func:`matrix_from_lambdas`)."""
        return cls(
            matrix_from_lambdas(lam_home, lam_away, rho=rho, size=size),
            lam_home=lam_home,
            lam_away=lam_away,
        )

    def cell(self, home: int, away: int) -> float:
        """P(主队进 home 球, 客队进 away 球)。"""
        if home >= self._size or away >= self._size:
            return 0.0
        return self.grid[home][away]

    # --- 固定赔率玩法视图 ---

    def had(self) -> dict[str, float]:
        """胜平负三格聚合。"""
        probs: dict[str, float] = dict.fromkeys(_WDL_CODES, 0.0)
        for h in range(self._size):
            for a in range(self._size):
                probs[_outcome_sign(h, a)] += self.grid[h][a]
        return probs

    def hhad(self, goal_line: float) -> dict[str, float]:
        """整数让球线三向（h/d/a，无 push；goal_line 为主队让球数，-1 即让 1 球）。"""
        probs: dict[str, float] = dict.fromkeys(_WDL_CODES, 0.0)
        for h in range(self._size):
            for a in range(self._size):
                probs[_outcome_sign(int(h + goal_line), a)] += self.grid[h][a]
        return probs

    def crs(self) -> dict[str, float]:
        """精确比分视图：官方 28 精确格 + 胜/平/负其他三档。"""
        probs: dict[str, float] = {f"{h}:{a}": 0.0 for h, a in CRS_EXACT_SCORES}
        probs |= dict.fromkeys(("h_other", "d_other", "a_other"), 0.0)
        for h in range(self._size):
            for a in range(self._size):
                code = (
                    f"{h}:{a}"
                    if (h, a) in _CRS_EXACT_SET
                    else (f"{_outcome_sign(h, a)}_other")
                )
                probs[code] += self.grid[h][a]
        return probs

    def ttg(self) -> dict[str, float]:
        """总进球八档视图（0..7+，「7+」归并尾部）。"""
        probs: dict[str, float] = {str(n): 0.0 for n in range(8)}
        for h in range(self._size):
            for a in range(self._size):
                probs[str(min(h + a, 7))] += self.grid[h][a]
        return probs

    def hafu(self, *, half_split: float = HALF_SPLIT) -> dict[str, float]:
        """
        半全场九格视图（半场结果×全场结果）。

        上/下半场按 λ 拆分比例各自独立 Poisson（附属近似视图，ADR 0006）：
        P(HT=X, FT=Y) = Σ P_half(h1,a1)·P_second(h2,a2)，其中 sign(h1,a1)=X
        且 sign(h1+h2, a1+a2)=Y。
        """
        half = matrix_from_lambdas(
            self.lam_home * half_split, self.lam_away * half_split, size=self._size
        )
        second = matrix_from_lambdas(
            self.lam_home * (1.0 - half_split),
            self.lam_away * (1.0 - half_split),
            size=self._size,
        )
        probs: dict[str, float] = {
            f"{x}{y}": 0.0 for x in _WDL_CODES for y in _WDL_CODES
        }
        size = self._size
        for h1 in range(size):
            for a1 in range(size):
                half_code = _outcome_sign(h1, a1)
                for h2 in range(size):
                    for a2 in range(size):
                        full_code = _outcome_sign(h1 + h2, a1 + a2)
                        probs[f"{half_code}{full_code}"] += (
                            half[h1][a1] * second[h2][a2]
                        )
        return probs

    # --- 池票边际（票 24：ttt14/pick9 三值；goals4/htft6 复用 crs/hafu）---

    def pool_wdl(self) -> dict[str, float]:
        """任9/14 场单场三值边际（官方编码 3=胜 1=平 0=负）。"""
        had = self.had()
        return {_POOL_WDL[code]: prob for code, prob in had.items()}

    def pool_margin(
        self, market_code: str, *, goal_line: float = 0.0
    ) -> dict[str, float]:
        """池票玩法的单场边际分布（goals4 复用 crs、htft6 复用 hafu）。"""
        if market_code in ("ttt14", "pick9"):
            return self.pool_wdl()
        if market_code == "goals4":
            return self.crs()
        if market_code == "htft6":
            return self.hafu()
        if market_code == "hhad":
            return self.hhad(goal_line)
        raise ValueError(f"未知池票/玩法边际: {market_code}")


def combination_probability(
    picks: Mapping[str, str], marginals: Mapping[str, Mapping[str, float]]
) -> float:
    """
    跨场条件独立组合概率（任9 连乘）。

    ``picks`` 把每场（match key）映射到所选选项编码；``marginals`` 提供每场
    的单场边际分布。任一场的选项不在边际内时概率记 0（组合不可能命中）。
    """
    prob = 1.0
    for match_key, selection in picks.items():
        prob *= marginals[match_key].get(selection, 0.0)
    return prob
