"""投注域仓储：Bet/BetLeg/BetSlip/Combination、结算落库与 Bankroll 事件。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from itertools import product
from typing import Any

from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.models import (
    BetMode,
    LegInput,
    MarketKind,
    SettlementInput,
)


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


def settled_purchased_bets(conn: sqlite3.Connection) -> list[SettledBet]:
    """全部已结算已购注（腿内联，按 settled_at,id 稳定排序）。"""
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
) -> int:
    """Create a bet record (建议未购或已回录)；返回 id。"""
    cur = conn.execute(
        """
            INSERT INTO bets
(slip_id, mode, market_kind, purchased, stake, placed_at,
            created_at, strategy_version)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    return _insert_id(cur)


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
            b.actual_stake, b.strategy_version,
            b.placed_at, b.created_at, b.status, b.payout, b.profit, b.settled_at,
            s.created_at AS locked_at,
            (SELECT COUNT(*) FROM bet_legs l WHERE l.bet_id = b.id) AS leg_count,
(SELECT
            json_group_array(json_object(
'fixture_id', l.fixture_id, 'market_code',
            l.market_code,
'selection_code', l.selection_code, 'locked_odds',
            l.locked_odds, 'actual_odds', l.actual_odds,
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
'goal_line', json_extract(l.meta,
            '$.goal_line')))
FROM bet_legs l WHERE l.bet_id = b.id) AS legs
FROM bets b LEFT JOIN bet_slips s ON s.id = b.slip_id
            WHERE b.id = ?
        """,
        (bet_id,),
    ).fetchone()


def mark_purchased(conn: sqlite3.Connection, bet_id: int, placed_at: str) -> None:
    """回录：标记建议注为实际购买。"""
    conn.execute(
        "UPDATE bets SET purchased = 1, placed_at = ? WHERE id = ?", (placed_at, bet_id)
    )


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


def materialize_combinations(
    conn: sqlite3.Connection, slip_id: int, *, stake_per_combination: float = 2.0
) -> int:
    """
    Expand pool picks into concrete combinations (任9 复式 1—N 组合)。

    重跑替换而非累加（幂等）；返回组合数。
    """
    rows = conn.execute(
        """
            SELECT match_seq, selection_code FROM pool_picks WHERE slip_id = ?
ORDER BY
            match_seq, selection_code
        """,
        (slip_id,),
    ).fetchall()
    by_match: dict[int, list[str]] = {}
    for row in rows:
        by_match.setdefault(int(row["match_seq"]), []).append(
            str(row["selection_code"])
        )
    seqs = sorted(by_match)
    combos = [
        [
            {"match_seq": seq, "selection_code": pick}
            for seq, pick in zip(seqs, picks, strict=True)
        ]
        for picks in product(*(by_match[seq] for seq in seqs))
    ]
    conn.execute("DELETE FROM combinations WHERE slip_id = ?", (slip_id,))
    conn.executemany(
        """
            INSERT INTO combinations
(slip_id, seq, stake, selections) VALUES (?, ?, ?,
            ?)
        """,
        [
            (
                slip_id,
                seq,
                stake_per_combination,
                json.dumps(sel_list, ensure_ascii=False),
            )
            for seq, sel_list in enumerate(combos)
        ],
    )
    return len(combos)


def get_slip(conn: sqlite3.Connection, slip_id: int) -> sqlite3.Row | None:
    """Fetch one slip with bet aggregates."""
    return conn.execute(
        """
        SELECT s.*,
        (SELECT COUNT(*) FROM bets b WHERE b.slip_id = s.id) AS bet_count,
        (SELECT COALESCE(SUM(b.stake), 0) FROM bets b WHERE b.slip_id = s.id)
        AS stake_total,
        (SELECT COALESCE(SUM(b.profit), 0) FROM bets b
        WHERE b.slip_id = s.id AND b.status != 'open') AS profit_total
        FROM bet_slips s WHERE s.id = ?
        """,
        (slip_id,),
    ).fetchone()


def list_slips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """List slips with bet aggregates."""
    return conn.execute(
        """
            SELECT s.*,
(SELECT COUNT(*) FROM bets b WHERE b.slip_id = s.id) AS
            bet_count,
(SELECT COALESCE(SUM(b.stake), 0) FROM bets b WHERE b.slip_id =
            s.id)
AS stake_total,
(SELECT COALESCE(SUM(b.profit), 0) FROM bets b
WHERE
            b.slip_id = s.id AND b.status != 'open') AS profit_total
FROM bet_slips s
            ORDER BY s.created_at DESC, s.id DESC
        """
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
