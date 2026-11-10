"""
策略版本状态机 + 回滚三触发器（vision-build-20261110 票 01：回路心脏地基）。

版本=三元组（estimator/calibration/params，来源挂 modelling 实现版本与
auto_strategy 参数指纹，内容寻址字符串——逻辑外键，目标侧是模块产出的
版本标识而非库表）。状态集六梯两出：

    registered→shadow→probe→candidate→live-paper→live-real
    demoted（可再升，唯一回梯口=重新注册，走 L2 人审）
    retired（人归档终态，trial 计数照记）

不变量（票 01）：
- transitions append-only（库层触发器拒绝 UPDATE/DELETE，trial ledger
  同构）；自动降级也落 transitions（actor=trigger:A/B/C 可审计）。
- 永无自动晋升：自动路径只写降级/出梯目标，人审晋升只经 CLI。
- 回滚触发器=确定性代码读指标（非 agent），数字=配置常量；月读可调，
  调整=版本变更走 L2（改常数=改代码=PR 人审）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import StrEnum
from hashlib import sha256
from statistics import fmean, pstdev

from goalx_backend.db import atomic, utc_now_iso


class StrategyState(StrEnum):
    """策略版本状态（六梯 + demoted/retired 两个出梯态）。"""

    REGISTERED = "registered"
    SHADOW = "shadow"
    PROBE = "probe"
    CANDIDATE = "candidate"
    LIVE_PAPER = "live-paper"
    LIVE_REAL = "live-real"
    DEMOTED = "demoted"
    RETIRED = "retired"


# 晋升梯（序=级别）：升/降只走相邻一级，跳级恒拒
LADDER: tuple[StrategyState, ...] = (
    StrategyState.REGISTERED,
    StrategyState.SHADOW,
    StrategyState.PROBE,
    StrategyState.CANDIDATE,
    StrategyState.LIVE_PAPER,
    StrategyState.LIVE_REAL,
)

# 消费端闸（auto_strategy 只对这两个态下纸面注）
LIVE_STATES: frozenset[StrategyState] = frozenset(
    {StrategyState.LIVE_PAPER, StrategyState.LIVE_REAL}
)

# 回滚触发器配置常量（票 01 初始默认；月读可调——调整=版本变更走 L2：
# 改常数=改代码=PR 人审，代码永不自改）。
TRIGGER_A_WINDOW = 50  # A：滚动 CLV 窗（注）
TRIGGER_A_SE_GATE = 2.0  # A：显著性门（SE 倍数）
TRIGGER_B_DRAWDOWN = 0.10  # B：账户自高点回撤阈值（比例）
TRIGGER_C_SE_GATE = 2.0  # C：注册校准指标越界门（SE 倍数）

_ALLOWED_TRANSITIONS: frozenset[tuple[StrategyState, StrategyState]] = frozenset(
    # 梯上/梯下相邻一步
    {(LADDER[i], LADDER[i + 1]) for i in range(len(LADDER) - 1)}
    | {(LADDER[i + 1], LADDER[i]) for i in range(len(LADDER) - 1)}
    # demoted 回梯唯一口=重新注册（L2 人审后从底爬）
    | {(StrategyState.DEMOTED, StrategyState.REGISTERED)}
    # 出梯：任意梯态→demoted（人降 / 触发器 B 冻结 live-real）；
    # 任意非 retired 态→retired（人归档终态）
    | {(state, StrategyState.DEMOTED) for state in LADDER}
    | {(state, StrategyState.RETIRED) for state in (*LADDER, StrategyState.DEMOTED)}
)


def transition_allowed(from_state: StrategyState, to_state: StrategyState) -> bool:
    """转移谓词：非法转移拒绝（全表单测钉死，跳级/回梯越口/终态复出皆拒）。"""
    return (from_state, to_state) in _ALLOWED_TRANSITIONS


class InvalidTransition(ValueError):
    """非法状态转移（谓词拒绝；转移零写入）。"""


class UnknownVersion(LookupError):
    """操作不存在的版本 id。"""


def register_version(
    conn: sqlite3.Connection,
    *,
    estimator_version: str,
    calibration_version: str,
    params_version: str,
    label: str,
) -> int:
    """
    注册一个策略版本（幂等：同三元组返回既有 id，不落新行）。

    新注册才写 genesis 转移行（from_state NULL → registered）——现态恒有
    定义（current_state 不必兜底「无转移=registered」分支）。
    """
    with atomic(conn):
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO strategy_versions
                (estimator_version, calibration_version, params_version, label,
                 created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                estimator_version,
                calibration_version,
                params_version,
                label,
                utc_now_iso(),
            ),
        )
        row = conn.execute(
            """
            SELECT id, label FROM strategy_versions
            WHERE estimator_version = ? AND calibration_version = ?
              AND params_version = ?
            """,
            (estimator_version, calibration_version, params_version),
        ).fetchone()
        if row is None:  # pragma: no cover - INSERT OR IGNORE 后必命中
            raise UnknownVersion("注册后读不到版本行")
        if cursor.rowcount == 1:
            conn.execute(
                """
                INSERT INTO version_transitions
                    (version_id, from_state, to_state, reason, actor, ts)
                VALUES (?, NULL, ?, ?, 'human', ?)
                """,
                (int(row["id"]), StrategyState.REGISTERED.value, "注册", utc_now_iso()),
            )
    return int(row["id"])


def current_state(conn: sqlite3.Connection, version_id: int) -> StrategyState | None:
    """版本现态=最新转移行的 to_state（无版本= None）。"""
    row = conn.execute(
        """
        SELECT to_state FROM version_transitions WHERE version_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (version_id,),
    ).fetchone()
    if row is None:
        return None
    return StrategyState(str(row["to_state"]))


def transition_version(
    conn: sqlite3.Connection,
    version_id: int,
    to_state: StrategyState,
    *,
    actor: str,
    reason: str,
) -> None:
    """
    落一次状态转移（先验谓词，非法抛 InvalidTransition 且零写入）。

    晋升（梯上/回梯）只允许 actor 以 ``human`` 起头的调用方（人 CLI）；
    自动路径（actor=trigger:*）只可达降级/出梯目标——永无自动晋升在
    调用侧结构成立，此处再设哨（防未来接线误用）。
    """
    exists = conn.execute(
        "SELECT 1 FROM strategy_versions WHERE id = ?", (version_id,)
    ).fetchone()
    if exists is None:
        raise UnknownVersion(f"版本不存在: {version_id}")
    from_state = current_state(conn, version_id)
    if from_state is None:  # pragma: no cover - register 必写 genesis 行
        raise InvalidTransition(f"版本 {version_id} 无转移历史")
    if not transition_allowed(from_state, to_state):
        raise InvalidTransition(f"非法转移: {from_state.value} → {to_state.value}")
    if not actor.startswith("human") and _is_promotion(from_state, to_state):
        raise InvalidTransition(
            f"自动 actor 不得晋升（{from_state.value} → {to_state.value}）"
        )
    with atomic(conn):
        conn.execute(
            """
            INSERT INTO version_transitions
                (version_id, from_state, to_state, reason, actor, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                from_state.value,
                to_state.value,
                reason,
                actor,
                utc_now_iso(),
            ),
        )


def _is_promotion(from_state: StrategyState, to_state: StrategyState) -> bool:
    """梯上一步或 demoted 回梯注册=晋升（自动路径禁止的目标面）。"""
    if from_state is StrategyState.DEMOTED:
        return to_state is StrategyState.REGISTERED
    if from_state in LADDER and to_state in LADDER:
        return LADDER.index(to_state) > LADDER.index(from_state)
    return False


# ---- 回滚触发器读数面（确定性代码读指标，非 agent）----


def rolling_clv_window(conn: sqlite3.Connection) -> list[float]:
    """
    最近 ``TRIGGER_A_WINDOW`` 注已结算 auto 纸面注的每注 CLV（时间升序）。

    读数面与 daily-wrap 回填的 ``bets.clv`` 同源（v46 列）：未结算/非
    auto/live 模式行不进窗；不足窗=如实返回短表（由谓词判不足）。
    """
    rows = conn.execute(
        """
        SELECT clv FROM bets
        WHERE mode = 'paper' AND strategy_version IS NOT NULL
          AND settled_at IS NOT NULL AND clv IS NOT NULL
        ORDER BY settled_at DESC, id DESC
        LIMIT ?
        """,
        (TRIGGER_A_WINDOW,),
    ).fetchall()
    return [float(row["clv"]) for row in reversed(rows)]


def clv_window_fired(clvs: Sequence[float]) -> bool:
    """
    触发器 A 谓词：滚动窗 CLV 均值为负且低出 0 达 2SE（严格不等）。

    「均值<0 且 2SE 下界<0」读作均值显著为负：mean + 2·SE < 0（等价
    mean < −2SE）——恰触边界（margin 恰为 0）不触发。窗不足
    ``TRIGGER_A_WINDOW`` 注恒不评估。
    """
    if len(clvs) < TRIGGER_A_WINDOW:
        return False
    mean = fmean(clvs)
    se = pstdev(clvs) / (len(clvs) ** 0.5)
    return mean < 0.0 and mean + TRIGGER_A_SE_GATE * se < 0.0


def bankroll_history(
    conn: sqlite3.Connection, *, initial_bankroll: float
) -> list[float]:
    """账户水位序列：初始资金 + 逐笔已结算 paper P&L 累计（升序）。"""
    rows = conn.execute(
        """
        SELECT profit FROM bets
        WHERE mode = 'paper' AND profit IS NOT NULL AND settled_at IS NOT NULL
        ORDER BY settled_at, id
        """
    ).fetchall()
    history = [initial_bankroll]
    running = initial_bankroll
    for row in rows:
        running += float(row["profit"])
        history.append(running)
    return history


def drawdown_fired(history: Sequence[float]) -> bool:
    """
    触发器 B 谓词：现值自序列高点回撤超阈（严格 >）。

    恰 ``TRIGGER_B_DRAWDOWN``（10%）不触发；高点 ≤0（退化面）不评估。
    """
    if not history:
        return False
    peak = max(history)
    if peak <= 0.0:
        return False
    drawdown = (peak - history[-1]) / peak
    return drawdown > TRIGGER_B_DRAWDOWN


def calibration_drift_fired(deviation: float, se: float) -> bool:
    """
    触发器 C 谓词：注册校准指标偏离越 2SE（严格 >，双向）。

    恰 2SE 不触发。deviation=观测指标与注册冻结口径之差；读数来源
    （校准回测/前瞻评分面）由调用方供给——票 07 challenger 质量门接
    线前的最小谓词面。
    """
    return abs(deviation) > TRIGGER_C_SE_GATE * se


# ---- 触发器编排：自动降级（永无自动晋升）----


@dataclass(frozen=True)
class Demotion:
    """一次已落账的自动降级（transitions 行的读数投影）。"""

    version_id: int
    label: str
    trigger: str
    from_state: StrategyState
    to_state: StrategyState
    reason: str


@dataclass(frozen=True)
class RollbackResult:
    """一轮触发器评估结果：fired=告警面（纸面态 B 仅告警），demotions=已落账降级。"""

    fired: dict[str, bool]
    demotions: list[Demotion]


def apply_rollback_triggers(
    conn: sqlite3.Connection,
    *,
    initial_bankroll: float,
    calibration_reading: tuple[float, float] | None = None,
) -> RollbackResult:
    """
    评估三触发器并对全部版本落自动降级（A/C 降一级、B 冻结 live-real）。

    顺序=B→A→C：B 先冻结（live-real→demoted），被冻版本不再受 A/C 步降
    ——「live-real 全冻结」按评估时点的原态生效。registered（梯底）无级
    可降；retired/demoted 不在梯上不动。晋升恒人审：本函数只写降级目标。
    """
    clvs = rolling_clv_window(conn)
    a_fired = clv_window_fired(clvs)
    b_fired = drawdown_fired(bankroll_history(conn, initial_bankroll=initial_bankroll))
    c_fired = (
        calibration_drift_fired(*calibration_reading)
        if calibration_reading is not None
        else False
    )
    reasons = {
        "A": (
            f"rolling clv mean={fmean(clvs):.6f} n={len(clvs)}"
            if clvs
            else "rolling clv window empty"
        ),
        "B": f"account drawdown>{TRIGGER_B_DRAWDOWN:.0%}",
    }
    if calibration_reading is not None:
        deviation, se = calibration_reading
        reasons["C"] = f"calibration drift deviation={deviation:.6f} se={se:.6f}"
    demotions: list[Demotion] = []
    versions = conn.execute(
        "SELECT id, label FROM strategy_versions ORDER BY id"
    ).fetchall()
    for fired, actor in (
        (b_fired, "trigger:B"),
        (a_fired, "trigger:A"),
        (c_fired, "trigger:C"),
    ):
        if not fired:
            continue
        for row in versions:
            version_id = int(row["id"])
            state = current_state(conn, version_id)
            if state is None or state not in LADDER:  # retired/demoted 不动
                continue
            if actor == "trigger:B":
                if state is not StrategyState.LIVE_REAL:
                    continue  # 纸面态仅告警（fired 面已记）
                target = StrategyState.DEMOTED
            else:
                if state is StrategyState.REGISTERED:
                    continue  # 梯底无级可降
                target = LADDER[LADDER.index(state) - 1]
            transition_version(
                conn,
                version_id,
                target,
                actor=actor,
                reason=reasons[actor[-1]],
            )
            demotions.append(
                Demotion(
                    version_id=version_id,
                    label=str(row["label"]),
                    trigger=actor[-1],
                    from_state=state,
                    to_state=target,
                    reason=reasons[actor[-1]],
                )
            )
    return RollbackResult(
        fired={"A": a_fired, "B": b_fired, "C": c_fired}, demotions=demotions
    )


# ---- 消费端最小接线（票 01 交付 5：下单前查状态，非 live 态跳过留痕）----


def params_fingerprint(params: object) -> str:
    """
    auto_strategy 参数指纹：冻结 dataclass 的规范序列化 sha256。

    同 dc_model._rows_fingerprint 的内容寻址先例——改参数=换指纹=升版本
    （prereg C 节纪律）。逐字段取值（键序钉死）；嵌套 dataclass 走 repr
    （frozen dataclass 的 repr 确定性成立）。
    """
    if not is_dataclass(params) or isinstance(params, type):  # pragma: no cover
        raise TypeError("params_fingerprint 只接受参数 dataclass 实例")
    values = {field.name: getattr(params, field.name) for field in fields(params)}
    canonical = json.dumps(
        values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=repr
    )
    return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class EngineGate:
    """消费端闸读数：allowed=可下纸面注；state/version_id=None=未注册。"""

    allowed: bool
    reason: str
    state: StrategyState | None = None
    version_id: int | None = None


def engine_gate(conn: sqlite3.Connection, *, params_version: str) -> EngineGate:
    """
    引擎下注闸：该参数指纹下任一版本处 live 态才放行。

    - 有版本、任一 live-paper/live-real → 放行（version_id=最新 live 行）；
    - 有版本、无 live → 拒（version_not_live——shadow/probe/registered/
      demoted/retired 一律拒）；
    - 无版本 → 放行（version_unregistered：票 03 challenger 自动注册前的
      未版本化时代 seam，闸随注册全覆盖）。
    """
    rows = conn.execute(
        """
        SELECT v.id, t.to_state AS state FROM strategy_versions v
        JOIN version_transitions t ON t.version_id = v.id
        WHERE v.params_version = ?
          AND t.id = (SELECT MAX(t2.id) FROM version_transitions t2
                      WHERE t2.version_id = v.id)
        ORDER BY v.id
        """,
        (params_version,),
    ).fetchall()
    if not rows:
        return EngineGate(allowed=True, reason="version_unregistered")
    for row in rows:
        if str(row["state"]) in LIVE_STATES:
            return EngineGate(
                allowed=True,
                reason="live",
                state=StrategyState(str(row["state"])),
                version_id=int(row["id"]),
            )
    first = StrategyState(str(rows[0]["state"]))
    return EngineGate(
        allowed=False,
        reason="version_not_live",
        state=first,
        version_id=int(rows[0]["id"]),
    )
