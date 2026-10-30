"""
延迟生存率 Stage 1 到达率审计（report-audit-20261027 票 07 预注册首跑）。

研究问题：同步报价账本的价格差候选在真实条件下的**到达率**——观察
延迟与销售窗口之后，优势还在不在。四层分母（L1 全部报价机会 →
L2 规则合格 → L3 延迟后可达 → L4 实际接受，L4 留空）+ 唯一主指标：
L2 行中决策时刻 value_proxy>0 且收盘仍为正（同身份对）的比例，
Wilson 95% CI。

口径硬约束（预注册 .scratch/report-audit-20261027/prereg-delay-survival.md；
首跑后主指标与分母不换，修订须记变更日志）：

- L2 销售状态用账本自带 sale_state（sale_statuses 是观测节奏，时点
  对不齐，不另查）；单关玩法另须 single_eligible=1；
- 回填行=决策时刻参考价不存在（事后 sweep 现算补齐），直接出 L3
  并在缺失原因分层单独计数；参考至今仍缺=no_reference 单列；
- 收盘比对仅参考身份同类（anchor/consensus 白名单，票 03）——
  unknown/mismatch 行分母外单列，不静默混层；
- Stage 1 只判读 d=0 档；30/60/180/300 秒档骨架保留标 pending；
- 停止条件（预注册定值）：回填占 L1 >30% 且剔除回填与陈旧(>300s)
  后 L3 正 EV <每周 1 个 → 判"到达性不足"归档；
- 首跑定位 shakeout 不产结论；≥30 销售日才出主指标正式读数。

收盘概率经注入缝 close_provider（生产=evaluation.clv.srct_closing_prob
同口径，由 cli/tasks 层装配；data 层不 import evaluation——ADR-0008
分层）。日界=observed_at 的 UTC 自然日（当期拍点 11:00 UTC 与北京
自然日一致；换拍点时刻时此口径须随变更日志复核）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import NormalDist, quantiles
from typing import Any

from goalx_backend import run_fingerprint as rfp
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.markets import SELECTIONS

REPORTS_DIR = "reports"
REPORT_BASENAME = "stage1-arrival-audit"

# 延迟档位=实验条件（秒）；Stage 1 只判读 0，其余骨架保留
DELAY_TIERS: tuple[int, ...] = (0, 30, 60, 180, 300)
REPORTED_TIER = 0

# 停止条件定值（prereg §6，首跑前定死不得回调）
BACKFILL_SHARE_STOP = 0.30
STALE_STOP_SECONDS = 300.0
L3_POSITIVE_PER_WEEK_STOP = 1.0

SALE_DAY_GATE = 30  # 正式判读门槛（prereg §7）

# 主指标收盘比对的身份白名单（票 03；与 betting.frozen 漂移白名单同源
# 常量 quote_evidence.IDENTITY_COMPARABLE_CLASSES，单一来源）
_METRIC_IDENTITY_CLASSES = qe.IDENTITY_COMPARABLE_CLASSES

CloseProvider = Callable[[str, str, str, str], tuple[float, str, str] | None]

_KNOWN_BOUNDARIES: tuple[str, ...] = (
    "账本自 2026-10-20 起（PR#172），首周含修复期（10-22 收盘回填链路修复"
    + "前的行参考身份为 unknown 层）。",
    "密集锚（票 47）自上线零触发，无独立高频重观察；陈旧度 s 是'报价已多旧'"
    + "的代理，不是'延迟后仍在'的直接验证——直接验证是 Stage 2 的事。",
    "would_bet 面与账本分母不同（漏斗有资格门），本协议分母以账本为准，"
    + "漏斗面仅作对照描述。",
    "首跑定位=协议 shakeout（验证口径与报告形态），不产结论。",
)

_QUANTILE_KEYS = ("min", "p25", "median", "p75", "p90", "max")

# 陈旧度直方桶上界（秒）→ 标签；全部越界落 >3600s
_STALENESS_BUCKETS: tuple[tuple[float, str], ...] = (
    (60.0, "0-60s"),
    (180.0, "60-180s"),
    (300.0, "180-300s"),
    (900.0, "300-900s"),
    (3600.0, "900-3600s"),
)


@dataclass
class _RowAudit:
    """账本一行的审计面（分母分层 + 主指标输入）。"""

    date: str
    market: str
    league: str
    staleness_s: float | None
    kickoff_distance_s: float | None
    sale_state: str | None
    single_eligible: int | None
    backfilled: bool
    has_reference: bool
    anchor_ref: str | None
    value_proxy: float | None
    identity_gate: str | None  # None=可比；identity_unknown/identity_mismatch
    close_positive: bool | None  # None=收盘未算（身份/数据缺）

    @property
    def l2(self) -> bool:
        """规则合格：可售且（单关玩法）单关资格在。"""
        return self.sale_state == "on_sale" and self.single_eligible == 1

    def l3(self, tier_s: float) -> bool:
        """延迟后可达：非回填、参考在、陈旧度≤档位。"""
        if not self.l2 or self.backfilled or not self.has_reference:
            return False
        return self.staleness_s is not None and self.staleness_s <= tier_s


def _wilson_ci(successes: int, n: int) -> list[float] | None:
    """比例的 Wilson 95% CI（n=0 → None，不假装有区间）。"""
    if n <= 0:
        return None
    z = NormalDist().inv_cdf(0.975)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / denom
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _staleness_bucket(s: float) -> str:
    """陈旧度直方桶（边界含上端；≤0 单列=拍点即源更新时刻）。"""
    if s <= 0:
        return "<=0s"
    for bound, label in _STALENESS_BUCKETS:
        if s <= bound:
            return label
    return ">3600s"


def _kickoff_bucket(seconds: float) -> str:
    """距开赛分层桶（prereg §5：≤2h / 2–12h / >12h；开赛后单列）。"""
    if seconds < 0:
        return "past_kickoff"
    if seconds <= 2 * 3600:
        return "<=2h"
    if seconds <= 12 * 3600:
        return "2-12h"
    return ">12h"


def _terminal_reason(row: _RowAudit, *, tier_s: float) -> str:
    """一行在漏斗上的终态标签（首中即返；分层数互斥不重复计）。"""
    if row.sale_state == "stopped":
        return "sale_stopped"
    if row.sale_state == "not_on_sale":
        return "sale_not_on_sale"  # 票 09 独立值：未开售≠停售≠未知
    if row.sale_state != "on_sale":
        return "sale_unknown"
    if row.single_eligible != 1:
        return "single_ineligible" if row.single_eligible == 0 else "single_unknown"
    return _post_l2_reason(row, tier_s=tier_s)


def _post_l2_reason(row: _RowAudit, *, tier_s: float) -> str:
    """L2 行的后续终态：参考/回填/陈旧三门外，交 L3 后估值/身份/收盘门。"""
    if not row.has_reference:
        return "no_reference"
    if row.backfilled:
        return "backfilled"
    if row.staleness_s is None or row.staleness_s > tier_s:
        return "stale"
    return _post_l3_reason(row)


def _post_l3_reason(row: _RowAudit) -> str:
    """L3 行终态：估值/身份/收盘（候选存活性）。"""
    if row.value_proxy is None:
        # 参考三向概率只映射 1x2 选项；非 had 玩法或选择列缺同归此类
        return "value_proxy_uncomputable"
    if row.value_proxy <= 0:
        return "no_edge"
    if row.identity_gate is not None:
        return row.identity_gate  # identity_unknown / identity_mismatch / close_missing
    if row.close_positive is None:
        return "close_missing"
    return "survived" if row.close_positive else "faded"


def _fetch_rows(
    conn: sqlite3.Connection, date_from: str, date_to: str
) -> list[sqlite3.Row]:
    """窗口内账本行 × 场次/队名/联赛（LEFT JOIN：缺链如实 NULL 不丢行）。"""
    return conn.execute(
        """
        SELECT l.id, l.market_code, l.selection_code, l.jc_odds,
               l.observed_at, l.source_updated_at,
               l.p_sharp_h, l.p_sharp_d, l.p_sharp_a, l.anchor_ref,
               l.sale_state, l.single_eligible, l.reference_backfilled_at,
               f.kickoff_utc, ht.canonical_name AS home_team,
               ta.canonical_name AS away_team, c.name AS competition_name
        FROM price_deviation_ledger l
        JOIN fixtures f ON f.id = l.fixture_id
        LEFT JOIN teams ht ON ht.id = f.home_team_id
        LEFT JOIN teams ta ON ta.id = f.away_team_id
        LEFT JOIN competitions c ON c.id = f.competition_id
        WHERE substr(l.observed_at, 1, 10) BETWEEN ? AND ?
        ORDER BY l.id
        """,
        (date_from, date_to),
    ).fetchall()


def _build_row_audit(
    raw: sqlite3.Row, close_provider: CloseProvider | None
) -> _RowAudit:
    """账本行 → 审计面（value_proxy/陈旧度/身份门/收盘）。"""
    observed_at = str(raw["observed_at"])
    source_updated_at = raw["source_updated_at"]
    staleness = (
        qe.seconds_between(observed_at, str(source_updated_at))
        if source_updated_at
        else None
    )
    kickoff = raw["kickoff_utc"]
    distance = qe.seconds_between(str(kickoff), observed_at) if kickoff else None
    market = str(raw["market_code"])
    has_reference = raw["p_sharp_h"] is not None
    value_proxy: float | None = None
    if has_reference and market == "had":
        selection = str(raw["selection_code"])
        if selection in SELECTIONS:
            p_sharp = raw[f"p_sharp_{selection}"]
            if p_sharp is not None:
                value_proxy = float(raw["jc_odds"]) * float(p_sharp) - 1
    identity_gate, close_positive = _close_face(raw, close_provider)
    sale_state = raw["sale_state"]
    single_eligible = raw["single_eligible"]
    return _RowAudit(
        date=observed_at[:10],
        market=market,
        league=str(raw["competition_name"] or "未知联赛"),
        staleness_s=staleness,
        kickoff_distance_s=distance,
        sale_state=str(sale_state) if sale_state is not None else None,
        single_eligible=(int(single_eligible) if single_eligible is not None else None),
        backfilled=raw["reference_backfilled_at"] is not None,
        has_reference=has_reference,
        anchor_ref=str(raw["anchor_ref"]) if raw["anchor_ref"] is not None else None,
        value_proxy=value_proxy,
        identity_gate=identity_gate,
        close_positive=close_positive,
    )


def _close_face(
    raw: sqlite3.Row, close_provider: CloseProvider | None
) -> tuple[str | None, bool | None]:
    """
    收盘面：身份门（票 03 白名单）与收盘仍正判定。

    返回 (identity_gate, close_positive)；gate 非 None 即分母外
    （identity_unknown / identity_mismatch / close_missing）。
    只对有参考的 1x2 行尝试（非 had 行参考三向不映射，不冒充可比）。
    """
    if close_provider is None or raw["p_sharp_h"] is None:
        return None, None
    if str(raw["market_code"]) != "had":
        return None, None
    anchor_ref = raw["anchor_ref"]
    entry_cls = qe.reference_identity_class(
        str(anchor_ref) if anchor_ref is not None else None
    )
    if entry_cls == "unknown":
        return "identity_unknown", None
    home, away, kickoff = raw["home_team"], raw["away_team"], raw["kickoff_utc"]
    close = (
        close_provider(str(home), str(away), str(kickoff), str(raw["selection_code"]))
        if home is not None and away is not None and kickoff
        else None
    )
    if close is None:
        return "close_missing", None
    close_cls = qe.reference_identity_class(close[1])
    if entry_cls == close_cls and entry_cls in _METRIC_IDENTITY_CLASSES:
        return None, float(raw["jc_odds"]) * float(close[0]) - 1 > 0
    return "identity_mismatch", None


def _metric(rows: list[_RowAudit]) -> dict[str, Any]:
    """
    主指标（prereg §4）：L2 可算行中，决策时刻 value_proxy>0 且收盘仍正的比例。

    分母=同身份收盘可比行（票 03 白名单）；identity_unknown/mismatch 与
    收盘缺的行**分母外单列**（positive_excluded），不静默混层——收盘面
    整体缺席（close_provider=None）时分母为 0、比例与 CI 如实 None，
    不冒充 0%。参考三向概率只映射 1x2：scope=had 单选。
    """
    base = [
        r
        for r in rows
        if r.l2 and r.has_reference and r.market == "had" and r.value_proxy is not None
    ]
    positive = [r for r in base if r.value_proxy is not None and r.value_proxy > 0]
    denominator = [
        r for r in base if r.identity_gate is None and r.close_positive is not None
    ]
    survived = [
        r
        for r in denominator
        if r.value_proxy is not None and r.value_proxy > 0 and r.close_positive
    ]
    excluded = {
        gate: sum(1 for r in positive if r.identity_gate == gate)
        for gate in ("identity_unknown", "identity_mismatch", "close_missing")
    }
    n_denominator = len(denominator)
    return {
        "scope": "had 单选（参考三向概率仅映射 1x2 选项）",
        "n_l2_computable": len(base),
        "n_denominator": n_denominator,
        "positive_at_decision": len(positive),
        "close_computable_positive": len(
            [r for r in denominator if r.value_proxy is not None and r.value_proxy > 0]
        ),
        "survived": len(survived),
        "survival_share": (len(survived) / n_denominator) if n_denominator else None,
        "wilson95": _wilson_ci(len(survived), n_denominator),
        "positive_excluded": excluded,
    }


def _reasons(rows: list[_RowAudit], *, tier_s: float) -> dict[str, int]:
    """终态标签分布（互斥；顺序=漏斗首中）。"""
    counts: dict[str, int] = {}
    for row in rows:
        reason = _terminal_reason(row, tier_s=tier_s)
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _staleness_distribution(rows: list[_RowAudit]) -> dict[str, Any]:
    """陈旧度 s 分布（分位数+直方桶；不可算行单列）。"""
    values = sorted(r.staleness_s for r in rows if r.staleness_s is not None)
    buckets: dict[str, int] = {}
    for s in values:
        key = _staleness_bucket(s)
        buckets[key] = buckets.get(key, 0) + 1
    if not values:
        quantile_map = dict.fromkeys(_QUANTILE_KEYS)
    elif len(values) == 1:
        quantile_map = dict.fromkeys(_QUANTILE_KEYS, values[0])
    else:
        quart = quantiles(values, n=4)
        k90 = min(len(values) - 1, max(0, round(0.9 * (len(values) - 1))))
        quantile_map = {
            "min": values[0],
            "p25": quart[0],
            "median": quart[1],
            "p75": quart[2],
            "p90": values[k90],
            "max": values[-1],
        }
    return {
        "n_computable": len(values),
        "n_missing": sum(1 for r in rows if r.staleness_s is None),
        "quantiles": quantile_map,
        "buckets": buckets,
        "gt_300s": sum(1 for s in values if s > STALE_STOP_SECONDS),
    }


def _missing_reason_key(r: _RowAudit) -> str:
    """缺失原因分层键（prereg §5：回填/陈旧/身份不明全维度）。"""
    if not r.l2:
        return "not_l2"
    if not r.has_reference:
        return "no_reference"
    if r.backfilled:
        return "backfilled"
    if r.staleness_s is None or r.staleness_s > STALE_STOP_SECONDS:
        return "stale_unknown" if r.staleness_s is None else "stale_gt_300s"
    if r.identity_gate is not None:
        # identity_unknown / identity_mismatch / close_missing
        return r.identity_gate
    return "fresh"


def _stratifications(rows: list[_RowAudit]) -> dict[str, list[dict[str, Any]]]:
    """描述层分层（计数与正候选数，不做检验不设门槛）。"""

    def table(key_of: Callable[[_RowAudit], str]) -> list[dict[str, Any]]:
        groups: dict[str, list[_RowAudit]] = {}
        for row in rows:
            groups.setdefault(key_of(row), []).append(row)
        return [
            {
                "key": key,
                "rows": len(group),
                "l2": sum(1 for r in group if r.l2),
                "positive_at_decision": sum(
                    1 for r in group if r.l2 and (r.value_proxy or 0) > 0
                ),
            }
            for key, group in sorted(
                groups.items(), key=lambda kv: (-len(kv[1]), kv[0])
            )
        ]

    return {
        "league": table(lambda r: r.league),
        "market": table(lambda r: r.market),
        "kickoff_distance": table(
            lambda r: (
                _kickoff_bucket(r.kickoff_distance_s)
                if r.kickoff_distance_s is not None
                else "unknown"
            )
        ),
        "missing_reason": table(_missing_reason_key),
        "identity_class": table(lambda r: qe.reference_identity_class(r.anchor_ref)),
    }


def _day_classes(
    conn: sqlite3.Connection, dates: list[str], ledger_counts: dict[str, int]
) -> list[dict[str, Any]]:
    """
    逐日分类（prereg §7）。

    active（账本有行）/ suspected_outage（在售而账本零行=疑似断采，需
    查证不算"无机会"）/ zero_opportunity（有销售态观测但全窗无在售行
    =无在售，prereg 措辞的真零机会日）/ outage_no_data（销售态观测也
    缺=管线无数据日）。注意：on_sale 观测是仪器面——全库从未落 on_sale
    行时 zero_opportunity 分类依赖"缺席证据"，报告头部照记在售行数供
    读者校验（shakeout 发现，非协议修订）。
    """
    status_by_date = {
        str(r["d"]): r
        for r in conn.execute(
            """
            SELECT substr(observed_at, 1, 10) AS d, COUNT(*) AS n,
                   SUM(CASE WHEN sale_state = 'on_sale' THEN 1 ELSE 0 END) AS on_sale
            FROM sale_statuses
            WHERE substr(observed_at, 1, 10) BETWEEN ? AND ?
            GROUP BY 1
            """,
            (dates[0], dates[-1]),
        ).fetchall()
    }
    out: list[dict[str, Any]] = []
    for date in dates:
        ledger_n = ledger_counts.get(date, 0)
        status = status_by_date.get(date)
        statuses_n = int(status["n"]) if status is not None else 0
        on_sale_n = int(status["on_sale"] or 0) if status is not None else 0
        if ledger_n > 0:
            day_class = "active"
        elif on_sale_n > 0:
            day_class = "suspected_outage"
        elif statuses_n > 0:
            day_class = "zero_opportunity"
        else:
            day_class = "outage_no_data"
        out.append(
            {
                "date": date,
                "class": day_class,
                "ledger_rows": ledger_n,
                "sale_status_rows": statuses_n,
                "on_sale_rows": on_sale_n,
            }
        )
    return out


def _stop_condition(
    rows: list[_RowAudit], *, window_days: int, active_days: int
) -> dict[str, Any]:
    """
    停止条件输入（prereg §6 定值；触发与否如实报，shakeout 不产结论）。

    per_week 主口径=日历窗（窗口定义即日历窗）；另报活跃日基准
    （账本有行的日子）——断采日计入日历分母会压低速率偏向触发，两个
    读数并陈供判读，不预设哪个为准。
    """
    l1 = len(rows)
    backfilled = sum(1 for r in rows if r.backfilled)
    share = (backfilled / l1) if l1 else None
    l3_positive = sum(
        1 for r in rows if r.l3(STALE_STOP_SECONDS) and (r.value_proxy or 0) > 0
    )
    per_week = (l3_positive / window_days * 7) if window_days > 0 else None
    per_week_active = (l3_positive / active_days * 7) if active_days > 0 else None
    triggered = bool(
        share is not None
        and share > BACKFILL_SHARE_STOP
        and per_week is not None
        and per_week < L3_POSITIVE_PER_WEEK_STOP
    )
    return {
        "backfilled": backfilled,
        "l1": l1,
        "backfill_share": share,
        "backfill_share_threshold": BACKFILL_SHARE_STOP,
        "l3_positive_ex_backfill_ex_stale": l3_positive,
        "per_week": per_week,
        "per_week_active_day_basis": per_week_active,
        "active_days": active_days,
        "per_week_threshold": L3_POSITIVE_PER_WEEK_STOP,
        "triggered": triggered,
        "note": "shakeout 首跑不产结论；触发判定累计窗≥30 销售日后生效",
    }


def _window_dates(date_from: str, date_to: str) -> list[str]:
    """窗口自然日列表（两端含；格式错/起始晚于截止 → fail-loud）。"""

    def parse(value: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"日期须为 YYYY-MM-DD: {value}") from exc

    start, end = parse(date_from), parse(date_to)
    if start > end:
        raise ValueError(f"起始日晚于截止日: {date_from} > {date_to}")
    return [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range((end - start).days + 1)
    ]


def _tier_table(rows: list[_RowAudit]) -> list[dict[str, Any]]:
    """延迟档位骨架：d=0 判读，其余 pending 照记数（不假装有判读）。"""
    l2 = sum(1 for r in rows if r.l2)
    return [
        {
            "tier_s": tier,
            "l2": l2,
            "l3": sum(1 for r in rows if r.l3(float(tier))),
            "stage1_reported": tier == REPORTED_TIER,
            "status": "reported" if tier == REPORTED_TIER else "pending",
        }
        for tier in DELAY_TIERS
    ]


def _day_payload(rows: list[_RowAudit]) -> dict[str, Any]:
    """分母逐层数 + 存活比例 + 主指标 + 终态分布（L4 留空如实记）。"""
    l2 = sum(1 for r in rows if r.l2)
    l3 = sum(1 for r in rows if r.l3(float(REPORTED_TIER)))
    return {
        "l1": len(rows),
        "l2": l2,
        "l3": l3,
        "l4": None,
        "survival_ratio": (l3 / l2) if l2 else None,
        "metric": _metric(rows),
        "reasons": _reasons(rows, tier_s=float(REPORTED_TIER)),
    }


def _overall_face(rows: list[_RowAudit]) -> dict[str, Any]:
    """总体面：分母逐层 + 主指标 + 预指定对照 + 档位骨架 + 陈旧度分布。"""
    return {
        **_day_payload(rows),
        "contrasts": {
            "excl_backfilled": _metric([r for r in rows if r.l2 and not r.backfilled]),
            "excl_stale_gt_300s": _metric(
                [
                    r
                    for r in rows
                    # 陈旧度不可算行同剔（与漏斗 stale 语义一致，Spec 轴三审）
                    if r.l2
                    and r.staleness_s is not None
                    and r.staleness_s <= STALE_STOP_SECONDS
                ]
            ),
        },
        "delay_tiers": _tier_table(rows),
        "staleness": _staleness_distribution(rows),
    }


def _protocol_face(sale_days_strict: int) -> dict[str, Any]:
    """协议面：阶段定位/门槛与主指标判读状态（prereg §2/§7）。"""
    return {
        "stage": 1,
        "prereg": ".scratch/report-audit-20261027/prereg-delay-survival.md",
        "first_run_position": "shakeout",
        "reported_tier_s": REPORTED_TIER,
        "sale_days_strict": sale_days_strict,
        "sale_day_gate": SALE_DAY_GATE,
        "metric_status": (
            "provisional" if sale_days_strict < SALE_DAY_GATE else "formal_ready"
        ),
    }


def _write_two_views(store: CorpusStore, report: dict[str, Any]) -> None:
    """
    报告落语料树 reports/（JSON+MD 两视图，temp+rename）。

    先写两份临时文件再各自 rename——崩溃窗从"写两段"缩到"两次 rename"，
    且崩溃遗留的是 .tmp 不半新半旧（correctness F3；重跑幂等覆盖自愈）。
    """
    reports = store.root / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    json_tmp = reports / f"{REPORT_BASENAME}.json.tmp"
    md_tmp = reports / f"{REPORT_BASENAME}.md.tmp"
    json_tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_tmp.write_text(_render_markdown(report), encoding="utf-8")
    json_tmp.replace(reports / f"{REPORT_BASENAME}.json")
    md_tmp.replace(reports / f"{REPORT_BASENAME}.md")


def _cached_provider(
    provider: CloseProvider, cache: dict[tuple[str, str, str, str], Any]
) -> CloseProvider:
    """
    收盘提供方按 (home,away,kickoff,selection) 记忆化（correctness F2）。

    收盘查 kickoff 锚定、确定性——同键重复查是纯浪费：CLI 桥对 30M 行
    未按 sid 索引的 odds_change_event 视图逐行扫，账本 append-only 下
    线性涨；缓存=精确值不是近似。None 结果同样缓存（查无也是答案）。
    """

    def wrapped(
        home: str, away: str, kickoff_utc: str, selection: str
    ) -> tuple[float, str, str] | None:
        key = (home, away, kickoff_utc, selection)
        if key not in cache:
            cache[key] = provider(home, away, kickoff_utc, selection)
        return cache[key]

    return wrapped


def build_arrival_audit_report(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    close_provider: CloseProvider | None = None,
    store: CorpusStore | None = None,
) -> dict[str, Any]:
    """
    Stage 1 到达率审计报告（窗口两端含）。

    conn=运行面连接（只读即可）；close_provider 缺席时收盘面整体标
    absent（主指标收盘不可算如实计数，不冒充）。store 在场时报告落
    语料树 reports/（JSON+MD 两视图），并随运行指纹。

    全部读同窗一个读事务（correctness F1：活库 WAL 并发写下，孤儿
    检查/账本 JOIN/销售态聚合/指纹各自成快照会跨表涂抹——计数与自证
    指纹描述不同的库态；指纹的 _db_face 见 in_transaction 会并入本
    事务）。收盘提供方经 (home,away,kickoff,selection) 记忆化（F2）。
    """
    dates = _window_dates(date_from, date_to)
    provider = (
        _cached_provider(close_provider, {}) if close_provider is not None else None
    )
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")  # 纯读快照：deferred，首读才锁定视图
    try:
        report = _assemble_report(
            conn,
            dates=dates,
            date_from=date_from,
            date_to=date_to,
            close_provider=provider,
            close_face="injected" if close_provider is not None else "absent",
            store=store,
        )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")  # 不留事务壳
    if store is not None:
        _write_two_views(store, report)
    return report


def _assemble_report(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    date_from: str,
    date_to: str,
    close_provider: CloseProvider | None,
    close_face: str,
    store: CorpusStore | None,
) -> dict[str, Any]:
    """单读事务内的报告装配（调用方负责事务壳与落盘）。"""
    # fail-closed：账本行必须能关联到场次（关联不上=数据完整性事故，
    # 不允许静默缩分母）
    orphan = conn.execute(
        """
        SELECT COUNT(*) FROM price_deviation_ledger l
        WHERE substr(l.observed_at, 1, 10) BETWEEN ? AND ?
          AND NOT EXISTS (SELECT 1 FROM fixtures f WHERE f.id = l.fixture_id)
        """,
        (date_from, date_to),
    ).fetchone()[0]
    if int(orphan) > 0:
        raise RuntimeError(f"账本存在无法关联场次的行({orphan}行)，拒绝出报告")
    rows = [
        _build_row_audit(raw, close_provider)
        for raw in _fetch_rows(conn, date_from, date_to)
    ]
    ledger_by_date: dict[str, int] = dict.fromkeys(dates, 0)
    for row in rows:
        ledger_by_date[row.date] += 1
    day_classes = _day_classes(conn, dates, ledger_by_date)
    sale_days_strict = sum(1 for d in day_classes if d["on_sale_rows"] > 0)
    active_days = sum(1 for d in day_classes if d["ledger_rows"] > 0)
    class_by_date = {d["date"]: d["class"] for d in day_classes}
    report: dict[str, Any] = {
        "schema": "delay-survival-stage1/1",
        "generated_at": datetime.now(tz=None).isoformat(timespec="seconds"),
        "window": {
            "date_from": date_from,
            "date_to": date_to,
            "n_days": len(dates),
            "date_basis": "observed_at UTC 自然日",
        },
        "protocol": _protocol_face(sale_days_strict),
        "overall": _overall_face(rows),
        "by_day": [
            {
                "date": date,
                "day_class": class_by_date[date],
                **_day_payload([r for r in rows if r.date == date]),
            }
            for date in dates
        ],
        "day_classification": day_classes,
        "stop_condition": _stop_condition(
            rows, window_days=len(dates), active_days=active_days
        ),
        "stratification": _stratifications(rows),
        "close_face": close_face,
        "known_boundaries": list(_KNOWN_BOUNDARIES),
        "failure_variants": [],
    }
    if store is not None:
        report["run_fingerprint"] = rfp.build_run_fingerprint(conn=conn, store=store)
    return report


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _metric_line(w: Callable[[str], None], label: str, m: dict[str, Any]) -> None:
    """主指标表格行（分母=同身份收盘可比；CI 缺=分母 0，如实留空）。"""
    ci = m["wilson95"]
    ci_txt = "—" if ci is None else f"[{ci[0]:.3f}, {ci[1]:.3f}]"
    share = "—" if m["survival_share"] is None else f"{m['survival_share']:.1%}"
    w(
        f"| {label} | {m['n_l2_computable']} | {m['n_denominator']}"
        + f" | {m['positive_at_decision']}"
        + f" | {m['close_computable_positive']} | {m['survived']} | {share} {ci_txt} |"
    )


def _md_summary(report: dict[str, Any], w: Callable[[str], None]) -> None:
    """头部摘要 + 主指标与预指定对照表。"""
    overall = report["overall"]
    metric = overall["metric"]
    w("# Stage 1 到达率审计（延迟生存率协议首跑）")
    w("")
    w(
        f"- 窗口 {report['window']['date_from']} → {report['window']['date_to']}"
        + f"（{report['window']['n_days']} 自然日，{report['window']['date_basis']}）"
    )
    w(
        f"- 判读状态：{report['protocol']['metric_status']}"
        + f"（严格销售日 {report['protocol']['sale_days_strict']}"
        + f"/门槛 {report['protocol']['sale_day_gate']}；"
        + f"首跑定位 {report['protocol']['first_run_position']}——不产结论）"
    )
    w(
        f"- 主指标：L2 中决策时刻正且收盘仍正的比例（{metric['scope']}）；"
        + f"收盘面 = {report['close_face']}"
    )
    w(
        f"- L1={overall['l1']} L2={overall['l2']} L3(d=0)={overall['l3']}"
        + f" 存活比例={_fmt_pct(overall['survival_ratio'])}"
    )
    w("")
    w("## 主指标（Wilson 95% CI）与预指定对照")
    w("")
    header = (
        "| 口径 | L2可算 | 分母(同身份收盘可比) |"
        " 决策时刻正 | 收盘可比正 | 存活 | 比例 [CI] |"
    )
    w(header)
    w("|---|---|---|---|---|---|---|")
    _metric_line(w, "总体（L2）", metric)
    _metric_line(w, "剔除回填前/后·后", overall["contrasts"]["excl_backfilled"])
    excl_stale = overall["contrasts"]["excl_stale_gt_300s"]
    _metric_line(w, "剔除陈旧>300s前/后·后", excl_stale)
    w("")
    excluded = metric["positive_excluded"]
    w(
        "正候选分母外分层："
        + ("，".join(f"{k}={v}" for k, v in excluded.items() if v) or "全部为 0")
    )
    w("")


def _md_day_tables(report: dict[str, Any], w: Callable[[str], None]) -> None:
    """逐日分母表 + 拒绝原因分布 + 延迟档位骨架。"""
    overall = report["overall"]
    w("## 逐日分母（L1→L2→L3）")
    w("")
    w("| 日期 | 日类 | L1 | L2 | L3(d=0) | 存活比例 | 主指标分母 | 决策正 | 存活 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for day in report["by_day"]:
        m = day["metric"]
        w(
            f"| {day['date']} | {day['day_class']} | {day['l1']} | {day['l2']}"
            + f" | {day['l3']} | {_fmt_pct(day['survival_ratio'])}"
            + f" | {m['n_denominator']} | {m['positive_at_decision']}"
            + f" | {m['survived']} |"
        )
    w("")
    w("## 拒绝原因分布（d=0，互斥首中）")
    w("")
    reasons = overall["reasons"]
    l1 = max(overall["l1"], 1)
    w("| 原因 | 行数 | 占 L1 |")
    w("|---|---|---|")
    for reason in sorted(reasons, key=lambda k: -reasons[k]):
        w(f"| {reason} | {reasons[reason]} | {reasons[reason] / l1:.1%} |")
    w("")
    w("## 延迟档位骨架（Stage 1 只判读 d=0）")
    w("")
    w("| 档位(s) | L2 | L3 | 状态 |")
    w("|---|---|---|---|")
    for tier in overall["delay_tiers"]:
        w(f"| {tier['tier_s']} | {tier['l2']} | {tier['l3']} | {tier['status']} |")
    w("")


def _md_dist_stop(report: dict[str, Any], w: Callable[[str], None]) -> None:
    """陈旧度分布 + 停止条件输入 + 描述层分层。"""
    st = report["overall"]["staleness"]
    w("## 陈旧度分布（s = observed_at − source_updated_at）")
    w("")
    w(
        f"- 可算 {st['n_computable']} 行（不可算 {st['n_missing']} 行）；"
        + f">300s 共 {st['gt_300s']} 行"
    )
    qd = st["quantiles"]
    quantile_txt = (
        "min={min} p25={p25} median={median} p75={p75} p90={p90} max={max}".format(
            **{k: _fmt(v) for k, v in qd.items()}
        )
    )
    w("- 分位数：" + quantile_txt)
    w("- 直方桶：" + "，".join(f"{k}={v}" for k, v in st["buckets"].items()))
    w("")
    sc = report["stop_condition"]
    w("## 停止条件输入（prereg §6 定值）")
    w("")
    w(
        f"- 回填占 L1：{sc['backfilled']}/{sc['l1']}"
        + f"={_fmt_pct(sc['backfill_share'])}"
        + f"（阈值 >{_fmt_pct(sc['backfill_share_threshold'])}）"
    )
    per_week_txt = "—" if sc["per_week"] is None else f"{sc['per_week']:.2f}"
    active_txt = (
        "—"
        if sc["per_week_active_day_basis"] is None
        else f"{sc['per_week_active_day_basis']:.2f}"
    )
    w(
        f"- 剔除回填与陈旧后 L3 正EV：{sc['l3_positive_ex_backfill_ex_stale']} 个"
        + f"≈{per_week_txt}/周（日历窗）/"
        + f" {active_txt}/周（活跃日 {sc['active_days']} 天）"
        + f"（阈值 <{sc['per_week_threshold']}/周）"
    )
    w(f"- 两臂同触发的形式判定：{'是' if sc['triggered'] else '否'}——{sc['note']}")
    w("")
    w("## 分层（描述层，不做检验）")
    w("")
    for name, table in report["stratification"].items():
        w(f"- {name}: " + "；".join(f"{r['key']}={r['rows']}" for r in table[:8]))
    w("")


def _md_tail(report: dict[str, Any], w: Callable[[str], None]) -> None:
    """日分类表 + 已知边界 + 失败变体账 + 运行指纹行。"""
    w("## 日分类（含零机会/疑似断采日照记）")
    w("")
    w("| 日期 | 类 | 账本行 | 销售态行 | 在售行 |")
    w("|---|---|---|---|---|")
    for day in report["day_classification"]:
        w(
            f"| {day['date']} | {day['class']} | {day['ledger_rows']}"
            + f" | {day['sale_status_rows']} | {day['on_sale_rows']} |"
        )
    w("")
    on_sale_total = sum(d["on_sale_rows"] for d in report["day_classification"])
    if on_sale_total == 0:
        w(
            "> 仪器面提示：全窗在售行=0——sale_statuses 从未落 on_sale 观测，"
            + "zero_opportunity/suspected_outage 分类依赖缺席证据，严格销售日"
            + "门槛（≥30）在本仪器状态下无法达成（shakeout 发现，非协议修订）。"
        )
        w("")
    w("## 已知数据边界（预注册 §9 原样）")
    w("")
    for boundary in report["known_boundaries"]:
        w(f"- {boundary}")
    w("")
    w("## 失败变体记录（防 p-hacking 自查账；首跑空）")
    w("")
    w("| 日期 | 变体描述 | 动机 | 是否入报告 | 裁决 |")
    w("|---|---|---|---|---|")
    w("| （空——首跑无变体） | | | | |")
    w("")
    fingerprint = report.get("run_fingerprint")
    if fingerprint is not None:
        git_face: dict[str, Any] = dict(fingerprint.get("git") or {})
        sha = str(git_face.get("sha") or "?")[:12]
        dirty = "*dirty" if git_face.get("dirty") else ""
        schema_txt = fingerprint.get("schema")
        w(f"## 运行指纹（git {sha}{dirty}；schema v{schema_txt}；细节在 JSON）")
        w("")


def _render_markdown(report: dict[str, Any]) -> str:
    """人读视图：分母/主指标/分布/停止条件输入/日分类（细节在 JSON）。"""
    lines: list[str] = []
    w = lines.append
    _md_summary(report, w)
    _md_day_tables(report, w)
    _md_dist_stop(report, w)
    _md_tail(report, w)
    return "\n".join(lines) + "\n"
