"""投注域仓储：Bet/BetLeg/BetSlip/Combination、结算落库与 Bankroll 事件。"""

from __future__ import annotations

import json
import sqlite3
from itertools import product
from typing import Any

from goalx_backend.db import utc_now_iso
from goalx_backend.models import (
    BetMode,
    LegInput,
    MarketKind,
    SettlementInput,
)


def _insert_id(cur: sqlite3.Cursor) -> int:
    """Read the rowid of a plain insert; raises RuntimeError when absent."""
    if not cur.lastrowid:
        raise RuntimeError("INSERT 未产生 rowid")
    return int(cur.lastrowid)


def create_bet(
    conn: sqlite3.Connection,
    mode: BetMode,
    market_kind: MarketKind,
    stake: float,
    *,
    purchased: bool = False,
    placed_at: str | None = None,
    slip_id: int | None = None,
) -> int:
    """Create a bet record (建议未购或已回录)；返回 id。"""
    cur = conn.execute(
        """
            INSERT INTO bets
(slip_id, mode, market_kind, purchased, stake, placed_at,
            created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slip_id,
            mode.value,
            market_kind.value,
            int(purchased),
            stake,
            placed_at,
            utc_now_iso(),
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
    """List bets with their legs aggregated as JSON (复盘列表)。"""
    sql = """
            SELECT b.id, b.slip_id, b.mode, b.market_kind, b.purchased, b.stake,
            b.placed_at, b.created_at, b.status, b.payout, b.profit, b.settled_at,
            (SELECT COUNT(*) FROM bet_legs l WHERE l.bet_id = b.id) AS leg_count,
(SELECT
            json_group_array(json_object(
'fixture_id', l.fixture_id, 'market_code',
            l.market_code,
'selection_code', l.selection_code, 'locked_odds',
            l.locked_odds,
'goal_line', json_extract(l.meta, '$.goal_line')))
FROM
            bet_legs l WHERE l.bet_id = b.id) AS legs
FROM bets b WHERE 1=1
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
    """Fetch one bet with aggregated legs."""
    return conn.execute(
        """
            SELECT b.*,
(SELECT json_group_array(json_object(
'fixture_id', l.fixture_id,
            'market_code', l.market_code,
'selection_code', l.selection_code,
            'locked_odds', l.locked_odds,
'goal_line', json_extract(l.meta,
            '$.goal_line')))
FROM bet_legs l WHERE l.bet_id = b.id) AS legs
FROM bets b
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
