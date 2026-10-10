"""
market-λ 联合反推 P1（backtest-decade 票 16，spec S16；research/22-27 候选）。

单 1X2 反推 (λh, λa)（goal_expectancy，现引擎市场隐含矩阵路径）在总
进球维度欠约束——draw 概率对 λ_total 的约束弱，反推分布欠分散（票 15
实测 ttg 桶 3-5 隐含低于实际 2-3pp）。加 OU 终盘联合反推可望收紧：

- **single**（基线）：era 正典 1X2 → ``market_implied_matrix`` 反推；
- **joint**：独立泊松下 NLS 拟合 (λh, λa)，约束 = 1X2 三向 fair 概率 +
  OU 半线 P(over)（真实线水位双向去水，票 15 同源），x0=single 反推
  （热启动，Nelder-Mead 400 迭代上限）；
- **判据**（十年分 era）：两法各自 OU 结果 Brier（联合显著更好 ⇒ OU
  信息被 single 丢弃）；1X2 拟合误差（联合不应显著变差）；ttg 桶隐含
  vs 实际（联合是否收敛欠分散缺口）。

P1=可行性对照；转正与否（定价通道）留用户裁决，本模块不接引擎。
样本口径与票 15 同（11 联赛、OU 半线、水位健全带）。
"""

from __future__ import annotations

# scipy 无官方 stub（metrics.py 先例）。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.backtest import (
    era_fair_probs,
    joint_lambdas,
    market_implied_matrix,
)
from goalx_backend.evaluation.unlock_report import (
    OU_LINE_MAX,
    half_line_only,
    two_way_prob,
    waters_sane,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix

REPORT_BASENAME = "market-lambda-p1"


@dataclass
class _Accum:
    """一法一 era 的样本累计。"""

    n: int = 0
    ou_brier: float = 0.0
    had_fit_err: float = 0.0
    over_prob_sum: float = 0.0
    actual_over: int = 0
    ttg: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def add(
        self,
        matrix: ScoreMatrix,
        fair_probs: dict[str, float],
        ou_line: float,
        actual_over: bool,
    ) -> None:
        had = matrix.had()
        ttg = matrix.ttg()
        model_over = sum(p for b, p in ttg.items() if float(b) > ou_line)
        outcome = 1.0 if actual_over else 0.0
        self.n += 1
        self.ou_brier += (model_over - outcome) ** 2
        self.had_fit_err += sum(
            (had[sel] - fair_probs[sel]) ** 2 for sel in ("h", "d", "a")
        )
        self.over_prob_sum += model_over
        self.actual_over += int(actual_over)
        for bucket, p in ttg.items():
            self.ttg[bucket] += p

    def as_dict(self) -> dict[str, Any]:
        """汇总行（均值口径）。"""
        if not self.n:
            return {"n": 0}
        return {
            "n": self.n,
            "ou_brier": round(self.ou_brier / self.n, 4),
            "had_fit_err": round(self.had_fit_err / self.n, 4),
            "over_prob_mean": round(self.over_prob_sum / self.n, 4),
            "actual_over_rate": round(self.actual_over / self.n, 4),
        }


def _fair_from_row(row: dict[str, Any]) -> dict[str, float] | None:
    """Era 正典收盘 fair（单一语义=``era_fair_probs``）。"""
    fair = era_fair_probs(
        str(row["era"]),
        (row["psc_home"], row["psc_draw"], row["psc_away"]),
        (row["avgc_home"], row["avgc_draw"], row["avgc_away"]),
        (row["close1x2_h"], row["close1x2_d"], row["close1x2_a"]),
        (row["close1x2_cons_h"], row["close1x2_cons_d"], row["close1x2_cons_a"]),
    )
    return fair[0] if fair else None


def build_market_lambda_report(
    store: CorpusStore,
    duck_con: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """P1 对照（十年分 era×league）→ 报告 dict 并落 reports/ 双视图。"""
    rows = gold_reader.fetch_market_face_rows(duck_con)
    acc: dict[str, dict[str, _Accum]] = {
        method: defaultdict(_Accum) for method in ("single", "joint")
    }
    skipped: dict[str, int] = defaultdict(int)
    ttg_actual: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_sampled = 0

    for row in rows:
        n_sampled += _collect_row(row, acc, skipped, ttg_actual)

    payload: dict[str, Any] = {
        "built_at": today.isoformat() if today else date.today().isoformat(),
        "n_sampled": n_sampled,
        "skipped": dict(skipped),
        "methods": {
            method: {
                scope: stats.as_dict() for scope, stats in sorted(acc[method].items())
            }
            for method in ("single", "joint")
        },
    }
    # ttg：实际分布（era 主 scope）与两法隐含对照
    for era, buckets in ttg_actual.items():
        total = sum(buckets.values())
        if total:
            payload.setdefault("ttg_actual", {})[era] = {
                b: round(c / total, 4) for b, c in sorted(buckets.items())
            }
    for method in ("single", "joint"):
        implied: dict[str, dict[str, float]] = {}
        for scope, stats in acc[method].items():
            if ":" in scope:  # 只报 era 主层（league 层噪声大）
                continue
            total = sum(stats.ttg.values())
            if total:
                implied[scope] = {
                    b: round(p / total, 4) for b, p in sorted(stats.ttg.items())
                }
        payload[f"ttg_implied_{method}"] = implied

    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )
    return payload


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：single vs joint 逐 era 对照 + ttg 桶收敛表。"""
    header = (
        "| era | n | single ou_brier | joint ou_brier | Δ |"
        " single 1X2 误差 | joint 1X2 误差 |"
    )
    lines = [
        "# market-λ 联合反推 P1（票 16，可行性对照）",
        "",
        "- 样本：{} 场（1X2 fair + OU 半线齐备）；跳过 {}".format(
            payload["n_sampled"], payload["skipped"]
        ),
        "- single=1X2 反推（goal_expectancy）；joint=1X2+OU 半线 NLS 联合。",
        "  判读：joint 的 ou_brier 显著低于 single 且 had_fit_err 未显著变差",
        "  ⇒ OU 信息值得进定价通道（转正=用户裁决）。",
        "",
        header,
        "|---|---|---|---|---|---|---|",
    ]
    singles = payload["methods"]["single"]
    joints = payload["methods"]["joint"]
    for era in sorted(singles):
        if ":" in era or singles[era].get("n", 0) == 0:
            continue
        j = joints.get(era, {})
        delta = (
            round(j["ou_brier"] - singles[era]["ou_brier"], 4)
            if j.get("ou_brier") is not None
            else None
        )
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} |".format(
                era,
                singles[era]["n"],
                singles[era]["ou_brier"],
                j.get("ou_brier"),
                delta,
                singles[era]["had_fit_err"],
                j.get("had_fit_err"),
            )
        )
    lines += [
        "",
        "## ttg 桶：隐含 vs 实际（era 分层，欠分散缺口收敛判据）",
        "",
    ]
    for era, actual in sorted(payload.get("ttg_actual", {}).items()):
        lines.append(f"### {era}")
        lines.append("")
        lines.append("| 桶 | single 隐含 | joint 隐含 | 实际 |")
        lines.append("|---|---|---|---|")
        s_imp = payload["ttg_implied_single"].get(era, {})
        j_imp = payload["ttg_implied_joint"].get(era, {})
        for bucket in sorted(actual):
            lines.append(
                "| {} | {} | {} | {} |".format(
                    bucket,
                    s_imp.get(bucket, "—"),
                    j_imp.get(bucket, "—"),
                    actual[bucket],
                )
            )
        lines.append("")
    lines.append("<!-- P1=可行性对照；转正与否=用户裁决，本报告不接引擎 -->")
    return "\n".join(lines) + "\n"


def _collect_row(
    row: dict[str, Any],
    acc: dict[str, dict[str, _Accum]],
    skipped: dict[str, int],
    ttg_actual: dict[str, dict[str, int]],
) -> int:
    """
    单场入样（1 入样返回 1，跳过返回 0 并计数）。

    joint 拟合失败回落 single 矩阵（joint_fit_failed 如实计数——P1 判读
    需区分"联合不可行"与"联合不增益"）。
    """
    fair_probs = _fair_from_row(row)
    if fair_probs is None:
        skipped["no_fair_baseline"] += 1
        return 0
    ou_line = (
        half_line_only(row["ou_close_line_med"])
        if row["ou_close_line_med"] is not None
        and float(row["ou_close_line_med"]) < OU_LINE_MAX
        else None
    )
    if ou_line is None or not waters_sane(
        (row["ou_close_over_water_med"], row["ou_close_under_water_med"])
    ):
        skipped["ou_no_half_line"] += 1
        return 0
    single_matrix = market_implied_matrix(fair_probs)
    if single_matrix is None:
        skipped["inversion_failed"] += 1
        return 0
    market_over = two_way_prob(
        float(row["ou_close_over_water_med"]),
        float(row["ou_close_under_water_med"]),
    )
    gh, ga = int(row["home_goals"]), int(row["away_goals"])
    actual_over = gh + ga > ou_line
    era = str(row["era"])
    scopes = (era, f"{era}:{row['league']}")
    for scope in scopes:
        acc["single"][scope].add(single_matrix, fair_probs, ou_line, actual_over)
    joint = joint_lambdas(
        fair_probs,
        ou_line,
        market_over,
        x0=(single_matrix.lam_home, single_matrix.lam_away),
    )
    if joint is None:
        skipped["joint_fit_failed"] += 1
        joint_matrix = single_matrix  # 失败回落 single（计数如实）
    else:
        joint_matrix = ScoreMatrix.from_lambdas(joint[0], joint[1])
    for scope in scopes:
        acc["joint"][scope].add(joint_matrix, fair_probs, ou_line, actual_over)
    ttg_actual[era][str(min(gh + ga, 7))] += 1
    return 1
