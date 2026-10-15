"""
机会漏斗观测（caliber-loop 票 04：漏斗核心+轨一估值）。

只读观测面：把"零注"拆成可审计的逐级分母——

1. 在售（当日竞彩在售且未开赛）→
2. 报价匹配（竞彩 had 三向 × 锚书报价挂上）→
3. 两源时点合格（双端同窗新鲜度）→
4. 市场锚估值（锚 devig p_sharp；价值代理 O_JC×p_sharp−1 三向）→
5. 可购资格（had 单固+未停售，复用 quote_evidence 判定接缝）。

纪律：只写自有表（funnel_days/funnel_fixture_days），**不写 bets 表**
（是否让冻结决策落纸面注=票 06 人裁决项）；价值代理与方向位移并列
（收盘列票 06 回填）；轨二列（track2_json）票 05 填充；判活口径=
漏斗表最新业务日（栈停摆教训：对运行面时间戳判活）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.quote_evidence import (
    DEFAULT_FRESHNESS_SECONDS,
    adjudicate_had_quote,
    parse_ts,
    sharp_probs_asof,
)
from goalx_backend.markets import SELECTIONS

# 双端同窗异钟：JC=源调盘龄（adjudicate 口径）/锚=本机观测龄（books 口径）
FRESHNESS_WINDOW_SECONDS = DEFAULT_FRESHNESS_SECONDS

# 漏斗级常量（落库 level_reached 1..5）
L_ON_SALE, L_MATCHED, L_FRESH, L_VALUATION, L_ELIGIBLE = 1, 2, 3, 4, 5


@dataclass
class FunnelFixtureRow:
    """一场一日的漏斗明细（内存形，落库前组装）。"""

    fixture_id: int
    level_reached: int
    loss_reason: str | None
    kickoff_utc: str | None = None
    jc_odds: dict[str, float] = field(default_factory=dict)
    jc_age_seconds: float | None = None
    anchor_ref: str | None = None
    p_sharp: dict[str, float] = field(default_factory=dict)

    @property
    def value_proxy(self) -> dict[str, float]:
        """三向价值代理 O_JC × p_sharp − 1（缺端选项不产）。"""
        return {
            sel: round(self.jc_odds[sel] * self.p_sharp[sel] - 1.0, 4)
            for sel in SELECTIONS
            if sel in self.jc_odds and sel in self.p_sharp
        }


@dataclass
class FunnelDayReport:
    """一日的漏斗汇总（funnel_days 行 + 返回值）。"""

    business_date: str
    as_of: str
    n_on_sale: int = 0
    n_quote_matched: int = 0
    n_fresh: int = 0
    n_valuation: int = 0
    n_eligible: int = 0
    loss_reasons: dict[str, int] = field(default_factory=dict)
    value_proxy_stats: dict[str, dict[str, float | int | None]] = field(
        default_factory=dict
    )

    def as_dict(self) -> dict[str, Any]:
        """平铺（日志/flow 返回）。"""
        return asdict(self)


# 五级漏斗逐级早退，天然多出口
def _classify_fixture(  # noqa: PLR0911
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> FunnelFixtureRow:
    """一场的漏斗级判定（L2 起逐级，先失级即停）。"""
    fixture = fx_store.get_fixture(conn, fixture_id)
    kickoff_utc = str(fixture["kickoff_utc"]) if fixture else None
    verdict = adjudicate_had_quote(
        conn, fixture_id, as_of, freshness_seconds=FRESHNESS_WINDOW_SECONDS
    )
    row = FunnelFixtureRow(
        fixture_id=fixture_id,
        level_reached=L_ON_SALE,
        loss_reason=None,
        kickoff_utc=kickoff_utc,
        jc_odds=dict(verdict.jc_odds),
        jc_age_seconds=verdict.jc_age_seconds,
    )
    if verdict.sale_state == "stopped":
        # 停售归 L5（资格面）。adjudicate 对 stopped 短路早退（不评报价），
        # 报价级未评估——level_reached 停在 L1，归因只进流失桶不进中级分母
        row.loss_reason = "l5_sale_stopped"
        return row
    if not verdict.jc_odds:
        # 竞彩三向缺（无快照/不完整/观测时刻不可证）——取首个 jc_ 原因
        detail = next(
            (r for r in verdict.reasons if r.startswith("jc_")), "jc_no_quote"
        )
        row.loss_reason = f"l2_jc_{detail.removeprefix('jc_')}"
        return row
    attached = sharp_probs_asof(conn, fixture_id, as_of)
    if attached is None:
        row.loss_reason = "l2_anchor_absent"
        return row
    row.level_reached = L_MATCHED
    fresh = sharp_probs_asof(
        conn, fixture_id, as_of, max_age_seconds=FRESHNESS_WINDOW_SECONDS
    )
    if fresh is None:
        row.loss_reason = "l3_anchor_stale"
        return row
    if verdict.jc_age_seconds is not None and (
        verdict.jc_age_seconds > FRESHNESS_WINDOW_SECONDS
    ):
        row.loss_reason = "l3_jc_stale"
        return row
    row.level_reached = L_FRESH
    probs, anchor_ref = fresh
    row.p_sharp = {sel: round(probs[sel], 4) for sel in SELECTIONS}
    row.anchor_ref = anchor_ref
    row.level_reached = L_VALUATION
    # 资格：单固+状态可知（未开赛由 L1 分母保证；停售已在前置闸；未知按不过）
    if verdict.sale_state is None or verdict.sale_state == "unknown":
        row.loss_reason = "l5_sale_unknown"
        return row
    if verdict.single_eligible is not True:
        row.loss_reason = (
            "l5_single_not_eligible"
            if verdict.single_eligible is False
            else "l5_single_unknown"
        )
        return row
    row.level_reached = L_ELIGIBLE
    return row


def record_funnel_day(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    business_date: str | None = None,
) -> FunnelDayReport:
    """
    记录一日漏斗（幂等：同业务日先删后插，事务内）。

    as_of=冻结时点（缺省 now）；L1=当日销售编排在册且未开赛的场次。
    """
    moment = as_of or datetime.now(UTC).isoformat(timespec="seconds")
    bdate = business_date or fx_store.beijing_business_date(
        datetime.fromisoformat(moment)
    )
    on_sale_rows: list[sqlite3.Row] = [
        row
        for row in fx_store.fixtures_for_business_date(conn, bdate)
        if parse_ts(str(row["kickoff_utc"])) > parse_ts(moment)
    ]
    # 同场同日双销售编号（改号/补位）去重：JOIN 会产重复行，UNIQUE 键不容
    seen: set[int] = set()
    on_sale: list[sqlite3.Row] = []
    for fx in on_sale_rows:
        fid = int(fx["id"])
        if fid not in seen:
            seen.add(fid)
            on_sale.append(fx)
    report = FunnelDayReport(business_date=bdate, as_of=moment)
    proxies: dict[str, list[float]] = {sel: [] for sel in SELECTIONS}
    per_fixture_proxies: dict[int, dict[str, float]] = {}
    fixture_rows: list[FunnelFixtureRow] = []
    for fx in on_sale:
        row = _classify_fixture(conn, int(fx["id"]), moment)
        fixture_rows.append(row)
        if row.level_reached >= L_MATCHED:
            report.n_quote_matched += 1
        if row.level_reached >= L_FRESH:
            report.n_fresh += 1
        if row.level_reached >= L_VALUATION:
            report.n_valuation += 1
        if row.level_reached >= L_ELIGIBLE:
            report.n_eligible += 1
            per_fixture_proxies[row.fixture_id] = dict(row.value_proxy)
            for sel, value in row.value_proxy.items():
                proxies[sel].append(value)
        if row.loss_reason:
            report.loss_reasons[row.loss_reason] = (
                report.loss_reasons.get(row.loss_reason, 0) + 1
            )
    report.n_on_sale = len(on_sale)
    # 日级统计面只聚合 L5 通过场（描述可投集）；fixture 行 L4 起即有三向值
    report.value_proxy_stats = {
        sel: {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 4) if vals else None,
            "max": round(max(vals), 4) if vals else None,
        }
        for sel, vals in proxies.items()
    }
    # 票 05：双轨对照面（轨一冻结 × 轨二 24h/12h 拍点，含衰减/停售/迟到）
    eligible_ids = [r.fixture_id for r in fixture_rows if r.level_reached >= L_ELIGIBLE]
    report.value_proxy_stats["track2_compare"] = _dual_track_face(
        conn, eligible_ids, per_fixture_proxies
    )
    _persist(conn, report, fixture_rows)
    return report


def _persist(
    conn: sqlite3.Connection,
    report: FunnelDayReport,
    fixture_rows: list[FunnelFixtureRow],
) -> None:
    """落库（同业务日幂等覆盖：事务内先删后插）。"""
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    with conn:
        conn.execute(
            "DELETE FROM funnel_days WHERE business_date = ?", (report.business_date,)
        )
        conn.execute(
            "DELETE FROM funnel_fixture_days WHERE business_date = ?",
            (report.business_date,),
        )
        conn.execute(
            """
            INSERT INTO funnel_days
            (business_date, as_of, n_on_sale, n_quote_matched, n_fresh,
             n_valuation, n_eligible, loss_reasons, value_proxy_stats, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report.business_date,
                report.as_of,
                report.n_on_sale,
                report.n_quote_matched,
                report.n_fresh,
                report.n_valuation,
                report.n_eligible,
                json.dumps(report.loss_reasons, ensure_ascii=False),
                json.dumps(report.value_proxy_stats, ensure_ascii=False),
                created_at,
            ),
        )
        conn.executemany(
            " ".join(
                (
                    "INSERT INTO funnel_fixture_days",
                    "(business_date, fixture_id, as_of, level_reached, loss_reason,",
                    " kickoff_utc, jc_odds_h, jc_odds_d, jc_odds_a, jc_age_seconds,",
                    " anchor_ref, p_sharp_h, p_sharp_d, p_sharp_a,",
                    " value_proxy_h, value_proxy_d, value_proxy_a, track2_json)",
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                )
            ),
            [
                (
                    report.business_date,
                    row.fixture_id,
                    report.as_of,
                    row.level_reached,
                    row.loss_reason,
                    row.kickoff_utc,
                    row.jc_odds.get("h"),
                    row.jc_odds.get("d"),
                    row.jc_odds.get("a"),
                    row.jc_age_seconds,
                    row.anchor_ref,
                    row.p_sharp.get("h"),
                    row.p_sharp.get("d"),
                    row.p_sharp.get("a"),
                    row.value_proxy.get("h"),
                    row.value_proxy.get("d"),
                    row.value_proxy.get("a"),
                    _track2_json_of(conn, row.fixture_id),
                )
                for row in fixture_rows
            ],
        )


def funnel_liveness(conn: sqlite3.Connection) -> str | None:
    """判活口径：漏斗表最新业务日（栈停摆教训——对运行面时间戳判活）。"""
    row = conn.execute("SELECT MAX(business_date) FROM funnel_days").fetchone()
    return str(row[0]) if row and row[0] else None


def funnel_day_row(conn: sqlite3.Connection, business_date: str) -> sqlite3.Row | None:
    """读一日汇总（日终报告/接口共用读口）。"""
    return conn.execute(
        "SELECT * FROM funnel_days WHERE business_date = ?", (business_date,)
    ).fetchone()


# ---- 票 05：轨二拍点（24h/12h 固定提前量）与双轨对照 ----

TRACK2_OFFSETS: tuple[tuple[int, str], ...] = ((24, "24h"), (12, "12h"))
TRACK2_LATE_TOLERANCE_SECONDS = 15 * 60  # 迟到拍点旗：captured−target 超 15 分钟


@dataclass(frozen=True)
class Track2Capture:
    """一笔轨二拍点采集（task 侧组装，store 侧落库）。"""

    fixture_id: int
    snap_type: str
    target_instant: str
    captured_at: str
    purchasable: bool
    purchasable_reason: str | None = None
    jc_odds: dict[str, float] = field(default_factory=dict)
    jc_age_seconds: float | None = None
    p_sharp: dict[str, float] = field(default_factory=dict)
    anchor_ref: str | None = None


def record_track2_snapshot(
    conn: sqlite3.Connection, capture: Track2Capture
) -> dict[str, float]:
    """
    落一笔轨二拍点快照（幂等覆盖）+ 合并进漏斗明细行 track2_json。

    价值代理=拍点 O_JC × p_sharp − 1 三向（缺端如实 None）；停售边界
    purchasable=0 的拍点仍记录（观察面）但双轨对照只对可购拍点算代理。
    """
    odds = capture.jc_odds
    sharp = capture.p_sharp
    proxy = {
        sel: round(odds[sel] * sharp[sel] - 1.0, 4)
        for sel in SELECTIONS
        if sel in odds and sel in sharp
    }
    late = (
        parse_ts(capture.captured_at) - parse_ts(capture.target_instant)
    ).total_seconds() > TRACK2_LATE_TOLERANCE_SECONDS
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO track2_snapshots
            (fixture_id, snap_type, target_instant, captured_at, late_flag,
             purchasable, purchasable_reason,
             jc_odds_h, jc_odds_d, jc_odds_a, jc_age_seconds,
             anchor_ref, p_sharp_h, p_sharp_d, p_sharp_a,
             value_proxy_h, value_proxy_d, value_proxy_a)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture.fixture_id,
                capture.snap_type,
                capture.target_instant,
                capture.captured_at,
                int(late),
                int(capture.purchasable),
                capture.purchasable_reason,
                odds.get("h"),
                odds.get("d"),
                odds.get("a"),
                capture.jc_age_seconds,
                capture.anchor_ref,
                sharp.get("h"),
                sharp.get("d"),
                sharp.get("a"),
                proxy.get("h"),
                proxy.get("d"),
                proxy.get("a"),
            ),
        )
    _merge_track2_into_funnel(conn, capture.fixture_id)
    return proxy


def has_track2_snapshot(
    conn: sqlite3.Connection, fixture_id: int, snap_type: str
) -> bool:
    """该场该拍点是否已有快照（幂等跳过判据；SQL 归本包）。"""
    row = conn.execute(
        "SELECT 1 FROM track2_snapshots WHERE fixture_id = ? AND snap_type = ?",
        (fixture_id, snap_type),
    ).fetchone()
    return row is not None


def track2_snapshots_of(conn: sqlite3.Connection, fixture_id: int) -> dict[str, Any]:
    """一场的轨二快照 {snap_type: row dict}。"""
    rows = conn.execute(
        "SELECT * FROM track2_snapshots WHERE fixture_id = ?", (fixture_id,)
    ).fetchall()
    return {str(r["snap_type"]): dict(r) for r in rows}


def _merge_track2_into_funnel(conn: sqlite3.Connection, fixture_id: int) -> None:
    """把该场最新轨二快照合并进 funnel_fixture_days.track2_json（行在则并）。"""
    row = conn.execute(
        " ".join(
            (
                "SELECT business_date, track2_json FROM funnel_fixture_days",
                "WHERE fixture_id = ? ORDER BY business_date DESC LIMIT 1",
            )
        ),
        (fixture_id,),
    ).fetchone()
    if row is None:
        return
    existing: dict[str, Any] = (
        json.loads(row["track2_json"]) if row["track2_json"] else {}
    )
    existing.update(track2_snapshots_of(conn, fixture_id))
    with conn:
        conn.execute(
            " ".join(
                (
                    "UPDATE funnel_fixture_days SET track2_json = ?",
                    "WHERE business_date = ? AND fixture_id = ?",
                )
            ),
            (
                json.dumps(existing, ensure_ascii=False),
                row["business_date"],
                fixture_id,
            ),
        )


def _track2_json_of(conn: sqlite3.Connection, fixture_id: int) -> str | None:
    """一场已落轨二快照的 JSON（无快照=None 列保持可空）。"""
    snaps = track2_snapshots_of(conn, fixture_id)
    return json.dumps(snaps, ensure_ascii=False) if snaps else None


def _dual_track_face(
    conn: sqlite3.Connection,
    fixture_ids: list[int],
    track1_proxies: dict[int, dict[str, float]],
) -> dict[str, Any]:
    """
    双轨对照面：轨一（日冻结）×轨二（24h/12h 拍点）价值代理并列+衰减。

    衰减链=同时具有 24h/12h/轨一三面的场上的分侧均值（24h→12h→冻结）；
    停售拍点（purchasable=0）不进代理均值（可购边界语义）。
    """
    stages: dict[str, dict[str, list[float]]] = {
        stage: {sel: [] for sel in SELECTIONS} for stage in ("24h", "12h", "freeze")
    }
    purchasable_counts = {"24h": [0, 0], "12h": [0, 0]}  # [可购, 总]
    late_counts = {"24h": 0, "12h": 0}
    for fid in fixture_ids:
        snaps = track2_snapshots_of(conn, fid)
        for snap_type, snap in snaps.items():
            purchasable_counts[snap_type][1] += 1
            if snap["late_flag"]:
                late_counts[snap_type] += 1
            if not snap["purchasable"]:
                continue
            purchasable_counts[snap_type][0] += 1
            for sel in SELECTIONS:
                value = snap.get(f"value_proxy_{sel}")
                if value is not None:
                    stages[snap_type][sel].append(float(value))
        for sel in SELECTIONS:
            value = track1_proxies.get(fid, {}).get(sel)
            if value is not None:
                stages["freeze"][sel].append(value)
    face: dict[str, Any] = {
        "mean_value_proxy": {
            stage: {
                sel: round(fmean(vals), 4) if vals else None
                for sel, vals in sels.items()
            }
            for stage, sels in stages.items()
        },
        "purchasable": {
            k: {"eligible": v[0], "total": v[1]} for k, v in purchasable_counts.items()
        },
        "late_snapshots": late_counts,
        "note": "衰减链=同场三面（24h→12h→冻结）分侧均值；停售拍点不计代理",
    }
    return face
