"""
市场 log-linear 融合层（model-iterate 票 02，spec S2）。

research/22 十年复验的定量兑现：p_final ∝ p_calib^α × p_fair^(1−α)
（log-linear，三向归一），α 由尾随窗内网格学习（log-score 目标，
步长 0.05）赛前冻结。era 分层（psc_proxy/trajectory 各自 α，窗内
该时代样本不足回落全局）与全局两档。

- **α=0 是合法解且必须显式报告**（模型权重全灭=诚实答案）；
- 融合不引入新信息源（只重分配模型/市场权重）；p_fair=引擎同链
  公平概率（era 正典）；
- walk-forward 协议同 K1 校准（窗内拟合、本周冻结、PIT：本周赛果
  不进拟合）；历史累积**融合前（校准后）**的三向——训练输入阶段
  与应用输入阶段一致（report-audit-20261027 票 01：旧口径在融合后
  历史上拟合、应用于融合前概率，输入空间错位已修）；
- 纯确定性（网格穷举无随机源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

import numpy as np

from goalx_backend.markets import SELECTIONS

# 融合词表（引擎校验与 CLI choices 皆从此取）
FUSION_MODES = ("off", "global", "era")
ALPHA_STEP = 0.05  # 网格步长；存活判据（α≥步长）同用
ALPHA_GRID = tuple(round(i * ALPHA_STEP, 2) for i in range(21))  # 0.00..1.00
MIN_ERA_SAMPLES = 30  # era 档窗内该时代最少样本（不足回落全局）
_EPS = 1e-15


class _HistoryEntry(NamedTuple):
    """一条融合学习历史：融合前（校准后）三向 / fair 三向 / 赛果 / era。"""

    applied: dict[str, float]
    fair: dict[str, float]
    ftr: str
    era: str


def fuse(
    model_probs: dict[str, float], fair_probs: dict[str, float], alpha: float
) -> dict[str, float]:
    """log-linear 融合：p ∝ p_model^α × p_fair^(1−α)，三向归一。"""
    log_model = {s: np.log(max(model_probs[s], _EPS)) for s in SELECTIONS}
    log_fair = {s: np.log(max(fair_probs[s], _EPS)) for s in SELECTIONS}
    raw = {
        s: float(np.exp(alpha * log_model[s] + (1.0 - alpha) * log_fair[s]))
        for s in SELECTIONS
    }
    total = sum(raw.values())
    if total <= 0.0 or not np.isfinite(total):
        return dict(fair_probs)  # 数值退化兜底=市场公平三向
    return {s: v / total for s, v in raw.items()}


def fit_alpha(entries: list[_HistoryEntry]) -> float:
    """尾随窗上网格 α 最大化平均 log-score（向量化穷举；并列取小 α）。"""
    if not entries:
        raise ValueError("融合拟合窗为空（引擎门槛前置，不可达路径 fail-loud）")
    logs_model = np.array(
        [[np.log(max(e.applied[s], _EPS)) for s in SELECTIONS] for e in entries]
    )
    logs_fair = np.array(
        [[np.log(max(e.fair[s], _EPS)) for s in SELECTIONS] for e in entries]
    )
    outcomes = np.array([SELECTIONS.index(e.ftr.lower()) for e in entries])
    grid = np.array(ALPHA_GRID)  # (A,)
    # log p_fused = log_softmax(α·log m + (1−α)·log f)，逐 α 一次向量化
    scaled = (
        logs_model[:, :, None] * grid[None, None, :]
        + logs_fair[:, :, None] * (1.0 - grid)[None, None, :]
    )  # (n, 3, A)
    scaled -= scaled.max(axis=1, keepdims=True)  # 数值稳定
    log_probs = scaled - np.log(np.exp(scaled).sum(axis=1, keepdims=True))  # (n,3,A)
    scores = log_probs[np.arange(len(outcomes)), outcomes, :].mean(axis=0)  # (A,)
    # 并列取首个=最小 α；先圆到 1e-12 防浮点噪声把 α=0.0 抬成 0.05
    return float(ALPHA_GRID[int(np.argmax(np.round(scores, 12)))])


@dataclass
class FusionState:
    """联赛作用域融合状态（K1 同款冻结协议）。"""

    mode: str
    window: int = 1000
    min_samples: int = 200
    history: list[_HistoryEntry] = field(default_factory=list)
    alpha_log: list[dict[str, object]] = field(default_factory=list)

    def _window(self) -> list[_HistoryEntry]:
        return self.history[-self.window :] if self.window > 0 else list(self.history)

    def fit_for_week(self, week_era: str) -> float | None:
        """
        热身充分 → 返回冻结 α（era 模式先按该时代窗，不足回落全局）。

        冷启动门槛按**全历史**长度（correctness F2：与校准同语义——
        联赛内历史不足即不融合，尾随窗只是拟合样本集）。
        """
        if len(self.history) < self.min_samples:
            return None
        window = self._window()
        if self.mode == "era":
            era_entries = [e for e in window if e.era == week_era]
            if len(era_entries) >= MIN_ERA_SAMPLES:
                return fit_alpha(era_entries)
        return fit_alpha(window)

    def record(
        self,
        applied_probs: dict[str, float],
        fair_probs: dict[str, float],
        ftr: str,
        era: str,
    ) -> None:
        """一场（融合前三向, fair, 赛果, era）入史（本周冻结完毕后调用）。"""
        self.history.append(_HistoryEntry(applied_probs, fair_probs, ftr, era))

    def log_week(self, entry: dict[str, object]) -> None:
        """周级 α 轨迹入账（run summary 落 alpha_log）。"""
        self.alpha_log.append(entry)


def alpha_survival(alpha_log: list[dict[str, object]]) -> dict[str, object]:
    """
    α 存活统计（run summary / 对照报告共用）。

    存活=α ≥ 一个网格步长（0.05）——模型权重被十年窗内 log-score 保留。
    含分布（直方图+分位）与分年轨迹（票面"最优 α 分布/按周/按 era"的
    有界落档形态；逐周全轨迹不进 summary——列表页会反复解析）。
    """
    if not alpha_log:
        return {"n_weeks": 0}
    alphas = [float(entry["alpha"]) for entry in alpha_log]  # type: ignore[arg-type]
    per_era: dict[str, dict[str, float]] = {}
    per_year: dict[str, dict[str, float]] = {}
    for entry in alpha_log:
        era = str(entry.get("era", "unknown"))
        # mixed 周=全局回退拟合（correctness F4），不冒充时代桶
        if era != "mixed":
            stats = per_era.setdefault(era, {"n": 0.0, "alpha_sum": 0.0})
            stats["n"] += 1
            stats["alpha_sum"] += float(entry["alpha"])  # type: ignore[arg-type]
        # week 形如 "2023-W40"（ISO 年-周）——按 ISO 年聚合
        week = str(entry.get("week") or "")
        year = week[:4] if week[:4].isdigit() else "unknown"
        year_stats = per_year.setdefault(year, {"n": 0.0, "alpha_sum": 0.0})
        year_stats["n"] += 1
        year_stats["alpha_sum"] += float(entry["alpha"])  # type: ignore[arg-type]
    array = np.array(alphas)
    histogram = {
        str(a): int((array == a).sum()) for a in ALPHA_GRID if (array == a).any()
    }
    return {
        "n_weeks": len(alphas),
        "alpha_mean": round(sum(alphas) / len(alphas), 4),
        "alpha_quantiles": {
            "p05": round(float(np.percentile(array, 5)), 4),
            "p50": round(float(np.percentile(array, 50)), 4),
            "p95": round(float(np.percentile(array, 95)), 4),
        },
        "alpha_histogram": histogram,
        "survival_rate": round(
            sum(1 for a in alphas if a >= ALPHA_STEP) / len(alphas), 4
        ),
        "per_era": {
            era: {"n": int(s["n"]), "alpha_mean": round(s["alpha_sum"] / s["n"], 4)}
            for era, s in sorted(per_era.items())
        },
        "per_year": {
            year: {"n": int(s["n"]), "alpha_mean": round(s["alpha_sum"] / s["n"], 4)}
            for year, s in sorted(per_year.items())
        },
    }
