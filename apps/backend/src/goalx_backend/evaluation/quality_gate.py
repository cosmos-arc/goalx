"""
challenger 质量门：历史回放窗评估 + 落账 + 连续失败告警（vision-build 票 11）。

链路：challenger 注册（票 07 beat / 人 CLI）→ **prereg 冻结**门指标面
（quality_gate_specs：指标名+阈值+评估窗，注册时冻结不得事后改）→
评估器在**十年语料 PIT 回放窗**上评估（评估窗=现代窗留出 era 只读；
champion/challenger 两侧=研究库 backtest_runs 的 walk-forward 预测行，
PIT 由回放引擎常开断言保证——assert_no_lookahead，本模块零重算回放）
→ 校准指标最小可复算集：**RPS 配对单侧检验**（challenger 不劣于
champion 的显著水平；metrics.diebold_mariano 同实现，HAC lag=1）+ ECE
分桶校准快照（观察面不作门）→ 门结果落账（quality_gate_events
append-only，数字全入行=可复算）→ 过门才可提交 L2 人审（promote 硬门：
shadow→probe 需最新门评估通过记录，不过=拒绝留痕）→ 门挂原因分布进
月读⑤ + 连续 N 次门挂告警进晨检（N=3 配置常量）。

不变量（票 11）：
- 评估窗=历史回放窗（人裁 7②）；live shadow 数据只作漂移确认不作门。
- 门指标在 challenger 注册时冻结，不得事后改（prereg 纪律）——冻结面
  append-only，首写恒胜。
- 永无自动晋升不变（票 01）：门只落账与拒绝晋升，不写 transitions。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import NormalDist, fmean
from typing import Any

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.leagues import ERA_TRAJECTORY
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import metrics
from goalx_backend.markets import SELECTIONS

# ---- 常数（调整=改代码=PR 人审，代码永不自改） ----

#: 门指标=1X2 RPS（排序意识，1X2 主指标——metrics 单一实现；「校准动不了
#: 排序的判读先例」：RPS 是唯一有意义的通过线/skill 基准族，票 29）
GATE_METRIC_NAME = "rps"
#: 单侧显著水平：challenger 配对 RPS 差显著劣于 champion（dm>z_{1−α}）
#: 才挂——恰在临界值（p=α）不挂（不劣于侧）
GATE_ALPHA = 0.05
#: 配对最小样本（calibration_min_samples=200 同量级先例；不足=insufficient）
GATE_MIN_PAIRED = 200
#: 评估窗=现代窗留出（trajectory era 起，era=gold 单一真相源；老窗
#: psc_proxy era=开发窗）：回放窗只读，留出纪律不变
GATE_WINDOW_ERA = ERA_TRAJECTORY
#: 连续门挂告警阈值（票面初始默认 N=3）
GATE_CONSECUTIVE_FAIL_N = 3
#: 配对覆盖率下限（F4）：配对数 / max(两 run 窗内行数)——challenger 只
#: 覆盖 champion 小切片（如重训联赛池缩水）时，门在缩水面上的判定不可
#: 外推，覆盖率 <0.5 挂 coverage_asymmetry（指标面照记，判定面挂）
GATE_MIN_PAIRED_RATIO = 0.5
#: 报告落语料树 reports/（edge-scan digest 先例：固定 basename=最新评估视图，
#: 账面数字恒在 quality_gate_events——报告是最新一次的人读对比面）
REPORTS_SUBDIR = Path("reports")
REPORT_BASENAME = "quality-gate"


def challenger_worse_significant(dm_stat: float, *, alpha: float = GATE_ALPHA) -> bool:
    """
    门谓词：challenger 配对 RPS 差显著劣于 champion（单侧，严格 >）。

    dm_stat>z_{1−α} ⇔ 单侧 p<α；恰在临界值（p=α）不挂——「不劣于显著
    水平」的边界语义。dm≤0（challenger 均值不差）恒不挂。
    """
    return dm_stat > NormalDist().inv_cdf(1.0 - alpha)


# ---- prereg 冻结面（注册时冻结指标名+阈值；首写恒胜） ----


@dataclass(frozen=True)
class GateSpec:
    """一个 challenger 版本注册时冻结的门指标面（prereg）。"""

    challenger_version_id: int
    metric_name: str
    alpha: float
    min_paired: int
    window_era: str
    frozen_at: str


def freeze_gate_spec(
    conn: sqlite3.Connection,
    challenger_version_id: int,
    *,
    newly_registered: bool,
    frozen_at: str | None = None,
) -> None:
    """
    冻结门指标面（幂等：同版本首写恒胜，后到冻结不改写）。

    指标名/阈值/评估窗取**代码常数**（调整=改代码=PR 人审）；落账为审
    计面——事后改代码常数不追溯已冻结版本（冻结行即当时的 prereg 证据）。
    注册拍（票 07 beat registered 分支=新建）与人 CLI 注册（新建分支）两
    处调用；``newly_registered=False``（幂等命中旧版本）且无既有冻结行 =
    事后补冻结，prereg 击穿口——拒绝（correctness F3，文案与 evaluate 侧
    「不得事后补」一致）。
    """
    with atomic(conn):
        existing = conn.execute(
            "SELECT 1 FROM quality_gate_specs WHERE challenger_version_id = ?",
            (challenger_version_id,),
        ).fetchone()
        if existing is not None:
            return  # 已冻结：首写恒胜
        if not newly_registered:
            raise LookupError(
                f"版本 {challenger_version_id} 为幂等命中旧版本且无冻结面——"
                + "门指标面不得事后补（prereg：冻结只随新建注册发生）"
            )
        conn.execute(
            """
            INSERT INTO quality_gate_specs
                (challenger_version_id, metric_name, alpha, min_paired,
                 window_era, frozen_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (challenger_version_id) DO NOTHING
            """,
            (
                challenger_version_id,
                GATE_METRIC_NAME,
                GATE_ALPHA,
                GATE_MIN_PAIRED,
                GATE_WINDOW_ERA,
                frozen_at or utc_now_iso(),
            ),
        )


def frozen_spec(
    conn: sqlite3.Connection, challenger_version_id: int
) -> GateSpec | None:
    """读冻结面（无冻结=None——评估前必须有冻结，prereg 前置）。"""
    row = conn.execute(
        """
        SELECT challenger_version_id, metric_name, alpha, min_paired, window_era,
               frozen_at
        FROM quality_gate_specs WHERE challenger_version_id = ?
        """,
        (challenger_version_id,),
    ).fetchone()
    if row is None:
        return None
    return GateSpec(
        challenger_version_id=int(row["challenger_version_id"]),
        metric_name=str(row["metric_name"]),
        alpha=float(row["alpha"]),
        min_paired=int(row["min_paired"]),
        window_era=str(row["window_era"]),
        frozen_at=str(row["frozen_at"]),
    )


# ---- 回放窗配对面（研究库 backtest_predictions 只读；十年回放先例复用） ----


@dataclass(frozen=True)
class PairedFace:
    """一场的 champion/challenger 配对预测（同 match_key=同 gold sid）。"""

    match_key: str
    ftr: str
    champion_probs: dict[str, float]
    challenger_probs: dict[str, float]


@dataclass(frozen=True)
class ReplayPairing:
    """一次配对取数：交集面 + 两 run 各自窗内行数（F4 覆盖率面）。"""

    faces: list[PairedFace]
    n_champion_window: int
    n_challenger_window: int

    @property
    def paired_ratio(self) -> float | None:
        """配对覆盖率=配对数 / max(两 run 窗内行数)（零窗=None）。"""
        denominator = max(self.n_champion_window, self.n_challenger_window)
        return len(self.faces) / denominator if denominator else None


def paired_replay_faces(
    research_conn: sqlite3.Connection,
    *,
    champion_run_id: int,
    challenger_run_id: int,
    window_era: str,
) -> ReplayPairing:
    """
    回放窗配对面：两 run 预测行按评估窗 era 取数、按 match_key 交集配对。

    回放窗=十年语料 PIT 重放的 walk-forward 预测行（backtest_predictions，
    assert_no_lookahead 常开断言的产物——本模块不重算回放）；ftr NULL 行
    （v25 前旧 run 无赛果列）不进配对面。只读研究库。行数面（F4）：各 run
    窗内行数随行返回——覆盖率缩水（如 challenger 220/5000）必须在判定面
    可见，不得静默在 4% 切片上过门。
    """
    faces: dict[int, dict[str, Any]] = {}
    counts: dict[int, int] = {}
    for run_id in (champion_run_id, challenger_run_id):
        rows = research_conn.execute(
            """
            SELECT match_key, ftr, had_probs FROM backtest_predictions
            WHERE run_id = ? AND era = ? AND ftr IS NOT NULL
            """,
            (run_id, window_era),
        ).fetchall()
        counts[run_id] = len(rows)
        faces[run_id] = {
            str(row["match_key"]): (
                str(row["ftr"]),
                json.loads(str(row["had_probs"])),
            )
            for row in rows
        }
    shared = sorted(faces[champion_run_id].keys() & faces[challenger_run_id].keys())
    return ReplayPairing(
        faces=[
            PairedFace(
                match_key=key,
                ftr=faces[champion_run_id][key][0],
                champion_probs=faces[champion_run_id][key][1],
                challenger_probs=faces[challenger_run_id][key][1],
            )
            for key in shared
        ],
        n_champion_window=counts[champion_run_id],
        n_challenger_window=counts[challenger_run_id],
    )


# ---- 评估器：冻结指标在回放窗上算 champion vs challenger ----


@dataclass(frozen=True)
class GateEvaluation:
    """一次门评估（数字全入账=可复算；None=insufficient 不伪造）。"""

    challenger_version_id: int
    champion_version_id: int | None
    metric_name: str
    champion_run_id: int
    challenger_run_id: int
    window_era: str
    alpha: float
    min_paired: int
    n_paired: int
    n_champion_window: int
    n_challenger_window: int
    paired_ratio: float | None
    metric_champion: float | None
    metric_challenger: float | None
    diff: float | None
    dm_stat: float | None
    p_one_sided: float | None
    passed: bool
    hang_reason: str
    ece_champion: dict[str, float] | None
    ece_challenger: dict[str, float] | None
    detail: str


def _outcome_index(ftr: str) -> int:
    """FTR → 有序结果索引（h=0,d=1,a=2；metrics 同编码单一落点）。"""
    return {"H": 0, "D": 1, "A": 2}[ftr]


def _ece_face(faces: list[PairedFace], *, which: str) -> dict[str, float]:
    """一侧的分桶校准快照（ECE 按结果类别；观察面不作门）。"""
    vectors: list[list[float]] = []
    outcomes: list[int] = []
    for face in faces:
        probs = face.champion_probs if which == "champion" else face.challenger_probs
        vectors.append([float(probs[sel]) for sel in SELECTIONS])
        outcomes.append(_outcome_index(face.ftr))
    scores = metrics.ece_per_class(vectors, outcomes)
    return dict(zip(("h", "d", "a"), (round(s, 6) for s in scores), strict=True))


def compute_gate_evaluation(
    research_conn: sqlite3.Connection,
    spec: GateSpec,
    *,
    champion_run_id: int,
    challenger_run_id: int,
    champion_version_id: int | None = None,
) -> GateEvaluation:
    """
    冻结指标在回放窗上的 champion vs challenger 校准对比（纯计算零落账）。

    真树只读面：本函数只读研究库——运维/审计可对真研究库复算对比报告
    而不触碰运行面库（落账走 record 路径）。配对不足冻结 min_paired =
    insufficient_pairs 门挂（指标面 NULL 如实不伪造）；覆盖率缩水
    （配对/max 窗内行数 < 阈）= coverage_asymmetry 门挂（F4——缩水面上的
    判定不可外推，指标面照记）。
    """
    if champion_run_id == challenger_run_id:
        raise ValueError(
            "champion/challenger 回放 run 相同——自对比逐场零差恒过门，不是质量门读数"
        )
    pairing = paired_replay_faces(
        research_conn,
        champion_run_id=champion_run_id,
        challenger_run_id=challenger_run_id,
        window_era=spec.window_era,
    )
    faces = pairing.faces
    base: dict[str, Any] = {
        "challenger_version_id": spec.challenger_version_id,
        "champion_version_id": champion_version_id,
        "metric_name": spec.metric_name,
        "champion_run_id": champion_run_id,
        "challenger_run_id": challenger_run_id,
        "window_era": spec.window_era,
        "alpha": spec.alpha,
        "min_paired": spec.min_paired,
        "n_paired": len(faces),
        "n_champion_window": pairing.n_champion_window,
        "n_challenger_window": pairing.n_challenger_window,
        "paired_ratio": pairing.paired_ratio,
    }
    if len(faces) < spec.min_paired:
        return GateEvaluation(
            **base,
            metric_champion=None,
            metric_challenger=None,
            diff=None,
            dm_stat=None,
            p_one_sided=None,
            passed=False,
            hang_reason="insufficient_pairs",
            ece_champion=None,
            ece_challenger=None,
            detail=(
                f"配对 {len(faces)} < 冻结 min_paired {spec.min_paired}"
                f"（窗 {spec.window_era}）——数据面不足，不伪造指标"
            ),
        )
    champion_losses: list[float] = []
    challenger_losses: list[float] = []
    for face in faces:
        outcome = _outcome_index(face.ftr)
        champion_losses.append(
            metrics.rps([face.champion_probs[sel] for sel in SELECTIONS], outcome)
        )
        challenger_losses.append(
            metrics.rps([face.challenger_probs[sel] for sel in SELECTIONS], outcome)
        )
    diffs = [c - p for c, p in zip(challenger_losses, champion_losses, strict=True)]
    dm_stat, _two_sided = metrics.diebold_mariano(diffs)
    metric_champion = fmean(champion_losses)
    metric_challenger = fmean(challenger_losses)
    diff = metric_challenger - metric_champion
    # F2：DM 方差退化（(0.0,1.0) 分支——常数劣差/交替劣差形态）不可读作
    # 不劣于：均值>0 而 dm=0 = 逐场恒劣但 SE 不可辨识——按恒劣挂，p 不伪造
    if dm_stat == 0.0 and fmean(diffs) > 0.0:
        return GateEvaluation(
            **base,
            metric_champion=metric_champion,
            metric_challenger=metric_challenger,
            diff=diff,
            dm_stat=dm_stat,
            p_one_sided=None,
            passed=False,
            hang_reason="degenerate_variance",
            ece_champion=_ece_face(faces, which="champion"),
            ece_challenger=_ece_face(faces, which="challenger"),
            detail=(
                f"方差退化：逐场差恒劣（mean diff={fmean(diffs):+.6f} 且方差"
                f" 退化 dm=0）——DM 统计量不可用，不劣于判定不成立，按恒劣挂"
            ),
        )
    p_one_sided = NormalDist().cdf(-dm_stat)
    passed = not challenger_worse_significant(dm_stat, alpha=spec.alpha)
    hang_reason = "" if passed else f"{spec.metric_name}_significantly_worse"
    # F4：显著性可判且未挂时，覆盖率缩水仍是不可外推面——独立挂
    ratio = pairing.paired_ratio
    if passed and ratio is not None and ratio < GATE_MIN_PAIRED_RATIO:
        passed = False
        hang_reason = "coverage_asymmetry"
    return GateEvaluation(
        **base,
        metric_champion=metric_champion,
        metric_challenger=metric_challenger,
        diff=diff,
        dm_stat=dm_stat,
        p_one_sided=p_one_sided,
        passed=passed,
        hang_reason=hang_reason,
        ece_champion=_ece_face(faces, which="champion"),
        ece_challenger=_ece_face(faces, which="challenger"),
        detail=(
            f"rps(champion)={metric_champion:.6f} "
            + f"rps(challenger)={metric_challenger:.6f} diff={diff:+.6f} "
            + f"dm={dm_stat:.4f} 单侧 p={p_one_sided:.4f}"
            + f"（α={spec.alpha}，恰界不挂）；配对 {len(faces)}/"
            + f"{max(pairing.n_champion_window, pairing.n_challenger_window)}"
            + f" 覆盖率 {ratio:.2%}（阈 {GATE_MIN_PAIRED_RATIO:.0%}）——"
            + ("过门" if passed else "门挂：" + hang_reason)
        ),
    )


def record_gate_event(conn: sqlite3.Connection, evaluation: GateEvaluation) -> None:
    """门评估落账（append-only；UNIQUE 吸收同 run 对重跑；F4 覆盖率三列随行）。"""
    with atomic(conn):
        conn.execute(
            """
            INSERT INTO quality_gate_events
                (challenger_version_id, champion_version_id, metric_name,
                 champion_run_id, challenger_run_id, window_era, n_paired,
                 n_champion_window, n_challenger_window, paired_ratio,
                 metric_champion, metric_challenger, diff, dm_stat, p_one_sided,
                 passed, hang_reason, detail, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (challenger_version_id, champion_run_id, challenger_run_id)
            DO NOTHING
            """,
            (
                evaluation.challenger_version_id,
                evaluation.champion_version_id,
                evaluation.metric_name,
                evaluation.champion_run_id,
                evaluation.challenger_run_id,
                evaluation.window_era,
                evaluation.n_paired,
                evaluation.n_champion_window,
                evaluation.n_challenger_window,
                evaluation.paired_ratio,
                evaluation.metric_champion,
                evaluation.metric_challenger,
                evaluation.diff,
                evaluation.dm_stat,
                evaluation.p_one_sided,
                int(evaluation.passed),
                evaluation.hang_reason,
                evaluation.detail,
                utc_now_iso(),
            ),
        )


def _require_done_runs(
    research_conn: sqlite3.Connection,
    *,
    champion_run_id: int,
    challenger_run_id: int,
) -> None:
    """
    回放 run 完备性守卫（F1）：两 run 必须 status='done' 且存在。

    backtest 生产者契约：running/failed run 的逐联赛 checkpoint 行已提交=
    局部预测面——在其上的门判定会被 UNIQUE 永久固化（同 run 对不可重评），
    stale-pass 可直接晋升。非 done/缺席一律 fail-loud 拒评估（零落账）。
    """
    rows = research_conn.execute(
        "SELECT id, status FROM backtest_runs WHERE id IN (?, ?)",
        (champion_run_id, challenger_run_id),
    ).fetchall()
    status_by_id = {int(row["id"]): str(row["status"]) for row in rows}
    for run_id, side in (
        (champion_run_id, "champion"),
        (challenger_run_id, "challenger"),
    ):
        if run_id not in status_by_id:
            raise ValueError(
                f"{side} 回放 run {run_id} 不存在（研究库 backtest_runs）"
                + "——run id 须为已存在回放"
            )
        if status_by_id[run_id] != "done":
            raise ValueError(
                f"{side} 回放 run {run_id} status={status_by_id[run_id]}"
                + " 非 done——局部判定会被门账 UNIQUE 永久固化（stale-pass），"
                + "拒绝评估；等 run 完成后再评"
            )


def evaluate_quality_gate(
    research_conn: sqlite3.Connection,
    conn: sqlite3.Connection,
    *,
    challenger_version_id: int,
    champion_run_id: int,
    challenger_run_id: int,
    champion_version_id: int | None = None,
) -> GateEvaluation:
    """
    门评估主链：run 完备性 → 查冻结面 → 回放窗配对计算 → 落账。

    prereg 前置：无冻结面 fail-loud（LookupError，零门账行）——门指标
    必须在 challenger 注册时冻结（票 07 beat registered 分支/人 CLI 注册）。
    run 完备性前置（F1）：非 done run 拒评估。
    """
    _require_done_runs(
        research_conn,
        champion_run_id=champion_run_id,
        challenger_run_id=challenger_run_id,
    )
    spec = frozen_spec(conn, challenger_version_id)
    if spec is None:
        raise LookupError(
            f"版本 {challenger_version_id} 无冻结门指标面"
            + "（prereg：注册时冻结，不得事后补）"
        )
    evaluation = compute_gate_evaluation(
        research_conn,
        spec,
        champion_run_id=champion_run_id,
        challenger_run_id=challenger_run_id,
        champion_version_id=champion_version_id,
    )
    record_gate_event(conn, evaluation)
    return evaluation


# ---- promote 硬门：shadow→probe 需最新门评估通过记录 ----


class GateNotPassed(RuntimeError):
    """shadow→probe 晋升被门拒绝（门账即拒绝留痕的证据面）。"""


def latest_evaluation(
    conn: sqlite3.Connection, challenger_version_id: int
) -> GateEvaluation | None:
    """版本最新一行门评估（无=None）。"""
    row = conn.execute(
        """
        SELECT challenger_version_id, champion_version_id, metric_name,
               champion_run_id, challenger_run_id, window_era, n_paired,
               n_champion_window, n_challenger_window, paired_ratio,
               metric_champion, metric_challenger, diff, dm_stat, p_one_sided,
               passed, hang_reason, detail
        FROM quality_gate_events WHERE challenger_version_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (challenger_version_id,),
    ).fetchone()
    if row is None:
        return None
    spec = frozen_spec(conn, challenger_version_id)
    return GateEvaluation(
        challenger_version_id=int(row["challenger_version_id"]),
        champion_version_id=row["champion_version_id"],
        metric_name=str(row["metric_name"]),
        champion_run_id=int(row["champion_run_id"]),
        challenger_run_id=int(row["challenger_run_id"]),
        window_era=str(row["window_era"]),
        alpha=spec.alpha if spec is not None else GATE_ALPHA,
        min_paired=spec.min_paired if spec is not None else GATE_MIN_PAIRED,
        n_paired=int(row["n_paired"]),
        n_champion_window=int(row["n_champion_window"] or 0),
        n_challenger_window=int(row["n_challenger_window"] or 0),
        paired_ratio=row["paired_ratio"],
        metric_champion=row["metric_champion"],
        metric_challenger=row["metric_challenger"],
        diff=row["diff"],
        dm_stat=row["dm_stat"],
        p_one_sided=row["p_one_sided"],
        passed=bool(row["passed"]),
        hang_reason=str(row["hang_reason"]),
        ece_champion=None,
        ece_challenger=None,
        detail=str(row["detail"]),
    )


def require_gate_pass(conn: sqlite3.Connection, challenger_version_id: int) -> None:
    """
    Promote 硬门（票面「过门才可提交 L2 人审」）：shadow→probe 晋升前查门账。

    **最新**门评估必须通过——先过后挂（复评更新一次失败）不粘滞放行。
    无记录/最新挂=GateNotPassed（调用方 fail-loud；transitions 零写入，
    门账行即拒绝留痕的证据面）。永无自动晋升不变：本函数只拒绝不写。
    """
    latest = latest_evaluation(conn, challenger_version_id)
    if latest is None:
        raise GateNotPassed(
            f"版本 {challenger_version_id} 无门评估记录——shadow→probe 晋升"
            + "需最新门评估通过（quality-gate-evaluate 先跑）"
        )
    if not latest.passed:
        raise GateNotPassed(
            f"版本 {challenger_version_id} 最新门评估挂（{latest.hang_reason}，"
            + "ts 窗口最新一行）——门账无有效过门记录，晋升被拒；"
            + "重训复评过门后再提交 L2 人审"
        )


# ---- 连续失败告警（晨检挂项 + 月读失败视图） ----


def consecutive_gate_failures(conn: sqlite3.Connection) -> int:
    """
    最新一段连续门挂的 **challenger 版本迭代数**（F5：按版本去重，非事件数）。

    票面「影子重训连续 N 次门挂」的 N=迭代数：同一版本的多次 run 对复评挂
    （人对同一 challenger 换 run 重评）是一个失败迭代；新版本挂才递增——
    事件计数会把单次重训的多 run 对复评三连挂误读成系统性失败。过门一行
    即断（间歇成功=回路还在产合格 challenger）；连续同版本挂折叠为 1 段。
    """
    rows = conn.execute(
        "SELECT passed, challenger_version_id FROM quality_gate_events ORDER BY id DESC"
    ).fetchall()
    count = 0
    last_version: int | None = None
    for row in rows:
        if int(row["passed"]) == 1:
            break
        version_id = int(row["challenger_version_id"])
        if version_id != last_version:
            count += 1
            last_version = version_id
    return count


def event_count(conn: sqlite3.Connection) -> int:
    """门账总行数（晨检人读面；SQL 归属闸——表 SQL 留在属主包）。"""
    row = conn.execute("SELECT COUNT(*) AS c FROM quality_gate_events").fetchone()
    return int(row["c"])


def gate_failure_face(
    conn: sqlite3.Connection, *, ts_from: str, ts_to: str
) -> dict[str, Any]:
    """
    月读⑤门挂面（只读）：窗内评估计数 + 过挂原因分布 + 连续失败读数与告警。

    门评估是人 CLI 触发（无业务日 period），窗=ts（[ts_from, ts_to)，
    llm_proposal_costs 同口径）；连续失败是全史最新段读数（告警语义不含
    「本月」限定——连续性跨月才真连续）。
    """
    rows = conn.execute(
        """
        SELECT passed, hang_reason, COUNT(*) AS c FROM quality_gate_events
        WHERE ts >= ? AND ts < ? GROUP BY passed, hang_reason
        """,
        (ts_from, ts_to),
    ).fetchall()
    n_evaluations = 0
    n_passed = 0
    hangs: dict[str, int] = {}
    for row in rows:
        count = int(row["c"])
        n_evaluations += count
        if int(row["passed"]) == 1:
            n_passed += count
        else:
            hangs[str(row["hang_reason"])] = count
    consecutive = consecutive_gate_failures(conn)
    return {
        "n_evaluations": n_evaluations,
        "n_passed": n_passed,
        "hangs_by_reason": dict(sorted(hangs.items())),
        "consecutive_failures": consecutive,
        "alert": consecutive >= GATE_CONSECUTIVE_FAIL_N,
        "consecutive_fail_alert_n": GATE_CONSECUTIVE_FAIL_N,
    }


# ---- 对比报告（champion vs challenger 校准对比，可复算） ----


def evaluation_payload(evaluation: GateEvaluation) -> dict[str, Any]:
    """报告载荷：评估产物全量（窗/run 对/指标双侧/dm/p/判定/ECE 观察面）。"""
    return asdict(evaluation) | {"generated_at": utc_now_iso()}


def render_markdown(payload: dict[str, Any]) -> str:
    """报告 MD 视图（人读对比面）：判定行 + 双侧数字表 + prereg 注记。"""
    verdict = "过门" if payload["passed"] else f"门挂：{payload['hang_reason']}"
    lines = [
        "# 质量门报告：champion vs challenger 校准对比（历史回放窗）",
        "",
        f"- 判定：**{verdict}**（{payload['detail']}）",
        "- 评估窗：era="
        + f"{payload['window_era']}（现代窗留出只读；回放=PIT"
        + " walk-forward，本报告零重算回放）",
        "- champion：version="
        + f"{payload['champion_version_id']} run={payload['champion_run_id']}"
        + " / challenger：version="
        + f"{payload['challenger_version_id']} run={payload['challenger_run_id']}",
        "- prereg：metric="
        + f"{payload['metric_name']} α={payload['alpha']}"
        + f" min_paired={payload['min_paired']}（注册时冻结）",
        "",
        "| 指标 | champion | challenger |",
        "| --- | --- | --- |",
        f"| RPS 均值 | {payload['metric_champion']} | {payload['metric_challenger']} |",
        f"| ECE h/d/a | {payload['ece_champion']} | {payload['ece_challenger']} |",
        "",
        f"- 配对样本 n={payload['n_paired']}；diff={payload['diff']}"
        + f" dm={payload['dm_stat']} 单侧 p={payload['p_one_sided']}",
        f"- 窗内行数：champion {payload['n_champion_window']} /"
        + f" challenger {payload['n_challenger_window']}；配对覆盖率"
        + f" {payload['paired_ratio']}（阈 {GATE_MIN_PAIRED_RATIO:.0%}，"
        + "缩水面判定不可外推）",
        "- 门语义：challenger 配对 RPS 显著劣于 champion（dm>z_{1−α} 单侧）"
        + "才挂；恰界不挂；方差退化（恒劣）挂 degenerate_variance；覆盖率缩水"
        + "挂 coverage_asymmetry；ECE=分桶校准观察面不作门",
    ]
    return "\n".join(lines) + "\n"


def write_report(store: CorpusStore, evaluation: GateEvaluation) -> tuple[Path, Path]:
    """报告落语料树 reports/（JSON+MD 双视图，temp+rename 原子写）。"""
    reports_dir = store.root / REPORTS_SUBDIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    payload = evaluation_payload(evaluation)
    json_tmp = reports_dir / f"{REPORT_BASENAME}.json.tmp"
    md_tmp = reports_dir / f"{REPORT_BASENAME}.md.tmp"
    json_tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_tmp.write_text(render_markdown(payload), encoding="utf-8")
    json_path = reports_dir / f"{REPORT_BASENAME}.json"
    md_path = reports_dir / f"{REPORT_BASENAME}.md"
    json_tmp.replace(json_path)
    md_tmp.replace(md_path)
    return json_path, md_path
