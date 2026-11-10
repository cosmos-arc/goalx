"""
月读报告生成器（vision-build 票 04）：人的裁定面——固定八节只读报告。

每月 1 日 09:35（cron ``35 9 1 * *``，datasets 注册面 ``monthly-read``，
首读 2026-12-01 部署注册后自然到点）产 CLI 报告文件（语料树
reports/monthly/，JSON+MD 双视图，append-only 拍次——票 03 同缝）。
报送月=刚结束的自然月（每月 1 日报送覆盖上月）。

八节（票面固定顺序）：
① 六门进度（家族总尝试数 DSR/BH 校正读数框架 + 注册表状态面；
  六步门逐门统计量接缝留——数据面在 edge-program 板）；
② CLV 审计双锚（主锚=srct Pinnacle 欧收盘已对账腿 clv_records
  close_prob，副锚=欧共识 as-of kickoff 多书 Shin 均值；同注两锚差
  超界=一致性告警；副锚覆盖不足如实报不伪造）；
③ champion/challenger 影子对比窗（票 07 未建=「无影子数据」占位；
  forecasts 版本标签读口=票 07 消费缝）；
④ 版本状态机事件流（version_transitions 回放：晋升/降级/出梯/触发器
  命中 reason 分类计数）；
⑤ 强制失败视图（实验失败率/引擎门挂原因分布/对抗质疑拦截计数/LLM
  花费汇总；User Story 8：自动化不得让过程致盲，本节不可省略）；
⑥ 双钟读数（票 08 双钟显式读并落账 + 四信号面评估落账 + B.3 到期
  读数——票 08 缝回填进本节）；
⑦ 教训库视图（lessons.monthly_review_view 读口③：新入库+复审到期）；
⑧ 下月预算定拍建议（提案配额/花费上限/影子重训开关——纯建议性数字
  从⑤花费汇总推导，规则写死可复算）。

不变量（票 04）：
- 报告纯只读：一切裁定动作是人按报告走 CLI 留痕，报告不写任何决策
  状态（不写 transitions/不翻假设/不切级别）；信号账（v55）与双钟读数
  账是观测面不是决策面，允许月读写。
- 拦截计数源裁定：票 03 的 rejected 只在逐拍提案文件（append-only
  受控目录=唯一事实源），账表 llm_proposal_costs 只有花费——⑤节拒收
  计数聚合语料树 proposals 文件，不读账表推断。
- 幂等重跑（auto-paper 03 同缝）：同月重跑报告文件拍次递增不覆盖；
  信号账 UNIQUE(signal, period) 吸收同期；双钟读数账=每次月读每钟追加
  一行（观测流水非吸收面——UNIQUE(clock, window_end) 只防同秒双写，
  correctness F3 裁定口径）。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import NormalDist, median
from typing import Any, cast

from duckdb import DuckDBPyConnection

from goalx_backend.betting import auto_strategy, versioning
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso
from goalx_backend.evaluation import contraction, hypothesis_registry, lessons, shadow
from goalx_backend.evaluation.clv import BASIS_SRCT_PINNACLE
from goalx_backend.evaluation.weekly_agent import MAX_PROPOSALS_PER_RUN
from goalx_backend.modelling import forecast

# ---- 常数（调整=改代码=PR 人审，代码永不自改） ----

#: ② 双锚一致性断言界（概率域）：sharp 收盘 vs 多书共识的 Shin 概率差
#: 正常 1-3pp；>5pp=锚结构异常/数据质量问题，告警进月读（不自动处置）。
DUAL_ANCHOR_ALERT_BOUND = 0.05
#: ① DSR/BH 读数框架的显著性水平（BH 族 FDR 控制）
GATE_ALPHA = 0.05
#: ⑧ 预算规则：花费上限=max(本月花费×headroom, 下限)；周批 4 拍/月
BUDGET_COST_HEADROOM = 1.5
BUDGET_COST_FLOOR_CNY = 20.0
BUDGET_BATCHES_PER_MONTH = 4
#: ⑧ 预算规则：周批拒收率过半=生成质量差信号 → 下月配额降档
BUDGET_REJECTION_SHARE_GATE = 0.5
BUDGET_REDUCED_QUOTA = 3
#: 报告落盘（语料树 reports/ 先例；月读独立子目录防与周报/提案互扰）
MONTHLY_REPORTS_SUBDIR = Path("reports") / "monthly"
#: 拍次哨兵（票 03 同款 O_EXCL 独占声明；常驻不删=拍名永久保留）
_CLAIM_SUFFIX = ".claim"
_MAX_BEATS_PER_MONTH = 999

_EULER_GAMMA = 0.5772156649015329  # Euler–Mascheroni（B&LdP 公式常数）
_DSR_MIN_ATTEMPTS = 2  # inv_cdf(1−1/N) 在 N=1 处无定义，单次退化 0

#: ① 六步门统计量接缝说明（v0=读数框架+注册表状态面；逐门统计量随
#: edge-program 六步门数据面点亮接入——读得到就带上）
GATE_STATISTICS_SEAM = (
    "六步门逐门统计量接缝留：门数据面在 edge-program 板"
    "（.scratch/edge-program-20261107/issues/04-prereg-r5-six-gate.md），"
    "本节 v0=DSR/BH 读数框架+注册表状态面；各门证据来源优先=历史回放验证面"
    "（人裁 7②），数据面点亮后逐门统计量接入本节"
)


# ---- 报送月推导（每月 1 日报送覆盖刚结束的自然月） ----


def report_month_of(run_date: str) -> str:
    """业务日（YYYY-MM-DD）→ 报送月（上一自然月，YYYY-MM）。"""
    first_of_month = date.fromisoformat(run_date).replace(day=1)
    return (first_of_month - timedelta(days=1)).strftime("%Y-%m")


_MONTHS_PER_YEAR = 12


def _month_bounds(report_month: str) -> tuple[str, str, str]:
    """报送月 → (首日, 末日, 次月首日)（字符串界；ISO 时间戳字典序可比）。"""
    year, month = (int(part) for part in report_month.split("-"))
    first = date(year, month, 1)
    next_first = (
        date(year + 1, 1, 1) if month == _MONTHS_PER_YEAR else date(year, month + 1, 1)
    )
    return (
        first.isoformat(),
        (next_first - timedelta(days=1)).isoformat(),
        next_first.isoformat(),
    )


# ---- ① 六门进度：家族总尝试数 + DSR/BH 校正读数框架 ----


def dsr_expected_max_z(n_attempts: int) -> float:
    """
    DSR 式期望 max-z（Bailey & López de Prado 2014，单侧标准正态）：

    ``(1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e))``。

    家族 N 次尝试在零假设下最亮 z 的期望——族内任何单个越门读数要活过
    DSR 折减须赢过此阈（edge-ledger 修订⑥'「DSR 式 max-z 折减报告」）。

    N<2 退化 0（单次抽取期望即 0，inv_cdf(0) 无穷不可用）。
    """
    if n_attempts < _DSR_MIN_ATTEMPTS:
        return 0.0
    nd = NormalDist()
    return (1.0 - _EULER_GAMMA) * nd.inv_cdf(1.0 - 1.0 / n_attempts) + (
        _EULER_GAMMA * nd.inv_cdf(1.0 - 1.0 / (n_attempts * math.e))
    )


def gate_progress(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    ① 六门进度（只读）：家族总尝试数 DSR/BH 校正读数框架。

    注册表状态面随行（六步门逐门统计量接缝见 GATE_STATISTICS_SEAM）。
    家族总尝试 m=hypothesis_attempts 账面 COUNT（票 02 口径=总尝试数），
    非行上 attempt_count 的 MAX——那是族史继承快照，同族多非终态行各自
    record_attempt 时 MAX 低估总尝试（DSR/BH 分母反保守失真，
    correctness F1）。BH 临界梯（k 序越门阈=α·k/m）报 k=1（最严，α/m）
    与 k=m（α）两点定梯形。
    """
    attempt_counts = {
        str(row["family"]): int(row["c"])
        for row in conn.execute(
            """
            SELECT r.family, COUNT(*) AS c
            FROM hypothesis_attempts a
            JOIN hypothesis_registry r ON r.h_id = a.h_id
            GROUP BY r.family
            """
        )
    }
    rows = hypothesis_registry.registry_view(conn)
    families: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = families.setdefault(
            str(row["family"]),
            {
                "family": str(row["family"]),
                "n_rows": 0,
                "statuses": {},
            },
        )
        entry["n_rows"] += 1
        status = str(row["status"])
        entry["statuses"][status] = entry["statuses"].get(status, 0) + 1
    for entry in families.values():
        m = attempt_counts.get(str(entry["family"]), 0)
        entry["m_attempts"] = m
        entry["dsr_expected_max_z"] = dsr_expected_max_z(m)
        entry["bh_thresholds"] = {
            "k1": GATE_ALPHA / m if m else None,
            "k_max": GATE_ALPHA if m else None,
        }
    return {
        "families": sorted(families.values(), key=lambda item: item["family"]),
        "gate_statistics_seam": GATE_STATISTICS_SEAM,
        "registry_rows": rows,
        "advice": "门进度读数框架；假设终态翻转=人 CLI hypothesis-mark",
    }


# ---- ② CLV 审计双锚（主锚 srct Pinnacle 欧收盘 / 副锚欧共识） ----


def clv_dual_anchor(
    conn: sqlite3.Connection,
    *,
    report_month: str,
    duck_con: DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """
    ② 双锚一致性审计（只读）：每注一锚自动取+同注两锚差超界告警。

    「每注一锚自动取」=对账面既有行为（优先梯产物，无人工选锚）；本
    审计消费它：主锚=clv_records.close_prob（close_basis='srct_pinnacle'，
    票 75 前向主通路——与 bets.clv 回填同源）；副锚=欧共识 as-of kickoff
    多书 Shin 均值（quote_evidence.eu_consensus_asof：corpus 有链走 srct
    逐书、缺席回退 legacy odds_api 行——两态都 PIT 安全：observed ≤
    kickoff）。人口=窗内已结算 auto 纸面注（触发器 A 同准入）。副锚覆盖
    不足=如实报覆盖率（不伪造绿、不静默丢腿）。
    """
    date_from, _, next_first = _month_bounds(report_month)
    bet_ids = versioning.settled_auto_bet_ids(conn, ts_from=date_from, ts_to=next_first)
    legs: list[sqlite3.Row] = []
    for bet_id in bet_ids:  # 月度量级（几十注）逐注取——零字符串拼 SQL
        rows = conn.execute(
            """
            SELECT bet_id, fixture_id, selection_code, close_prob
            FROM clv_records
            WHERE close_basis = ? AND bet_id = ?
            ORDER BY bet_id, fixture_id
            """,
            (BASIS_SRCT_PINNACLE, bet_id),
        ).fetchall()
        legs.extend(rows)
    alerts: list[dict[str, Any]] = []
    divergences: list[float] = []
    n_both = 0
    kickoffs: dict[int, str] = {}
    for leg in legs:
        fixture_id = int(leg["fixture_id"])
        kickoff = kickoffs.get(fixture_id)
        if kickoff is None:
            fixture = fx_store.get_fixture(conn, fixture_id)
            if fixture is None:
                continue  # pragma: no cover - clv 对账面建行时 fixture 必在
            kickoff = str(fixture["kickoff_utc"])
            kickoffs[fixture_id] = kickoff
        consensus = quote_evidence.eu_consensus_asof(
            conn,
            fixture_id,
            kickoff,
            duck_con=duck_con,
            sid=mapping.srct_sid_for_fixture(conn, fixture_id),
        )
        selection = str(leg["selection_code"])
        if consensus is None or selection not in consensus:
            continue  # 副锚缺席：不计入双锚面（覆盖率如实分母）
        n_both += 1
        p_main = float(leg["close_prob"])
        p_cons = float(consensus[selection])
        divergence = abs(p_main - p_cons)
        divergences.append(divergence)
        if divergence > DUAL_ANCHOR_ALERT_BOUND:
            alerts.append(
                {
                    "bet_id": int(leg["bet_id"]),
                    "fixture_id": fixture_id,
                    "selection": selection,
                    "p_main": round(p_main, 4),
                    "p_consensus": round(p_cons, 4),
                    "divergence": round(divergence, 4),
                }
            )
    n_main = len(legs)
    status = (
        "no_anchored_legs"
        if n_main == 0
        else "ok"
        if n_both
        else "no_secondary_coverage"
    )
    return {
        "status": status,
        "window": {"from": date_from, "to_exclusive": next_first},
        "bound": DUAL_ANCHOR_ALERT_BOUND,
        "n_legs_main": n_main,
        "n_legs_both": n_both,
        "coverage": (n_both / n_main) if n_main else 0.0,
        "max_divergence": round(max(divergences), 4) if divergences else None,
        "median_divergence": round(median(divergences), 4) if divergences else None,
        "alerts": alerts,
        "note": (
            "主锚=srct Pinnacle 欧收盘（clv_records.close_prob，票 75 主通路）；"
            "副锚=欧共识 as-of kickoff 多书 Shin 均值；覆盖率=副锚可得比例，"
            "历史面不足如实报不伪造"
        ),
        "advice": "双锚差超界=锚结构异常告警：核查该场锚书轨迹（不自动处置）",
    }


# ---- ③ champion/challenger 影子对比窗（票 07 未建=占位+消费缝） ----


def shadow_compare(conn: sqlite3.Connection, *, report_month: str) -> dict[str, Any]:
    """
    ③ 影子对比窗（票 07 已落地）：窗内 champion(ml)+challenger(shadow) 版本面。

    读口=forecasts 版本标签（model_version_counts）：影子轨（track=
    'shadow'）行在场=对比面就绪（status=ok，⑧预算影子开关随之 on）；
    无影子行如实 no_shadow_data 不伪造对比。逐版本对**读数**（命中率/
    校准对比、质量门评估）=票 11 历史回放窗面——本节只出对比窗数据面。
    """
    date_from, _, next_first = _month_bounds(report_month)
    shadow_rows = forecast.model_version_counts(
        conn, ts_from=date_from, ts_to=next_first, track="shadow"
    )
    return {
        "status": "ok" if shadow_rows else "no_shadow_data",
        "forecast_versions": forecast.model_version_counts(
            conn, ts_from=date_from, ts_to=next_first
        ),
        "shadow_versions": shadow_rows,
        "detail": (
            "champion/challenger 影子双算=票 07 面（重训触发→注册 shadow→"
            "forecasts 影子轨双写带版本标签）；本节按版本对出对比窗数据，"
            "逐版本质量读数与晋升门=票 11 历史回放窗（live shadow=持续"
            "确认面非等待门，晋升不等 live 月数积累）"
        ),
        "advice": "对比窗读数人裁：晋升走 version-promote（恒人审）",
    }


# ---- ④ 版本状态机事件流 ----


def version_events(
    conn: sqlite3.Connection, *, report_month: str, limit: int = 20
) -> dict[str, Any]:
    """④ transitions 回放（只读）：窗内分类计数 + 全史总数 + 最新事件清单。"""
    date_from, _, next_first = _month_bounds(report_month)
    all_events = versioning.transition_events(conn)
    window_events = [
        event for event in all_events if date_from <= event.ts < next_first
    ]
    counts = Counter(event.kind for event in window_events)
    return {
        "window": {"from": date_from, "to_exclusive": next_first},
        "counts": dict(sorted(counts.items())),
        "total_all_time": len(all_events),
        "events": [
            {
                "version_id": event.version_id,
                "label": event.label,
                "from_state": event.from_state,
                "to_state": event.to_state,
                "kind": event.kind,
                "reason": event.reason,
                "actor": event.actor,
                "ts": event.ts,
            }
            for event in window_events[:limit]
        ],
        "advice": "晋升恒人审：goalx version-promote --id N --to <state> --reason …",
    }


# ---- ⑤ 强制失败视图（不可省略，User Story 8） ----


def _rejections_from_beats(proposals_dir: Path, *, report_month: str) -> dict[str, Any]:
    """
    周批拒收计数：聚合语料树 proposals 逐拍文件。

    拒收只存在于拍文件（账表 llm_proposal_costs 只有花费）；拍名 claim
    独占+append-only=不可改写的事实源，故文件聚合是唯一可靠源。
    """
    n_beats = n_rejected = n_validated = unparseable = 0
    reasons: Counter[str] = Counter()
    if proposals_dir.is_dir():
        for path in sorted(proposals_dir.glob("weekly-proposals-*.json")):
            try:
                loaded: object = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                unparseable += 1
                continue
            if not isinstance(loaded, dict):
                unparseable += 1
                continue
            beat = cast("dict[str, object]", loaded)
            run_date = beat.get("run_date")
            if isinstance(run_date, str) and run_date.startswith(f"{report_month}-"):
                n_beats += 1
                rejected = beat.get("rejected")
                proposals = beat.get("proposals")
                rejected_items = (
                    cast("list[object]", rejected) if isinstance(rejected, list) else []
                )
                proposal_items = (
                    cast("list[object]", proposals)
                    if isinstance(proposals, list)
                    else []
                )
                n_rejected += len(rejected_items)
                n_validated += len(proposal_items)
                for item in rejected_items:
                    if not isinstance(item, dict):
                        continue
                    rejected_item = cast("dict[str, object]", item)
                    item_reasons = rejected_item.get("reasons")
                    if (
                        isinstance(item_reasons, list)
                        and item_reasons
                        and isinstance(item_reasons[0], (str, int, float))
                    ):
                        reasons[str(item_reasons[0])] += 1
    total = n_rejected + n_validated
    return {
        "n_beats": n_beats,
        "n_rejected": n_rejected,
        "n_validated": n_validated,
        "share": (n_rejected / total) if total else 0.0,
        "top_reasons": dict(reasons.most_common(5)),
        "unparseable_files": unparseable,
    }


def forced_failure(
    conn: sqlite3.Connection, *, store: CorpusStore, report_month: str
) -> dict[str, Any]:
    """
    ⑤ 强制失败视图（只读）：自动化过程不得致盲。

    四面：实验失败率（注册表 refuted 占比）/引擎门挂原因分布
    （auto_paper_runs zero_note）/对抗质疑管线拦截计数（语料树拍文件
    聚合）/LLM 花费汇总（llm_proposal_costs，⑧节预算的输入面）。
    """
    date_from, date_to, next_first = _month_bounds(report_month)
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM hypothesis_registry GROUP BY status"
    ).fetchall()
    statuses = {str(row["status"]): int(row["c"]) for row in status_rows}
    total = sum(statuses.values())
    refuted = statuses.get("refuted", 0)
    cost_rows = conn.execute(
        """
        SELECT phase, COUNT(*) AS c, SUM(cost_cny) AS s FROM llm_proposal_costs
        WHERE ts >= ? AND ts < ? GROUP BY phase
        """,
        (date_from, next_first),
    ).fetchall()
    by_phase = {
        str(row["phase"]): round(float(row["s"] or 0.0), 6) for row in cost_rows
    }
    return {
        "window": {"from": date_from, "to": date_to},
        "experiment_failure": {
            "refuted": refuted,
            "total": total,
            "refuted_share": (refuted / total) if total else 0.0,
        },
        "engine_gate_hangs": auto_strategy.zero_note_counts(
            conn, date_from=date_from, date_to=date_to
        ),
        "rejections": _rejections_from_beats(
            store.root / "reports" / "proposals", report_month=report_month
        ),
        "llm_costs": {
            "n_calls": sum(int(row["c"]) for row in cost_rows),
            "total_cny": round(sum(by_phase.values()), 6),
            "by_phase": by_phase,
        },
        # F5 口径：按 period=报送月计数（与事件落账 period 同源，月首
        # 北京 0-8 点拍 ts 仍在上一 UTC 日，ts 窗会错收进上一月）
        "shadow_retrains": shadow.retrain_event_counts(conn, period=report_month),
        "advice": "失败面观察项：异常聚集（门挂/拒收/失败率）由人裁决处置",
    }


# ---- ⑥ 双钟读数（含票 08 缝：四信号评估落账 + B.3 到期读数） ----


def clocks_section(
    conn: sqlite3.Connection, *, now: datetime, report_month: str
) -> dict[str, Any]:
    """
    ⑥ 双钟读数：contraction-read 同款显式读并落账 + 月读专属两写。

    月读写路径（晨检只读红线）：双钟读数账（每次月读每钟追加一行观测
    流水，F3 裁定口径）+ 四信号触发史账（v55，UNIQUE(signal, period)
    吸收同月）+ B.3 到期读数（只读派生，建议进报告不执行）。全部
    观测面非决策面。
    """
    clv, exploration = contraction.record_clock_readings(conn, now=now)
    signals = contraction.record_rescan_signals(conn, period=report_month, now=now)
    b3 = contraction.l3l4_timer_reading(conn, now=now)
    return {
        "level": contraction.current_level(conn).value,
        "clv_main": asdict(clv) | {"fired": clv.fired},
        "exploration": asdict(exploration) | {"fired": exploration.fired},
        "rescan_signals": [
            {"name": face.name, "status": face.status, "detail": face.detail}
            for face in signals
        ],
        "b3_timer": {
            "status": b3.status,
            "l2_entered_at": b3.l2_entered_at,
            "days": b3.days,
            "fired_count": b3.fired_count,
            "zero_triggers": b3.zero_triggers,
            "suggest_to": b3.suggest_to.value if b3.suggest_to is not None else None,
            "detail": b3.detail,
        },
        "advice": "钟只建议不执行：goalx contraction-set --level <L> --reason …",
    }


# ---- ⑦ 教训库视图（票 05 读口③） ----


def lessons_section(directory: Path | None, *, today: date) -> dict[str, Any]:
    """⑦ 月读教训库视图：新入库（上个自然月 created）+ 复审到期清单。"""
    base = lessons.lessons_dir() if directory is None else directory
    if not base.is_dir():
        return {
            "status": "dir_absent",
            "new_entries": [],
            "review_due_entries": [],
            "advice": "教训库目录缺席（docs/plans/lessons/）：人审定稿后入库",
        }
    view = lessons.monthly_review_view(base, today=today)

    def _row(lesson: lessons.Lesson) -> dict[str, Any]:
        return {
            "id": lesson.id,
            "status": lesson.status,
            "confidence": lesson.confidence,
            "created": lesson.created.isoformat(),
            "review_due": lesson.review_due.isoformat()
            if lesson.review_due is not None
            else None,
            "cause": lesson.cause,
        }

    return {
        "status": "ok",
        "new_entries": [_row(lesson) for lesson in view.new_entries],
        "review_due_entries": [_row(lesson) for lesson in view.review_due_entries],
        "advice": "复审到期=人改 lessons 条目（git PR 定稿制，脚本只报不改）",
    }


# ---- ⑧ 下月预算定拍建议（规则写死可复算；纯建议性） ----


def budget_suggestion(
    failure: dict[str, Any], *, shadow_available: bool
) -> dict[str, Any]:
    """
    ⑧ 下月预算建议：数字全部从⑤推导，规则固定可复算。

    规则（写死可复算）：

    - 花费上限 = max(本月 LLM 花费 × 1.5, ¥20 下限)；
    - 提案配额/拍 = 5（满额）；本月拒收率 >50% → 降 3（生成质量差信号）；
    - 影子重训开关 = off（③无影子数据）；票 07 落地后按对比窗驱动。

    纯建议性：定拍动作是人按报告调整（下月执行面无自动消费本节）。
    """
    total_cny = float(failure["llm_costs"]["total_cny"])
    share = float(failure["rejections"]["share"])
    return {
        "cost_cap_cny": round(
            max(total_cny * BUDGET_COST_HEADROOM, BUDGET_COST_FLOOR_CNY), 2
        ),
        "quota_per_batch": (
            BUDGET_REDUCED_QUOTA
            if share > BUDGET_REJECTION_SHARE_GATE
            else MAX_PROPOSALS_PER_RUN
        ),
        "batches_next_month": BUDGET_BATCHES_PER_MONTH,
        "shadow_retrain": "on" if shadow_available else "off",
        "rules": (
            f"cost_cap=max(月花费×{BUDGET_COST_HEADROOM},"
            f" ¥{BUDGET_COST_FLOOR_CNY:.0f})；"
            f"拒收率>{BUDGET_REJECTION_SHARE_GATE:.0%}→配额"
            f" {MAX_PROPOSALS_PER_RUN}→{BUDGET_REDUCED_QUOTA}；"
            "影子重训=③有数据才 on（票 07 后按对比窗驱动）——规则写死可复算"
        ),
        "advice": "预算定拍=人按本节建议执行（纯建议性，无 CLI 自动消费）",
    }


# ---- 报告装配与落盘（语料树 reports/monthly/，append-only 拍次） ----


def _claim_beat(monthly_dir: Path, report_month: str) -> int:
    """
    拍次独占声明（票 03 同款 O_EXCL）。

    同月重跑各抢各的拍次，报告文件 append-only 不被同名互覆；哨兵常驻
    不删（拍名永久保留，崩溃残拍不复用）。
    """
    monthly_dir.mkdir(parents=True, exist_ok=True)
    compact = report_month.replace("-", "")
    beat = 1
    while beat <= _MAX_BEATS_PER_MONTH:
        tag = "" if beat == 1 else f"-{beat}"
        claim = monthly_dir / f"monthly-read-{compact}{tag}{_CLAIM_SUFFIX}"
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            beat += 1
            continue
        os.close(fd)
        return beat
    raise RuntimeError(f"{report_month} 拍次声明超过 {_MAX_BEATS_PER_MONTH}：管线病态")


_SECTION_HEADERS = (
    ("gate_progress", "① 六门进度（DSR/BH 校正读数框架）"),
    ("clv_dual_anchor", "② CLV 审计双锚（主锚 srct Pinnacle/副锚欧共识）"),
    ("shadow_compare", "③ champion/challenger 影子对比窗"),
    ("version_events", "④ 版本状态机事件流"),
    ("forced_failure", "⑤ 强制失败视图（不可省略）"),
    ("clocks", "⑥ 双钟读数（含四信号面与 B.3 到期读数）"),
    ("lessons_view", "⑦ 教训库视图（新入库+复审到期）"),
    ("budget_suggestion", "⑧ 下月预算定拍建议（规则可复算）"),
)


def render_markdown(report: dict[str, Any]) -> str:
    """月读 MD 视图（人读面）：八节固定序，每节尾带「建议动作+CLI」提示。"""
    sections: dict[str, Any] = report["sections"]
    lines = [
        f"# 月读报告 {report['report_month']}",
        "",
        f"- 生成 {report['generated_at']}（run_date {report['run_date']}，"
        + f"拍次 {report['beat']}，收缩级别 {sections['clocks']['level']}）",
        "- 报告纯只读：一切裁定动作是人按本报告走 CLI 留痕；本报告不写任何决策状态",
        "",
    ]
    for key, header in _SECTION_HEADERS:
        section = sections[key]
        lines += [f"## {header}", ""]
        body = json.dumps(
            {k: v for k, v in section.items() if k != "advice"},
            ensure_ascii=False,
            indent=2,
        )
        lines += [
            body,
            "",
            f"**建议动作（只读提示）**: {section.get('advice', '')}",
            "",
        ]
    lines += [
        "## 首读说明",
        "",
        "- 月读 deployment（datasets 注册面 monthly-read，cron 35 9 1 * *）"
        + "注册后自然到点",
        "- 首读 2026-12-01 覆盖 11 月（半月数据，跑通优先于完整）",
    ]
    return "\n".join(lines) + "\n"


def write_report_files(
    store: CorpusStore, report: dict[str, Any], *, beat_suffix: int = 1
) -> tuple[Path, Path]:
    """报告落语料树 reports/monthly/（JSON+MD 双视图，temp+rename 原子写）。"""
    monthly_dir = store.root / MONTHLY_REPORTS_SUBDIR
    monthly_dir.mkdir(parents=True, exist_ok=True)
    tag = "" if beat_suffix == 1 else f"-{beat_suffix}"
    basename = f"monthly-read-{report['report_month'].replace('-', '')}{tag}"
    json_tmp = monthly_dir / f"{basename}.json.tmp"
    md_tmp = monthly_dir / f"{basename}.md.tmp"
    json_tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_tmp.write_text(render_markdown(report), encoding="utf-8")
    json_path = monthly_dir / f"{basename}.json"
    md_path = monthly_dir / f"{basename}.md"
    json_tmp.replace(json_path)
    md_tmp.replace(md_path)
    return json_path, md_path


def run_monthly_read(
    conn: sqlite3.Connection,
    *,
    store: CorpusStore,
    now: datetime | None = None,
    lessons_dir: Path | None = None,
    duck_con: DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """
    一次月读（八节装配+报告落盘，返回完整报告 dict）。

    幂等语义=append-only 拍次：同月重跑文件递增不覆盖；信号账同期吸收。
    观测面落账两处（双钟读数账/信号触发史账）——报告不写任何决策状态。
    """
    resolved = now or datetime.now(UTC)
    run_date = fx_store.beijing_business_date(resolved)
    report_month = report_month_of(run_date)
    failure = forced_failure(conn, store=store, report_month=report_month)
    shadow = shadow_compare(conn, report_month=report_month)
    sections = {
        "gate_progress": gate_progress(conn),
        "clv_dual_anchor": clv_dual_anchor(
            conn, report_month=report_month, duck_con=duck_con
        ),
        "shadow_compare": shadow,
        "version_events": version_events(conn, report_month=report_month),
        "forced_failure": failure,
        "clocks": clocks_section(conn, now=resolved, report_month=report_month),
        "lessons_view": lessons_section(
            lessons_dir, today=date.fromisoformat(run_date)
        ),
        "budget_suggestion": budget_suggestion(
            failure, shadow_available=shadow["status"] == "ok"
        ),
    }
    beat = _claim_beat(store.root / MONTHLY_REPORTS_SUBDIR, report_month)
    report: dict[str, Any] = {
        "schema": "monthly-read/1",
        "generated_at": utc_now_iso(),
        "run_date": run_date,
        "report_month": report_month,
        "beat": beat,
        "sections": sections,
    }
    json_path, md_path = write_report_files(store, report, beat_suffix=beat)
    report["files"] = {"json": str(json_path), "md": str(md_path)}
    return report
