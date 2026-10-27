"""
OU 多盘口约束求界 + AH 联合一致性（measure-integrity 票 10：B 线可行性）。

问题：单点市场侧定价（market-λ 联合反推）给中心估计，但去水/水位噪声
带不确定性——本模块在"满足 1X2 + OU 半线 P(over)∈[p±eps] 约束"的可行
分布族（双 Poisson λ 参数化）上，对竞彩 TTG 八档与让球整数线三向求
**min/max 界**（约束端点重拟包络——可行域在该参数化下沿 P(over) 单
调，端点即包络），并回答研究判据：**保守下界（买方最不利）下模拟竞彩
价是否仍有非零正 EV 候选**。

AH 半线作**联合一致性诊断**：中心拟合的模型 P(主赢盘) vs 市场 AH 水位
去水值的缺口分布——多盘口"联合约束"的可行性直接证据（缺口普遍越带=
约束族不可联合满足）。AH 整数线走盘质量不混同（票面不变量），不作约束。

线约定沿 unlock_report（cid8 线正=主让，主赢盘=净胜球−线>0，矩阵侧
goal_line=−cid8线）；水位健全带外不入样。

只读研究面：不改引擎、不下注；报告落语料树 reports/（抽样口径标注，
非全量结论——转正与否由结论行+人裁决）。
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.backtest import (
    OU_LINE_MAX,
    era_fair_probs,
    joint_lambdas,
    market_implied_matrix,
    pick_hhad_line,
    simulated_jc_odds,
)
from goalx_backend.evaluation.unlock_report import (
    half_line_only,
    two_way_prob,
    waters_sane,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix

EPS = 0.015  # OU 约束区间半宽（研究值：水位去水不确定性量级）
AH_TOL = 0.03  # AH 联合一致性容差（eps+水位噪声余量）
SAMPLE_SEED = 20261020
SAMPLE_N = 2000  # 十年分层抽样（era×联赛），全量另跑


@dataclass
class BoundsStudy:
    """求界研究聚合面。"""

    built_at: str
    eps: float
    sample_n: int
    n_matches: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    fitted: int = 0
    certified: int = 0  # 约束可行（票 04：拟合端点全落声明区间内）
    conflicts: int = 0  # 约束冲突：端点落带外——报冲突不造概率
    ou_resids: list[float] = field(default_factory=list)
    x12_resids: list[float] = field(default_factory=list)
    ah_joint_total: int = 0
    ah_joint_ok: int = 0
    ah_gaps: list[float] = field(default_factory=list)
    width_sums: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    ev_lower_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    ev_lower_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def skip(self, key: str) -> None:
        """入样失败计数（诚实账）。"""
        self.skipped[key] = self.skipped.get(key, 0) + 1


def _fit_matrix(
    fair: dict[str, float], ou_line: float, p_over: float
) -> ScoreMatrix | None:
    """1X2 + P(T>line)=p_over 联合反推（票 23 同口径；失败 None）。"""
    single = market_implied_matrix(fair)
    if single is None:
        return None
    lambdas = joint_lambdas(
        fair, ou_line, p_over, x0=(single.lam_home, single.lam_away)
    )
    if lambdas is None:
        return None
    return ScoreMatrix.from_lambdas(lambdas[0], lambdas[1])


def run_bounds_study(
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    haircut: float,
    sample_n: int = SAMPLE_N,
    eps: float = EPS,
    today: date | None = None,
) -> dict[str, Any]:
    """十年分层抽样求界 → 报告 dict 并落语料树 reports/ 双视图。"""
    rows = gold_reader.fetch_market_face_rows(duck_con)
    rng = random.Random(SAMPLE_SEED)  # noqa: S311 研究抽样非密码学
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['era']}:{row['league']}"].append(row)
    sampled: list[dict[str, Any]] = []
    per_stratum = max(1, sample_n // max(1, len(strata)))
    for bucket in sorted(strata):
        pool = strata[bucket]
        sampled.extend(rng.sample(pool, min(per_stratum, len(pool))))
    study = BoundsStudy(
        built_at=(today or date.today()).isoformat(), eps=eps, sample_n=len(sampled)
    )
    for row in sampled:
        _study_row(row, study, haircut)
    payload = _payload(study)
    _write_report(store, payload)
    return payload


def _constraint_feasibility(
    study: BoundsStudy,
    fits: tuple[ScoreMatrix | None, ScoreMatrix | None, ScoreMatrix | None],
    fair: dict[str, float],
    ou_line: float,
    p_over: float,
) -> bool:
    """
    逐拟合约束可行性（report-audit-20261027 票 04）。

    无约束 NLS 的折中解可能把端点拟到声明区间 [p_over±eps] 外——此时
    端点不是认证的可行族极值。OU 残差（vs 各自目标）与 1X2 贴合度入
    账；返回全端点是否在带内（可行）。
    """
    targets = (p_over, p_over - study.eps, p_over + study.eps)
    in_band_all = True
    x12_resid_max = 0.0
    for mat, target in zip(fits, targets, strict=False):
        if mat is None:
            continue
        model_over = sum(p for b, p in mat.ttg().items() if float(b) > ou_line)
        study.ou_resids.append(abs(model_over - target))
        if abs(model_over - p_over) > study.eps + 1e-9:
            in_band_all = False
        had = mat.had()
        row_x12 = max(abs(had[s] - fair[s]) for s in ("h", "d", "a"))
        x12_resid_max = max(x12_resid_max, row_x12)
    study.x12_resids.append(x12_resid_max)
    return in_band_all


def _study_row(row: dict[str, Any], study: BoundsStudy, haircut: float) -> None:
    """单场：OU 约束端点包络 + AH 联合一致性 + 保守 EV。"""
    study.n_matches += 1
    fair3 = era_fair_probs(
        str(row["era"]),
        (row["psc_home"], row["psc_draw"], row["psc_away"]),
        (row["avgc_home"], row["avgc_draw"], row["avgc_away"]),
        (row["close1x2_h"], row["close1x2_d"], row["close1x2_a"]),
        (row["close1x2_cons_h"], row["close1x2_cons_d"], row["close1x2_cons_a"]),
    )
    if fair3 is None:
        study.skip("no_fair_baseline")
        return
    fair = fair3[0]
    ou_line = half_line_only(row.get("ou_close_line_med"))
    over_w = row.get("ou_close_over_water_med")
    under_w = row.get("ou_close_under_water_med")
    if not (
        ou_line is not None
        and ou_line < OU_LINE_MAX
        and over_w is not None
        and under_w is not None
        and waters_sane((over_w, under_w))
    ):
        study.skip("no_ou_half_line")
        return
    p_over = two_way_prob(over_w, under_w)
    center = _fit_matrix(fair, ou_line, p_over)
    lo_mat = _fit_matrix(fair, ou_line, p_over - study.eps)
    hi_mat = _fit_matrix(fair, ou_line, p_over + study.eps)
    mats = [m for m in (center, lo_mat, hi_mat) if m is not None]
    min_envelope = 2  # 中心+至少一个端点才有包络意义
    if center is None or len(mats) < min_envelope:
        study.skip("fit_failed")
        return
    study.fitted += 1
    # AH 半线联合一致性诊断（cid8 正=主让 → 矩阵 goal_line=−ah_line）；
    # 认证门前入账——冲突行的 AH 缺口正是"约束族不可联合满足"的直接证据
    ah_line = half_line_only(row.get("ah_close_line_med"))
    ah_home_w = row.get("ah_close_home_water_med")
    ah_away_w = row.get("ah_close_away_water_med")
    if (
        ah_line is not None
        and ah_home_w is not None
        and ah_away_w is not None
        and waters_sane((ah_home_w, ah_away_w))
    ):
        p_ah = two_way_prob(ah_home_w, ah_away_w)
        gap = abs(center.cover_prob(-ah_line) - p_ah)
        study.ah_joint_total += 1
        study.ah_gaps.append(gap)
        if gap <= AH_TOL:
            study.ah_joint_ok += 1
    # 可行/冲突分开计数，冲突行不进认证面（包络与保守 EV 不可信）
    if not _constraint_feasibility(
        study, (center, lo_mat, hi_mat), fair, ou_line, p_over
    ):
        study.conflicts += 1
        return
    study.certified += 1
    # 信息分离定价：1X2-only 反推矩阵（不含 OU 约束）——与约束族源分离
    pricing = market_implied_matrix(fair)
    if pricing is None:
        study.skip("pricing_fit_failed")
        return
    _ttg_bounds(study, mats, pricing, haircut)
    _hhad_bounds(study, mats, pricing, haircut)


def _ttg_bounds(
    study: BoundsStudy,
    mats: list[ScoreMatrix],
    pricing: ScoreMatrix,
    haircut: float,
) -> None:
    """TTG 八档：界宽累计 + 保守下界 EV 计数（vs 1X2-only 模拟价）。"""
    odds = simulated_jc_odds(pricing.ttg(), haircut)
    for bucket in range(8):
        code = str(bucket)
        lo = min(m.ttg()[code] for m in mats)
        hi = max(m.ttg()[code] for m in mats)
        key = f"ttg:{code}"
        study.width_sums[key].append(hi - lo)
        study.ev_lower_total[key] += 1
        if lo * odds[code] - 1.0 > 0:
            study.ev_lower_hits[key] += 1


def _hhad_bounds(
    study: BoundsStudy,
    mats: list[ScoreMatrix],
    pricing: ScoreMatrix,
    haircut: float,
) -> None:
    """让球线（定价矩阵近似官方调线选一条整数线）三向界与保守 EV。"""
    line = pick_hhad_line(pricing)
    odds = simulated_jc_odds(pricing.hhad(float(line)), haircut)
    for sel in ("h", "d", "a"):
        lo = min(m.hhad(float(line))[sel] for m in mats)
        hi = max(m.hhad(float(line))[sel] for m in mats)
        key = f"hhad:{line}:{sel}"
        study.width_sums[key].append(hi - lo)
        study.ev_lower_total[key] += 1
        if lo * odds[sel] - 1.0 > 0:
            study.ev_lower_hits[key] += 1


def _median(values: list[float]) -> float:
    """中位数（空表 0）。"""
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _payload(study: BoundsStudy) -> dict[str, Any]:
    """聚合：界宽中位 + 保守 EV 命中率 + AH 联合一致性；结论行。"""
    faces: dict[str, Any] = {}
    for key in sorted(study.width_sums):
        widths = study.width_sums[key]
        total = study.ev_lower_total.get(key, 0)
        hits = study.ev_lower_hits.get(key, 0)
        faces[key] = {
            "n": len(widths),
            "width_median": round(_median(widths), 4),
            "width_max": round(max(widths), 4),
            "ev_lower_hits": hits,
            "ev_lower_rate": round(hits / total, 4) if total else None,
        }
    total_hits = sum(study.ev_lower_hits.values())
    total_face = sum(study.ev_lower_total.values())
    ah_rate = (
        round(study.ah_joint_ok / study.ah_joint_total, 4)
        if study.ah_joint_total
        else None
    )
    return {
        "built_at": study.built_at,
        "eps": study.eps,
        "ah_tol": AH_TOL,
        "sample": {
            "n": study.n_matches,
            "fitted": study.fitted,
            "certified": study.certified,
            "conflicts": study.conflicts,
            "skipped": study.skipped,
        },
        "constraint_feasibility": {
            "certified": study.certified,
            "conflicts": study.conflicts,
            "ou_resid_median": round(_median(study.ou_resids), 4),
            "ou_resid_max": round(max(study.ou_resids), 4) if study.ou_resids else None,
            "x12_resid_median": round(_median(study.x12_resids), 4),
            "x12_resid_max": round(max(study.x12_resids), 4)
            if study.x12_resids
            else None,
            "note": (
                "冲突=拟合端点落 OU 声明区间 [p±eps] 外（约束族不可联合满足"
                "或 NLS 折中越带）——该行不进界/保守 EV 认证面；faces 分母"
                "=certified 行（非 fitted）"
            ),
        },
        "ah_joint": {
            "n": study.ah_joint_total,
            "ok": study.ah_joint_ok,
            "rate": ah_rate,
            "gap_median": round(_median(study.ah_gaps), 4),
            "gap_max": round(max(study.ah_gaps), 4) if study.ah_gaps else None,
        },
        "faces": faces,
        "ev_lower_total_hits": total_hits,
        "ev_lower_total_faces": total_face,
        "conclusion": (
            "认证面（约束可行行）保守界下仍有非零正 EV 候选"
            if total_hits > 0
            else "认证面（约束可行行）保守界下无正 EV 候选——B 线按界口径不可转正"
        ),
    }


def _write_report(store: CorpusStore, payload: dict[str, Any]) -> None:
    """reports/ 双视图（与 unlock 报告同惯例；数据面资产不进 repo）。"""
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "multiline-bounds.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    ah = payload["ah_joint"]
    feas = payload["constraint_feasibility"]
    lines = [
        "# OU 多盘口约束求界 + AH 联合一致性（measure-10，B 线可行性）",
        "",
        f"抽样 {payload['sample']['n']} 场（era×联赛分层，seed 固定）"
        + f"｜拟合 {payload['sample']['fitted']}"
        + f"｜认证 {feas['certified']}｜约束冲突 {feas['conflicts']}"
        + f"｜eps={payload['eps']}",
        f"跳过：{payload['sample']['skipped']}",
        f"约束可行性：OU 残差中位 {feas['ou_resid_median']}"
        + f"/max {feas['ou_resid_max']}，1X2 残差中位 "
        + f"{feas['x12_resid_median']}/max {feas['x12_resid_max']}",
        f"AH 联合一致性：{ah['ok']}/{ah['n']}"
        + f"（rate={ah['rate']}，gap 中位 {ah['gap_median']},max {ah['gap_max']}）",
        "",
        "| 面 | n | 界宽中位 | 界宽max | 保守EV命中 | 命中率 |",
        "|---|---|---|---|---|---|",
    ]
    for key, face in payload["faces"].items():
        lines.append(
            f"| {key} | {face['n']} | {face['width_median']} | {face['width_max']}"
            + f" | {face['ev_lower_hits']} | {face['ev_lower_rate']} |"
        )
    lines += [
        "",
        f"## 结论行：{payload['conclusion']}",
        "",
        f"保守 EV 候选总计 {payload['ev_lower_total_hits']}"
        + f"/{payload['ev_lower_total_faces']} 面。",
        " ".join(
            (
                "判读注意：界=双 Poisson 族内 OU 约束端点近似包络（Poisson pmf",
                "单峰，峰值邻桶上界可能低估；非分布无关界）；拟合端点落声明",
                "区间外=约束冲突行，不进认证面（faces 分母=certified）；模拟价",
                "=1X2-only 反推定价×(1−haircut)（与约束族信息分离）；",
                "no_ou_half_line 跳过且缺向随 era/联赛而异（拟合面非随机",
                "子样）；抽样口径非全量。",
            )
        ),
    ]
    (reports / "multiline-bounds.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
