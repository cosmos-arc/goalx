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
  不进拟合）；历史累积**融合后已应用**的三向（α 在模型实际输出含
  上游校准上学习，校准变则 α 自适应）；
- 纯确定性（网格穷举无随机源）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from goalx_backend.markets import SELECTIONS

# 融合词表（引擎校验与 CLI choices 皆从此取）
FUSION_MODES = ("off", "global", "era")
ALPHA_STEP = 0.05  # 网格步长；存活判据（α≥步长）同用
ALPHA_GRID = tuple(round(i * ALPHA_STEP, 2) for i in range(21))  # 0.00..1.00
MIN_ERA_SAMPLES = 30  # era 档窗内该时代最少样本（不足回落全局）
MIN_FUSION_SAMPLES = 2  # 拟合最少样本
_EPS = 1e-15


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


def fit_alpha(
    samples: list[tuple[dict[str, float], dict[str, float], str]],
) -> float:
    """尾随窗上网格 α 最大化平均 log-score（向量化穷举；并列取小 α）。"""
    if len(samples) < MIN_FUSION_SAMPLES:
        return 0.0
    logs_model = np.array(
        [[np.log(max(m[s], _EPS)) for s in SELECTIONS] for m, _, _ in samples]
    )
    logs_fair = np.array(
        [[np.log(max(f[s], _EPS)) for s in SELECTIONS] for _, f, _ in samples]
    )
    outcomes = np.array([SELECTIONS.index(r.lower()) for _, _, r in samples])
    grid = np.array(ALPHA_GRID)  # (A,)
    # log p_fused = log_softmax(α·log m + (1−α)·log f)，逐 α 一次向量化
    scaled = (
        logs_model[:, :, None] * grid[None, None, :]
        + logs_fair[:, :, None] * (1.0 - grid)[None, None, :]
    )  # (n, 3, A)
    scaled -= scaled.max(axis=1, keepdims=True)  # 数值稳定
    log_probs = scaled - np.log(np.exp(scaled).sum(axis=1, keepdims=True))  # (n,3,A)
    scores = log_probs[np.arange(len(outcomes)), outcomes, :].mean(axis=0)  # (A,)
    return float(ALPHA_GRID[int(np.argmax(scores))])  # 并列取首个=最小 α


@dataclass
class FusionState:
    """
    联赛作用域融合状态（K1 同款冻结协议）。

    历史条目 =（融合后已应用三向, fair 三向, 赛果, era）。
    """

    mode: str
    window: int = 1000
    min_samples: int = 200
    history: list[tuple[dict[str, float], dict[str, float], str, str]] = field(
        default_factory=list
    )
    alpha_log: list[dict[str, object]] = field(default_factory=list)

    def _window(self) -> list[tuple[dict[str, float], dict[str, float], str, str]]:
        return self.history[-self.window :] if self.window > 0 else list(self.history)

    def fit_for_week(self, week_era: str) -> float | None:
        """热身充分 → 返回冻结 α（era 模式先按该时代窗，不足回落全局）。"""
        window = self._window()
        if len(window) < self.min_samples:
            return None
        if self.mode == "era":
            era_samples = [s for s in window if s[3] == week_era]
            if len(era_samples) >= MIN_ERA_SAMPLES:
                return fit_alpha([(m, f, r) for m, f, r, _ in era_samples])
        return fit_alpha([(m, f, r) for m, f, r, _ in window])

    def record(
        self,
        applied_probs: dict[str, float],
        fair_probs: dict[str, float],
        ftr: str,
        era: str,
    ) -> None:
        """一场（融合后三向, fair, 赛果, era）入史（本周冻结完毕后调用）。"""
        self.history.append((applied_probs, fair_probs, ftr, era))

    def log_week(self, entry: dict[str, object]) -> None:
        """周级 α 轨迹入账（run summary 落 alpha_log）。"""
        self.alpha_log.append(entry)


def alpha_survival(alpha_log: list[dict[str, object]]) -> dict[str, object]:
    """
    α 存活统计（run summary / 对照报告共用）。

    存活=α ≥ 一个网格步长（0.05）——模型权重被十年窗内 log-score 保留。
    """
    if not alpha_log:
        return {"n_weeks": 0}
    alphas = [float(entry["alpha"]) for entry in alpha_log]  # type: ignore[arg-type]
    per_era: dict[str, dict[str, float]] = {}
    for entry in alpha_log:
        era = str(entry.get("era", "unknown"))
        stats = per_era.setdefault(era, {"n": 0.0, "alpha_sum": 0.0})
        stats["n"] += 1
        stats["alpha_sum"] += float(entry["alpha"])  # type: ignore[arg-type]
    return {
        "n_weeks": len(alphas),
        "alpha_mean": round(sum(alphas) / len(alphas), 4),
        "alpha_median": round(float(np.median(alphas)), 4),
        "survival_rate": round(
            sum(1 for a in alphas if a >= ALPHA_STEP) / len(alphas), 4
        ),
        "per_era": {
            era: {"n": int(s["n"]), "alpha_mean": round(s["alpha_sum"] / s["n"], 4)}
            for era, s in sorted(per_era.items())
        },
    }
