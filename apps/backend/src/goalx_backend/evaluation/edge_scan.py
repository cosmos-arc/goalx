"""
探索轨扫描器阵列（autoresearch-20261107 票 01/04）。

常设机制=扫描器注册表 + 发现台账 + 周报：每天自己跑、自己留痕、自己报
候选，不再依赖 episodic 手工分析。两表（v45，SQL 归本包 ADR-0008）：

- ``edge_findings``：发现台账（append-only，探索轨唯一事实源）。越门发现
  一行，UNIQUE(scanner, segment_key, window) 吸收同窗重跑；status
  生命周期 new→promoted|dead 由人裁经 ``mark_finding_status``（晋升=
  开验证票，不自动建）。
- ``edge_observations``：运行/观测留痕（s1 逐窗统计快照、s4 观测日——
  无信号日如实记零、s5 期次回看）。派生态非证据，同键重跑 REPLACE。

扫描器 v0（S2/S3 随数据面点亮后向 ``SCANNERS`` 注册）：

- **s1 市场校准**：滚动 90 日窗已结算场，JC had as-of 开球三向比例去水，
  按价格带×方向分桶累计实际频率 vs 均值概率，二项 SE 越门
  （n≥门 且 |dev|>2SE）落发现。``run_s1_decade`` = 研究库
  backtest_predictions 十年回放（口径验证面；只落观测不落发现——十年
  手工版已裁决"长赔高估 3-4pp、扣 13% 抽水后无正 EV 桶"，research/60）。
- **s4 陈盘观测日**（票 04）：逐场决策时点（开球−60min）陈盘信号
  confirmed/fired/命中计数留痕——"有分辨观测日"积累器（had_quote 闸 +
  ``stale_line_signal`` 既有面，B1 已关闭可交易性→纯监测不产候选）。
- **s5 滚存回看**（票 04）：池期次兑付后回看奖池结构（滚存标记/池规模/
  派彩分布）；按缺席自补（近 7 日发布期次夜刷保鲜），首跑即历史回放。
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, cast

from goalx_backend import odds_math as om
from goalx_backend import run_fingerprint as rfp
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso

REPORTS_DIR = "reports"
REPORT_BASENAME = "edge-scan-digest"

# ---- S1 口径常量（v0；显著性门形式化随票 05，先小后改须过人裁） ----
S1_WINDOW_DAYS = 90
S1_MIN_N = 100
S1_GATE_SE = 2.0
#: 价格带边界（上界, 标签）——与十年手工版/edge 板 03 同一分桶口径
BUCKET_EDGES: tuple[tuple[float, str], ...] = (
    (0.10, "p00-10"),
    (0.20, "p10-20"),
    (0.30, "p20-30"),
    (0.40, "p30-40"),
    (0.50, "p40-50"),
    (0.65, "p50-65"),
    (1.01, "p65+"),
)

# ---- S4 口径常量 ----
S4_DECISION_LEAD_MINUTES = 60
#: 陈旧门=15min（H4 清洁口径：15min 未跟概率 92.5%，票 08）
S4_STALE_MINUTES = 15
#: 有分辨门=sharp 漂移≥1.5pp（v0 常量，形式化随票 05）
S4_DRIFT_THRESHOLD = 0.015

#: s5 已观测期次的保鲜窗：近 7 日发布期次夜刷（官方 state 可后补/更正）
S5_REFRESH_DAYS = 7

DIRECTIONS: tuple[str, str, str] = ("h", "d", "a")
_LETTER_BY_DIRECTION = {"h": "H", "d": "D", "a": "A"}
_SELECTION_INDEX = {"h": 0, "d": 1, "a": 2}


@dataclass(frozen=True)
class ScanContext:
    """一次扫描的运行上下文（注册表统一签名；各扫描器按需取用）。"""

    business_date: str
    duck_con: qe.DuckCon | None = None
    resolve_sid: Callable[[int], str | None] | None = None


@dataclass(frozen=True)
class ScannerSpec:
    """扫描器注册项：名字/节奏/描述/统一入口。"""

    name: str
    cadence: str
    description: str
    run: Callable[[sqlite3.Connection, ScanContext], dict[str, Any]]


# ---- 分桶核心（s1 两面共用；纯函数，测试直证口径） ----


@dataclass(frozen=True)
class ObsRow:
    """一个方向观测：去水概率 + 是否命中。"""

    direction: str
    prob: float
    hit: bool


def bucket_label(prob: float) -> str:
    """去水概率 → 价格带标签（0≤p<1.01 全覆盖）。"""
    for upper, label in BUCKET_EDGES:
        if prob < upper:
            return label
    msg = f"去水概率越界: {prob}"
    raise ValueError(msg)


def calibration_segments(rows: list[ObsRow]) -> dict[str, dict[str, Any]]:
    """
    分桶累计 + 二项 SE 越门判定（十年手工版同款口径）。

    每段：n / mean_prob（去水概率均值）/ actual_freq（实测频率）/
    dev=af−mean / se=sqrt(p̄(1−p̄)/n)；越门 = n≥S1_MIN_N 且
    |dev|>S1_GATE_SE·se。
    """
    grouped: dict[str, list[ObsRow]] = {}
    for row in rows:
        key = f"{bucket_label(row.prob)}:{row.direction}"
        grouped.setdefault(key, []).append(row)
    segments: dict[str, dict[str, Any]] = {}
    for key in sorted(grouped):
        group = grouped[key]
        n = len(group)
        mean_prob = sum(r.prob for r in group) / n
        actual_freq = sum(r.hit for r in group) / n
        dev = actual_freq - mean_prob
        se = math.sqrt(mean_prob * (1.0 - mean_prob) / n)
        segments[key] = {
            "n": n,
            "mean_prob": round(mean_prob, 6),
            "actual_freq": round(actual_freq, 6),
            "dev": round(dev, 6),
            "se": round(se, 6),
            "crossed": n >= S1_MIN_N and abs(dev) > S1_GATE_SE * se,
        }
    return segments


# ---- 台账读写（两表 SQL 归本包） ----


def _record_observation(
    conn: sqlite3.Connection, scanner: str, window_key: str, payload: dict[str, Any]
) -> None:
    """观测留痕（派生态非证据，同键 REPLACE 重建）。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (scanner, window_key, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
    )


def insert_findings(
    conn: sqlite3.Connection,
    *,
    scanner: str,
    window: str,
    segments: dict[str, dict[str, Any]],
    extra_notes: dict[str, Any],
) -> int:
    """
    越门段落发现台账（INSERT OR IGNORE：同窗重跑不重复落行）。

    已人裁段（promoted|dead）粘滞：滚动窗滑日后同段再越门不再落新行——
    死刑/晋升是段级终态（spec"死刑=计数留痕"），重开须人裁翻案。
    守卫读+插入同一写事务（BEGIN IMMEDIATE）：与 CLI --mark 人裁并发时
    不落"刚裁死段"的新行（check-then-act 关窗）。
    """
    conn.execute("BEGIN IMMEDIATE")
    adjudicated = {
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT segment_key FROM edge_findings
            WHERE scanner = ? AND status != 'new'
            """,
            (scanner,),
        )
    }
    landed = 0
    for key, seg in segments.items():
        if not seg["crossed"] or key in adjudicated:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO edge_findings
                (scanner, segment_key, metric_value, n, se, window, status,
                 found_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                scanner,
                key,
                float(seg["dev"]),
                int(seg["n"]),
                float(seg["se"]),
                window,
                utc_now_iso(),
                json.dumps({**extra_notes, **seg}, ensure_ascii=False),
            ),
        )
        landed += cur.rowcount
    return landed


def mark_finding_status(
    conn: sqlite3.Connection, *, scanner: str, segment_key: str, status: str
) -> int:
    """发现状态人裁（new→promoted=开验证票 / dead；返回受影响行数）。"""
    if status not in ("promoted", "dead"):
        msg = f"status 只接受 promoted|dead: {status}"
        raise ValueError(msg)
    cur = conn.execute(
        "UPDATE edge_findings SET status = ? WHERE scanner = ? AND segment_key = ?",
        (status, scanner, segment_key),
    )
    changed = cur.rowcount
    conn.commit()
    return changed


def latest_observation_at(conn: sqlite3.Connection, scanner: str) -> str | None:
    """
    某扫描器最新留痕时点（晨检面，只认运行面窗）。

    decade 验证行是一次性手动面——计入会让晨检把死掉的日频面读成
    新鲜（correctness F1），排除之。
    """
    row = conn.execute(
        """
        SELECT MAX(created_at) FROM edge_observations
        WHERE scanner = ? AND window_key NOT LIKE 'decade:%'
        """,
        (scanner,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


# ---- S1：市场校准日扫 ----


def _outcome_letter(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"


def _settled_jc_rows(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> tuple[list[ObsRow], dict[str, int]]:
    """
    窗内已结算场 × JC had as-of 开球三向 → 方向观测行 + 覆盖账。

    已结算=draw_results 非 void（settled_jingcai_fixtures 同底盘）；JC 价
    =latest_odds_evidence_asof（有效观测时刻 ≤ 开球，重放确定性）。
    """
    coverage = {"n_settled": 0, "n_with_jc": 0, "n_incomplete_triple": 0}
    rows: list[ObsRow] = []
    for fixture in rs_store.settled_jingcai_fixtures(conn):
        business_date = str(fixture["business_date"])
        if not date_from <= business_date <= date_to:
            continue
        coverage["n_settled"] += 1
        kickoff = str(fixture["kickoff_utc"])
        evidence = fx_store.latest_odds_evidence_asof(
            conn, fixture["fixture_id"], "had", kickoff
        )
        if not evidence:
            continue
        prices = [evidence.get(sel) for sel in DIRECTIONS]
        if any(price is None or price["odds"] is None for price in prices):
            coverage["n_incomplete_triple"] += 1
            continue
        coverage["n_with_jc"] += 1
        triple = tuple(float(price["odds"]) for price in prices if price is not None)
        probs = om.normalized_implied(triple)
        letter = _outcome_letter(int(fixture["home_goals"]), int(fixture["away_goals"]))
        for position, direction in enumerate(DIRECTIONS):
            rows.append(
                ObsRow(
                    direction=direction,
                    prob=probs[position],
                    hit=letter == _LETTER_BY_DIRECTION[direction],
                )
            )
    return rows, coverage


def run_s1(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """S1 市场校准滚动窗扫描（发现+观测留痕；同窗重跑幂等）。"""
    window_end = _validated_business_date(ctx.business_date)
    window_from = (
        date.fromisoformat(window_end) - timedelta(days=S1_WINDOW_DAYS - 1)
    ).isoformat()
    window = f"{window_from}..{window_end}"
    # 读相一个 deferred 读事务（price_edge 同款）：场列表+逐场 odds 不被
    # 并发采集拍涂改；读毕回滚再进写相（correctness F3）
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        rows, coverage = _settled_jc_rows(
            conn, date_from=window_from, date_to=window_end
        )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    segments = calibration_segments(rows)
    landed = insert_findings(
        conn,
        scanner="s1",
        window=window,
        segments=segments,
        extra_notes={"face": "runtime", "window_days": S1_WINDOW_DAYS},
    )
    _record_observation(
        conn,
        "s1",
        window_end,
        {
            "face": "runtime",
            "window": window,
            "window_days": S1_WINDOW_DAYS,
            "gate": {"min_n": S1_MIN_N, "gate_se": S1_GATE_SE},
            "coverage": coverage,
            "segments": segments,
        },
    )
    conn.commit()
    return {
        "scanner": "s1",
        "window": window,
        "n_rows": len(rows),
        "coverage": coverage,
        "n_segments": len(segments),
        "crossed": sorted(k for k, v in segments.items() if v["crossed"]),
        "findings_landed": landed,
    }


def run_s1_decade(
    conn: sqlite3.Connection, *, research_conn: sqlite3.Connection
) -> dict[str, Any]:
    """
    S1 十年回放（口径验证面）：研究库 backtest_predictions fair_probs。

    与手工版（research/60 十年市场自校准表）同分桶同判定；只落观测不落
    发现——该面结论已人裁归档，发现台账不重复收录历史已知偏差。
    """
    run = research_conn.execute(
        """
        SELECT r.id, r.label FROM backtest_runs r
        WHERE r.status = 'done' AND EXISTS (
            SELECT 1 FROM backtest_predictions p
            WHERE p.run_id = r.id AND p.ftr IN ('H', 'D', 'A')
        )
        ORDER BY r.id DESC LIMIT 1
        """
    ).fetchone()
    if run is None:
        msg = "研究库无可判读回放 run（backtest_predictions 无已结算行）"
        raise FileNotFoundError(msg)
    rows: list[ObsRow] = []
    for probs_json, ftr in research_conn.execute(
        """
        SELECT fair_probs, ftr FROM backtest_predictions
        WHERE run_id = ? AND ftr IN ('H', 'D', 'A')
        """,
        (run["id"],),
    ):
        probs = json.loads(str(probs_json))
        for direction in DIRECTIONS:
            rows.append(
                ObsRow(
                    direction=direction,
                    prob=float(probs[direction]),
                    hit=str(ftr) == _LETTER_BY_DIRECTION[direction],
                )
            )
    segments = calibration_segments(rows)
    window_key = f"decade:{run['id']}"
    _record_observation(
        conn,
        "s1",
        window_key,
        {
            "face": "decade",
            "run_id": int(run["id"]),
            "run_label": str(run["label"]),
            "gate": {"min_n": S1_MIN_N, "gate_se": S1_GATE_SE},
            "segments": segments,
        },
    )
    conn.commit()
    return {
        "scanner": "s1-decade",
        "run_id": int(run["id"]),
        "run_label": str(run["label"]),
        "n_rows": len(rows),
        "segments": segments,
    }


# ---- S4：陈盘观测日（票 04） ----


def _triple_groups(
    rows: list[sqlite3.Row],
) -> dict[str, tuple[tuple[float, float, float], str]]:
    """
    JC had 全时线 → {captured_at: (三向赔率, 组内最晚有效观测时刻)}。

    仅保留 h/d/a 齐全的调盘组（残组不可比价）。
    """
    odds_by_move: dict[str, dict[str, float]] = {}
    seen_by_move: dict[str, str] = {}
    for row in rows:
        move = str(row["captured_at"])
        seen = str(row["observed_at"] or row["captured_at"])
        if row["selection_code"] in DIRECTIONS and row["odds"] is not None:
            odds_by_move.setdefault(move, {})[str(row["selection_code"])] = float(
                row["odds"]
            )
        seen_by_move[move] = max(seen_by_move.get(move, ""), seen)
    return {
        move: ((g["h"], g["d"], g["a"]), seen_by_move[move])
        for move, g in odds_by_move.items()
        if set(g) >= set(DIRECTIONS)
    }


def run_s4(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """
    S4 陈盘观测日：决策时点（开球−60min）信号计数留痕（无信号日记零）。

    confirmed=陈盘（≥15min 未调）且 sharp 漂移≥1.5pp；fired=决策后到
    开球前 JC 再调盘（分母=confirmed）；命中=再调方向与漂移同号。
    """
    counts = {
        "n_fixtures": 0,
        "n_observed": 0,
        "n_stale": 0,
        "n_confirmed": 0,
        "n_fired": 0,
        "n_hit": 0,
    }
    business_date = _validated_business_date(ctx.business_date)
    # 读相一个 deferred 读事务：逐场时线+sharp 锚不被并发采集拍涂改
    # （correctness F3）；循环零写，读毕回滚再落观测
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        _s4_read_phase(conn, ctx, counts)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    _record_observation(
        conn,
        "s4",
        business_date,
        {
            "business_date": business_date,
            **counts,
            "hit_rate": (
                round(counts["n_hit"] / counts["n_fired"], 6)
                if counts["n_fired"]
                else None
            ),
            "thresholds": {
                "decision_lead_minutes": S4_DECISION_LEAD_MINUTES,
                "stale_minutes": S4_STALE_MINUTES,
                "drift_threshold": S4_DRIFT_THRESHOLD,
            },
        },
    )
    conn.commit()
    return {"scanner": "s4", **counts}


def _s4_read_phase(
    conn: sqlite3.Connection,
    ctx: ScanContext,
    counts: dict[str, int],
) -> None:
    """S4 逐场读相（调用方负责读事务壳；只递增 counts 不写库）。"""
    for fixture in fx_store.fixtures_for_business_date(conn, ctx.business_date):
        counts["n_fixtures"] += 1
        kickoff = str(fixture["kickoff_utc"])
        decision = (
            datetime.fromisoformat(kickoff)
            - timedelta(minutes=S4_DECISION_LEAD_MINUTES)
        ).isoformat()
        history = fx_store.odds_history(conn, fixture["id"], "had", source="sporttery")
        # as-of 开球准入（重放确定性）：开球后补录行不进"决策后触发"判定
        admitted = [
            row
            for row in history
            if str(row["observed_at"] or row["captured_at"]) <= kickoff
        ]
        groups = _triple_groups(admitted)
        visible = {m: v for m, v in groups.items() if v[1] <= decision}
        if not visible:
            continue  # had_quote 闸：决策时点无可见完整三向不可观测
        counts["n_observed"] += 1
        signal = qe.stale_line_signal(
            conn,
            fixture["id"],
            as_of=decision,
            duck_con=ctx.duck_con,
            sid=ctx.resolve_sid(fixture["id"]) if ctx.resolve_sid else None,
        )
        if signal is None:
            continue
        stale = signal.minutes_since_move >= S4_STALE_MINUTES
        if stale:
            counts["n_stale"] += 1
        confirmed = (
            stale
            and signal.drift is not None
            and abs(signal.drift) >= S4_DRIFT_THRESHOLD
            and signal.drift_selection is not None
        )
        if not confirmed or signal.drift is None or signal.drift_selection is None:
            continue
        counts["n_confirmed"] += 1
        final_move = max(groups)
        if final_move <= decision:
            continue  # 决策后再无调盘：信号未触发
        counts["n_fired"] += 1
        p_decision = om.normalized_implied(visible[max(visible)][0])
        p_final = om.normalized_implied(groups[final_move][0])
        idx = _SELECTION_INDEX[signal.drift_selection]
        moved = p_final[idx] - p_decision[idx]
        if moved != 0 and (moved > 0) == (signal.drift > 0):
            counts["n_hit"] += 1


# ---- S5：池期次滚存回看（票 04） ----


def _summarize_tiers(prize_tiers: str | None) -> dict[str, dict[str, Any]]:
    """官方 prize_tiers JSON → 玩法级结构摘要（pool_after/档位数/头奖）。"""
    games: dict[str, dict[str, Any]] = {}
    if not prize_tiers:
        return games
    try:
        raw: dict[str, Any] = json.loads(prize_tiers)
    except ValueError:
        return games  # 坏档如实空摘要（payload 无 games 不冒充）
    for game, face in raw.items():
        if not isinstance(face, dict):
            continue
        # isinstance 把 Any 收窄成 dict[Unknown, Unknown]——cast 回显式键型
        game_face = cast("dict[str, Any]", face)
        tiers: list[dict[str, Any]] = list(game_face.get("tiers") or [])
        top = max(
            (t for t in tiers if t.get("prize_amount") is not None),
            key=lambda t: float(t["prize_amount"]),
            default=None,
        )
        games[str(game)] = {
            "pool_after": game_face.get("pool_after"),
            "n_tiers": len(tiers),
            "top_prize": float(top["prize_amount"]) if top else None,
            "top_stake_count": float(top["stake_count"]) if top else None,
        }
    return games


def run_s5(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """
    S5 滚存回看：已发布奖池结构期次落观测留痕（按缺席自补+近窗保鲜）。

    首跑=存量全量历史回放；此后每晚只补未观测期次 + 刷新近 7 日发布
    （官方 state 可后补更正）。滚存期 EV 结构标注=rollover_in 携带额与
    销量比（boost_ratio），池结构证据在 payload 不另算 EV（H7 已裁决）。
    """
    business_date = _validated_business_date(ctx.business_date)
    refresh_from = (
        date.fromisoformat(business_date) - timedelta(days=S5_REFRESH_DAYS)
    ).isoformat()
    # 读相一个 deferred 读事务：已观测集+期次面同快照（correctness F3）；
    # 计算纯函数化进 pending，读毕回滚再落观测
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        observed = {
            str(r[0])
            for r in conn.execute(
                "SELECT window_key FROM edge_observations WHERE scanner = 's5'"
            )
        }
        pending: list[tuple[str, dict[str, Any], bool, bool]] = []
        for row in pool_store.settled_state_rows(conn):
            market = str(row["market_code"])
            period_no = str(row["period_no"])
            key = f"{market}:{period_no}"
            published_day = str(row["published_at"] or "")[:10]
            # 已观测且明确非近窗才冻结；发布日未知（NULL published_at）
            # 恒保鲜——官方 state 可后补/更正，未知不得冒充古老（F2）
            if key in observed and published_day and published_day < refresh_from:
                continue
            games = _summarize_tiers(row["prize_tiers"])
            sales = row["sales_amount"]
            carry = float(row["rollover_in"] or 0.0)
            rolled_out = any(
                float(g.get("pool_after") or 0) > 0 for g in games.values()
            )
            pending.append(
                (
                    key,
                    {
                        "market": market,
                        "period_no": period_no,
                        "sales_deadline": row["sales_deadline"],
                        "published_at": row["published_at"],
                        "sales_amount": sales,
                        "rollover_in": row["rollover_in"],
                        "boost_ratio": (
                            round(carry / float(sales), 4) if sales else None
                        ),
                        "rollover_out": rolled_out,
                        "games": games,
                    },
                    carry > 0,
                    rolled_out,
                )
            )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    scanned = rollover_in = rollover_out = 0
    for key, payload, carries, rolled_out in pending:
        _record_observation(conn, "s5", key, payload)
        scanned += 1
        rollover_in += carries
        rollover_out += rolled_out
    conn.commit()
    return {
        "scanner": "s5",
        "periods_scanned": scanned,
        "rollover_in_periods": rollover_in,
        "rollover_out_periods": rollover_out,
    }


# ---- 注册表 + 日扫编排（wrap 挂步/CLI 共用） ----

_S1_SPEC = ScannerSpec(
    name="s1",
    cadence="daily",
    description="市场校准滚动窗（90 日，价格带×方向，二项 SE 越门）",
    run=run_s1,
)
_S4_SPEC = ScannerSpec(
    name="s4",
    cadence="daily",
    description="陈盘观测日（决策时点 confirmed/fired/命中计数留痕）",
    run=run_s4,
)
_S5_SPEC = ScannerSpec(
    name="s5",
    cadence="daily",
    description="池期次滚存回看（奖池结构/滚存标注，缺席自补）",
    run=run_s5,
)

SCANNERS: dict[str, ScannerSpec] = {
    spec.name: spec for spec in (_S1_SPEC, _S4_SPEC, _S5_SPEC)
}
#: 日扫定拍序（wrap 23:30 收尾后；S2/S3 点亮后追加）
ACTIVE_SCANNERS: tuple[str, ...] = ("s1", "s4", "s5")


def yesterday_business_date(now: datetime | None = None) -> str:
    """扫描日=北京业务日昨日（wrap 夜跑时昨日场全部已结算）。"""
    today = fx_store.beijing_business_date(now)
    return (date.fromisoformat(today) - timedelta(days=1)).isoformat()


def _validated_business_date(business_date: str) -> str:
    """
    观察日防御（correctness F5）。

    未来日不可有已结算面，笔误远未来窗会以空 payload 顶掉"最新窗"位
    （_latest_s1_face 按 window_key 排序，周报主位被空窗霸占 14 个月）。
    上限=北京今日（当日凌晨场可日间补扫）。
    """
    day = date.fromisoformat(business_date)  # 坏串 ValueError fail-loud
    if day > date.fromisoformat(fx_store.beijing_business_date()):
        msg = f"观察日 {business_date} 晚于北京今日（未来日无已结算面）"
        raise ValueError(msg)
    return business_date


def edge_scan_day(
    conn: sqlite3.Connection,
    *,
    business_date: str | None = None,
    duck_con: qe.DuckCon | None = None,
    resolve_sid: Callable[[int], str | None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    日扫编排：全部在册扫描器对同一观察日跑一轮（读写同连接）。

    扫描器间独立异常边界（m3/freshness_shadow 先例）：单扫描器炸只降级
    自身，其余照常留痕——"无信号日如实记零"的保证不因部分失败丢失。
    """
    ctx = ScanContext(
        business_date=business_date or yesterday_business_date(now),
        duck_con=duck_con,
        resolve_sid=resolve_sid,
    )
    stats: dict[str, Any] = {"business_date": ctx.business_date}
    for name in ACTIVE_SCANNERS:
        try:
            stats[name] = SCANNERS[name].run(conn, ctx)
        except Exception as exc:
            stats[name] = {"error": str(exc), "degraded": True}
    return stats


# ---- 周报 digest（票 04） ----


def _latest_s1_face(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """最新 s1 运行面观测（decade 验证面不进周报主位）。"""
    row = conn.execute(
        """
        SELECT payload FROM edge_observations
        WHERE scanner = 's1' AND window_key NOT LIKE 'decade:%'
        ORDER BY window_key DESC LIMIT 1
        """
    ).fetchone()
    return json.loads(str(row[0])) if row is not None else None


def build_edge_digest(
    conn: sqlite3.Connection,
    *,
    week_end: str,
    store: CorpusStore | None = None,
) -> dict[str, Any]:
    """
    周报：各扫描器窗口统计 + 本周新台账行 + 晋升建议（只读，两视图落盘）。

    晋升建议=本周仍是 new 且最新 s1 观测里仍越门的段（人裁开验证票，
    不自动晋升）。窗口过滤按台账 window 末日（s1 约定 "from..end"）。
    全读一个 deferred 读事务（price_edge 同款）：期次面/留痕/台账同
    快照——并发 pool-sync 落新期不让周报永久漏期（correctness F3）。
    """
    week_end = _validated_business_date(week_end)
    week_start = (date.fromisoformat(week_end) - timedelta(days=6)).isoformat()
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        report = _assemble_digest(conn, week_start=week_start, week_end=week_end)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    if store is not None:
        report["run_fingerprint"] = rfp.build_run_fingerprint(conn=conn, store=store)
        _write_two_views(store, report)
    return report


def _assemble_digest(
    conn: sqlite3.Connection, *, week_start: str, week_end: str
) -> dict[str, Any]:
    """周报读相装配（调用方负责读事务壳与落盘）。"""
    latest_s1 = _latest_s1_face(conn)
    latest_segments: dict[str, Any] = (latest_s1 or {}).get("segments") or {}
    crossed_now: set[str] = {
        key for key, seg in latest_segments.items() if seg.get("crossed")
    }
    findings_week = [
        {
            "scanner": str(r["scanner"]),
            "segment_key": str(r["segment_key"]),
            "metric_value": float(r["metric_value"]),
            "n": int(r["n"]),
            "se": float(r["se"]),
            "window": str(r["window"]),
            "status": str(r["status"]),
        }
        for r in conn.execute(
            """
            SELECT scanner, segment_key, metric_value, n, se, window, status
            FROM edge_findings
            WHERE substr(window, -10) BETWEEN ? AND ?
            ORDER BY found_at
            """,
            (week_start, week_end),
        )
    ]
    status_counts = {
        f"{r['scanner']}:{r['status']}": int(r["c"])
        for r in conn.execute(
            """
            SELECT scanner, status, COUNT(*) AS c FROM edge_findings
            GROUP BY scanner, status
            """
        )
    }
    s4_days = [
        {"day": str(r["window_key"]), **json.loads(str(r["payload"]))}
        for r in conn.execute(
            """
            SELECT window_key, payload FROM edge_observations
            WHERE scanner = 's4' AND window_key BETWEEN ? AND ?
            ORDER BY window_key
            """,
            (week_start, week_end),
        )
    ]
    s5_week_keys = [
        f"{r['market_code']!s}:{r['period_no']!s}"
        for r in pool_store.settled_state_rows(
            conn, published_from=week_start, published_to=week_end
        )
    ]
    s5_payload_by_key = {
        str(r["window_key"]): json.loads(str(r["payload"]))
        for r in conn.execute(
            "SELECT window_key, payload FROM edge_observations WHERE scanner = 's5'"
        )
    }
    s5_week = [
        s5_payload_by_key[key] for key in s5_week_keys if key in s5_payload_by_key
    ]
    n_states, n_rollover_in = pool_store.pool_state_counts(conn)
    s5_all_time = {"n_states": n_states, "n_rollover_in": n_rollover_in}
    suggestions = sorted(
        {
            f"{f['scanner']}:{f['segment_key']}"
            for f in findings_week
            if f["status"] == "new" and f["segment_key"] in crossed_now
        }
    )
    report: dict[str, Any] = {
        "schema": "edge-scan-digest/1",
        "generated_at": utc_now_iso(),
        "week": {"from": week_start, "to": week_end},
        "s1": {
            "latest_window": latest_s1.get("window") if latest_s1 else None,
            "coverage": latest_s1.get("coverage") if latest_s1 else None,
            "segments_crossed": sorted(crossed_now),
            "segments": latest_s1.get("segments") if latest_s1 else None,
        },
        "s4": {"days": s4_days},
        "s5": {"week_periods": len(s5_week), "week": s5_week, "all_time": s5_all_time},
        "findings": {
            "week": findings_week,
            "status_counts": status_counts,
            "promotion_suggestions": suggestions,
        },
        "notes": [
            "晋升=人裁开验证票（六步门），不自动建；dead 同理。",
            "S1 多重比较未校正（21 段×逐日；形式化随票 05）。",
            "S5 标注不另算 EV——池面 EV 结论已由 H7 裁决归档。",
        ],
    }
    return report


def _write_two_views(store: CorpusStore, report: dict[str, Any]) -> None:
    """报告落语料树 reports/（JSON+MD 两视图，temp+rename；同款先例×3）。"""
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


def _render_markdown(report: dict[str, Any]) -> str:
    """MD 视图（周报人读面；数字与 JSON 同源）。"""
    lines: list[str] = []
    w = lines.append
    w("# 探索轨周报（扫描器阵列 digest）")
    w("")
    w(
        f"- 周窗 {report['week']['from']} → {report['week']['to']}"
        + f"；生成 {report['generated_at']}"
    )
    s1 = report["s1"]
    w(
        f"- S1 最新窗 {s1['latest_window']}：越门段 "
        + (", ".join(s1["segments_crossed"]) or "无")
    )
    s4_days = report["s4"]["days"]
    totals = {
        k: sum(d.get(k, 0) for d in s4_days)
        for k in ("n_observed", "n_stale", "n_confirmed", "n_fired", "n_hit")
    }
    w(
        f"- S4 观测日 {len(s4_days)} 天：观察 {totals['n_observed']} 场"
        + f" / 陈盘 {totals['n_stale']} / 有分辨 {totals['n_confirmed']}"
        + f" / 触发 {totals['n_fired']} / 命中 {totals['n_hit']}"
    )
    s5 = report["s5"]
    w(
        f"- S5 本周兑付 {s5['week_periods']} 期（存量 {s5['all_time'].get('n_states')}"
        + f" 期，滚存携带 {s5['all_time'].get('n_rollover_in')} 期）"
    )
    w("")
    w("## S1 分桶表（最新窗）")
    w("")
    w("| 段 | n | mean_prob | actual_freq | dev | se | 越门 |")
    w("|---|---|---|---|---|---|---|")
    render_segments: dict[str, Any] = s1["segments"] or {}
    for key, seg in sorted(render_segments.items()):
        w(
            f"| {key} | {seg['n']} | {seg['mean_prob']} | {seg['actual_freq']}"
            + f" | {seg['dev']} | {seg['se']} | {'✓' if seg['crossed'] else ''} |"
        )
    w("")
    w("## 本周新台账行")
    w("")
    findings = report["findings"]["week"]
    if findings:
        w("| 扫描器 | 段 | dev | n | 窗 | 状态 |")
        w("|---|---|---|---|---|---|")
        for f in findings:
            w(
                f"| {f['scanner']} | {f['segment_key']} | {f['metric_value']}"
                + f" | {f['n']} | {f['window']} | {f['status']} |"
            )
    else:
        w("- 无新发现行")
    w("")
    suggestions = report["findings"]["promotion_suggestions"]
    w(
        "- 晋升建议："
        + (", ".join(suggestions) if suggestions else "无（本周无持续越门 new 段）")
    )
    for note in report["notes"]:
        w(f"- 注：{note}")
    return "\n".join(lines) + "\n"
