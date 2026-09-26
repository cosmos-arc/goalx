"""
CLV 跟踪与收盘对账（票 32 起底座；34 口径边界、40 基准分层、75 srct 锚接管）。

- CLV_proxy（概率域）= close_prob − 1/竞彩买入价；close 侧取 purpose=closing
  的 odds_api 快照（票 40 起三级取锚，基准来源随结果标注 close_basis）：
  1. 主锚 Pinnacle（sharp 定价者，closing 行业无偏，单独 Shin）；
  2. 辅锚 Betfair 交易所（back 价按佣金调整后归一化，佣金率参数化默认 2%）；
  3. fallback 多 book 完整三向共识 Shin（分层前唯一口径）。
  高 margin 书（如 1xBet/onexbet）只进共识、永不作锚（调研
  odds-consensus-methodology.md §5.2 建议 1）；
- 票 75 第四级（前向主通路）：eu-odds-closing 面 2026-09-25 判死后，
  odds_api 三级对新注单必然空手——srct:1x2:177（源T 百家行主锚，
  corpus.duckdb odds_change_event 全轨迹）接管收盘锚，basis=srct_pinnacle。
  场次匹配两步确定性键：竞彩中文队名+北京日期 → 兜底 kickoff 时刻精确+
  主队名精确（解命名变体）；两步不中即诚实无锚（CorpusScope 外结构性）。
  票 34 防前视边界的源语义适配：odds_api 行以 observed_at（本机观测）为
  资格线，srct 行以 published_at（源自报时点）为资格线——约束同为
  「信息时点严格早于开球」。
- 纳入边界（票 34/handoff）：closing 必须实际在该腿开赛前观测——
  有效观测时间（observed_at，旧行按源解释）> kickoff 的迟到快照一律排除；
- 口径（票 34）：
  - 单关与 2串1 分开、paper/live 分开报告，不混成一个通过数字；
  - 串关按票级联合概率（两腿 close_prob 连乘，标独立性假设），
    腿级 CLV 仅诊断；回归只用单关，不复制票级 profit 做独立样本；
  - 分母按冻结的决策身份去重（同 mode+同选项组合+同锁定赔率+同 placed_at
    的重试/拆分金额只计一次），原始 Bet 数另报；不用腿数凑 200 注分母。
- 口径切换（票 40）：分层只对新对账行生效，历史行 close_basis=NULL 不重算，
  报表以 by_close_basis 分列（legacy = 分层前共识口径），新旧并行呈现
  一个窗口期（建议至下一整轮销售周结束，待人追认）。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import duckdb
from loguru import logger

from goalx_backend import odds_math as om
from goalx_backend.betting import store as bt_store
from goalx_backend.betting.store import DecisionKey
from goalx_backend.config import get_settings
from goalx_backend.data import corpus_duckdb, quote_evidence
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

MINUTES_BUCKETS = ((0, 10), (10, 30), (30, 10**9))
CLOSE_LOOKBACK_MINUTES = 90  # closing 快照须落在开球前该窗口内
MIN_REGRESSION_SAMPLES = 3  # 回归最少样本

# --- 基准分层（票 40，调研 odds-consensus-methodology.md §5.2 建议 1） ---

PINNACLE_SOURCE = "odds_api:pinnacle"  # 主锚：sharp 书，closing 行业无偏
BETFAIR_EXCHANGE_SOURCES = (  # 辅锚：交易所（区域变体都认，实采 EU 区）
    "odds_api:betfair_ex_eu",
    "odds_api:betfair_ex_uk",
)
BETFAIR_COMMISSION = 0.02  # back 价佣金率（调研 2–5%，默认取下沿）

BASIS_PINNACLE = "pinnacle"
BASIS_BETFAIR = "betfair_ex"
BASIS_CONSENSUS = "consensus"
BASIS_SRCT_PINNACLE = "srct_pinnacle"  # 票 75：源T cid177 接管（odds_api 判死后前向）
BASIS_LEGACY = "legacy"  # close_basis IS NULL 的分层前历史行（不重算）
BASIS_MIXED = "mixed"  # 串关两腿基准不同（腿级见 clv_records.close_basis）

CLOSE_BASIS_NOTE = (
    "pinnacle 主锚 → betfair_ex 辅(back 价扣佣金, 默认 2%) → consensus fallback；"
    "srct_pinnacle = 源T cid177 收盘接管(odds_api 判死后前向主通路, 票 75)；"
    "legacy = 分层前共识口径行(历史不重算)；mixed = 串关跨基准"
)

DuckCon = duckdb.DuckDBPyConnection


@dataclass
class ReconcileStats:
    """一次对账的统计。"""

    recorded: int = 0
    skipped: list[str] = field(default_factory=list)


def _exchange_back_probs(
    odds: tuple[float, ...], commission: float
) -> tuple[float, ...]:
    """
    Betfair back 价 → 公允概率（票 40 辅锚）。

    交易所 back 价无 baked-in margin，摩擦是赢利侧佣金：按佣金调整
    有效赔率（赢时净收益 × (1−commission)），再归一化到和为 1。
    佣金率参数化（默认 2%，调研区间 2-5%）。
    """
    effective = tuple(1.0 + (odds_i - 1.0) * (1.0 - commission) for odds_i in odds)
    return om.normalized_implied(effective)


@contextmanager
def corpus_anchor() -> Generator[DuckCon | None]:
    """
    Srct 收盘锚连接的统一开关（票 75）；库缺席优雅降级 None。

    生产调用方（daily-wrap / clv-reconcile CLI）一律走本上下文管理器，
    打开与关闭不散落（评审修正：去两处 open/close 样板）。
    """
    try:
        con: DuckCon | None = corpus_duckdb.connect(get_settings())
    except (duckdb.Error, OSError) as exc:
        logger.warning("clv srct anchor unavailable, degraded: {}", exc)
        con = None
    try:
        yield con
    finally:
        if con is not None:
            con.close()


def srct_closing_prob(
    duck_con: DuckCon,
    home: str,
    away: str,
    kickoff_utc: str,
    selection: str,
) -> tuple[float, str, str] | None:
    """
    源T 主锚收盘概率（票 75 第四级，odds_api 判死后的前向主通路）。

    场次匹配与事件资格线（published_at ≤ kickoff）在 data 包语料桥
    `srct_pinnacle_closing_triplet`（ADR-0008：语料表查询归 data）；
    本层只做 Shin 去水与基准标注。匹配两步确定性键见该函数 docstring。
    """
    triplet = corpus_duckdb.srct_pinnacle_closing_triplet(
        duck_con, home, away, kickoff_utc
    )
    if triplet is None:
        return None
    probs = om.shin_implied(triplet)
    return probs[SELECTIONS.index(selection)], "srct_1x2_closing", BASIS_SRCT_PINNACLE


def _closing_prob(
    conn: sqlite3.Connection,
    fixture_id: int,
    selection: str,
    *,
    betfair_commission: float = BETFAIR_COMMISSION,
    duck_con: DuckCon | None = None,
) -> tuple[float, str, str] | None:
    """
    该场 had 选择的收盘公允概率 + 基准来源标注（票 40 三级 + 票 75 第四级）。

    返回 ``(prob, close_source, close_basis)``；三级为 Pinnacle 主锚（单独
    Shin）→ Betfair 交易所辅锚（back 价扣佣金归一化，佣金率参数化默认 2%，
    调研区间 2-5%）→ 多 book 完整三向共识 Shin fallback（分层前唯一口径）。
    高 margin 书只进共识不做基准。三级空手且 duck_con 传入时走 srct:1x2:177
    收盘接管（票 75；odds_api 判死后新注单的主通路）。

    有效快照 = 观测时间（observed_at，旧行按源解释为 captured_at）严格
    早于 kickoff 且在开球前 CLOSE_LOOKBACK_MINUTES 内——开赛后才查到的
    快照只能作历史研究，不能倒填前瞻 closing（票 34）。
    """
    fixture = fx_store.get_fixture(conn, fixture_id)
    if fixture is None:
        return None
    kickoff = str(fixture["kickoff_utc"])
    books = quote_evidence.books_complete_asof(
        conn,
        fixture_id,
        kickoff,
        purpose="closing",
        exclusive=True,
        max_age_seconds=CLOSE_LOOKBACK_MINUTES * 60,
    )
    if not books:
        if duck_con is None:
            return None
        info = fx_store.fixture_team_info(conn, fixture_id)  # 队名读取走 data 属主
        if info is None:
            return None
        return srct_closing_prob(
            duck_con, str(info["home_name"]), str(info["away_name"]), kickoff, selection
        )
    return _odds_api_close(books, selection, betfair_commission=betfair_commission)


def _odds_api_close(
    books: dict[str, dict[str, float]],
    selection: str,
    *,
    betfair_commission: float,
) -> tuple[float, str, str]:
    """odds_api 三级取锚（票 40）：Pinnacle → Betfair 交易所 → 多书共识。"""
    idx = SELECTIONS.index(selection)
    if PINNACLE_SOURCE in books:
        # 主锚：~2% margin 的 sharp closing，Shin 与归一化差异极小（调研），
        # 沿用共识同法 Shin 保持单一去水口径。
        probs = om.shin_implied(
            tuple(books[PINNACLE_SOURCE][sel] for sel in SELECTIONS)
        )
        return probs[idx], "odds_api_closing", BASIS_PINNACLE
    exchange = next(
        (src for src in BETFAIR_EXCHANGE_SOURCES if src in books),
        None,
    )
    if exchange is not None:
        probs = _exchange_back_probs(
            tuple(books[exchange][sel] for sel in SELECTIONS), betfair_commission
        )
        return probs[idx], "odds_api_closing", BASIS_BETFAIR
    consensus = tuple(
        sum(books[book][sel] for book in books) / len(books) for sel in SELECTIONS
    )
    probs = om.shin_implied(consensus)
    return probs[idx], "odds_api_closing", BASIS_CONSENSUS


def _minutes_to_kickoff(placed_at: str | None, kickoff_utc: str) -> float | None:
    """下注时距开赛分钟数（无 placed_at 返回 None）。"""
    if not placed_at:
        return None
    placed = datetime.fromisoformat(str(placed_at))
    kickoff = datetime.fromisoformat(kickoff_utc)
    return (kickoff - placed).total_seconds() / 60.0


def reconcile_clv(
    conn: sqlite3.Connection, *, duck_con: DuckCon | None = None
) -> ReconcileStats:
    """
    已结算注单自动对账（幂等：UNIQUE(bet_id, fixture_id) 吸收重跑）。

    只处理 had 腿（v1 可映射口径）；串关逐腿写记录，票级指标在报表层聚合。
    注单读取走 betting 共享口径（settled_purchased_bets）。duck_con 传入即
    启用 srct 收盘锚（票 75；生产调用方经 open_corpus_anchor 打开）。
    """
    stats = ReconcileStats()
    kickoffs: dict[int, str] = {}
    for bet in bt_store.settled_purchased_bets(conn):
        for leg in bet.legs:
            if leg.market_code != "had":
                continue
            fixture_id = leg.fixture_id
            kickoff = kickoffs.get(fixture_id)
            if kickoff is None:
                fixture = fx_store.get_fixture(conn, fixture_id)
                if fixture is None:
                    stats.skipped.append(f"fixture_missing:bet={bet.bet_id}")
                    continue
                kickoff = str(fixture["kickoff_utc"])
                kickoffs[fixture_id] = kickoff
            close = _closing_prob(
                conn, fixture_id, leg.selection_code, duck_con=duck_con
            )
            if close is None:
                stats.skipped.append(f"no_pre_kickoff_close:bet={bet.bet_id}")
                continue
            close_prob, close_source, close_basis = close
            taken_odds = leg.locked_odds
            clv = close_prob - 1.0 / taken_odds
            minutes = _minutes_to_kickoff(bet.placed_at, kickoff)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clv_records
                (bet_id, fixture_id, market_code, selection_code, taken_odds,
                 close_prob, clv_prob, close_source, close_basis,
                 minutes_to_kickoff, computed_at)
                VALUES (?, ?, 'had', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bet.bet_id,
                    fixture_id,
                    leg.selection_code,
                    taken_odds,
                    close_prob,
                    clv,
                    close_source,
                    close_basis,
                    minutes,
                    utc_now_iso(),
                ),
            )
            if cur.rowcount > 0:
                stats.recorded += 1
    conn.commit()
    return stats


def _bucket_minutes(minutes: float | None) -> str:
    """距开赛时间桶标签。"""
    if minutes is None:
        return "unknown"
    for low, high in MINUTES_BUCKETS:
        if low <= minutes < high:
            return f"[{low},{'inf' if high > 10**6 else high})min"
    return "unknown"


@dataclass(frozen=True)
class _BetView:
    """报表层的注级视图（决策身份去重后）。"""

    bet_id: int
    mode: str
    placed_at: str | None
    legs: tuple[
        tuple[int, str, float, float, float, str], ...
    ]  # (fixture, sel, taken, close_p, clv, basis)
    profit: float | None
    minutes_to_kickoff: float | None
    decision: DecisionKey  # betting 共享公式冻结的决策身份（票 34）

    @property
    def kind(self) -> str:
        return "single" if len(self.legs) == 1 else "parlay2"

    @property
    def basis(self) -> str:
        """票级基准来源：全腿同基准即该级，否则 mixed（票 40）。"""
        bases = {leg[5] for leg in self.legs}
        return next(iter(bases)) if len(bases) == 1 else BASIS_MIXED

    @property
    def clv_ticket(self) -> float:
        """票级 CLV：单关即腿值；串关为联合概率 − 联合隐含。"""
        joint_close = 1.0
        joint_implied = 1.0
        for _, _, taken, close_p, _, _ in self.legs:
            joint_close *= close_p
            joint_implied *= 1.0 / taken
        return joint_close - joint_implied


def _close_records(conn: sqlite3.Connection) -> dict[tuple[int, int], sqlite3.Row]:
    """clv_records 全量，按 (bet_id, fixture_id) 索引（本表归本模块）。"""
    rows = conn.execute(
        """
        SELECT bet_id, fixture_id, close_prob, clv_prob, close_basis,
               minutes_to_kickoff
        FROM clv_records
        """
    ).fetchall()
    return {(int(r["bet_id"]), int(r["fixture_id"])): r for r in rows}


def closing_leg_counts(conn: sqlite3.Connection) -> dict[int, int]:
    """
    每注已有的 closing 记录腿数（复盘页缺 closing 标记，票 36）。

    只有 reconcile_clv 跑过之后才有数据；缺失即诚实显示「缺 closing」。
    """
    rows = conn.execute("SELECT bet_id, COUNT(*) AS n FROM clv_records GROUP BY bet_id")
    return {int(r["bet_id"]): int(r["n"]) for r in rows}


def _record_basis(record: sqlite3.Row) -> str:
    """行的基准来源：NULL = 分层前历史行 → legacy（不重算，票 40）。"""
    basis = record["close_basis"]
    return str(basis) if basis is not None else BASIS_LEGACY


def _collect_bets(conn: sqlite3.Connection) -> tuple[list[_BetView], dict[str, int]]:
    """
    已结算已购注 → 去重前的注级视图 + 分母统计（读取走 betting 共享口径）。

    只收全部腿均为 had 且腿数为 1（单关）或 2（2串1）的注——
    含非 had 腿或多腿串关注单整注排除并计数（v1 可映射口径之外）。
    """
    records = _close_records(conn)
    stats = {
        "settled_bets": 0,
        "legs": 0,
        "no_close_bets": 0,
        "unsupported_bets": 0,
    }
    views: list[_BetView] = []
    for bet in bt_store.settled_purchased_bets(conn):
        stats["settled_bets"] += 1
        stats["legs"] += len(bet.legs)
        supported = all(leg.market_code == "had" for leg in bet.legs)
        legs: list[tuple[int, str, float, float, float, str]] = []
        has_close = True
        for leg in bet.legs:
            record = records.get((bet.bet_id, leg.fixture_id))
            if record is None or record["close_prob"] is None:
                has_close = False
                continue
            legs.append(
                (
                    leg.fixture_id,
                    leg.selection_code,
                    leg.locked_odds,
                    float(record["close_prob"]),
                    float(record["clv_prob"]),
                    _record_basis(record),
                )
            )
        if not (supported and len(bet.legs) in (1, 2)):
            stats["unsupported_bets"] += 1
            continue
        if not has_close or not legs:
            stats["no_close_bets"] += 1
            continue
        first = records[(bet.bet_id, bet.legs[0].fixture_id)]
        minutes = first["minutes_to_kickoff"]
        views.append(
            _BetView(
                bet_id=bet.bet_id,
                mode=bet.mode,
                placed_at=bet.placed_at,
                legs=tuple(legs),
                profit=bet.profit,
                minutes_to_kickoff=(float(minutes) if minutes is not None else None),
                decision=bet.decision_key,
            )
        )
    return views, stats


def _dedup_by_decision(
    views: list[_BetView],
) -> tuple[list[_BetView], int]:
    """按决策身份去重（重试/拆分金额只计一次）；返回 (唯一注, 重复数)。"""
    seen: dict[DecisionKey, _BetView] = {}
    duplicates = 0
    for view in views:
        if view.decision in seen:
            duplicates += 1
        else:
            seen[view.decision] = view
    return sorted(seen.values(), key=lambda v: v.bet_id), duplicates


def _group_stats(group: list[_BetView]) -> dict[str, Any]:
    """一组的 beat/CLV 汇总（票级口径）。"""
    if not group:
        return {"n_bets": 0, "beat_rate": None, "avg_clv": None}
    clvs = [v.clv_ticket for v in group]
    beats = sum(1 for c in clvs if c > 0)
    return {
        "n_bets": len(group),
        "beat_rate": beats / len(clvs),
        "avg_clv": sum(clvs) / len(clvs),
    }


def _basis_breakdown(
    unique: list[_BetView], records: dict[tuple[int, int], sqlite3.Row]
) -> dict[str, Any]:
    """
    基准来源分层报表（票 40 窗口期新旧口径并行呈现）。

    每级给腿数（clv_records 计数）、全腿同级的唯一注数与该级内的
    单关/2串1 × paper/live 分组（与主口径同构，空组省略）；串关跨基准
    计 mixed 只计注数。legacy = 分层前共识口径行，历史不重算。
    """
    leg_counts: dict[str, int] = {}
    for record in records.values():
        basis = _record_basis(record)
        leg_counts[basis] = leg_counts.get(basis, 0) + 1
    out: dict[str, Any] = {}
    for basis in (
        BASIS_PINNACLE,
        BASIS_BETFAIR,
        BASIS_SRCT_PINNACLE,
        BASIS_CONSENSUS,
        BASIS_LEGACY,
    ):
        bets = [v for v in unique if v.basis == basis]
        if not bets and leg_counts.get(basis, 0) == 0:
            continue
        sections: dict[str, dict[str, Any]] = {}
        for kind in ("single", "parlay2"):
            per_kind: dict[str, Any] = {}
            for mode in ("paper", "live"):
                stats = _group_stats(
                    [v for v in bets if v.kind == kind and v.mode == mode]
                )
                if stats["n_bets"]:
                    per_kind[mode] = stats
            if per_kind:
                sections[kind] = per_kind
        out[basis] = {
            "legs": leg_counts.get(basis, 0),
            "bets": len(bets),
            "groups": sections,
        }
    mixed = [v for v in unique if v.basis == BASIS_MIXED]
    if mixed:
        out[BASIS_MIXED] = {
            "bets": len(mixed),
            "note": "串关两腿基准不同，腿级见 clv_records.close_basis",
        }
    return out


def clv_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    票级 CLV 报表：单关/2串1 × paper/live 分组 + 去重分母 + 单关回归。

    beat 定义：票级 clv > 0（串关按联合概率，独立性假设已声明）。
    主口径分组不分基准（legacy 与分层后行并列计入）；票 40 起另以
    by_close_basis 分列新旧口径供窗口期并行判读。
    """
    views, denom = _collect_bets(conn)
    unique, duplicates = _dedup_by_decision(views)
    denom |= {
        "raw_bets": len(views) + duplicates,
        "reconciled_bets": len(views),
        "unique_bets": len(unique),
        "deduped_duplicates": duplicates,
        "fixtures": len({fx for v in unique for fx, *_ in v.legs}),
    }
    groups: dict[str, dict[str, dict[str, Any]]] = {"single": {}, "parlay2": {}}
    for kind, sections in groups.items():
        for mode in ("paper", "live"):
            sections[mode] = _group_stats(
                [v for v in unique if v.kind == kind and v.mode == mode]
            )
    buckets: dict[str, dict[str, float]] = {}
    for view in (v for v in unique if v.kind == "single"):
        key = _bucket_minutes(view.minutes_to_kickoff)
        entry = buckets.setdefault(key, {"n": 0, "beats": 0})
        entry["n"] += 1
        entry["beats"] += 1 if view.clv_ticket > 0 else 0
    singles = [
        (v.clv_ticket, v.profit)
        for v in unique
        if v.kind == "single" and v.profit is not None
    ]
    slope, r_squared = _ols_slope(
        [clv for clv, _ in singles], [float(profit) for _, profit in singles]
    )
    return {
        "singles": groups["single"],
        "parlay2": groups["parlay2"],
        # 串关票级联合概率按两腿独立连乘（票 34：声明假设，不作回归样本）
        "independence_assumed": True,
        # 基准分层标注（票 40）：窗口期新旧口径并行判读用
        "close_basis_note": CLOSE_BASIS_NOTE,
        "by_close_basis": _basis_breakdown(unique, _close_records(conn)),
        "denominator": denom,
        "by_minutes_bucket_single": {
            key: {"n": int(v["n"]), "beat_rate": v["beats"] / v["n"]}
            for key, v in sorted(buckets.items())
            if v["n"]
        },
        "regression": {
            "n": len(singles),
            "slope": slope,
            "r_squared": r_squared,
            "note": "singles only",
        },
    }


def _ols_slope(xs: list[float], ys: list[float]) -> tuple[float | None, float | None]:
    """一元 OLS 斜率与 R²（样本不足返回 None）。"""
    n = len(xs)
    if n < MIN_REGRESSION_SAMPLES:
        return None, None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None, None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx
    syy = sum((y - mean_y) ** 2 for y in ys)
    r_squared = (sxy**2 / (sxx * syy)) if syy > 0 else None
    return slope, r_squared


def clv_json(conn: sqlite3.Connection) -> str:
    """报表 JSON 序列化（API/CLI 展示用）。"""
    return json.dumps(clv_report(conn), ensure_ascii=False, indent=2)
