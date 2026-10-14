"""
模型层对照报告（model-iterate 票 01/02：校准与融合变体 vs 基线）。

校准层（可扩展至 K2 融合/K3 判别）变体 run vs 基线 run 的十年对照：

- **预测面**（match_key 对齐的配对样本）：RPS skill（evaluate_predictions
  复用）+ 分侧 ECE（ece_per_class 复用）+ **校准三元组**（模型概率均值
  vs 公平概率均值 vs 实际命中率——K1 的靶子是 0.334/0.246/0.236 失准）；
- **下注面**：had 市场注（腿含 had 的注，compute_run_metrics 口径）与
  全注的 n/staked/ROI 对照（校准缩 EV → 注量下降是预期行为，须对照）；
- 结论句：ECE 降幅、三元组闭合度、注量变化（机械阈值生成）。

纯 sqlite 读面；报告落语料树 reports/ MD+JSON。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.backtest import run_summary
from goalx_backend.evaluation.metrics import (
    ece_per_class,
    evaluate_predictions,
    load_backtest_run,
)
from goalx_backend.markets import SELECTIONS

REPORT_BASENAME = "model-layers"
# 字面量拼接只为行宽（同 signal_attribution 先例）
_PREDICTIONS_SQL = (
    "SELECT match_key, era, ftr, had_probs, fair_probs"  # noqa: S608
    + " FROM backtest_predictions WHERE run_id = ? ORDER BY id"
)


@dataclass
class _PredFace:
    """一个 run 预测面的累计（配对样本上统计）。"""

    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """RPS skill + 分侧 ECE + 校准三元组。"""
        if not self.samples:
            return {"n": 0}
        face = evaluate_predictions(self.samples)
        prob_vecs = [
            [s["had_probs"][side] for side in SELECTIONS] for s in self.samples
        ]
        outcomes = [s["outcome_index"] for s in self.samples]
        ece = ece_per_class(prob_vecs, outcomes)
        face["ece"] = dict(zip(SELECTIONS, (round(v, 4) for v in ece), strict=True))
        face["calibration_triple"] = {
            side: {
                "model": round(
                    sum(s["had_probs"][side] for s in self.samples) / len(self.samples),
                    4,
                ),
                "fair": round(
                    sum(s["fair_probs"][side] for s in self.samples)
                    / len(self.samples),
                    4,
                ),
                "actual": round(
                    sum(1.0 for s in self.samples if s["ftr"].lower() == side)
                    / len(self.samples),
                    4,
                ),
            }
            for side in SELECTIONS
        }
        return face


def _samples_of(
    conn: sqlite3.Connection, run_id: int, keys: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Run 预测 → {match_key: 样本}（可选按 key 集过滤做配对）。"""
    rows = conn.execute(_PREDICTIONS_SQL, (run_id,)).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["ftr"] is None:
            continue
        key = str(row["match_key"])
        if keys is not None and key not in keys:
            continue
        out[key] = {
            "had_probs": json.loads(str(row["had_probs"])),
            "fair_probs": json.loads(str(row["fair_probs"])),
            "ftr": str(row["ftr"]),  # 大写 H/D/A（metrics._outcome_index 口径）
            "outcome_index": SELECTIONS.index(str(row["ftr"]).lower()),
        }
    return out


def _bet_face(
    conn: sqlite3.Connection, run_id: int, market: str | None = None
) -> dict[str, Any]:
    """
    一个 run 的注面（market=None 全注；否则该玩法注）。

    归属口径同 compute_run_metrics：串关进其每条腿的玩法、同玩法只计
    一次。
    """
    rows = conn.execute(
        "SELECT stake, profit, ev, legs FROM backtest_bets WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    stakes: list[float] = []
    profits: list[float] = []
    expected = 0.0
    for row in rows:
        if market is not None:
            legs = json.loads(str(row["legs"]))
            if not any(str(leg["market_code"]) == market for leg in legs):
                continue
        stakes.append(float(row["stake"]))
        profits.append(float(row["profit"]))
        expected += float(row["ev"]) * float(row["stake"])
    staked = sum(stakes)
    profit_sum = sum(profits)
    return {
        "n": len(stakes),
        "staked": round(staked, 2),
        "profit": round(profit_sum, 2),
        "roi": round(profit_sum / staked, 4) if staked else None,
        "ev_realization": round(profit_sum / expected, 4) if expected > 0 else None,
    }


def _verdicts(
    baseline: dict[str, Any], treatments: dict[str, dict[str, Any]]
) -> list[str]:
    """结论句：ECE 降幅 + 三元组闭合 + 注量变化（机械阈值）。"""
    lines: list[str] = []
    base_face = baseline.get("prediction", {})
    if base_face.get("n", 0) == 0:
        return ["- 基线预测面为空（run 配对失败）"]
    for label, payload in treatments.items():
        face = payload.get("prediction", {})
        if face.get("n", 0) == 0:
            lines.append(f"- {label}: 配对样本为空")
            continue
        ece_base = sum(base_face["ece"].values())
        ece_treat = sum(face["ece"].values())
        lines.append(
            f"- {label}: 配对 n={face['n']}，ΣECE {ece_base:.4f}→{ece_treat:.4f}"
            + f"（{'改善' if ece_treat < ece_base else '未改善'}）"
            + f"，skill_rps {base_face['skill_rps']:+.4f}→{face['skill_rps']:+.4f}"
        )
        # 三元组闭合：|模型−实际| 均值（三侧）
        gap_base = (
            sum(
                abs(v["model"] - v["actual"])
                for v in base_face["calibration_triple"].values()
            )
            / 3
        )
        gap_treat = (
            sum(
                abs(v["model"] - v["actual"])
                for v in face["calibration_triple"].values()
            )
            / 3
        )
        lines.append(
            f"  - 校准三元组闭合度 |模型−实际| 均值 {gap_base:.4f}→{gap_treat:.4f}"
            + ("（失准收窄）" if gap_treat < gap_base else "（未收窄）")
        )
        bets_base = baseline.get("bets_had", {})
        bets_treat = payload.get("bets_had", {})
        n_base = bets_base.get("n", 0)
        n_treat = bets_treat.get("n", 0)
        if n_treat > n_base:
            direction = "注量上升（校准重排 EV 面，非预期收缩）"
        elif n_treat < n_base:
            direction = "EV 收缩致注量下降=预期行为"
        else:
            direction = "注量持平"
        lines.append(
            f"  - had 注量 {n_base}→{n_treat}"
            + f"（staked {bets_base.get('staked')}→{bets_treat.get('staked')}"
            + f"，ROI {bets_base.get('roi')}→{bets_treat.get('roi')}；{direction}）"
        )
        alpha = payload.get("fusion_alpha")
        if alpha and alpha.get("n_weeks"):
            lines.append(
                f"  - 融合 α：均值 {alpha.get('alpha_mean')} / 中位 "
                + f"{alpha.get('alpha_median')}，存活率（α≥0.05）"
                + f"{alpha.get('survival_rate')}，分时代 {alpha.get('per_era')}"
            )
    return lines


def build_model_layers_report(
    conn: sqlite3.Connection,
    store: CorpusStore,
    *,
    treatments: list[tuple[str, int]],
    baseline_run_id: int,
    today: date | None = None,
) -> dict[str, Any]:
    """多 treatment vs 基线的模型层对照 → reports/ 双视图。"""
    labels = [label for label, _ in treatments]
    if len(set(labels)) != len(labels):
        raise ValueError(f"--treatment 标签重复：{labels}")
    base_rid, base_label, base_params, base_status = load_backtest_run(
        conn, baseline_run_id
    )
    # 配对面=全部 run 键交集（correctness F3：基线也须过滤到交集，
    # 覆盖不对称时不得拿全集对照子集）
    base_all = _samples_of(conn, base_rid)
    paired_keys = set(base_all)
    treatment_samples: dict[str, dict[str, dict[str, Any]]] = {}
    for label, run_id in treatments:
        rid, _label, params, status = load_backtest_run(conn, run_id)
        samples = _samples_of(conn, rid)
        paired_keys &= set(samples)
        treatment_samples[label] = samples
    base_samples = {k: v for k, v in base_all.items() if k in paired_keys}

    treatment_payloads: dict[str, dict[str, Any]] = {}
    for label, run_id in treatments:
        rid, _label, params, status = load_backtest_run(conn, run_id)
        treat_samples = {
            k: v for k, v in treatment_samples[label].items() if k in paired_keys
        }
        face = _PredFace(list(treat_samples.values())).as_dict()
        summary = run_summary(conn, rid) or {}
        treatment_payloads[label] = {
            "run_id": rid,
            "run_status": status,
            "params": {
                k: params.get(k)
                for k in (
                    "calibration",
                    "calibration_min_samples",
                    "calibration_window",
                    "fusion",
                    "fusion_min_samples",
                    "fusion_window",
                )
            },
            "fusion_alpha": summary.get("fusion_alpha"),
            "prediction": face,
            "bets_had": _bet_face(conn, rid, "had"),
            "bets_all": _bet_face(conn, rid),
        }

    baseline_face = _PredFace(list(base_samples.values())).as_dict()
    baseline_payload = {
        "run_id": base_rid,
        "label": base_label,
        "run_status": base_status,
        "params": {
            k: base_params.get(k)
            for k in ("calibration", "calibration_min_samples", "calibration_window")
        },
        "prediction": baseline_face,
        "bets_had": _bet_face(conn, base_rid, "had"),
    }

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "n_baseline_total": len(base_all),
        "n_paired": len(paired_keys),
        "baseline": baseline_payload,
        "treatments": treatment_payloads,
        "note": (
            "配对面=两 run match_key 交集（ftr 齐备）；had 注面口径同"
            + " compute_run_metrics（串关进其每条腿的玩法）；三元组在"
            + "**全预测集**判读——票 19 的 0.334/0.246/0.236 高估三元组"
            + "在**下注子集**（选择偏差），两口径不可互比"
        ),
    }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：结论句 + 三元组表。"""
    base = payload["baseline"]
    lines = [
        "# 模型层对照（model-iterate 校准/融合）",
        "",
        f"基线 run={base['run_id']}（calibration={base['params'].get('calibration')}）"
        + f" 配对样本 n={base['prediction'].get('n', 0)}",
        "",
        "## 结论",
        "",
        *_verdicts(base, payload["treatments"]),
        "",
        "## 校准三元组（模型 vs 公平 vs 实际；基线）",
        "",
        "| 侧 | 模型 | 公平 | 实际 |",
        "|---|---|---|---|",
    ]
    triple = base["prediction"].get("calibration_triple", {})
    for side, values in triple.items():
        lines.append(
            f"| {side} | {values['model']} | {values['fair']} | {values['actual']} |"
        )
    for label, treat in payload["treatments"].items():
        lines += [
            "",
            f"## 校准三元组（{label}）",
            "",
            "| 侧 | 模型 | 公平 | 实际 |",
            "|---|---|---|---|",
        ]
        for side, values in treat["prediction"].get("calibration_triple", {}).items():
            lines.append(
                f"| {side} | {values['model']} | {values['fair']} "
                + f"| {values['actual']} |"
            )
    lines += [
        "",
        "- 判读注意：" + payload["note"],
        "- 完整 ECE/skill/注面数字见 model-layers.json。",
    ]
    return "\n".join(lines) + "\n"
