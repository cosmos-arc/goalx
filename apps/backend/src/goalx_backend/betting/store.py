"""投注域仓储：Bet/BetLeg/BetSlip/Combination、结算落库与 Bankroll 事件。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import combinations, product
from typing import Any

from goalx_backend.betting.snapshot import EMPTY_SNAPSHOT, BetEvSnapshot
from goalx_backend.data import fixtures as fx
from goalx_backend.data.pool import pool_matches_for_period
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.markets import GOALS4_CODES, POOL_HAD_CODES
from goalx_backend.models import (
    BetMode,
    LegInput,
    MarketKind,
    SettlementInput,
)

# 复式组合物化上限（票 07④ 人裁决②：10 万量级；14 场×3 向=478 万曾可
# 整展开）。超限拒绝发生在任何写入之前。
MAX_MATERIALIZED_COMBINATIONS = 100_000

# 任9 固定腿数（票 01 口径单源：子集展开与玩法校验共用；决策侧
# pool_strategy 同引用）。
PICK9_LEGS = 9
# goals4 固定场次数（票 10：四场进球，每场主/客档各成一选）。
GOALS4_MATCHES = 4
# ttt14 固定场次数（票 08：14 场完备才可创建；预览允许未选完整）。
TTT14_LEGS = 14


class CombinationLimitExceeded(ValueError):
    """复式组合数超上限（票 07④）：API 层据此精确映射 422。"""


@dataclass(frozen=True)
class LegSummary:
    """已结注的一腿投影（决策身份与报表用）。"""

    fixture_id: int
    market_code: str
    selection_code: str
    locked_odds: float


DecisionKey = tuple[str, tuple[tuple[int, str, str, float], ...], str | None]


@dataclass(frozen=True)
class SettledBet:
    """一注已结算已购注的领域视图（腿内联，票 34 验证分母的读取口径）。"""

    bet_id: int
    mode: str
    status: str
    market_kind: str
    placed_at: str | None
    settled_at: str | None
    created_at: str | None
    stake: float
    profit: float | None
    legs: tuple[LegSummary, ...]

    @property
    def decision_key(self) -> DecisionKey:
        """该注的决策身份（全仓唯一公式；CLV 与验证看板共用，票 34）。"""
        return decision_key(self.mode, self.legs, self.placed_at)


def decision_key(
    mode: str, legs: tuple[LegSummary, ...], placed_at: str | None
) -> DecisionKey:
    """
    冻结的决策身份：mode + 选项组合与锁定赔率 + 锁定时点（不含金额）。

    同身份的重试/拆分金额注在验证分母里只计一次（重试是同一决策）。
    """
    combo = tuple(
        sorted(
            (leg.fixture_id, leg.market_code, leg.selection_code, leg.locked_odds)
            for leg in legs
        )
    )
    return (mode, combo, placed_at)


def paper_has_actual_terms(bet: sqlite3.Row) -> bool:
    """
    Paper 行是否携带实际条款（票 01：修复前异常旧行判据）。

    actuals 写入已对 paper 关闭（record_purchase 领域拒绝），存量携带者=
    异常旧行：结算/正式分母按绑定价忽略 actuals。审计（ledger_audit）、
    更正通道（settle）与复盘资格（bet_review）共用本判据，行须为
    list_bets/get_bet 口径（含 actual_stake 列与带 actual_odds 的 legs JSON）；
    旧库缺列/缺键按无 actuals（.get 回退）。
    """
    return str(bet["mode"]) == "paper" and (
        dict(bet).get("actual_stake") is not None
        or any(leg.get("actual_odds") is not None for leg in json.loads(bet["legs"]))
    )


def settled_purchased_bets(conn: sqlite3.Connection) -> list[SettledBet]:
    """
    全部已结算已购注（腿内联，按 settled_at,id 稳定排序）。

    正式纸面成绩面闸门（boundary-hardening 票 04 范围④）：manual_research
    纸面注排除（验证/CLV 共用本分母）；历史 NULL 不回溯排除（裁决③）。
    live 注不受影响。票 01：paper 行携带 actual 条款（修复前异常旧行）
    一并排除——正式成绩价链只认服务器绑定价，异常行见 audit-ledger。
    """
    rows = conn.execute(
        """
        SELECT b.id AS bet_id, b.mode, b.status, b.market_kind, b.placed_at,
               b.settled_at, b.created_at,
               COALESCE(b.actual_stake, b.stake) AS stake, b.profit,
               l.fixture_id, l.market_code AS leg_market, l.selection_code,
               COALESCE(l.actual_odds, l.locked_odds) AS locked_odds
        FROM bets b
        JOIN bet_legs l ON l.bet_id = b.id
        WHERE b.status IN ('won', 'lost', 'void') AND b.purchased = 1
          AND NOT (b.mode = 'paper' AND b.price_source IS 'manual_research')
          AND NOT (b.mode = 'paper' AND (
                b.actual_stake IS NOT NULL
                OR EXISTS (SELECT 1 FROM bet_legs la
                           WHERE la.bet_id = b.id AND la.actual_odds IS NOT NULL)))
        ORDER BY b.settled_at, b.id, l.id
        """
    ).fetchall()
    by_bet: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = by_bet.setdefault(int(row["bet_id"]), {"row": row, "legs": []})
        entry["legs"].append(
            LegSummary(
                fixture_id=int(row["fixture_id"]),
                market_code=str(row["leg_market"]),
                selection_code=str(row["selection_code"]),
                locked_odds=float(row["locked_odds"]),
            )
        )
    bets = [
        SettledBet(
            bet_id=int(entry["row"]["bet_id"]),
            mode=str(entry["row"]["mode"]),
            status=str(entry["row"]["status"]),
            market_kind=str(entry["row"]["market_kind"]),
            placed_at=(
                str(entry["row"]["placed_at"])
                if entry["row"]["placed_at"] is not None
                else None
            ),
            settled_at=(
                str(entry["row"]["settled_at"])
                if entry["row"]["settled_at"] is not None
                else None
            ),
            created_at=(
                str(entry["row"]["created_at"])
                if entry["row"]["created_at"] is not None
                else None
            ),
            stake=float(entry["row"]["stake"]),
            profit=(
                float(entry["row"]["profit"])
                if entry["row"]["profit"] is not None
                else None
            ),
            legs=tuple(entry["legs"]),
        )
        for entry in by_bet.values()
    ]
    return sorted(bets, key=lambda b: (b.settled_at or "", b.bet_id))


def bets_total(conn: sqlite3.Connection) -> int:
    """累计注数（晨检观察项）。"""
    return int(conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0])


def count_unpurchased_open(conn: sqlite3.Connection) -> int:
    """未回录且未结算的固定注数（验证页「待回录」计数）。"""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM bets WHERE purchased = 0
              AND status = 'open' AND market_kind = 'fixed'
            """
        ).fetchone()[0]
    )


def dedupe_by_decision(bets: list[SettledBet]) -> tuple[list[SettledBet], int]:
    """按决策身份去重（重试/拆分金额只计一次）；返回 (唯一注, 重复数)。"""
    seen: dict[DecisionKey, SettledBet] = {}
    duplicates = 0
    for bet in bets:
        key = bet.decision_key
        if key in seen:
            duplicates += 1
        else:
            seen[key] = bet
    return sorted(
        seen.values(), key=lambda b: (b.settled_at or "", b.bet_id)
    ), duplicates


def _insert_id(cur: sqlite3.Cursor) -> int:
    """Read the rowid of a plain insert; raises RuntimeError when absent."""
    if not cur.lastrowid:
        raise RuntimeError("INSERT 未产生 rowid")
    return int(cur.lastrowid)


def create_bet(  # noqa: PLR0913 - 仓储原始层, 参数即列
    conn: sqlite3.Connection,
    mode: BetMode,
    market_kind: MarketKind,
    stake: float,
    *,
    purchased: bool = False,
    placed_at: str | None = None,
    slip_id: int | None = None,
    strategy_version: str | None = None,
    ev_snapshot: BetEvSnapshot | None = None,
    price_source: str | None = None,
    client_request_id: str | None = None,
    intent_fingerprint: str | None = None,
) -> int:
    """
    Create a bet record (建议未购或已回录；含建注 EV 快照，票 41)；返回 id。

    price_source=价格来源三分类（票 04，v35 列）；None=不落标签（旧行为）。
    client_request_id=客户端幂等键（票 06，v37 列）：撞唯一索引抛
    IntegrityError——调用侧捕获后以 bet_id_for_request 取回既有注；
    None=未带键（旧行为，池票/内部调用不受约束）。
    intent_fingerprint=提交意图指纹（票 09，v41 列）：建注时一次写入，
    锁票重绑不改值；None=未带键（不受约束）。
    """
    snap = ev_snapshot or EMPTY_SNAPSHOT
    cur = conn.execute(
        """
            INSERT INTO bets
            (slip_id, mode, market_kind, purchased, stake, placed_at,
            created_at, strategy_version,
            snap_prob_consensus, snap_prob_model, snap_ev_consensus, snap_ev_model,
            price_source, client_request_id, intent_fingerprint)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slip_id,
            mode.value,
            market_kind.value,
            int(purchased),
            stake,
            placed_at,
            utc_now_iso(),
            strategy_version,
            snap.prob_consensus,
            snap.prob_model,
            snap.ev_consensus,
            snap.ev_model,
            price_source,
            client_request_id,
            intent_fingerprint,
        ),
    )
    return _insert_id(cur)


def bet_id_for_request(conn: sqlite3.Connection, client_request_id: str) -> int | None:
    """同幂等键的既有注 id（无则 None）——重复提交去重回带。"""
    row = conn.execute(
        "SELECT id FROM bets WHERE client_request_id = ?", (client_request_id,)
    ).fetchone()
    return int(row["id"]) if row is not None else None


def update_leg_price_binding(
    conn: sqlite3.Connection,
    bet_id: int,
    fixture_id: int,
    locked_odds: float,
    snapshot_id: int | None,
) -> None:
    """
    锁定时价重绑（票 04）：locked_odds 刷新为服务器决策价+快照身份。

    仅 system_paper 纸面注的确认路径调用（同票事务内）；(bet, fixture)
    唯一——一注内 fixture 不重复由建注串关校验保证。
    """
    conn.execute(
        """
        UPDATE bet_legs SET locked_odds = ?, snapshot_id = ?
        WHERE bet_id = ? AND fixture_id = ?
        """,
        (locked_odds, snapshot_id, bet_id, fixture_id),
    )


def legs_for_bet(conn: sqlite3.Connection, bet_id: int) -> list[sqlite3.Row]:
    """一注的腿终态（票 09 confirm 阶段读源：bet_legs 当前值，按插入序）。"""
    return conn.execute(
        """
        SELECT fixture_id, selection_code, locked_odds, snapshot_id
        FROM bet_legs WHERE bet_id = ?
        ORDER BY id
        """,
        (bet_id,),
    ).fetchall()


def record_price_stage(  # noqa: PLR0913 - 仓储原始层, 参数即列
    conn: sqlite3.Connection,
    bet_id: int,
    fixture_id: int,
    selection_code: str,
    stage: str,
    *,
    odds: float | None,
    snapshot_id: int | None,
    recorded_at: str,
) -> None:
    """
    落一行五价时点（票 09）：odds NULL=事件发生但价缺（诚实记缺）。

    UNIQUE(bet, fixture, stage) INSERT OR IGNORE——重跑/幂等重放吸收，
    不产生重复时间线（首行即事件首发生时刻，重放不覆盖）。
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO bet_price_timeline
            (bet_id, fixture_id, selection_code, stage, odds, snapshot_id,
             recorded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (bet_id, fixture_id, selection_code, stage, odds, snapshot_id, recorded_at),
    )


def official_paper_decision_legs(
    conn: sqlite3.Connection, date_from: str, date_to: str
) -> list[sqlite3.Row]:
    """
    正式纸面决策的腿级读数面（票 09 读数脚本的 betting 共享口径）。

    分母=预注册 §2：paper 已锁（purchased）非 manual_research（历史 NULL
    不排除），以 confirm 阶段行的 recorded_at 落窗（无时间线的存量注天然
    不进）。discovery/confirm 两阶段价 LEFT JOIN 带出；decision/ticket 由
    调用方按需另查（存活与 live 面协报告）。
    """
    return conn.execute(
        """
        SELECT b.id AS bet_id, b.price_source, b.placed_at, b.created_at,
               l.fixture_id, l.market_code, l.selection_code,
               l.locked_odds, l.snapshot_id AS leg_snapshot_id,
               d.recorded_at AS discovery_at, d.odds AS discovery_odds,
               d.snapshot_id AS discovery_snapshot_id,
               c.recorded_at AS confirm_at, c.odds AS confirm_odds,
               c.snapshot_id AS confirm_snapshot_id
        FROM bets b
        JOIN bet_legs l ON l.bet_id = b.id
        LEFT JOIN bet_price_timeline d
            ON d.bet_id = b.id AND d.fixture_id = l.fixture_id
            AND d.selection_code = l.selection_code AND d.stage = 'discovery'
        LEFT JOIN bet_price_timeline c
            ON c.bet_id = b.id AND c.fixture_id = l.fixture_id
            AND c.selection_code = l.selection_code AND c.stage = 'confirm'
        WHERE b.mode = 'paper' AND b.purchased = 1
          AND b.price_source IS NOT 'manual_research'
          AND c.recorded_at IS NOT NULL
          AND substr(c.recorded_at, 1, 10) BETWEEN ? AND ?
        ORDER BY c.recorded_at, b.id, l.id
        """,
        (date_from, date_to),
    ).fetchall()


def add_leg(conn: sqlite3.Connection, bet_id: int, leg: LegInput) -> int:
    """Append one leg to a bet (锁定下注时赔率与让球线)。"""
    meta = (
        json.dumps({"goal_line": leg.goal_line}) if leg.goal_line is not None else None
    )
    cur = conn.execute(
        """
            INSERT INTO bet_legs
(bet_id, fixture_id, market_code, selection_code,
            locked_odds, snapshot_id, meta)
VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bet_id,
            leg.fixture_id,
            leg.market_code,
            leg.selection_code,
            leg.locked_odds,
            leg.snapshot_id,
            meta,
        ),
    )
    return _insert_id(cur)


def list_bets(
    conn: sqlite3.Connection, *, mode: BetMode | None = None, only_open: bool = False
) -> list[sqlite3.Row]:
    """List bets with their legs aggregated as JSON (复盘列表; 含实际条款列)。"""
    sql = """
            SELECT b.id, b.slip_id, b.mode, b.market_kind, b.purchased, b.stake,
            b.actual_stake, b.strategy_version, b.price_source,
            b.client_request_id,
            b.placed_at, b.created_at, b.status, b.payout, b.profit, b.settled_at,
            b.snap_prob_consensus, b.snap_prob_model,
            b.snap_ev_consensus, b.snap_ev_model,
            s.created_at AS locked_at,
            (SELECT COUNT(*) FROM bet_legs l WHERE l.bet_id = b.id) AS leg_count,
(SELECT
            json_group_array(json_object(
'fixture_id', l.fixture_id, 'market_code',
            l.market_code,
'selection_code', l.selection_code, 'locked_odds',
            l.locked_odds, 'actual_odds', l.actual_odds,
'snapshot_id', l.snapshot_id,
'goal_line', json_extract(l.meta, '$.goal_line')))
FROM
            bet_legs l WHERE l.bet_id = b.id) AS legs
FROM bets b LEFT JOIN bet_slips s ON s.id = b.slip_id WHERE 1=1
        """
    params: list[Any] = []
    if mode is not None:
        sql += " AND b.mode = ?"
        params.append(mode.value)
    if only_open:
        sql += " AND b.status = 'open'"
    sql += " ORDER BY b.created_at DESC, b.id DESC"
    return conn.execute(sql, params).fetchall()


def get_bet(conn: sqlite3.Connection, bet_id: int) -> sqlite3.Row | None:
    """Fetch one bet with aggregated legs (含实际条款与服务器锁定时点)。"""
    return conn.execute(
        """
            SELECT b.*, s.created_at AS locked_at,
(SELECT json_group_array(json_object(
'fixture_id', l.fixture_id,
            'market_code', l.market_code,
'selection_code', l.selection_code,
            'locked_odds', l.locked_odds, 'actual_odds', l.actual_odds,
'snapshot_id', l.snapshot_id,
'goal_line', json_extract(l.meta,
            '$.goal_line')))
FROM bet_legs l WHERE l.bet_id = b.id) AS legs
FROM bets b LEFT JOIN bet_slips s ON s.id = b.slip_id
            WHERE b.id = ?
        """,
        (bet_id,),
    ).fetchone()


def create_slip(
    conn: sqlite3.Connection,
    mode: BetMode,
    *,
    pool_period_id: int | None = None,
    placed_at: str | None = None,
    note: str | None = None,
    source: str = "manual",
) -> int:
    """Create a bet slip (一张实际投注票)；返回 id。"""
    cur = conn.execute(
        """
            INSERT INTO bet_slips
(mode, source, pool_period_id, placed_at, note,
            created_at)
VALUES (?, ?, ?, ?, ?, ?)
        """,
        (mode.value, source, pool_period_id, placed_at, note, utc_now_iso()),
    )
    return _insert_id(cur)


def attach_bets_to_slip(
    conn: sqlite3.Connection, slip_id: int, bet_ids: list[int], placed_at: str
) -> int:
    """票级回录：把勾选的建议注挂到一张票并标记已购；返回注数。"""
    for bet_id in bet_ids:
        conn.execute(
            "UPDATE bets SET slip_id = ?, purchased = 1, placed_at = ? WHERE id = ?",
            (slip_id, placed_at, bet_id),
        )
    return len(bet_ids)


def set_actual_terms(
    conn: sqlite3.Connection,
    bet_id: int,
    stake: float,
    odds_by_fixture: dict[int, float],
) -> None:
    """记录实际执行条款（真实回录）；不覆盖建议快照，结算读实际值。"""
    conn.execute("UPDATE bets SET actual_stake = ? WHERE id = ?", (stake, bet_id))
    for fixture_id, odds in odds_by_fixture.items():
        conn.execute(
            "UPDATE bet_legs SET actual_odds = ? WHERE bet_id = ? AND fixture_id = ?",
            (odds, bet_id, fixture_id),
        )


def add_pool_pick(
    conn: sqlite3.Connection,
    slip_id: int,
    match_seq: int,
    selection_code: str,
    *,
    fixture_id: int | None = None,
) -> int:
    """Add one multi-select pick to a pool slip (复式票的一格多选之一)。"""
    cur = conn.execute(
        """
            INSERT INTO pool_picks (slip_id, match_seq, fixture_id, selection_code)
            VALUES (?, ?, ?, ?)
ON CONFLICT(slip_id, match_seq, selection_code) DO
            NOTHING
        """,
        (slip_id, match_seq, fixture_id, selection_code),
    )
    return _insert_id(cur)


def pool_pick_matrix(
    pick_rows: Iterable[tuple[int, str]],
) -> dict[int, list[str]]:
    """
    (match_seq, selection_code) 行 → 逐场选择矩阵（去重+排序，票 01 单源）。

    创建（落库后回读）与预览（载荷直入）共用，保证同输入同展开序。
    """
    seen: dict[int, set[str]] = {}
    for seq, code in pick_rows:
        seen.setdefault(int(seq), set()).add(str(code))
    return {seq: sorted(seen[seq]) for seq in sorted(seen)}


def _validate_market_code(market_code: str) -> None:
    """票级 Market 字面量守卫：未知 Market fail-closed，不静默落到连乘口径。"""
    if market_code not in ("pick9", "ttt14", "goals4"):
        raise ValueError(f"未知池票 Market {market_code!r}（可选 pick9/ttt14/goals4）")


def validate_pool_selection_codes(
    by_match: dict[int, list[str]], market_code: str
) -> None:
    """
    选择码闭集守卫（报告审计票 08）。

    预览/创建共用——非法码 4xx 拒绝，不静默物化死码（goals4 闭集校验
    此前只在完备路径生效，预览会吞掉）。
    """
    _validate_market_code(market_code)
    closed = GOALS4_CODES if market_code == "goals4" else POOL_HAD_CODES
    for seq in sorted(by_match):
        bad = sorted(pick for pick in by_match[seq] if pick not in closed)
        if bad:
            allowed = "/".join(sorted(closed))
            msg = f"{market_code} 选择码须为闭集内池码（{allowed}）: 场 {seq} 含 {bad}"
            raise ValueError(msg)


def require_pool_slip_creatable(
    by_match: dict[int, list[str]], market_code: str
) -> None:
    """
    创建完备性守卫（票 08）。

    ttt14 需 14 场有效选择（此前 1 场也落通用连乘建票）；pick9 ≥9 场 /
    goals4 双侧齐由 count_pool_combinations 拒。选择码闭集一并复检
    （预览/创建同校验）。
    """
    validate_pool_selection_codes(by_match, market_code)
    if market_code == "ttt14" and len(by_match) != TTT14_LEGS:
        raise ValueError(f"ttt14 需 {TTT14_LEGS} 场有效选择（当前 {len(by_match)} 场）")


def validate_pool_period_ownership(
    conn: sqlite3.Connection,
    by_match: dict[int, list[str]],
    pool_period_id: int,
) -> None:
    """
    期次归属守卫（票 08）。

    已指定期次的池票：每个 match_seq 都须是该期场次——跨期场次（或
    期次无场次）不落库。预览载荷无期次身份，只在创建侧校验。
    """
    period_seqs = {
        int(row["match_seq"]) for row in pool_matches_for_period(conn, pool_period_id)
    }
    foreign = sorted(set(by_match) - period_seqs)
    if foreign:
        raise ValueError(
            f"场次 {foreign} 不属于期次 {pool_period_id}（已指定期次须用该期场次）"
        )


def require_fixtures_exist(
    conn: sqlite3.Connection, fixture_ids: list[int | None]
) -> None:
    """
    池票 pick 的 fixture 存在性守卫（票 08 预存注记收口）。

    fixture_id 可空（期次场次未链映射时带 null）；带值时须真实存在——
    否则 add_pool_pick 撞 FK 裸 500。预览/创建同校验（票 08 模式）。
    """
    wanted = sorted({fid for fid in fixture_ids if fid is not None})
    if not wanted:
        return
    # 存在性探测走 data 包函数（ADR-0008：包外不裸写 fixtures SQL）
    found = fx.kickoffs_for_fixtures(conn, wanted)
    missing = [fid for fid in wanted if fid not in found]
    if missing:
        raise ValueError(f"fixture {missing} 不存在（pick 的 fixture_id 须为真实场次）")


def _goals4_sides(picks: list[str]) -> tuple[list[str], list[str]]:
    """
    goals4 一场的双侧档位（票 10）：返回 (主档升序, 客档升序)。

    非法码（闭集 h0..h3/a0..a3 外）或单侧缺选 raise——组合无法成形，
    fail-loud（死档不落库：永不命中的选择码只会物化废票）。
    """
    home = sorted(p for p in picks if p.startswith("h"))
    away = sorted(p for p in picks if p.startswith("a"))
    other = [p for p in picks if p not in GOALS4_CODES]
    if other:
        # 闭集校验（correctness W3）：仅前缀校验会放进 h9/ax 之类死档
        raise ValueError(f"goals4 选择码须为档位码（h0..h3/a0..a3）: {other}")
    if not home or not away:
        raise ValueError("goals4 每场须主/客档各至少一选")
    return home, away


def goals4_selections_complete(by_match: dict[int, list[str]]) -> bool:
    """goals4 预览完备探针（票 10）：4 场且每场双侧齐、无非法码。"""
    if len(by_match) != GOALS4_MATCHES:
        return False
    try:
        for picks in by_match.values():
            _goals4_sides(picks)
    except ValueError:
        return False
    return True


def pool_slip_incomplete(by_match: dict[int, list[str]], market_code: str) -> bool:
    """
    池票"未成票"单源判定（预览诚实 0 用；审计批C #9）。

    与创建侧完备守卫同口径（ttt14 与 require_pool_slip_creatable 同判
    ``len != 14``），规则集中本模块——预览端点只消费布尔，不再各市场
    inline 级联。
    """
    if market_code == "pick9":
        return len(by_match) < PICK9_LEGS
    if market_code == "ttt14":
        return len(by_match) != TTT14_LEGS
    if market_code == "goals4":
        return not goals4_selections_complete(by_match)
    return False


def _count_goals4(by_match: dict[int, list[str]]) -> int:
    """goals4 组合数：每场 |主档|×|客档| 之积（4 场固定，双侧齐才成形）。"""
    if len(by_match) != GOALS4_MATCHES:
        raise ValueError(
            f"goals4 需 {GOALS4_MATCHES} 场各选主/客档（当前 {len(by_match)} 场）"
        )
    total = 1
    for picks in by_match.values():
        home, away = _goals4_sides(picks)
        total *= len(home) * len(away)
        if total > MAX_MATERIALIZED_COMBINATIONS:
            raise CombinationLimitExceeded(
                f"组合数已连乘至 {total} 超上限 {MAX_MATERIALIZED_COMBINATIONS}"
                + f"（{len(by_match)} 场 goals4 复式）；缩小复式面后重试"
            )
    return total


def count_pool_combinations(by_match: dict[int, list[str]], market_code: str) -> int:
    """
    组合数（票 01/10 单源）：任9 子集积 / 14 场连乘 / goals4 双侧积。

    pick9 = 全部 9 场子集 × 各子集内逐场 picks 之积；ttt14 = 整体
    连乘；goals4 = 每场 |主档|×|客档| 之积（4 场固定，双侧齐才成形）。
    先算后拒：超 ``MAX_MATERIALIZED_COMBINATIONS`` 抛
    ``CombinationLimitExceeded``（票 07④），调用方须在写入前调用。
    """
    _validate_market_code(market_code)
    if not by_match:
        # 空矩阵守卫：连乘口径的空积=1 会静默物化一条零腿组合（票 01）
        raise ValueError("无已选场次，组合数为 0")
    sizes = [len(picks) for picks in by_match.values()]
    if market_code == "goals4":
        return _count_goals4(by_match)
    if market_code == "pick9":
        if len(sizes) < PICK9_LEGS:
            raise ValueError(f"任9 需至少 {PICK9_LEGS} 场已选（当前 {len(sizes)} 场）")
        total = 0
        for subset in combinations(sizes, PICK9_LEGS):
            subtotal = 1
            for size in subset:
                subtotal *= size
            total += subtotal
            if total > MAX_MATERIALIZED_COMBINATIONS:
                # total 是逐子集累加中途值（真实组合数 ≥ total），消息如实
                raise CombinationLimitExceeded(
                    f"组合数已累加至 {total} 超上限 {MAX_MATERIALIZED_COMBINATIONS}"
                    + f"（{len(sizes)} 场任9 复式）；缩小复式面后重试"
                )
        return total
    total = 1
    for size in sizes:
        total *= size
        if total > MAX_MATERIALIZED_COMBINATIONS:
            # total 是连乘中途值（真实组合数 ≥ total），消息按"已连乘至"如实
            raise CombinationLimitExceeded(
                f"组合数已连乘至 {total} 超上限 {MAX_MATERIALIZED_COMBINATIONS}"
                + f"（{len(sizes)} 场复式）；缩小复式面后重试"
            )
    return total


def iter_pool_selections(
    by_match: dict[int, list[str]], market_code: str
) -> Iterator[list[dict[str, int | str]]]:
    """
    逐组合惰性产出选择行（票 01/10 单源；物化与预览摘要共用同一生成序）。

    pick9 = 逐 9 场子集内连乘；ttt14 = 全部所选场整体连乘（14 腿口径）；
    goals4 = 每场 |主档|×|客档| 配对连乘，一组合内每场两行（主档+客档）。
    """
    _validate_market_code(market_code)
    seqs = list(by_match)
    if market_code == "goals4":
        sides = [_goals4_sides(by_match[s]) for s in seqs]
        for pairs in product(*(product(h, a) for h, a in sides)):
            yield [
                {"match_seq": s, "selection_code": code}
                for s, (home, away) in zip(seqs, pairs, strict=True)
                for code in (home, away)
            ]
        return
    subsets: Iterator[tuple[int, ...]] = (
        combinations(seqs, PICK9_LEGS)
        if market_code == "pick9"
        else iter((tuple(seqs),))
    )
    for subset in subsets:
        for picks in product(*(by_match[s] for s in subset)):
            yield [
                {"match_seq": s, "selection_code": pick}
                for s, pick in zip(subset, picks, strict=True)
            ]


def pool_selections_sha256(by_match: dict[int, list[str]], market_code: str) -> str:
    """组合流的可校验摘要（票 01）：预览 ↔ 物化对账用，canon json 行拼接。"""
    digest = hashlib.sha256()
    for selections in iter_pool_selections(by_match, market_code):
        digest.update(
            json.dumps(selections, ensure_ascii=False, separators=(",", ":")).encode()
        )
        digest.update(b"\n")
    return digest.hexdigest()


# 空组合流的摘要（票 01 预览 <9 场未成票读数；sha256(b"") 恒定值提级为常量）
EMPTY_SELECTIONS_SHA256 = hashlib.sha256(b"").hexdigest()


def materialize_combinations(
    conn: sqlite3.Connection,
    slip_id: int,
    *,
    stake_per_combination: float = 2.0,
    market_code: str = "pick9",
) -> int:
    """
    Expand pool picks into concrete combinations (任9 复式 1—N 组合).

    market_code（票 01 票级 Market 口径）：``pick9`` = 全部 9 场子集 ×
    各子集内逐场 picks 之积（n≥9）；``ttt14`` = 整体所选场连乘（14 腿
    不动）。

    重跑替换而非累加（幂等）；返回组合数。组合数先算后展开：超上限
    直接拒绝（票 07④，人裁决②取 10 万量级），拒绝发生在任何写入之前；
    规模内按生成器分批插入，不整建组合列表在内存（14 场×3 向
    =478 万曾整展开）。
    """
    rows = conn.execute(
        """
            SELECT match_seq, selection_code FROM pool_picks WHERE slip_id = ?
ORDER BY
            match_seq, selection_code
        """,
        (slip_id,),
    ).fetchall()
    by_match = pool_pick_matrix(
        (int(row["match_seq"]), str(row["selection_code"])) for row in rows
    )
    total = count_pool_combinations(by_match, market_code)
    conn.execute("DELETE FROM combinations WHERE slip_id = ?", (slip_id,))
    conn.executemany(
        """
            INSERT INTO combinations (slip_id, seq, stake, selections)
            VALUES (?, ?, ?, ?)
        """,
        (
            (
                slip_id,
                seq,
                stake_per_combination,
                json.dumps(selections, ensure_ascii=False, separators=(",", ":")),
            )
            for seq, selections in enumerate(
                iter_pool_selections(by_match, market_code)
            )
        ),
    )
    return total


def _slip_aggregates() -> str:
    """
    票级聚合子查询（票 01）：bets 面与池票组合回读面共用。

    组合活口径排除标记隔离史行（quarantine_reason IS NULL）；隔离行单列
    计数 = 对账注记（人裁决 2026-11-03：原行保留、不改写）。
    """
    return """
        (SELECT COUNT(*) FROM bets b WHERE b.slip_id = s.id) AS bet_count,
        (SELECT COALESCE(SUM(b.stake), 0) FROM bets b WHERE b.slip_id = s.id)
        AS stake_total,
        (SELECT COALESCE(SUM(b.profit), 0) FROM bets b
         WHERE b.slip_id = s.id AND b.status != 'open') AS profit_total,
        (SELECT COUNT(*) FROM combinations c
         WHERE c.slip_id = s.id AND c.quarantine_reason IS NULL)
        AS combination_count,
        (SELECT COALESCE(SUM(c.stake), 0) FROM combinations c
         WHERE c.slip_id = s.id AND c.quarantine_reason IS NULL)
        AS pool_stake_total,
        (SELECT COUNT(*) FROM combinations c
         WHERE c.slip_id = s.id AND c.quarantine_reason IS NOT NULL)
        AS quarantined_combinations
    """


def get_slip(conn: sqlite3.Connection, slip_id: int) -> sqlite3.Row | None:
    """Fetch one slip with bet and combination aggregates."""
    return conn.execute(
        f"""
        SELECT s.*, {_slip_aggregates()}
        FROM bet_slips s WHERE s.id = ?
        """,  # noqa: S608 常量子查询拼接
        (slip_id,),
    ).fetchone()


def list_slips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List slips with bet and combination aggregates."""
    return conn.execute(
        f"""
        SELECT s.*, {_slip_aggregates()}
        FROM bet_slips s
        ORDER BY s.created_at DESC, s.id DESC
        """,  # noqa: S608 常量子查询拼接
    ).fetchall()


def save_settlement(
    conn: sqlite3.Connection, record: SettlementInput, *, reason: str = "settlement"
) -> int:
    """Persist a settlement (upsert by bet or slip)；同步 bet 状态。"""
    previous = conn.execute(
        "SELECT * FROM settlements WHERE bet_id = ? OR slip_id = ?",
        (record.bet_id, record.slip_id),
    ).fetchone()
    payload = json.dumps(record.detail, ensure_ascii=False)
    at = utc_now_iso()
    if record.bet_id is not None:
        conn.execute(
            """
                INSERT INTO settlements
(bet_id, slip_id, status, stake, payout, profit,
                detail, computed_at)
VALUES (?, NULL, ?, ?, ?, ?, ?, ?)
ON
                CONFLICT(bet_id) DO UPDATE SET status=excluded.status,
                stake=excluded.stake, payout=excluded.payout, profit=excluded.profit,
                detail=excluded.detail, computed_at=excluded.computed_at
            """,
            (
                record.bet_id,
                record.status.value,
                record.stake,
                record.payout,
                record.profit,
                payload,
                at,
            ),
        )
        conn.execute(
            """
                UPDATE bets SET status = ?, payout = ?, profit = ?, settled_at = ?
WHERE
                id = ?
            """,
            (record.status.value, record.payout, record.profit, at, record.bet_id),
        )
        row = conn.execute(
            "SELECT id FROM settlements WHERE bet_id = ?", (record.bet_id,)
        ).fetchone()
    else:
        slip_id = record.slip_id
        if slip_id is None:
            raise ValueError("settlement 必须挂 bet 或 slip 之一")
        conn.execute(
            """
                INSERT INTO settlements
(bet_id, slip_id, status, stake, payout, profit,
                detail, computed_at)
VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
ON
                CONFLICT(slip_id) DO UPDATE SET status=excluded.status,
                stake=excluded.stake, payout=excluded.payout, profit=excluded.profit,
                detail=excluded.detail, computed_at=excluded.computed_at
            """,
            (
                slip_id,
                record.status.value,
                record.stake,
                record.payout,
                record.profit,
                payload,
                at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM settlements WHERE slip_id = ?", (slip_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("settlement upsert 后未找到行")
    conn.execute(
        """INSERT INTO settlement_revisions
           (settlement_id, previous, replacement, reason, recorded_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            int(row["id"]),
            json.dumps(dict(previous)) if previous else None,
            record.model_dump_json(),
            reason,
            at,
        ),
    )
    if record.bet_id is not None and record.detail.get("status") == "open":
        conn.execute(
            """UPDATE bets SET status='open', payout=NULL, profit=NULL, settled_at=NULL
               WHERE id=?""",
            (record.bet_id,),
        )
    return int(row["id"])


def bankroll_balance(conn: sqlite3.Connection) -> float | None:
    """Current bankroll (None before the first event)."""
    row = conn.execute(
        "SELECT balance_after FROM bankroll_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return float(row["balance_after"]) if row is not None else None


def record_bankroll_event(
    conn: sqlite3.Connection,
    kind: str,
    amount_cny: float,
    *,
    bet_id: int | None = None,
    slip_id: int | None = None,
    note: str | None = None,
    occurred_at: str | None = None,
) -> int:
    """Append a bankroll movement (仅 live 结算/出入金调用，ADR 0002)。"""
    at = occurred_at or utc_now_iso()
    balance = bankroll_balance(conn) or 0.0
    cur = conn.execute(
        """
            INSERT INTO bankroll_events
(occurred_at, kind, amount_cny, balance_after,
            bet_id, slip_id, note)
VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (at, kind, amount_cny, balance + amount_cny, bet_id, slip_id, note),
    )
    return _insert_id(cur)


def list_bankroll_events(
    conn: sqlite3.Connection, limit: int = 50
) -> list[sqlite3.Row]:
    """Recent bankroll events (newest first)."""
    return conn.execute(
        "SELECT * FROM bankroll_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def record_deposit(
    conn: sqlite3.Connection,
    amount_cny: float,
    *,
    note: str | None = None,
    occurred_at: str | None = None,
) -> sqlite3.Row:
    """
    Record a deposit (kind=deposit, 票 20 入金端点的领域入口)；返回新事件行。

    金额必须 > 0（API 层 pydantic 先拦，这里守域不变量——CLI/脚本直调同样受约束）；
    occurred_at 缺省取当前 UTC 时间。withdraw 不做（后续按需）。
    """
    if not amount_cny > 0:
        raise ValueError("入金金额必须大于 0")
    with atomic(conn):
        event_id = record_bankroll_event(
            conn, "deposit", amount_cny, note=note, occurred_at=occurred_at
        )
        row = conn.execute(
            "SELECT * FROM bankroll_events WHERE id = ?", (event_id,)
        ).fetchone()
    if row is None:
        raise RuntimeError("deposit 落库后未找到行")
    return row


def bankroll_events_for_bet(conn: sqlite3.Connection, bet_id: int) -> list[sqlite3.Row]:
    """一注的全部资金流水（结算冲正的一致性核查用）。"""
    return conn.execute(
        "SELECT kind, amount_cny, slip_id FROM bankroll_events WHERE bet_id = ?",
        (bet_id,),
    ).fetchall()


def has_settlement_or_bankroll_event(conn: sqlite3.Connection, bet_id: int) -> bool:
    """回录守卫：已有结算或资金记录的注不可再次回录。"""
    return (
        conn.execute(
            """SELECT 1 FROM bankroll_events WHERE bet_id=? UNION ALL
               SELECT 1 FROM settlements WHERE bet_id=?""",
            (bet_id, bet_id),
        ).fetchone()
        is not None
    )


def settlement_exists(conn: sqlite3.Connection, bet_id: int) -> bool:
    """该注是否已有结算行（更正重算的跳过条件之一）。"""
    return (
        conn.execute("SELECT 1 FROM settlements WHERE bet_id = ?", (bet_id,)).fetchone()
        is not None
    )


def count_pending_pool_slips(conn: sqlite3.Connection) -> int:
    """无结算行的奖池票数（奖池兑付未支持前保持待结，不清零）。"""
    return int(
        conn.execute(
            """SELECT COUNT(*) FROM bet_slips s
           WHERE EXISTS (SELECT 1 FROM pool_picks p WHERE p.slip_id = s.id)
           AND NOT EXISTS (SELECT 1 FROM settlements st WHERE st.slip_id = s.id)"""
        ).fetchone()[0]
    )
