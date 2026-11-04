"""
真实价格前瞻检查点读数（report-audit-20261103 票 09 预注册首跑）。

研究问题（路线 A 价格存活）：正式纸面决策发现的价格优势，到决策重绑、
确认落票、开球各时点还剩多少。分母=锁定且非 manual_research 的纸面注
（had 腿），五价时点由 bet_price_timeline 自动记录（票 09 记录管道）；
本模块纯读数——不改任何门禁行为。

口径硬约束（预注册 docs/plans/report-audit-20261103/prereg-price-edge.md；
主指标与分母不换，修订须记变更日志）：

- 主指标唯一：确认价 EV 中位数（O_confirm × p_ref(t_confirm) − 1，
  p_ref=12 书完整三向均值+Shin 共识，PIT as-of 确认时刻）；区间 =
  fixture 簇 bootstrap 95% CI（B=2000 种子 20261113，重抽场次腿随簇
  ——有效独立场次口径，串关共享腿不夸大独立性）；
- 协报告（描述层）：正候选率、+5/15/30 分钟与收盘的价差存活、票 08
  新鲜度影子双口径摘要（读数背景对表）；
- 正式判读门槛 ≥30 销售日（与延迟生存率姊妹协议共用
  delay_survival.count_on_sale_days 口径）；停止条件=30 销售日里可算
  腿 <30 判"决策流供给不足"；
- 参考缺（EU 无完整三向）的腿分母外单列 missing_reference，不冒充 0；
  非本协议窗口/非 had 腿/存量无时间线注天然不进（诚实记缺不倒填）。
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median
from typing import Any

from goalx_backend import run_fingerprint as rfp
from goalx_backend.betting import store as bt_store
from goalx_backend.data import delay_survival, mapping
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso

REPORTS_DIR = "reports"
REPORT_BASENAME = "price-edge-checkpoint"

# 存活偏移档（秒）：+5/15/30 分钟（prereg §5；描述层）
SURVIVAL_OFFSETS: tuple[int, ...] = (300, 900, 1800)

# 簇 bootstrap 定值（prereg §4，首读前定死不得回调）
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 20261113

# 判读门槛与停止条件定值（prereg §6/§7；门槛与姊妹协议共用常量）
SALE_DAY_GATE = delay_survival.SALE_DAY_GATE
LEG_SUPPLY_FLOOR = 30
# 簇 bootstrap 出区间的最少簇数；半衰判定的留存阈值（prereg §4/§5）
MIN_BOOTSTRAP_CLUSTERS = 2
HALF_LIFE_RETENTION = 0.5


@dataclass(frozen=True)
class _LegObservation:
    """一条确认价腿的读数面（EV 各时点 None=参考缺，不冒充）。"""

    bet_id: int
    fixture_id: int
    selection_code: str
    confirm_odds: float | None  # NULL=确认行落了但价缺（预注册 §3 诚实记缺）
    confirm_at: str
    ev_confirm: float | None
    discovery_at: str | None
    discovery_odds: float | None
    ev_discovery: float | None


def _ev_at(
    conn: sqlite3.Connection,
    fixture_id: int,
    selection: str,
    odds: float | None,
    as_of: str,
    duck_con: qe.DuckCon | None,
) -> float | None:
    """某时点价 × 该时点共识参考 − 1（价或参考缺 → None）。"""
    if odds is None:
        return None
    probs = qe.eu_consensus_asof(
        conn,
        fixture_id,
        as_of,
        duck_con=duck_con,
        sid=mapping.srct_sid_for_fixture(conn, fixture_id),
    )
    if probs is None or selection not in probs:
        return None
    return odds * probs[selection] - 1.0


def _jc_odds_asof(
    conn: sqlite3.Connection, fixture_id: int, selection: str, as_of: str
) -> float | None:
    """as_of 可证的该选项最新有效 JC 价（观测时点晚于 as_of 不算）。"""
    latest, _reason = qe.latest_jc_had_rows(conn, fixture_id, as_of)
    row = latest.get(selection)
    return float(row["odds"]) if row is not None else None


def _cluster_bootstrap_ci(
    legs: list[_LegObservation],
) -> tuple[float, float] | None:
    """
    Fixture 簇 bootstrap 的中位数 95% 区间（prereg §4）。

    有放回重抽 fixture 簇（种子固定=可复现），簇内全部腿合并算中位数；
    分位索引 = 排序后第 int(0.025·B) / int(0.975·B) 位（B=2000 即
    第 50/1950 位）——索引式在预注册落档，改动即口径变更。
    """
    clusters: dict[int, list[float]] = {}
    for leg in legs:
        if leg.ev_confirm is not None:
            clusters.setdefault(leg.fixture_id, []).append(leg.ev_confirm)
    values = [v for vs in clusters.values() for v in vs]
    if not values or len(clusters) < MIN_BOOTSTRAP_CLUSTERS:
        return None
    fixture_ids = sorted(clusters)
    # 种子固定的可复现重抽（统计用途，非密码学——S311 不适用）
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311
    medians: list[float] = []
    for _ in range(BOOTSTRAP_B):
        sampled = [
            v
            for fid in (rng.choice(fixture_ids) for _ in fixture_ids)
            for v in clusters[fid]
        ]
        medians.append(median(sampled))
    medians.sort()
    lower = medians[int(0.025 * BOOTSTRAP_B)]
    upper = medians[int(0.975 * BOOTSTRAP_B)]
    return lower, upper


def _survival_readout(
    conn: sqlite3.Connection,
    duck_con: qe.DuckCon | None,
    as_of: str,
    odds: float | None,
    leg: _LegObservation,
) -> tuple[float, _LegObservation] | None:
    """一腿某时点的 (EV, 腿)；价或参考缺返回 None。"""
    ev = _ev_at(conn, leg.fixture_id, leg.selection_code, odds, as_of, duck_con)
    return None if ev is None else (ev, leg)


def _survival_pair_face(
    pairs: list[tuple[float, _LegObservation]], missing: int
) -> dict[str, Any]:
    """一组 (EV, 腿) 的存活读数（留存比=EV_t/EV_discovery 中位数）。"""
    evs = [ev for ev, _leg in pairs]
    retention = (
        median(
            [
                ev / leg.ev_discovery
                for ev, leg in pairs
                if leg.ev_discovery  # 候选集已保证 >0；守除零不隐匿
            ]
        )
        if pairs
        else None
    )
    return {
        "n_ev_computable": len(evs),
        "n_missing_price_or_reference": missing,
        "positive_share": sum(1 for ev in evs if ev > 0) / len(evs) if evs else None,
        "median_ev": median(evs) if evs else None,
        "median_retention": retention,
    }


def _offset_faces(
    conn: sqlite3.Connection,
    candidates: list[_LegObservation],
    duck_con: qe.DuckCon | None,
) -> tuple[list[dict[str, Any]], int | None]:
    """逐偏移档读数 + 半衰档（中位留存 ≤50% 的最小偏移；未破记 None）。"""
    offsets: list[dict[str, Any]] = []
    half_life_s: int | None = None
    for offset in SURVIVAL_OFFSETS:
        pairs: list[tuple[float, _LegObservation]] = []
        missing = 0
        for leg in candidates:
            t = (
                datetime.fromisoformat(str(leg.discovery_at))
                + timedelta(seconds=offset)
            ).isoformat()
            odds = _jc_odds_asof(conn, leg.fixture_id, leg.selection_code, t)
            pair = _survival_readout(conn, duck_con, t, odds, leg)
            if pair is None:
                missing += 1
            else:
                pairs.append(pair)
        face = _survival_pair_face(pairs, missing)
        retention = face["median_retention"]
        if (
            half_life_s is None
            and retention is not None
            and retention <= HALF_LIFE_RETENTION
        ):
            half_life_s = offset
        offsets.append({"offset_s": offset, "n_candidates": len(candidates), **face})
    return offsets, half_life_s


def _close_asof(conn: sqlite3.Connection, fixture_id: int) -> str | None:
    """
    收盘锚时刻（开球 UTC ISO）。

    kickoff 经 kickoff_to_utc 归一（v1 legacy 北京墙钟裸串与 as-of 字典序
    比较恒晚于当日观测，会让收盘面静默打空——correctness F4）；解析失败
    返回 None 记缺。
    """
    fixture = fx_store.get_fixture(conn, fixture_id)
    if fixture is None:
        return None
    kickoff = fx_store.kickoff_to_utc(str(fixture["kickoff_utc"]))
    return kickoff.isoformat(timespec="seconds") if kickoff is not None else None


def _close_face(
    conn: sqlite3.Connection,
    candidates: list[_LegObservation],
    duck_con: qe.DuckCon | None,
) -> dict[str, Any]:
    """收盘读数：JC 收盘价=开球前最后有效快照 × 开球时点共识。"""
    pairs: list[tuple[float, _LegObservation]] = []
    missing = 0
    for leg in candidates:
        kickoff = _close_asof(conn, leg.fixture_id)
        pair = (
            None
            if kickoff is None
            else _survival_readout(
                conn,
                duck_con,
                kickoff,
                _jc_odds_asof(conn, leg.fixture_id, leg.selection_code, kickoff),
                leg,
            )
        )
        if pair is None:
            missing += 1
        else:
            pairs.append(pair)
    return _survival_pair_face(pairs, missing)


def _survival_face(
    conn: sqlite3.Connection,
    legs: list[_LegObservation],
    duck_con: qe.DuckCon | None,
) -> dict[str, Any]:
    """价差存活协报告（正候选子集 EV_discovery>0，prereg §5 描述层）。"""
    candidates = [
        leg
        for leg in legs
        if leg.ev_discovery is not None
        and leg.ev_discovery > 0.0
        and leg.discovery_at is not None
    ]
    offsets, half_life_s = _offset_faces(conn, candidates, duck_con)
    # 半衰口径=偏移档内最小破 50% 档（prereg §5 原文）；收盘留存另列
    # close 面，不折进半衰读数
    return {
        "n_candidates": len(candidates),
        "offsets": offsets,
        "close": _close_face(conn, candidates, duck_con),
        "median_half_life_offset_s": half_life_s,
    }


def _collect_legs(
    conn: sqlite3.Connection,
    date_from: str,
    date_to: str,
    duck_con: qe.DuckCon | None,
) -> tuple[list[_LegObservation], dict[str, int]]:
    """正式纸面决策腿 → 观察面（had 外/参考缺如实分层计数）。"""
    legs: list[_LegObservation] = []
    counts = {"n_bets": 0, "n_legs": 0, "n_non_had": 0, "missing_reference": 0}
    seen_bets: set[int] = set()
    for row in bt_store.official_paper_decision_legs(conn, date_from, date_to):
        bet_id = int(row["bet_id"])
        if bet_id not in seen_bets:
            seen_bets.add(bet_id)
            counts["n_bets"] += 1
        if str(row["market_code"]) != "had":
            counts["n_non_had"] += 1
            continue
        counts["n_legs"] += 1
        fixture_id = int(row["fixture_id"])
        selection = str(row["selection_code"])
        confirm_at = str(row["confirm_at"])
        confirm_odds = (
            float(row["confirm_odds"]) if row["confirm_odds"] is not None else None
        )
        discovery_at = (
            str(row["discovery_at"]) if row["discovery_at"] is not None else None
        )
        discovery_odds = (
            float(row["discovery_odds"]) if row["discovery_odds"] is not None else None
        )
        ev_confirm = _ev_at(
            conn, fixture_id, selection, confirm_odds, confirm_at, duck_con
        )
        if ev_confirm is None:
            counts["missing_reference"] += 1
        ev_discovery = (
            _ev_at(conn, fixture_id, selection, discovery_odds, discovery_at, duck_con)
            if discovery_at is not None
            else None
        )
        legs.append(
            _LegObservation(
                bet_id=bet_id,
                fixture_id=fixture_id,
                selection_code=selection,
                confirm_odds=confirm_odds,
                confirm_at=confirm_at,
                ev_confirm=ev_confirm,
                discovery_at=discovery_at,
                discovery_odds=discovery_odds,
                ev_discovery=ev_discovery,
            )
        )
    return legs, counts


def _route_verdict(ci: tuple[float, float] | None) -> str:
    """
    预注册三向判读（2026-11-03 人裁决②的机械规则，prereg §4）。

    CI 缺（簇 <2）时不出判读——如实留待数据面补足，不拿"证据不足"
    冒充区间结论。仅正式判读窗（≥30 销售日）由装配层注入——provisional
    阶段恒 None，不给 JSON 消费方提前行动的抓手（correctness F2）。
    """
    if ci is None:
        return "区间不可算（fixture 簇<2）——不出判读，继续积累"
    lower, upper = ci
    if upper <= 0.0:
        return "路线否定（区间上界≤0）"
    if lower > 0.0:
        return "升级裁决（区间下界>0）"
    return "证据不足继续积累（区间跨零）"


def _main_metric(legs: list[_LegObservation]) -> dict[str, Any]:
    """主指标：确认价 EV 中位数 + fixture 簇 bootstrap 95% CI（prereg §4）。"""
    evs = [leg.ev_confirm for leg in legs if leg.ev_confirm is not None]
    return {
        "n_computable": len(evs),
        "median_ev_confirm": median(evs) if evs else None,
        "bootstrap95": _cluster_bootstrap_ci(legs),
        "positive_candidate_rate": (
            sum(1 for ev in evs if ev > 0) / len(evs) if evs else None
        ),
        "n_effective_fixtures": len(
            {leg.fixture_id for leg in legs if leg.ev_confirm is not None}
        ),
    }


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def build_price_edge_report(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    duck_con: qe.DuckCon | None = None,
    store: CorpusStore | None = None,
) -> dict[str, Any]:
    """
    价格存活检查点报告（窗口两端含，按 confirm 阶段落窗）。

    conn=运行面连接（只读即可）；duck_con 缺席时共识参考仅 legacy
    ``odds_api`` 行可算（corpus 有链场次全落 missing_reference——CLI
    生产路径必开语料）。store 在场时报告落语料树 reports/（JSON+MD
    两视图）并随运行指纹。全读同窗一个读事务（并发写下计数不自相
    涂抹，同 delay-audit 事务纪律）。
    """
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")  # 纯读快照：deferred，首读才锁定视图
    try:
        report = _assemble_report(
            conn, date_from=date_from, date_to=date_to, duck_con=duck_con, store=store
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
    date_from: str,
    date_to: str,
    duck_con: qe.DuckCon | None,
    store: CorpusStore | None,
) -> dict[str, Any]:
    """单读事务内的报告装配（调用方负责事务壳与落盘）。"""
    legs, counts = _collect_legs(conn, date_from, date_to, duck_con)
    sale_days = delay_survival.count_on_sale_days(conn, date_from, date_to)
    metric = _main_metric(legs)
    shadow_date = qe.latest_freshness_shadow_date(conn)
    shadow = (
        qe.freshness_shadow_summary(conn, shadow_date)
        if shadow_date is not None
        else None
    )
    supply_short = (
        sale_days >= SALE_DAY_GATE and metric["n_computable"] < LEG_SUPPLY_FLOOR
    )
    formal_ready = sale_days >= SALE_DAY_GATE
    if formal_ready:
        # 三向判读只在正式窗注入（correctness F2）；provisional 恒 None
        metric["route_verdict"] = _route_verdict(metric["bootstrap95"])
    report: dict[str, Any] = {
        "schema": "price-edge-checkpoint/1",
        "generated_at": utc_now_iso(),
        "window": {
            "date_from": date_from,
            "date_to": date_to,
            "date_basis": "confirm 阶段 recorded_at UTC 自然日",
        },
        "protocol": {
            "prereg": "docs/plans/report-audit-20261103/prereg-price-edge.md",
            "reference": "12 书完整三向均值+Shin（eu_consensus_asof 同口径）",
            "sale_days": sale_days,
            "sale_day_gate": SALE_DAY_GATE,
            "metric_status": (
                "provisional" if sale_days < SALE_DAY_GATE else "formal_ready"
            ),
            "first_read": "2026-11-13",
            "cadence": "双周检查点（与延迟生存率协议共用读数日）",
        },
        "denominator": counts,
        "main_metric": metric,
        "survival": _survival_face(conn, legs, duck_con),
        "freshness_shadow": {
            "business_date": shadow_date,
            "summary": shadow,
            "note": "票 08 双口径覆盖差对表（规则口径拒掉的观测口径新鲜面）",
        },
        "stop_condition": {
            "leg_supply_floor": LEG_SUPPLY_FLOOR,
            "supply_insufficient": supply_short,
            "verdict": (
                "决策流供给不足（30 销售日里可算腿<30）→ 归档，重开须用户裁决"
                if supply_short
                else "未触发"
            ),
        },
        "close_face": "derived" if duck_con is not None else "legacy_odds_only",
        "known_boundaries": [
            "时间线自记录管道上线起（存量已锁注无 confirm 行，不倒填）。",
            "发现→确认可能同会话极短间隔；存活偏移自 discovery 起算照算不弃。",
            "参考缺（EU 无完整三向）的腿分母外单列 missing_reference，不冒充 0。",
            "协报告全部描述层；未来任何分层正式检验用 Holm 校正（prereg §7）。",
        ],
    }
    if store is not None:
        report["run_fingerprint"] = rfp.build_run_fingerprint(conn=conn, store=store)
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    """MD 视图（检查点人读面；数字与 JSON 同源）。"""
    lines: list[str] = []
    w = lines.append
    metric = report["main_metric"]
    proto = report["protocol"]
    ci = metric["bootstrap95"]
    ci_txt = "—" if ci is None else f"[{ci[0]:.3f}, {ci[1]:.3f}]"
    shadow = report["freshness_shadow"]
    shadow_summary = shadow["summary"]
    w("# 价格存活检查点（路线 A 真实价格前瞻）")
    w("")
    w(
        f"- 窗口 {report['window']['date_from']} → {report['window']['date_to']}"
        + f"（{report['window']['date_basis']}）；收盘面 = {report['close_face']}"
    )
    w(
        f"- 判读状态：{proto['metric_status']}"
        + f"（销售日 {proto['sale_days']}/门槛 {proto['sale_day_gate']}；"
        + f"首读 {proto['first_read']}，{proto['cadence']}）"
    )
    w(
        f"- 分母：{report['denominator']['n_bets']} 注 /"
        + f" {report['denominator']['n_legs']} had 腿（非 had"
        + f" {report['denominator']['n_non_had']} 腿单列；参考缺"
        + f" {report['denominator']['missing_reference']} 腿单列）"
    )
    w(
        f"- **主指标：确认价 EV 中位数 = {_fmt(metric['median_ev_confirm'])}"
        + f" {ci_txt}**（可算 {metric['n_computable']} 腿 /"
        + f" {metric['n_effective_fixtures']} 有效独立场次，fixture 簇"
        + f" bootstrap B={BOOTSTRAP_B}）"
    )
    w(
        f"- 协报告·正候选率：{_fmt_pct(metric['positive_candidate_rate'])}"
        + "（EV_confirm>0 腿占比，描述层）"
    )
    if proto["metric_status"] == "formal_ready":
        w(f"- 三向判读：{metric['route_verdict']}")
    w("")
    w("## 价差存活（正候选子集，描述层）")
    w("")
    w("| 时点 | 可算 | 缺价/缺参考 | 正占比 | 中位 EV | 中位留存 |")
    w("|---|---|---|---|---|---|")
    surv = report["survival"]
    for offset in surv["offsets"]:
        w(
            f"| +{offset['offset_s'] // 60}min | {offset['n_ev_computable']}"
            + f" | {offset['n_missing_price_or_reference']}"
            + f" | {_fmt_pct(offset['positive_share'])}"
            + f" | {_fmt(offset['median_ev'])}"
            + f" | {_fmt(offset['median_retention'])} |"
        )
    close = surv["close"]
    w(
        f"| 收盘 | {close['n_ev_computable']} |"
        + f" {close['n_missing_price_or_reference']}"
        + f" | {_fmt_pct(close['positive_share'])} | {_fmt(close['median_ev'])}"
        + f" | {_fmt(close['median_retention'])} |"
    )
    w(
        f"- 中位半衰偏移：{_fmt(surv['median_half_life_offset_s'])}"
        + "（None=偏移档内未破 50%；收盘留存另列上表）"
    )
    w("")
    w("## 票 08 新鲜度影子对表（读数背景）")
    w("")
    if shadow_summary is None:
        w(f"- 快照日 {shadow['business_date']}：无摘要（未拍或表未建）")
    else:
        w(
            f"- 快照日 {shadow['business_date']}：可见 {shadow_summary['n']}"
            + f" / 可比 {shadow_summary['n_comparable']}——规则新鲜"
            + f" {shadow_summary['rule_fresh']} · 观测新鲜"
            + f" {shadow_summary['observed_fresh']} · 分歧(规则旧/观测新)"
            + f" {shadow_summary['divergent_rule_stale_observed_fresh']}"
        )
    w("")
    w(f"- 停止条件：{report['stop_condition']['verdict']}")
    w("")
    w("## 已知边界")
    w("")
    for boundary in report["known_boundaries"]:
        w(f"- {boundary}")
    return "\n".join(lines) + "\n"


def _write_two_views(store: CorpusStore, report: dict[str, Any]) -> None:
    """报告落语料树 reports/（JSON+MD 两视图，temp+rename 同 delay-audit）。"""
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
