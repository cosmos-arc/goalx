"""
错价桶方向映射限定复核（caliber-loop 票 03，一次性）。

回答一个问题：**市场残差在正确的方向映射下是否有独立价值**——
model-v2 D1 桶门控（−21.4%）买的是负残差侧（fade 但钱去错侧），
本复核按裁决①严格正侧规则买正残差侧：

- 谓词（裁决①命名，数字从 signal-attribution 报告读取为唯一真相源，
  不手抄）：`late_move_h:pos → 买 d`、`ah_phase_pre:low → 买 h`；
- 解耦模型：候选=桶成员**全体**（非 DC 愿下注子集），固定注额 1，
  不经 EV 阈值、不经 Kelly、无串关；
- 等量分层随机对照：联赛×era×玩法×赔率档（五分位）分层，对照臂
  逐层等量配对抽取（池=非映射桶全体，确定性种子）；
- 不确定性：比赛周联合分块 bootstrap（两臂同周重抽）。

结论词汇限定：一次性限定复核，同十年样本无干净留出集，结论上限=
「方向映射后探索性一致/不一致」，不得宣称终审（裁决②：结果不升级
B 相前瞻臂）。观察性研究：不进引擎不改参数。
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean, quantiles
from typing import Any, cast

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.metrics import load_backtest_run
from goalx_backend.evaluation.signal_attribution import (
    DERIVED_FEATURES,
    FEATURES,
    MatchSample,
    bucket_of,
    enumerate_match_samples,
    tertile_cuts,
)
from goalx_backend.evaluation.signal_attribution import (
    REPORT_BASENAME as SOURCE_BASENAME,
)
from goalx_backend.evaluation.signal_attribution import (
    T_SIGNAL as SOURCE_T_SIGNAL,
)

REPORT_BASENAME = "bucket-reaudit"
FLAT_STAKE = 1.0
CONTROL_SEED = 20261016  # 等量分层抽取确定性种子
ODDS_BAND_WIDTH = 0.2  # 赔率档：隐含概率五分位（与票 01 概率档同粒度）
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 20261016
MIN_BOOTSTRAP_UNITS = 2  # 周/有效抽样少于此=区间不可估（置 None）

# 裁决①命名谓词（特征, 桶, 买侧）；方向与数字验证自源报告
NAMED_PREDICATES: tuple[tuple[str, str, str], ...] = (
    ("late_move_h", "pos", "d"),
    ("ah_phase_pre", "low", "h"),
)
STRATA_DIMS = ("league", "era", "market", "odds_band")

_FEATURE_KIND: dict[str, str] = {key: kind for key, _col, kind in FEATURES} | dict(
    DERIVED_FEATURES
)
_Face = dict[str, Any]


@dataclass(frozen=True)
class _Bet:
    """一笔复核注（桶臂或对照臂）。"""

    sid: str
    predicate: str  # "late_move_h:pos→d" / "ah_phase_pre:low→h" / "control"
    side: str
    odds: float
    profit: float  # 平注结算：win→odds−1，lose→−1
    week_monday: date
    stratum: tuple[str, str, str, int]  # 联赛×era×玩法×赔率档


@dataclass
class _ArmStats:
    """一臂的平注累计。"""

    bets: list[_Bet] = field(default_factory=list)

    def face(self) -> dict[str, Any]:
        n = len(self.bets)
        profit = sum(b.profit for b in self.bets)
        hits = sum(1 for b in self.bets if b.profit > 0)
        return {
            "n": n,
            "profit": round(profit, 4),
            "roi_flat": round(profit / (n * FLAT_STAKE), 4) if n else None,
            "hit_rate": round(hits / n, 4) if n else None,
        }


def _verify_mapping(source_report: dict[str, Any]) -> dict[str, Any]:
    """
    命名谓词对照源报告逐项验证（fail-loud）+ 纯正侧候选全集对账。

    验证：映射侧的 market_bias > 0 且 |t| ≥ 门（方向映射错=票面失败）。
    对账：纯正侧规则在源报告全部 |t|≥2 行上的全集 vs 命名谓词——
    多出的正侧候选=裁决排除（多重检验面按裁决最小化），如实列出不动。
    """
    faces = cast("dict[str, dict[str, _Face]]", source_report.get("faces") or {})
    provenance: list[dict[str, Any]] = []
    for feature, bucket, side in NAMED_PREDICATES:
        face = (faces.get(feature) or {}).get(bucket)
        if face is None:
            raise ValueError(f"源报告缺桶 {feature}:{bucket}——先跑 signal-attribution")
        bias = face.get(f"market_bias_{side}")
        t_stat = face.get(f"market_bias_{side}_t")
        if bias is None or t_stat is None:
            raise ValueError(f"源报告 {feature}:{bucket} 缺 {side} 侧偏差面")
        if float(bias) <= 0:
            raise ValueError(
                f"方向映射违例：{feature}:{bucket}→{side} 偏差 {bias} 非正"
                + "（严格正侧规则不买负残差侧）"
            )
        if abs(float(t_stat)) < SOURCE_T_SIGNAL:
            raise ValueError(
                f"{feature}:{bucket}→{side} |t|={t_stat} < {SOURCE_T_SIGNAL}"
            )
        provenance.append(
            {
                "feature": feature,
                "bucket": bucket,
                "side": side,
                "market_bias": bias,
                "t": t_stat,
                "n": face.get("n"),
            }
        )
    positives: list[dict[str, Any]] = []
    for feature, buckets in faces.items():
        for bucket, face in buckets.items():
            for side in ("h", "d", "a"):
                t_stat = face.get(f"market_bias_{side}_t")
                bias = face.get(f"market_bias_{side}")
                if (
                    t_stat is not None
                    and abs(float(t_stat)) >= SOURCE_T_SIGNAL
                    and bias is not None
                    and float(bias) > 0
                ):
                    positives.append(
                        {
                            "feature": feature,
                            "bucket": bucket,
                            "side": side,
                            "market_bias": bias,
                            "t": t_stat,
                        }
                    )
    named = {(f, b, s) for f, b, s in NAMED_PREDICATES}
    excluded = [
        p for p in positives if (p["feature"], p["bucket"], p["side"]) not in named
    ]
    return {"predicates": provenance, "excluded_by_ruling": excluded}


def _bucket_labels(samples: list[MatchSample]) -> dict[str, dict[str, str]]:
    """
    逐场桶标签 {sid: {feature: bucket}}（仅命名谓词涉及的特征）。

    切点=样本全集分位（与源报告同口径——同一枚举口，切点必然一致）。
    """
    wanted = {feature for feature, _bucket, _side in NAMED_PREDICATES}
    cuts: dict[str, tuple[float, float]] = {}
    for feature in wanted:
        values = [
            s.signal.features[feature] for s in samples if feature in s.signal.features
        ]
        if _FEATURE_KIND[feature] == "dist" and values:
            cuts[feature] = tertile_cuts(values)
    labels: dict[str, dict[str, str]] = {}
    for sample in samples:
        row: dict[str, str] = {}
        for feature in wanted:
            value = sample.signal.features.get(feature)
            if value is None:
                continue
            row[feature] = bucket_of(
                value, _FEATURE_KIND[feature], cuts.get(feature, (0.0, 0.0))
            )
        labels[sample.sid] = row
    return labels


def _odds_band(odds: float) -> int:
    """赔率档=隐含概率五分位（1/odds；抗浮点边界 epsilon；顶档收口）。"""
    return min(4, int((1.0 / odds) / ODDS_BAND_WIDTH + 1e-9))


def _make_bet(sample: MatchSample, side: str, predicate: str, haircut: float) -> _Bet:
    """固定注额 1 注：价=引擎 had 面（era 公允 × (1−haircut) 模拟竞彩价）。"""
    odds = (1.0 / sample.fair[side]) * (1.0 - haircut)
    profit = odds - 1.0 if sample.ftr == side else -1.0
    iso = date.fromisoformat(sample.match_date[:10]).isocalendar()
    return _Bet(
        sid=sample.sid,
        predicate=predicate,
        side=side,
        odds=round(odds, 4),
        profit=profit,
        week_monday=date.fromisocalendar(iso[0], iso[1], 1),
        stratum=(sample.competition, sample.signal.era, "had", _odds_band(odds)),
    )


def _two_arms(
    samples: list[MatchSample], labels: dict[str, dict[str, str]], haircut: float
) -> tuple[list[_Bet], list[_Bet], list[dict[str, Any]], int]:
    """桶臂全体 + 等量分层对照臂（+短缺层账+双桶重叠数）。"""
    mapped = {(f, b) for f, b, _s in NAMED_PREDICATES}
    arm: list[_Bet] = []
    pool: dict[tuple[str, str, str, int], list[_Bet]] = {}
    overlap = 0
    for sample in samples:
        row = labels.get(sample.sid, {})
        hit: tuple[str, str, str] | None = None
        hits = sum(1 for f, b, _s in NAMED_PREDICATES if row.get(f) == b)
        if hits > 1:
            overlap += 1
        for feature, bucket, side in NAMED_PREDICATES:
            if row.get(feature) == bucket:
                hit = (feature, bucket, side)
                break
        if hit is not None:
            name = f"{hit[0]}:{hit[1]}→{hit[2]}"
            arm.append(_make_bet(sample, hit[2], name, haircut))
            continue
        # 对照池=非映射桶全体（映射桶内任何场次不进池——桶条件污染）
        if any(row.get(f) == b for f, b in mapped):
            continue
        for _feature, _bucket, side in NAMED_PREDICATES:
            bet = _make_bet(sample, side, "control", haircut)
            pool.setdefault(bet.stratum, []).append(bet)
    control, shortfall = _stratified_control(arm, pool)
    return arm, control, shortfall, overlap


def _stratified_control(
    arm: list[_Bet],
    pool: dict[tuple[str, str, str, int], list[_Bet]],
) -> tuple[list[_Bet], list[dict[str, Any]]]:
    """等量分层配对抽取：逐层取 min(臂数, 池数)，确定性种子。"""
    rng = random.Random(CONTROL_SEED)  # noqa: S311
    picked: list[_Bet] = []
    shortfall: list[dict[str, Any]] = []
    by_stratum: dict[tuple[str, str, str, int], list[_Bet]] = {}
    for bet in arm:
        by_stratum.setdefault(bet.stratum, []).append(bet)
    for stratum in sorted(by_stratum, key=repr):
        n_arm = len(by_stratum[stratum])
        candidates = list(pool.get(stratum, []))
        take = n_arm
        if len(candidates) < n_arm:
            shortfall.append(
                {"stratum": repr(stratum), "arm": n_arm, "pool": len(candidates)}
            )
            take = len(candidates)
        picked.extend(rng.sample(candidates, take))
    return picked, shortfall


def _dim_diffs(arm: list[_Bet], control: list[_Bet]) -> dict[str, dict[str, int]]:
    """分维计数差（等量分层验收断言面；全零=匹配平衡）。"""

    def counts(bets: list[_Bet], idx: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for bet in bets:
            key = repr(bet.stratum[idx])
            out[key] = out.get(key, 0) + 1
        return out

    diffs: dict[str, dict[str, int]] = {}
    for i, dim in enumerate(STRATA_DIMS):
        arm_counts = counts(arm, i)
        ctrl_counts = counts(control, i)
        keys = set(arm_counts) | set(ctrl_counts)
        nonzero = {
            k: arm_counts.get(k, 0) - ctrl_counts.get(k, 0)
            for k in keys
            if arm_counts.get(k, 0) != ctrl_counts.get(k, 0)
        }
        if nonzero:
            diffs[dim] = nonzero
    return diffs


def _paired_week_bootstrap(
    arm: list[_Bet], control: list[_Bet]
) -> dict[str, Any] | None:
    """比赛周联合分块 bootstrap：同周重抽两臂，roi 差的分位区间。"""
    arm_weeks: dict[date, list[float]] = {}
    for bet in arm:
        arm_weeks.setdefault(bet.week_monday, []).append(bet.profit)
    ctrl_weeks: dict[date, list[float]] = {}
    for bet in control:
        ctrl_weeks.setdefault(bet.week_monday, []).append(bet.profit)
    weeks = sorted(set(arm_weeks) | set(ctrl_weeks))
    if len(weeks) < MIN_BOOTSTRAP_UNITS:
        return None
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311
    diffs: list[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        sampled = rng.choices(weeks, k=len(weeks))
        arm_vals = [v for w in sampled for v in arm_weeks.get(w, [])]
        ctrl_vals = [v for w in sampled for v in ctrl_weeks.get(w, [])]
        if arm_vals and ctrl_vals:
            diffs.append(fmean(arm_vals) - fmean(ctrl_vals))
    if len(diffs) < MIN_BOOTSTRAP_UNITS:
        return None
    cuts = quantiles(diffs, n=40)
    return {
        "p2_5": round(cuts[0], 4),
        "p97_5": round(cuts[-1], 4),
        "draws": len(diffs),
        "unit": "iso_week_joint",
    }


def _load_inputs(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    run_id: int | None,
) -> tuple[int, str, str, float, list[MatchSample], dict[str, int], dict[str, Any]]:
    """Run 元信息 + 源报告验证 + 带身份逐场样本（单一枚举口）。"""
    rid, label, params_raw, run_status = load_backtest_run(conn, run_id)
    params = (
        json.loads(params_raw)
        if isinstance(params_raw, str)
        else dict(params_raw or {})
    )
    haircut = float(params.get("haircut", 0.0) or 0.0)
    source_path = store.root / "reports" / f"{SOURCE_BASENAME}.json"
    if not source_path.exists():
        raise ValueError("源报告缺席：先跑 signal-attribution-report（唯一真相源）")
    source_report = json.loads(source_path.read_text(encoding="utf-8"))
    if int(source_report.get("run_id", -1)) != rid:
        raise ValueError(
            f"源报告 run={source_report.get('run_id')} 与复核 run={rid} 不一致"
            + "（先对同一 run 重跑 signal-attribution）"
        )
    sql = " ".join(
        (
            "SELECT match_key, ftr, fair_probs, had_probs, competition, match_date",
            "FROM backtest_predictions WHERE run_id = ? ORDER BY id",
        )
    )
    pred_rows = conn.execute(sql, (rid,)).fetchall()
    sids = [str(r["match_key"]) for r in pred_rows if r["ftr"] is not None]
    gold = gold_reader.fetch_signal_rows_by_sids(duck_con, sids)
    samples, skipped = enumerate_match_samples(pred_rows, gold)
    return rid, label, run_status, haircut, samples, skipped, source_report


def build_bucket_reaudit_report(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """桶方向映射限定复核 → reports/ 双视图（一次性，观察性）。"""
    rid, label, run_status, haircut, samples, skipped, source_report = _load_inputs(
        conn, duck_con, store, run_id
    )
    mapping = _verify_mapping(source_report)
    labels = _bucket_labels(samples)
    arm, control, shortfall, overlap = _two_arms(samples, labels, haircut)
    dim_diffs = _dim_diffs(arm, control)
    if dim_diffs and not shortfall:
        raise ValueError(f"分维计数差非零（等量分层被破坏）：{dim_diffs}")

    arm_face = _ArmStats(arm).face()
    ctrl_face = _ArmStats(control).face()
    boot = _paired_week_bootstrap(arm, control)
    diff_roi = (
        round(arm_face["roi_flat"] - ctrl_face["roi_flat"], 4)
        if arm_face["roi_flat"] is not None and ctrl_face["roi_flat"] is not None
        else None
    )
    if ctrl_face["n"] == 0:
        conclusion = "无法对照（对照臂空）"
    elif diff_roi is not None and diff_roi > 0:
        conclusion = "方向映射后探索性一致"
    else:
        conclusion = "方向映射后探索性不一致"
    if shortfall:
        conclusion += f"（对照臂 {len(shortfall)} 层短缺，配对不完全）"
    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "label": label,
        "run_status": run_status,
        "params": {
            "mapping_rule": "严格正侧规则（裁决①）",
            "predicates": [f"{f}:{b}→{s}" for f, b, s in NAMED_PREDICATES],
            "flat_stake": FLAT_STAKE,
            "ev_threshold": None,
            "kelly": None,
            "parlay": None,
            "odds_face": f"era fair × (1−haircut={haircut})（引擎 had 同面）",
            "strata": STRATA_DIMS,
            "odds_band_width": ODDS_BAND_WIDTH,
            "control_seed": CONTROL_SEED,
            "candidate_denominator": "桶成员全体（非 DC 愿下注子集）",
            "overlap_attribution": "双桶场次按谓词顺序先到先得（一注）",
        },
        "mapping_provenance": mapping,
        "n_samples": len(samples),
        "skipped": skipped,
        "arm": arm_face,
        "control": ctrl_face,
        "diff_roi": diff_roi,
        "week_joint_bootstrap": boot,
        "dim_count_diffs": dim_diffs,
        "control_shortfall": shortfall,
        "overlap_double_bucket": overlap,
        "by_predicate": {
            name: _ArmStats([b for b in arm if b.predicate == name]).face()
            for name in sorted({b.predicate for b in arm})
        },
        "conclusion": conclusion,
        "note": (
            "一次性限定复核：同十年样本无干净留出集，结论上限=探索性一致/"
            + "不一致，不得宣称终审；复核结果不升级 B 相前瞻臂（裁决②）；"
            + "固定注额不经 EV/Kelly/串关，候选分母=桶成员全体；对照=联赛×"
            + "era×玩法×赔率档等量分层配对（池=非映射桶）；不确定性=比赛周"
            + "联合分块 bootstrap；方向与数字自 signal-attribution 报告读取"
            + "（唯一真相源不手抄）；观察性研究不进引擎不改参数"
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
    """人读视图（结论词汇限定常驻）。"""
    arm, ctrl = payload["arm"], payload["control"]
    boot = payload["week_joint_bootstrap"]
    boot_text = f"[{boot['p2_5']}, {boot['p97_5']}]" if boot else "样本不足"
    lines = [
        "# 错价桶方向映射限定复核（一次性）",
        "",
        f"run={payload['run_id']}（{payload['label']}）样本 {payload['n_samples']}",
        f"映射：{payload['params']['predicates']}"
        + f"（{payload['params']['mapping_rule']}）",
        f"价面：{payload['params']['odds_face']}"
        + f"；双桶重叠 {payload['overlap_double_bucket']} 场（先到先得一注）",
        "",
        "## 两臂对照（固定注额，不经 EV/Kelly/串关）",
        "",
        f"- 桶臂：n={arm['n']} profit={arm['profit']} roi={arm['roi_flat']}"
        + f" hit={arm['hit_rate']}",
        f"- 对照臂（等量分层）：n={ctrl['n']} profit={ctrl['profit']}"
        + f" roi={ctrl['roi_flat']} hit={ctrl['hit_rate']}",
        f"- roi 差：{payload['diff_roi']}"
        + f"（比赛周联合 bootstrap 2.5–97.5% {boot_text}）",
        "",
        "## 分谓词",
        "",
    ]
    for name, face in payload["by_predicate"].items():
        lines.append(
            f"- {name}: n={face['n']} roi={face['roi_flat']} hit={face['hit_rate']}"
        )
    lines += [
        "",
        "## 映射对账（源报告=唯一真相源）",
        "",
        f"- 命名谓词验证通过：{payload['mapping_provenance']['predicates']}",
        "- 裁决排除的正侧候选（未纳入，多重检验面最小化）："
        + str(payload["mapping_provenance"]["excluded_by_ruling"]),
        f"- 对照短缺层：{payload['control_shortfall'] or '无'}",
        "",
        f"## 结论：{payload['conclusion']}",
        "",
        "- 判读注意：" + payload["note"],
    ]
    return "\n".join(lines) + "\n"
