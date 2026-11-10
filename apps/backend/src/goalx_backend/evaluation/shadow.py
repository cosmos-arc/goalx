"""
自学习触发→影子注册→影子双算（vision-build 票 07：L1 自动学习 + 发布权在人）。

链路：漂移检测（模型输出分布 PSI——**接票 08 contraction.psi 纯函数**，
窗=当前 30 日 vs 前 90 日参考，同四信号 league_mix 口径）越阈即时触发 +
月度定拍兜底 → 重训缝（modelling.train_competition 写**独立 challenger
模型目录**，生产 ml 轨工件不动）→ challenger 三元组自动注册 shadow 态
（betting.versioning.register_version；注册≠晋升，晋升恒人审）→
champion/challenger 同流量双算（同 fixture 集双写 forecasts，challenger
行 track='shadow' 带版本标签；champion 行不动）→ 触发落账
shadow_retrain_events（月读⑤消费）。

不变量（票 07）：
- 永无自动晋升：beat 只注册（genesis=shadow 态）与写影子行，不落任何
  transitions——票 01 晋升哨保证，测试钉死。
- 校准重估/特征刷新属 L1 全自动直通不进影子（现状即满足，本模块不触）。
- L2+ 收缩态停拍：月拍/漂移拍在 deployment_plan.shadow_retrain=False 的
  级别跳过并留痕（烧算力面收缩；漂移监测恒开是另一面——只监测不重训）。
- 幂等重跑（auto-paper 03 同缝）：注册幂等（三元组 UNIQUE）、影子行幂等
  （forecasts (fixture, track, content_hash) UNIQUE）、事件行幂等（版本行
  UNIQUE(period, reason, version) / skip 行部分唯一索引）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from goalx_backend.betting import auto_strategy, versioning
from goalx_backend.config import get_settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import contraction
from goalx_backend.modelling import dc_model
from goalx_backend.modelling import forecast as fc

# ---- 常数（调整=改代码=PR 人审，代码永不自改） ----

#: 漂移窗=当前 30 日 vs 前 90 日参考（contraction 四信号 league_mix 同窗；
#: PSI 函数/阈值复用票 08 contraction.psi/psi_exceeds——单一漂移统计实现）
SHADOW_DRIFT_MIN_N = 30  # 每窗最小 ml 前瞻行数（不足=insufficient 只报不判）
#: 重训联赛池=weekly-refresh 同池（五大+N1，与 data/models 现役工件池一致）
SHADOW_RETRAIN_COMPETITIONS: tuple[str, ...] = (
    *dc_model.TIER1_COMPETITIONS,
    "N1",
)
#: challenger 注册 label 前缀（月键随行——显示面，身份恒=三元组）
_CHALLENGER_LABEL = "shadow-challenger"


@dataclass(frozen=True)
class DriftReading:
    """模型输出分布漂移一次读数（had 三选向 PSI；insufficient 只报不判）。"""

    window_start: str  # 参考窗起点（=now−当前窗−参考窗）
    current_start: str  # 当前窗起点
    window_end: str
    n_reference: int
    n_current: int
    psi: dict[str, float]  # h/d/a 三选向 PSI（insufficient 时空 dict）
    status: str  # "ok" | "insufficient"
    detail: str

    @property
    def fired(self) -> bool:
        """任一选向 PSI 越阈（严格 >0.25，恰阈不触发）；窗不足恒不触发。"""
        return self.status == "ok" and any(
            contraction.psi_exceeds(value) for value in self.psi.values()
        )


def forecast_output_drift(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> DriftReading:
    """
    漂移读数：champion 生产轨（track='ml'）had 概率分布的 PSI 对比。

    选型（票 07）：「现有 PSI/KS 面」=票 08 contraction.psi 纯函数（PSI
    十分位等频桶）——模型**输出**分布漂移的直接读数（forecast had 概率，
    PIT 安全：issued ≤ kickoff 的生产轨行）；drift_replay.collect_drift_rows
    是开→收价格漂移复验（一次性研究面），非模型输入/输出漂移，不接。
    """
    resolved = now or datetime.now(UTC)
    current_start = (
        resolved - timedelta(days=contraction.SIGNAL_CURRENT_WINDOW_DAYS)
    ).isoformat(timespec="seconds")
    reference_start = (
        resolved
        - timedelta(
            days=contraction.SIGNAL_CURRENT_WINDOW_DAYS
            + contraction.SIGNAL_REFERENCE_WINDOW_DAYS
        )
    ).isoformat(timespec="seconds")
    window_end = resolved.isoformat(timespec="seconds")
    reference = fc.ml_had_probabilities(
        conn, ts_from=reference_start, ts_to=current_start
    )
    current = fc.ml_had_probabilities(conn, ts_from=current_start, ts_to=window_end)
    if len(reference) < SHADOW_DRIFT_MIN_N or len(current) < SHADOW_DRIFT_MIN_N:
        return DriftReading(
            window_start=reference_start,
            current_start=current_start,
            window_end=window_end,
            n_reference=len(reference),
            n_current=len(current),
            psi={},
            status="insufficient",
            detail=(
                f"窗样本不足（参考 {len(reference)} / 当前 {len(current)}，"
                f" 各需 ≥{SHADOW_DRIFT_MIN_N}）——只报不判"
            ),
        )
    psi_values = {
        selection: contraction.psi(
            [row[selection] for row in reference],
            [row[selection] for row in current],
        )
        for selection in ("h", "d", "a")
    }
    fired = any(contraction.psi_exceeds(value) for value in psi_values.values())
    top = max(psi_values, key=lambda key: psi_values[key])
    return DriftReading(
        window_start=reference_start,
        current_start=current_start,
        window_end=window_end,
        n_reference=len(reference),
        n_current=len(current),
        psi=dict(psi_values),
        status="ok",
        detail=(
            "had PSI h/d/a="
            + "/".join(f"{psi_values[key]:.4f}" for key in ("h", "d", "a"))
            + f"（最重 {top}；阈 {contraction.PSI_THRESHOLD} 严格 >）——"
            + ("越阈" if fired else "未越阈")
        ),
    )


def challenger_triple(
    params: object, artifacts: Mapping[str, str]
) -> tuple[str, str, str]:
    """
    Challenger 三元组。

    基座=engine_version_triple（estimator=modelling 实现
    版本面 / calibration=口径族 / params=参数指纹），estimator 再并入重训
    工件摘要——重训即换工件=换版本（基座本身代码级，纯重训不动它）。

    工件面取 ``{联赛: artifact_id}`` 规范串 sha256 前 16 位（内容寻址：
    工件序无关；任一联赛工件变=新版本；全同=同版本→幂等注册 no_change）。
    """
    if not artifacts:
        raise ValueError("重训工件面为空（challenger 无从定义）")
    estimator, calibration, params_fp = auto_strategy.engine_version_triple(params)
    face = ";".join(f"{league}={artifacts[league]}" for league in sorted(artifacts))
    digest = sha256(face.encode()).hexdigest()[:16]
    return f"{estimator}|artifacts={digest}", calibration, params_fp


# ---- beat 编排：触发→重训→注册 shadow→同流量双写→落账 ----


def _record_event(
    conn: sqlite3.Connection,
    *,
    period: str,
    reason: str,
    outcome: str,
    challenger_version_id: int | None,
    drift_face: dict[str, Any],
    detail: str,
) -> None:
    """落一行触发账（幂等吸收在库层 UNIQUE，见 v57；ts=写入时刻）。"""
    with atomic(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO shadow_retrain_events
                (period, reason, outcome, challenger_version_id, drift_detail,
                 detail, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                period,
                reason,
                outcome,
                challenger_version_id,
                json.dumps(drift_face, ensure_ascii=False),
                detail,
                utc_now_iso(),
            ),
        )


def monthly_floor_due(conn: sqlite3.Connection, *, period: str) -> bool:
    """
    月度兜底到期判定。

    本期（yyyy-mm）尚无**真重训落账**（版本行
    registered/no_change——重训缝真跑过并注册/命中 challenger）。

    廉价失败行（no_live_champion/no_traffic/skipped_contraction）不消兜底
    ——次日拍廉价重试（账被部分唯一索引吸收，不膨胀）；月内漂移重训
    （版本行）自然消兜底：兜底的目的=「每月至少一次新鲜 challenger」。
    """
    row = conn.execute(
        """
        SELECT 1 FROM shadow_retrain_events
        WHERE period = ? AND challenger_version_id IS NOT NULL
        LIMIT 1
        """,
        (period,),
    ).fetchone()
    return row is None


def retrain_event_counts(
    conn: sqlite3.Connection, *, ts_from: str, ts_to: str
) -> dict[str, int]:
    """
    触发账窗口计数（月读⑤消费面）：``{reason|outcome: n}`` + total。

    窗=[ts_from, ts_to)（ts 半开——⑤节各面同界）；只读。
    """
    rows = conn.execute(
        """
        SELECT reason, outcome, COUNT(*) AS c FROM shadow_retrain_events
        WHERE ts >= ? AND ts < ? GROUP BY reason, outcome ORDER BY reason, outcome
        """,
        (ts_from, ts_to),
    ).fetchall()
    counts = {f"{row['reason']}|{row['outcome']}": int(row["c"]) for row in rows}
    counts["total"] = sum(value for key, value in counts.items() if key != "total")
    return counts


def _drift_handled_this_period(conn: sqlite3.Connection, *, period: str) -> bool:
    """
    漂移节流。

    本期已有漂移触发的版本行（已重训响应过；窗惯性约 30 日，
    日日重训=烧算力——一月一响应，PSI 持续越阈下月再训）。
    """
    row = conn.execute(
        """
        SELECT 1 FROM shadow_retrain_events
        WHERE period = ? AND reason = 'drift' AND challenger_version_id IS NOT NULL
        LIMIT 1
        """,
        (period,),
    ).fetchone()
    return row is not None


def _default_retrain(
    conn: sqlite3.Connection,
    *,
    challenger_models_dir: Path,
    competitions: tuple[str, ...],
) -> Callable[[], dict[str, str]]:
    """
    生产重训缝。

    modelling 既有 fit 管线（train_competition）写**独立
    challenger 模型目录**——生产 ml 轨的 models_dir 不动（champion 下注/
    前瞻面消费的 latest 工件不被重训顶替，champion 不动的工件层保证）。

    返回重训后的工件面 ``{联赛: artifact_id}``（内容寻址=三元组的工件侧）。
    """

    def _retrain() -> dict[str, str]:
        settings = get_settings()
        artifacts: dict[str, str] = {}
        for competition in competitions:
            run = dc_model.train_competition(
                conn,
                competition,
                models_dir=challenger_models_dir,
                half_life_days=settings.dc_half_life_days,
                n_boot=settings.bootstrap_samples,
                seed=settings.bootstrap_seed,
            )
            artifacts[competition] = run.artifact_id()
        return artifacts

    return _retrain


def run_shadow_retrain_beat(
    conn: sqlite3.Connection,
    *,
    challenger_models_dir: Path,
    now: datetime | None = None,
    business_date: str | None = None,
    competitions: tuple[str, ...] = SHADOW_RETRAIN_COMPETITIONS,
    retrain: Callable[[], Mapping[str, str]] | None = None,
    params: object = None,
) -> dict[str, Any]:
    """
    影子重训拍（票 07 主链）。

    漂移越阈即时（日拍粒度）+ 月度兜底 → 收缩
    门 → champion/流量前置 → 重训缝 → 注册 shadow → 同流量双写 → 落账。

    - 无触发（漂移未越阈/已节流 且 兜底已消）=心跳 no_trigger，零写零算；
    - L2+（plan.shadow_retrain=False）：留痕 skipped_contraction，不烧算力
      （漂移监测恒开是只读面，与重训是两面）；
    - 重训产物只进 shadow：注册 initial_state=shadow（genesis 行，永无自动
      晋升），forecasts 写 track='shadow' 带版本标签——champion ml 轨零改动；
    - 同流量=champion 当日已覆盖 fixture 集（读口按业务日取），键一致由
      构造保证；幂等重跑：三元组 UNIQUE+内容哈希吸收+事件账吸收三重。
    """
    resolved_params = auto_strategy.AUTO_V1_PARAMS if params is None else params
    resolved = now or datetime.now(UTC)
    run_date = business_date or fx_store.beijing_business_date(resolved)
    period = run_date[:7]
    level = contraction.current_level(conn)
    drift = forecast_output_drift(conn, now=resolved)
    drift_face: dict[str, Any] = {
        "status": drift.status,
        "fired": drift.fired,
        "psi": drift.psi,
        "n_reference": drift.n_reference,
        "n_current": drift.n_current,
        "detail": drift.detail,
    }
    face: dict[str, Any] = {
        "business_date": run_date,
        "level": level.value,
        "drift": drift_face,
    }
    if drift.fired and not _drift_handled_this_period(conn, period=period):
        reason = "drift"
    elif monthly_floor_due(conn, period=period):
        reason = "monthly"
    else:
        return face | {"status": "no_trigger", "reason": None}
    face["reason"] = reason

    if not contraction.deployment_plan(level).shadow_retrain:
        _record_event(
            conn,
            period=period,
            reason=reason,
            outcome="skipped_contraction",
            challenger_version_id=None,
            drift_face=drift_face,
            detail=f"收缩级别 {level.value} 停影子重训（漂移监测恒开，重训停）",
        )
        return face | {"status": "skipped_contraction"}

    if versioning.live_version_id(conn) is None:
        _record_event(
            conn,
            period=period,
            reason=reason,
            outcome="no_live_champion",
            challenger_version_id=None,
            drift_face=drift_face,
            detail="无 live 态版本（champion 未定义），影子对比面无从建立",
        )
        return face | {"status": "no_live_champion"}

    traffic = fc.champion_traffic_fixture_ids(conn, business_date=run_date)
    if not traffic:
        _record_event(
            conn,
            period=period,
            reason=reason,
            outcome="no_traffic",
            challenger_version_id=None,
            drift_face=drift_face,
            detail=f"业务日 {run_date} 无 champion(ml) 前瞻覆盖——无双算流量",
        )
        return face | {"status": "no_traffic"}

    run_retrain = (
        retrain
        if retrain is not None
        else _default_retrain(
            conn,
            challenger_models_dir=challenger_models_dir,
            competitions=competitions,
        )
    )
    artifacts = run_retrain()
    triple = challenger_triple(resolved_params, artifacts)
    existing = versioning.find_version_id(
        conn,
        estimator_version=triple[0],
        calibration_version=triple[1],
        params_version=triple[2],
    )
    if existing is None:
        version_id = versioning.register_version(
            conn,
            estimator_version=triple[0],
            calibration_version=triple[1],
            params_version=triple[2],
            label=f"{_CHALLENGER_LABEL}-{period}",
            initial_state=versioning.StrategyState.SHADOW,
        )
        outcome = "registered"
    else:
        version_id = existing
        outcome = "no_change"
    stats = fc.generate_forecasts(
        conn,
        business_date=run_date,
        models_dir=str(challenger_models_dir),
        track="shadow",
        fixture_ids=tuple(traffic),
        strategy_version_id=version_id,
    )
    _record_event(
        conn,
        period=period,
        reason=reason,
        outcome=outcome,
        challenger_version_id=version_id,
        drift_face=drift_face,
        detail=(
            f"{outcome}：工件面 {len(artifacts)} 联赛；双写 {stats.generated} 行"
            f"（幂等吸收 {stats.duplicates}）"
        ),
    )
    return face | {
        "status": outcome,
        "challenger_version_id": version_id,
        "generated": stats.generated,
        "duplicates": stats.duplicates,
        "skipped_model": stats.skipped,
    }
