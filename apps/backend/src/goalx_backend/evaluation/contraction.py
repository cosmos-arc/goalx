"""
收缩双钟与五级反向金丝雀（vision-build 票 08）。

prereg 附录（edge-program prereg 文件族 06-prereg-appendix-contraction-
clocks，冻结 2026-11-10）的代码面：五级反向金丝雀（L0 全速→L1 停新注册
→L2 停探针+自动选票→L3 采集缩监测最小集→L4 全停只留漂移监测）、双钟
触发判据（CLV 主钟滚动 183 日 / 探索钟幸存者 365 日窗）、四信号重扫唤醒
（PSI 越阈等）。

不变量（票 08）：
- 分级/触发/重扫全预注册在先；级别变更只能人 CLI（actor=human 前缀，
  agent 无路径）；变更落痕 contraction_transitions（v52 append-only），
  现态=最新行。
- 钟只建议不执行：读数越门=告警面（晨检+月读），永无自动降级。
- 逐级降逐级可逆（转移图=相邻一级，重升必经 L2）；漂移监测+季度轻扫
  常设不撤。
- 常数=prereg D 节冻结值；调整=改代码=PR 人审（代码永不自改）。
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from statistics import fmean, median, pstdev, quantiles

from goalx_backend.betting.versioning import settled_clvs_since
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import edge_scan

# ---- prereg 常数（附录 D 节冻结值；调整=改代码=PR 人审） ----

CLV_CLOCK_WINDOW_DAYS = 183  # 滚动 6 月窗
CLV_CLOCK_THRESHOLD = 0.03  # 3%（概率域）
CLV_CLOCK_MIN_N = 50  # 票 01 触发器 A 滚动窗同数
CLV_CLOCK_SE_GATE = 2.0  # 单侧 2SE（票 01 同门）
EXPLORATION_WINDOW_DAYS = 365  # 12 月窗
#: B.3 探针全停计时窗（prereg 附录 B.3：L2+ 满窗且四信号零触发→建议再降一级）
B3_WINDOW_DAYS = 183
#: 探索钟锚点=周批首跑日（prereg B.2：不折算历史段，只有前向能答）。
#: 运行面真源=exploration_clock_anchor 账行（v54，周批首跑经
#: ensure_exploration_anchor 一次性钉值）；本常量=无账行时的 prereg 兜底
#: （票 08 冻结值 None，账行落值前读数恒 unanchored）。
EXPLORATION_CLOCK_ANCHOR: str | None = None
EM_SWEEP_FRESH_HOURS = 26.0  # EM 阵列新鲜界（晨检同口径；s5 期次节奏不进）
#: EM 日频阵列（新鲜度面）：与晨检项 17 同集合
_EM_DAILY_SCANNERS = ("s1", "s2", "s4")
PSI_THRESHOLD = 0.25  # 越阈（严格 >；业界 0.1/0.25 档，0.25=显著移位）
PSI_BINS = 10  # 等频分箱数（参考十分位）
SIGNAL_CURRENT_WINDOW_DAYS = 30  # 信号当前窗
SIGNAL_REFERENCE_WINDOW_DAYS = 90  # 信号参考窗


class ContractionLevel(StrEnum):
    """收缩级别（五级反向金丝雀，prereg 附录 A 节）。"""

    L0 = "L0"  # 全速
    L1 = "L1"  # 停新注册
    L2 = "L2"  # 停探针+自动选票
    L3 = "L3"  # 采集缩到监测最小集
    L4 = "L4"  # 全停只留漂移监测


#: 梯序（相邻一级可转移；跳级/自环恒拒）
_LEVEL_LADDER: tuple[ContractionLevel, ...] = (
    ContractionLevel.L0,
    ContractionLevel.L1,
    ContractionLevel.L2,
    ContractionLevel.L3,
    ContractionLevel.L4,
)


def _adjacent_pairs() -> list[tuple[ContractionLevel, ContractionLevel]]:
    """相邻级别对（(L0,L1)…(L3,L4)）。"""
    return list(pairwise(_LEVEL_LADDER))


# 逐级降 + 逐级可逆（重升必经 L2 是相邻步进的自然结果——深收缩态回
# L0 唯一路径 L4→L3→L2→L1→L0，L2=重验证停靠级，prereg A 节）
_ADJACENT = _adjacent_pairs()
_ALLOWED_TRANSITIONS: frozenset[tuple[ContractionLevel, ContractionLevel]] = frozenset(
    _ADJACENT + [(nxt, lvl) for lvl, nxt in _ADJACENT]
)


def transition_allowed(
    from_level: ContractionLevel, to_level: ContractionLevel
) -> bool:
    """转移谓词：恰允许相邻一级（跳级/自环拒绝，全表单测钉死）。"""
    return (from_level, to_level) in _ALLOWED_TRANSITIONS


def level_rank(level: ContractionLevel) -> int:
    """级别的梯序（L0=0 … L4=4；建议已处置判定=现级序 ≥ 建议级序）。"""
    return _LEVEL_LADDER.index(level)


@dataclass(frozen=True)
class DeploymentPlan:
    """
    一个收缩级别的 deployment 动作面（纯映射，票 03 周批落地后消费）。

    weekly_batch/monthly_read_cadence/ingest_scope 是「暂停/改名不删」的
    表达：deployment 配置仍在，动作按级别改道。
    """

    new_registrations: bool
    probes_and_auto_selection: bool
    weekly_batch: str  # "run" | "paused"
    monthly_read_cadence: str  # "monthly" | "quarterly"
    ingest_scope: str  # "full" | "monitoring_minimal" | "drift_only"
    drift_monitoring: bool


#: 级别→动作面（prereg E 节映射；漂移监测恒开=常设不撤）
_PLANS: dict[ContractionLevel, DeploymentPlan] = {
    ContractionLevel.L0: DeploymentPlan(
        new_registrations=True,
        probes_and_auto_selection=True,
        weekly_batch="run",
        monthly_read_cadence="monthly",
        ingest_scope="full",
        drift_monitoring=True,
    ),
    ContractionLevel.L1: DeploymentPlan(
        new_registrations=False,
        probes_and_auto_selection=True,
        weekly_batch="run",
        monthly_read_cadence="monthly",
        ingest_scope="full",
        drift_monitoring=True,
    ),
    ContractionLevel.L2: DeploymentPlan(
        new_registrations=False,
        probes_and_auto_selection=False,
        weekly_batch="paused",
        monthly_read_cadence="quarterly",
        ingest_scope="full",
        drift_monitoring=True,
    ),
    ContractionLevel.L3: DeploymentPlan(
        new_registrations=False,
        probes_and_auto_selection=False,
        weekly_batch="paused",
        monthly_read_cadence="quarterly",
        ingest_scope="monitoring_minimal",
        drift_monitoring=True,
    ),
    ContractionLevel.L4: DeploymentPlan(
        new_registrations=False,
        probes_and_auto_selection=False,
        weekly_batch="paused",
        monthly_read_cadence="quarterly",
        ingest_scope="drift_only",
        drift_monitoring=True,
    ),
}


def deployment_plan(level: ContractionLevel) -> DeploymentPlan:
    """级别→deployment 动作映射（纯函数可复算；L0→L1→L0 演练的恢复面）。"""
    return _PLANS[level]


# ---- 级别账（contraction_transitions，v52 append-only；变更恒人 CLI）----


class InvalidLevelTransition(ValueError):
    """非法级别转移（谓词拒绝或非人 actor；转移零写入）。"""


def current_level(conn: sqlite3.Connection) -> ContractionLevel:
    """现收缩级别=最新 transitions 行 to 级（genesis L0 随 v52 落账，账非空）。"""
    row = conn.execute(
        "SELECT level FROM contraction_transitions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:  # pragma: no cover - v52 genesis 钉死账非空
        return ContractionLevel.L0
    return ContractionLevel(str(row["level"]))


def current_plan(conn: sqlite3.Connection) -> DeploymentPlan:
    """现级别的 deployment 动作面（票 03 周批 deployment 消费口）。"""
    return deployment_plan(current_level(conn))


def set_level(
    conn: sqlite3.Connection,
    to_level: ContractionLevel,
    *,
    actor: str,
    reason: str,
) -> ContractionLevel:
    """
    落一次级别变更（先验谓词；非法抛 InvalidLevelTransition 且零写入）。

    级别变更恒人审（prereg A 节：agent 无路径）——只允许 actor 以
    ``human`` 起头的调用方（人 CLI），任何方向的变更（升/降）都不得走
    自动路径：钟只建议不执行（晨检/月读=告警面）。check-then-act 在
    BEGIN IMMEDIATE 写事务内闭环（票 01 correctness F1 同构）。
    """
    with atomic(conn):
        from_level = current_level(conn)
        if not transition_allowed(from_level, to_level):
            raise InvalidLevelTransition(
                f"非法级别转移: {from_level.value} → {to_level.value}"
                + "（相邻一级，跳级/自环拒绝；要快=连发相邻 CLI）"
            )
        if not actor.startswith("human"):
            raise InvalidLevelTransition(f"级别变更恒人 CLI（actor={actor} 无路径）")
        conn.execute(
            """
            INSERT INTO contraction_transitions (level, actor, reason, ts)
            VALUES (?, ?, ?, ?)
            """,
            (to_level.value, actor, reason, utc_now_iso()),
        )
    return from_level


# ---- CLV 主钟（prereg B.1：滚动 183 日 / 3% 阈 / 2SE 单侧 / n≥50）----


@dataclass(frozen=True)
class CLVClockReading:
    """CLV 主钟一次读数（概率域均值；insufficient 只报不判）。"""

    window_start: str
    window_end: str
    n: int
    mean: float | None
    se: float | None
    status: str  # "ok" | "insufficient"
    clock: str = "clv_main"

    @property
    def fired(self) -> bool:
        """触发建议 L1（严格不等恰界不触发；n 不足恒不触发）。"""
        if self.status != "ok" or self.mean is None or self.se is None:
            return False
        return clv_clock_fired_from_stats(self.n, self.mean, self.se)


def clv_clock_fired(clvs: Sequence[float]) -> bool:
    """
    主钟谓词：n≥50 ∧ mean<3% ∧ 非显著为正（严格不等，恰界不触发）。

    「非显著为正」= 无法在单侧 2SE 水平确认 CLV>0，即 ¬(mean>0 ∧
    mean>2·SE)；恰 2SE 不显著（严格 > 才显著）。SE=pstdev/√n（票 01
    触发器 A 同款算路）。
    """
    if len(clvs) < CLV_CLOCK_MIN_N:
        return False
    mean = fmean(clvs)
    se = pstdev(clvs) / (len(clvs) ** 0.5)
    return clv_clock_fired_from_stats(len(clvs), mean, se)


def clv_clock_fired_from_stats(n: int, mean: float, se: float) -> bool:
    """主钟谓词（统计量形：读数账回放与 B.1 边界测试的独立算路口）。"""
    if n < CLV_CLOCK_MIN_N:
        return False
    return mean < CLV_CLOCK_THRESHOLD and not (
        mean > 0.0 and mean > CLV_CLOCK_SE_GATE * se
    )


def clv_clock_reading(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> CLVClockReading:
    """
    主钟读数：探针注面（bets.clv，票 01 触发器 A 同源）滚动 183 日闭窗。

    n<50=insufficient 只报不判（prereg B.1）。窗端点 ISO 串随读数返回
    （读数账 window_start/window_end 落账口径）。
    """
    resolved = now or datetime.now(UTC)
    window_start = (resolved - timedelta(days=CLV_CLOCK_WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )
    window_end = resolved.isoformat(timespec="seconds")
    clvs = settled_clvs_since(conn, window_start=window_start)
    if len(clvs) < CLV_CLOCK_MIN_N:
        return CLVClockReading(
            window_start=window_start,
            window_end=window_end,
            n=len(clvs),
            mean=None,
            se=None,
            status="insufficient",
        )
    return CLVClockReading(
        window_start=window_start,
        window_end=window_end,
        n=len(clvs),
        mean=fmean(clvs),
        se=pstdev(clvs) / (len(clvs) ** 0.5),
        status="ok",
    )


_MONTHS_PER_YEAR = 12

# ---- PSI（prereg C 节：分箱分布比较；>0.25 越阈严格不等）----


def psi_exceeds(value: float) -> bool:
    """PSI 越阈谓词：严格 >0.25（恰阈不触发，票 01 严格不等先例）。"""
    return value > PSI_THRESHOLD


def _half_sample_proportion(count: int, n: int) -> float:
    """桶比例：空桶以 1/(2n) 半样本代理（prereg C 口径），否则 count/n。"""
    if count == 0:
        return 1.0 / (2 * n)
    return count / n


def psi(
    reference: Sequence[float], current: Sequence[float], *, bins: int = PSI_BINS
) -> float:
    """
    连续分布 PSI：参考样本十分位等频桶，两样本同桶计数比例对比。

    桶边界=参考样本 ``bins`` 分位（statistics.quantiles inclusive）；桶
    半开 [e_i, e_{i+1})，末桶闭端且首/末桶外无界（<e_1 归首桶、≥e_{k-1}
    归末桶）。空桶比例 1/(2n)（各自样本量）代理；空样本 fail-loud
    （ValueError——调用方判 insufficient，不伪造分布）。
    """
    if not reference or not current:
        raise ValueError("PSI 空样本（参考/当前窗各需非空样本）")
    edges = quantiles(reference, n=bins, method="inclusive")
    n_ref, n_cur = len(reference), len(current)

    def bucketize(values: Sequence[float]) -> list[int]:
        counts = [0] * bins
        for value in values:
            index = bisect.bisect_right(edges, value)
            counts[min(index, bins - 1)] += 1
        return counts

    total = 0.0
    for ref_count, cur_count in zip(
        bucketize(reference), bucketize(current), strict=True
    ):
        p_ref = _half_sample_proportion(ref_count, n_ref)
        p_cur = _half_sample_proportion(cur_count, n_cur)
        total += (p_cur - p_ref) * math.log(p_cur / p_ref)
    return total


def psi_categorical(reference: Mapping[str, int], current: Mapping[str, int]) -> float:
    """类别分布 PSI：类别=两计数面并集；空类 1/(2n) 半样本代理。"""
    total_ref = sum(reference.values())
    total_cur = sum(current.values())
    if total_ref == 0 or total_cur == 0:
        raise ValueError("类别 PSI 空样本（参考/当前窗各需非空计数）")
    total = 0.0
    for category in sorted(set(reference) | set(current)):
        p_ref = _half_sample_proportion(reference.get(category, 0), total_ref)
        p_cur = _half_sample_proportion(current.get(category, 0), total_cur)
        total += (p_cur - p_ref) * math.log(p_cur / p_ref)
    return total


# ---- 阈值历史基线核对报告缝（prereg B.1；回放全量跑=消费票）----


@dataclass(frozen=True)
class ReplayWindowReading:
    """回放窗读数一行（十年回放滚动 6 月窗；消费票喂，本票只落核对缝）。"""

    window_end: str
    n: int
    mean: float
    se: float


def clv_threshold_baseline(
    readings: Sequence[ReplayWindowReading],
) -> dict[str, float | int]:
    """
    3% 阈现实性核对报告（人裁 7② 输入面；fixture 可复算）。

    消费票把十年回放的滚动 6 月窗读数喂进来：报告窗均值分布对阈值的
    位置与「若按主钟口径逐窗判读会触发多少」——赢面系统的回放窗若大
    比例触发，即阈失真（校准结论=人裁，报告只出数）。n<50 的窗不进
    判读面（evaluated 如实分列，不冒充）。
    """
    evaluated = [r for r in readings if r.n >= CLV_CLOCK_MIN_N]
    means = sorted(r.mean for r in evaluated)
    n_evaluated = len(evaluated)
    below = sum(1 for r in evaluated if r.mean < CLV_CLOCK_THRESHOLD)
    fired = sum(1 for r in evaluated if clv_clock_fired_from_stats(r.n, r.mean, r.se))
    return {
        "n_windows": len(readings),
        "evaluated": n_evaluated,
        "share_below_threshold": below / n_evaluated if n_evaluated else 0.0,
        "share_fired": fired / n_evaluated if n_evaluated else 0.0,
        "median_mean": median(means) if means else 0.0,
        "min_mean": means[0] if means else 0.0,
        "max_mean": means[-1] if means else 0.0,
    }


# ---- 四信号重扫唤醒（prereg C 节：信号面 + 接线状态）----


@dataclass(frozen=True)
class RescanSignalFace:
    """一个重扫唤醒信号面的读数（fired=唤醒告警；unwired=留缝不伪造绿）。"""

    name: str
    status: str  # "ok" | "fired" | "insufficient" | "unwired"
    detail: str


def _next_quarter_start(now: datetime) -> str:
    """下次季度首月首日（信号④定拍锚：1/4/7/10 月 1 日，UTC）。"""
    year, quarter_month = now.year, ((now.month - 1) // 3 + 1) * 3 + 1
    if quarter_month > _MONTHS_PER_YEAR:
        year, quarter_month = year + 1, 1
    return datetime(year, quarter_month, 1, tzinfo=UTC).date().isoformat()


def rescan_signals(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> list[RescanSignalFace]:
    """
    四信号重扫唤醒读数（prereg C 节；收缩态下恢复探索的唯一口子）。

    信号触发只告警不自动恢复（晨检黄面+月读记档；恢复走人 CLI 相邻
    步进）。已接线面=③ 联赛分布（fixtures 面，类别 PSI 当前 30 日 vs
    前 90 日）；① PSI 函数已落、统计量定义随消费票人裁后接线；②④
    无在滚检测面，如实 unwired（不伪造绿）。第五面 l3l4_timer=B.3 到期
    读数（票 04 接线）：状态词表独立于四信号（not_applicable|timing|
    due_*，永不为 fired——B.3 建议的是再收缩，不是唤醒重扫）。
    """
    resolved = now or datetime.now(UTC)
    iso = resolved.isoformat(timespec="seconds")
    current_start = (resolved - timedelta(days=SIGNAL_CURRENT_WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )
    reference_start = (
        resolved
        - timedelta(days=SIGNAL_CURRENT_WINDOW_DAYS + SIGNAL_REFERENCE_WINDOW_DAYS)
    ).isoformat(timespec="seconds")
    current = fx_store.fixture_counts_by_competition(
        conn, kickoff_from_iso=current_start, kickoff_to_iso=iso
    )
    reference = fx_store.fixture_counts_by_competition(
        conn, kickoff_from_iso=reference_start, kickoff_to_iso=current_start
    )
    if not current or not reference:
        league = RescanSignalFace(
            name="league_mix", status="insufficient", detail="窗内无场次样本"
        )
    else:
        value = psi_categorical(reference, current)
        top = max(current, key=lambda name: current[name])
        league = RescanSignalFace(
            name="league_mix",
            status="fired" if psi_exceeds(value) else "ok",
            detail=f"PSI={value:.4f}（当前窗 {sum(current.values())} 场"
            + f" · 最重 {top} {current[top]} 场；阈 0.25 严格 >）",
        )
    timer = l3l4_timer_reading(conn, now=resolved)
    return [
        RescanSignalFace(
            name="odds_psi",
            status="unwired",
            detail="PSI 函数已落（contraction.psi）；场级结构统计量"
            + "（had 归一化热门度候选）与 PIT 准入随消费票人裁钉后接线",
        ),
        RescanSignalFace(
            name="jc_rules",
            status="unwired",
            detail="JC 玩法/抽水面无在滚检测面（jc-audit 为一次性分析面）",
        ),
        league,
        RescanSignalFace(
            name="quarterly_sweep",
            status="unwired",
            detail=f"下次季度轻扫 {_next_quarter_start(resolved)}"
            + "（定拍 deployment 未建，四信号静默期保底）",
        ),
        # prereg B.3 计时面（票 04 接线）：L2+ 满 183 日且四信号零触发→
        # 建议再降一级；触发史账=rescan_signal_events（v55，月读写口）。
        RescanSignalFace(
            name="l3l4_timer",
            status=timer.status,
            detail=timer.detail,
        ),
    ]


# ---- B.3 探针全停计时（prereg 附录 B.3；票 04 缝回填） ----


@dataclass(frozen=True)
class L3L4TimerReading:
    """
    B.3 到期读数：锚=最新进入 L2 的 transitions ts（重升打断即重启计时）。

    只建议不执行（月读⑥节读数）；建议目标=相邻一级（L4=底，无更降）。
    """

    # status: not_applicable|timing|due_suggest_downgrade|due_triggers_present|due_floor
    status: str
    l2_entered_at: str | None
    days: int | None
    fired_count: int | None
    zero_triggers: bool | None
    suggest_to: ContractionLevel | None
    detail: str


def fired_rescan_since(conn: sqlite3.Connection, *, since_iso: str) -> int:
    """锚（ISO 闭端含）以来 fired 信号计数（B.3 触发史读口，v55 账）。"""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM rescan_signal_events
            WHERE status = 'fired' AND evaluated_at >= ?
            """,
            (since_iso,),
        ).fetchone()[0]
    )


def _probe_stop_anchor(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """
    探针全停段锚行：末段连续 ≥L2 的首行（=最新进入 L2）。

    前向扫一遍：见 <L2 行即清锚（重升期探针曾恢复，计时重启），见 ≥L2
    行且无锚即记锚——循环终了留存的即末段锚。当前级别 <L2 → None。
    """
    anchor: sqlite3.Row | None = None
    for row in conn.execute(
        "SELECT level, ts FROM contraction_transitions ORDER BY id"
    ).fetchall():
        rank = level_rank(ContractionLevel(str(row["level"])))
        if rank >= level_rank(ContractionLevel.L2):
            if anchor is None:
                anchor = row
        else:
            anchor = None
    return anchor


def l3l4_timer_reading(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> L3L4TimerReading:
    """
    B.3 读数：L2+ 满 183 日且四信号零触发→建议再降一级（人 CLI 执行）。

    窗起点=探针全停锚（唯一时钟——L2→L3→L4 不重启，L2→L1→L2 重启）；
    零触发=锚以来 rescan_signal_events 无 fired 行（v55 账，月读落值——
    账存在月读面之前的历史段如实按零计：无评估=无触发记录）。
    """
    resolved = now or datetime.now(UTC)
    anchor = _probe_stop_anchor(conn)
    if anchor is None:
        return L3L4TimerReading(
            status="not_applicable",
            l2_entered_at=None,
            days=None,
            fired_count=None,
            zero_triggers=None,
            suggest_to=None,
            detail="现级 <L2（探针未全停），B.3 计时不适用",
        )
    entered_at = str(anchor["ts"])
    try:
        days = (resolved - _parse_iso_utc(entered_at)).days
    except ValueError:
        # 坏锚串不炸读数面（_age_hours 同防御）：晨检消费本读数，账面
        # 完整性问题显式上抛会红掉整个晨检项——如实标坏锚，人修账。
        return L3L4TimerReading(
            status="bad_anchor",
            l2_entered_at=entered_at,
            days=None,
            fired_count=None,
            zero_triggers=None,
            suggest_to=None,
            detail=f"锚 ts 非法 ISO 串 {entered_at!r}（contraction_transitions "
            + "账面完整性问题，人核查修复）",
        )
    fired = fired_rescan_since(conn, since_iso=entered_at)
    zero = fired == 0
    current = current_level(conn)
    due = days >= B3_WINDOW_DAYS
    if due and zero and current is ContractionLevel.L4:
        status, suggest = "due_floor", None
    elif due and zero:
        status = "due_suggest_downgrade"
        suggest = _LEVEL_LADDER[level_rank(current) + 1]
    elif due:
        status, suggest = "due_triggers_present", None
    else:
        status, suggest = "timing", None
    detail = (
        f"探针全停自 {entered_at}（{days} 日 / 窗 {B3_WINDOW_DAYS} 日）；"
        + f"锚后 fired 信号 {fired} 次"
        + (
            f"；建议再降一级 → {suggest.value}（人 CLI contraction-set）"
            if suggest is not None
            else "；无降级建议"
            if due
            else f"；窗未满（余 {B3_WINDOW_DAYS - days} 日）"
        )
    )
    return L3L4TimerReading(
        status=status,
        l2_entered_at=entered_at,
        days=days,
        fired_count=fired,
        zero_triggers=zero,
        suggest_to=suggest,
        detail=detail,
    )


def record_rescan_signals(
    conn: sqlite3.Connection, *, period: str, now: datetime | None = None
) -> list[RescanSignalFace]:
    """
    四信号评估落账（v55；月读写口——晨检只读红线，信号史只能月读补）。

    月读跑时点评估一次并落账：UNIQUE(signal, period) 吸收同月重跑（账面
    =每月首读留痕，报告拍次递增不影响账）。B.3 timer 是读数不落账；返回
    =四信号面 + 落账后重算的 B.3 timer 面（本月 fired 已计入触发史）。
    """
    resolved = now or datetime.now(UTC)
    faces = rescan_signals(conn, now=resolved)
    evaluated_at = resolved.isoformat(timespec="seconds")
    with atomic(conn):
        for face in faces:
            if face.name == "l3l4_timer":
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO rescan_signal_events
                    (signal, period, status, detail, evaluated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (face.name, period, face.status, face.detail, evaluated_at),
            )
    timer = l3l4_timer_reading(conn, now=resolved)
    signal_faces = [face for face in faces if face.name != "l3l4_timer"]
    return [
        *signal_faces,
        RescanSignalFace(name="l3l4_timer", status=timer.status, detail=timer.detail),
    ]


# ---- 探索钟（prereg B.2：365 日窗 / 锚点 / EM 全阴 + 零幸存者）----


def ensure_exploration_anchor(
    conn: sqlite3.Connection, anchor_date: str, *, actor: str
) -> str:
    """
    探索钟锚点一次性钉值（票 03 周批首跑写口）：无锚=写入，有锚=返回既有。

    append-only（v54 账行 UPDATE/DELETE 库层拒绝）且一次性：幂等重跑
    （同值）与后续周拍（异值）都不覆盖——锚量的是「新回路首跑日」，
    只有第一次真跑算数。周批 flow 每次真跑前调用（幂等可常调）；
    返回生效锚（ISO 日期串）供留痕。
    """
    with atomic(conn):
        row = conn.execute(
            "SELECT anchor_date FROM exploration_clock_anchor WHERE id = 1"
        ).fetchone()
        if row is not None:
            return str(row["anchor_date"])
        conn.execute(
            """
            INSERT INTO exploration_clock_anchor (id, anchor_date, actor, ts)
            VALUES (1, ?, ?, ?)
            """,
            (anchor_date, actor, utc_now_iso()),
        )
    return anchor_date


def _effective_anchor(conn: sqlite3.Connection) -> str | None:
    """生效锚点：DB 账行优先，缺席回退 prereg 常量（票 08 行为兜底）。"""
    row = conn.execute(
        "SELECT anchor_date FROM exploration_clock_anchor WHERE id = 1"
    ).fetchone()
    if row is not None:
        return str(row["anchor_date"])
    return EXPLORATION_CLOCK_ANCHOR


@dataclass(frozen=True)
class ExplorationClockReading:
    """探索钟一次读数（幸存者计数面；unanchored/insufficient 只报不判）。"""

    window_start: str | None
    window_end: str
    new_findings: int
    survivors: int
    scanners_fresh: bool
    status: str  # "ok" | "unanchored" | "insufficient"
    clock: str = "exploration"

    @property
    def fired(self) -> bool:
        """触发建议 L2：满弦 ∧ 阵列新鲜 ∧ 窗内零越门发现 ∧ 零幸存者。"""
        return (
            self.status == "ok"
            and self.scanners_fresh
            and self.new_findings == 0
            and self.survivors == 0
        )


def _parse_iso_utc(stamp: str) -> datetime:
    """
    ISO 串解析；naive 串按 UTC 补 tzinfo（晨检 _age_hours 同防御）。

    aware−naive 相减抛 TypeError（correctness F1）——旧数据/手拍行的
    naive 时间戳不少见，统一按 UTC 读不炸读数面；坏串 ValueError 由
    调用方按语境定（新鲜度=不新鲜，常数=代码审 fail-loud）。
    """
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    """
    ISO 串距 now 的小时数；空/坏串 None。

    未知不算新鲜（晨检 _age_hours 同语义）：坏留痕读作不新鲜→钟
    insufficient，不炸晨检；naive 串按 UTC 补（_parse_iso_utc）。
    """
    if stamp is None:
        return None
    try:
        parsed = _parse_iso_utc(stamp)
    except ValueError:
        return None
    return (now - parsed).total_seconds() / 3600.0


def _em_sweeps_fresh(conn: sqlite3.Connection, *, now: datetime) -> bool:
    """EM 日频阵列新鲜：s1/s2/s4 各自最新留痕 ≤26h（晨检项 17 同口径）。"""
    for scanner in _EM_DAILY_SCANNERS:
        age = _age_hours(edge_scan.latest_observation_at(conn, scanner), now)
        if age is None or age > EM_SWEEP_FRESH_HOURS:
            return False
    return True


def exploration_clock_reading(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> ExplorationClockReading:
    """
    探索钟读数：窗内越门发现计数 + 幸存者计数（prereg B.2 闭窗为凭）。

    幸存者=promoted 发现（found_at 为凭）∪ supported 假设
    （status_changed_at 为凭）。锚点（周批首跑日）未钉=unanchored；
    满弦（锚点 ≤ now−365 日，恰 365 日=满弦）前=insufficient；阵列停摆
    =insufficient（缺数据的「全阴」不是全阴）——三者皆只报不判。
    """
    resolved = now or datetime.now(UTC)
    window_end = resolved.isoformat(timespec="seconds")
    fresh = _em_sweeps_fresh(conn, now=resolved)
    anchor_raw = _effective_anchor(conn)
    if anchor_raw is None:
        return ExplorationClockReading(
            window_start=None,
            window_end=window_end,
            new_findings=0,
            survivors=0,
            scanners_fresh=fresh,
            status="unanchored",
        )
    anchor = _parse_iso_utc(anchor_raw)
    if anchor > resolved - timedelta(days=EXPLORATION_WINDOW_DAYS):
        return ExplorationClockReading(
            window_start=anchor.isoformat(timespec="seconds"),
            window_end=window_end,
            new_findings=0,
            survivors=0,
            scanners_fresh=fresh,
            status="insufficient",
        )
    window_start = (resolved - timedelta(days=EXPLORATION_WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )
    new_findings = int(
        conn.execute(
            "SELECT COUNT(*) FROM edge_findings WHERE found_at >= ?",
            (window_start,),
        ).fetchone()[0]
    )
    promoted = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM edge_findings
            WHERE status = 'promoted' AND found_at >= ?
            """,
            (window_start,),
        ).fetchone()[0]
    )
    supported = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM hypothesis_registry
            WHERE status = 'supported' AND status_changed_at >= ?
            """,
            (window_start,),
        ).fetchone()[0]
    )
    status = "ok" if fresh else "insufficient"
    return ExplorationClockReading(
        window_start=window_start,
        window_end=window_end,
        new_findings=new_findings,
        survivors=promoted + supported,
        scanners_fresh=fresh,
        status=status,
    )


def record_clock_readings(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> tuple[CLVClockReading, ExplorationClockReading]:
    """
    双钟显式读并落账（人 CLI contraction-read/月读口；晨检只读不写）。

    每次显式读每钟追加一行（观测流水）：window_end=运行时刻秒精度，
    UNIQUE(clock, window_end) 的真实职责是同窗（同秒）双写防重——不同
    时刻的真实重跑各留一行（append 无害，账面=读数流水非幂等面，
    correctness F3 裁定口径）。value 语义随钟：
    clv_main=概率域均值（insufficient 无均值记 0.0，status 在 detail）；
    exploration=幸存者计数（n=窗内越门发现数）。
    """
    clv = clv_clock_reading(conn, now=now)
    exploration = exploration_clock_reading(conn, now=now)
    with atomic(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO contraction_clock_readings
                (clock, window_start, window_end, n, value, se, fired, detail, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clv.clock,
                clv.window_start,
                clv.window_end,
                clv.n,
                clv.mean if clv.mean is not None else 0.0,
                clv.se,
                int(clv.fired),
                json.dumps(
                    {"status": clv.status, "mean": clv.mean, "se": clv.se},
                    ensure_ascii=False,
                ),
                utc_now_iso(),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO contraction_clock_readings
                (clock, window_start, window_end, n, value, se, fired, detail, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exploration.clock,
                exploration.window_start or "",
                exploration.window_end,
                exploration.new_findings,
                float(exploration.survivors),
                None,
                int(exploration.fired),
                json.dumps(
                    {
                        "status": exploration.status,
                        "survivors": exploration.survivors,
                        "new_findings": exploration.new_findings,
                        "scanners_fresh": exploration.scanners_fresh,
                    },
                    ensure_ascii=False,
                ),
                utc_now_iso(),
            ),
        )
    return clv, exploration
