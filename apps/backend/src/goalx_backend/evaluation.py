"""
评估与指标集（票 29）。

预测侧（RPS/Brier/log loss/ECE/skill + DM 检验）与下注侧（flat-stake
ROI + t 统计量），分层汇总落 backtest_metrics：

- RPS 为 1X2 主指标（Constantinou & Fenton 2012；排序意识），实现与
  penaltyblog ``rps_average`` 交叉验证；
- skill = 1 − L_model/L_market，L_market 用同指标的 fair 基准（Shin 收盘）；
  这是唯一有意义的通过线（skill ≥ 0，研究 03 §6）；
- RPS 差配对 Diebold-Mariano（HAC lag=1，双侧正态 p 值）；
- ECE 按结果类别分别分桶（draw 桶单独看，研究 03 §6）。
"""

# scipy/penaltyblog 的类型存根不完整，以下规则的第三方 unknown 在本文件放宽。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from typing import Any

from scipy.stats import norm

SELECTIONS = ("h", "d", "a")
ECE_BINS = 10
MIN_DM_SAMPLES = 3  # DM 检验最少样本
MIN_TSTAT_SAMPLES = 2  # t 统计量最少样本


def rps(probs: Sequence[float], outcome_index: int) -> float:
    """单场 ranked probability score（有序三分类，1/2 Σ(F−O)²）。"""
    cum_p, cum_o, total = 0.0, 0.0, 0.0
    for i, p in enumerate(probs):
        cum_p += p
        cum_o += 1.0 if i == outcome_index else 0.0
        if i < len(probs) - 1:
            total += (cum_p - cum_o) ** 2
    return total / (len(probs) - 1)


def multiclass_brier(probs: Sequence[float], outcome_index: int) -> float:
    """多分类 Brier：Σ(p_i − o_i)²。"""
    return sum(
        (p - (1.0 if i == outcome_index else 0.0)) ** 2 for i, p in enumerate(probs)
    )


def log_loss(probs: Sequence[float], outcome_index: int) -> float:
    """对数损失（尾部概率敏感，作第二指标）。"""
    p = max(probs[outcome_index], 1e-15)
    return -math.log(p)


def ece_per_class(
    prob_vectors: Sequence[Sequence[float]],
    outcome_indices: Sequence[int],
    *,
    n_bins: int = ECE_BINS,
) -> list[float]:
    """每类的期望校准误差（按预测概率分桶 |准确率−置信度| 加权平均）。"""
    n_classes = len(prob_vectors[0]) if prob_vectors else 0
    scores: list[float] = []
    for c in range(n_classes):
        # bucket → ([该桶各样本的预测概率], [该桶各样本的实际指示])
        buckets: dict[int, tuple[list[float], list[float]]] = {}
        for probs, outcome in zip(prob_vectors, outcome_indices, strict=True):
            bucket = min(int(probs[c] * n_bins), n_bins - 1)
            probs_list, hits = buckets.setdefault(bucket, ([], []))
            probs_list.append(probs[c])
            hits.append(1.0 if outcome == c else 0.0)
        total_n = 0
        acc_sum = 0.0
        for probs_list, hits in buckets.values():
            count = len(hits)
            accuracy = sum(hits) / count
            confidence = sum(probs_list) / count
            acc_sum += count * abs(accuracy - confidence)
            total_n += count
        scores.append(acc_sum / total_n if total_n else 0.0)
    return scores


def skill_score(loss_model: float, loss_market: float) -> float:
    """对市场 skill = 1 − L_model/L_market（市场基准不可比时返回 0）。"""
    if loss_market <= 0:
        return 0.0
    return 1.0 - loss_model / loss_market


def diebold_mariano(
    loss_diffs: Sequence[float], *, lag: int = 1
) -> tuple[float, float]:
    """
    配对 Diebold-Mariano 检验（HAC/Newey-West 方差，双侧正态 p 值）。

    ``loss_diffs`` = 每场 L_model − L_market；返回 (dm 统计量, p 值)。
    样本 < 3 或方差退化时返回 (0.0, 1.0)。
    """
    n = len(loss_diffs)
    if n < MIN_DM_SAMPLES:
        return 0.0, 1.0
    mean = sum(loss_diffs) / n
    gamma0 = sum((d - mean) ** 2 for d in loss_diffs) / n
    variance = gamma0
    for lag_index in range(1, min(lag, n - 1) + 1):
        gamma_l = (
            sum(
                (loss_diffs[t] - mean) * (loss_diffs[t - lag_index] - mean)
                for t in range(lag_index, n)
            )
            / n
        )
        variance += 2.0 * gamma_l
    if variance <= 0:
        return 0.0, 1.0
    dm = mean / math.sqrt(variance / n)
    p_value = 2.0 * float(norm.sf(abs(dm)))
    return dm, p_value


def flat_stake_stats(
    profits: Sequence[float], stakes: Sequence[float]
) -> dict[str, float]:
    """flat-stake ROI 与 t 统计量（单注收益率序列的 t 检验）。"""
    n = len(profits)
    if n == 0 or not stakes:
        return {"n": 0, "roi": 0.0, "t_stat": 0.0}
    returns = [p / s for p, s in zip(profits, stakes, strict=True) if s > 0]
    n = len(returns)
    if n == 0:
        return {"n": 0, "roi": 0.0, "t_stat": 0.0}
    mean = sum(returns) / n
    if n < MIN_TSTAT_SAMPLES:
        return {"n": n, "roi": mean, "t_stat": 0.0}
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance)
    t_stat = mean / (std / math.sqrt(n)) if std > 0 else 0.0
    return {"n": n, "roi": mean, "t_stat": t_stat}


def _outcome_index(ftr: str) -> int:
    """FTR 编码 → 有序结果索引（h=0, d=1, a=2）。"""
    return {"H": 0, "D": 1, "A": 2}[ftr]


def evaluate_predictions(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    预测侧指标：samples 每项含 had_probs/fair_probs/ftr。

    输出 RPS/Brier/log loss 双侧（model vs fair 基准）、skill、DM 检验、
    ECE（按结果类别）。
    """
    if not samples:
        return {"n": 0}
    model_losses: dict[str, list[float]] = {"rps": [], "brier": [], "logloss": []}
    market_losses: dict[str, list[float]] = {"rps": [], "brier": [], "logloss": []}
    for sample in samples:
        had = sample["had_probs"]
        fair = sample["fair_probs"]
        outcome = _outcome_index(sample["ftr"])
        model_vec = [had[s] for s in SELECTIONS]
        market_vec = [fair[s] for s in SELECTIONS]
        model_losses["rps"].append(rps(model_vec, outcome))
        market_losses["rps"].append(rps(market_vec, outcome))
        model_losses["brier"].append(multiclass_brier(model_vec, outcome))
        market_losses["brier"].append(multiclass_brier(market_vec, outcome))
        model_losses["logloss"].append(log_loss(model_vec, outcome))
        market_losses["logloss"].append(log_loss(market_vec, outcome))

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    dm, dm_p = diebold_mariano(
        [m - f for m, f in zip(model_losses["rps"], market_losses["rps"], strict=True)]
    )
    return {
        "n": len(samples),
        "rps_model": mean(model_losses["rps"]),
        "rps_market": mean(market_losses["rps"]),
        "skill_rps": skill_score(mean(model_losses["rps"]), mean(market_losses["rps"])),
        "dm_stat": dm,
        "dm_p": dm_p,
        "brier_model": mean(model_losses["brier"]),
        "brier_market": mean(market_losses["brier"]),
        "logloss_model": mean(model_losses["logloss"]),
        "logloss_market": mean(market_losses["logloss"]),
        "ece_h": ece_per_class(
            [[s["had_probs"][sel] for sel in SELECTIONS] for s in samples],
            [_outcome_index(s["ftr"]) for s in samples],
        )[0],
        "ece_d": ece_per_class(
            [[s["had_probs"][sel] for sel in SELECTIONS] for s in samples],
            [_outcome_index(s["ftr"]) for s in samples],
        )[1],
        "ece_a": ece_per_class(
            [[s["had_probs"][sel] for sel in SELECTIONS] for s in samples],
            [_outcome_index(s["ftr"]) for s in samples],
        )[2],
    }


def _prediction_scopes(
    samples: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """预测样本分层：overall / 每联赛 / 每赛季 / 每联赛:赛季。"""
    scopes: dict[str, list[dict[str, Any]]] = {"overall": samples}
    for key in ("competition", "season"):
        grouped: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            grouped.setdefault(str(sample[key]), []).append(sample)
        scopes.update(grouped)
    for sample in samples:
        pair = f"{sample['competition']}:{sample['season']}"
        scopes.setdefault(pair, []).append(sample)
    return scopes


def compute_run_metrics(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """
    计算一次 run 的分层指标并落 backtest_metrics（幂等覆盖）。

    分层维度：overall / 每联赛 / 每赛季 / 每联赛:赛季 / 每玩法（下注侧）。
    返回写入的 scope 数。
    """
    rows = conn.execute(
        """
        SELECT p.*, h.ftr FROM backtest_predictions p
        JOIN hist_matches h ON h.id = p.hist_match_id
        WHERE p.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    samples = [
        {
            "had_probs": json.loads(str(row["had_probs"])),
            "fair_probs": json.loads(str(row["fair_probs"])),
            "ftr": str(row["ftr"]),
            "competition": str(row["competition"]),
            "season": str(row["season"]),
        }
        for row in rows
    ]

    def store(scope: str, metrics: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO backtest_metrics (run_id, scope, metrics)
            VALUES (?, ?, ?)
            """,
            (run_id, scope, json.dumps(metrics, ensure_ascii=False)),
        )

    bet_rows = conn.execute(
        "SELECT * FROM backtest_bets WHERE run_id = ?", (run_id,)
    ).fetchall()
    market_bets: dict[str, list[sqlite3.Row]] = {}
    for bet in bet_rows:
        for leg in json.loads(str(bet["legs"])):
            market_bets.setdefault(str(leg["market_code"]), []).append(bet)

    written = 0
    for scope, group in sorted(_prediction_scopes(samples).items()):
        if not group:
            continue
        store(scope, evaluate_predictions(group))
        written += 1
    for market, bets in sorted(market_bets.items()):
        stats = flat_stake_stats(
            [float(b["profit"]) for b in bets], [float(b["stake"]) for b in bets]
        )
        stats |= {
            "staked": sum(float(b["stake"]) for b in bets),
            "profit": sum(float(b["profit"]) for b in bets),
        }
        store(f"bets:{market}", stats)
        written += 1
    if bet_rows:
        overall = flat_stake_stats(
            [float(b["profit"]) for b in bet_rows],
            [float(b["stake"]) for b in bet_rows],
        )
        overall |= {
            "staked": sum(float(b["stake"]) for b in bet_rows),
            "profit": sum(float(b["profit"]) for b in bet_rows),
        }
        store("bets:overall", overall)
        written += 1
    conn.commit()
    return {"written": written}
