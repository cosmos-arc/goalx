"""
/bets/summary 全量聚合面（iterate 票 10）：复盘页经济账的服务端单源。

根因=「一端点两职责」：列表要有界（渲染事实），聚合要全量（经济账事实）。
本面只承担后者——totalPnl/命中率/累计序列（按日桶）永远全量口径，不受
列表有界 100 截断；history 五指标与 overview 快照卡各取所需。

结算口径 = 历史页 canon「已锁定且已结算」（purchased 且 settled_at 非空）：
未锁定的死建议（结算批照跑）不进经济账。日桶 = 北京业务日
（beijing_business_date 全仓唯一出处；account.py 的 UTC substr 是其自家
shortcut，不在此复用）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from goalx_backend.data.fixtures import beijing_business_date
from goalx_backend.models import BetMode


@dataclass(frozen=True)
class DayBucket:
    """一个北京业务日的已结算聚合（客户端按日求和可复原任意时间范围口径）。"""

    date: str
    settled_n: int
    won_n: int
    decided_n: int
    pnl: float
    sum_ev_consensus: float
    n_ev_consensus: int
    sum_ev_model: float
    n_ev_model: int


@dataclass(frozen=True)
class ModeSummary:
    """一个模式（paper/live）的全量聚合行。"""

    mode: str
    bets_n: int
    open_n: int
    settled_n: int
    won_n: int
    decided_n: int
    total_pnl: float
    stake_settled: float
    sum_ev_consensus: float
    n_ev_consensus: int
    sum_ev_model: float
    n_ev_model: int
    days: list[DayBucket] = field(default_factory=list)


@dataclass
class _ModeAcc:
    """聚合过程中的可变累计器（结算行进日桶，计数器进总量）。"""

    mode: str
    bets_n: int = 0
    open_n: int = 0
    settled_n: int = 0
    won_n: int = 0
    decided_n: int = 0
    total_pnl: float = 0.0
    stake_settled: float = 0.0
    sum_ev_consensus: float = 0.0
    n_ev_consensus: int = 0
    sum_ev_model: float = 0.0
    n_ev_model: int = 0
    days: dict[str, dict[str, float]] = field(default_factory=dict)


def _add_settled(acc: _ModeAcc, row: sqlite3.Row) -> None:
    """一行已结算注进累计器与日桶（profit NULL 按 0 入账，与历史页客户端口径一致）。"""
    acc.settled_n += 1
    status = str(row["status"])
    if status == "won":
        acc.won_n += 1
    if status != "void":
        acc.decided_n += 1
    pnl = float(row["profit"] or 0.0)
    acc.total_pnl += pnl
    acc.stake_settled += float(
        row["actual_stake"] if row["actual_stake"] is not None else row["stake"]
    )
    if row["snap_ev_consensus"] is not None:
        acc.sum_ev_consensus += float(row["snap_ev_consensus"])
        acc.n_ev_consensus += 1
    if row["snap_ev_model"] is not None:
        acc.sum_ev_model += float(row["snap_ev_model"])
        acc.n_ev_model += 1
    day = beijing_business_date(datetime.fromisoformat(str(row["settled_at"])))
    bucket = acc.days.setdefault(
        day,
        {
            "settled_n": 0,
            "won_n": 0,
            "decided_n": 0,
            "pnl": 0.0,
            "evc": 0.0,
            "nec": 0,
            "evm": 0.0,
            "nem": 0,
        },
    )
    bucket["settled_n"] += 1
    if status == "won":
        bucket["won_n"] += 1
    if status != "void":
        bucket["decided_n"] += 1
    bucket["pnl"] += pnl
    if row["snap_ev_consensus"] is not None:
        bucket["evc"] += float(row["snap_ev_consensus"])
        bucket["nec"] += 1
    if row["snap_ev_model"] is not None:
        bucket["evm"] += float(row["snap_ev_model"])
        bucket["nem"] += 1


def bets_summary(conn: sqlite3.Connection) -> list[ModeSummary]:
    """两模式全量聚合（[paper, live] 恒定序；SQL 归 betting 域，ADR-0008）。"""
    accs = {mode.value: _ModeAcc(mode=mode.value) for mode in BetMode}
    rows = conn.execute(
        """
        SELECT mode, purchased, status, settled_at, profit, stake, actual_stake,
               snap_ev_consensus, snap_ev_model
        FROM bets
        """
    ).fetchall()
    for row in rows:
        acc = accs.get(str(row["mode"]))
        if acc is None:  # CHECK 约束外的脏行不炸聚合面，跳过计数
            continue
        acc.bets_n += 1
        if row["status"] == "open":
            acc.open_n += 1
        if row["purchased"] and row["settled_at"]:
            _add_settled(acc, row)
    faces: list[ModeSummary] = []
    for mode in BetMode:
        acc = accs[mode.value]
        faces.append(
            ModeSummary(
                mode=acc.mode,
                bets_n=acc.bets_n,
                open_n=acc.open_n,
                settled_n=acc.settled_n,
                won_n=acc.won_n,
                decided_n=acc.decided_n,
                total_pnl=round(acc.total_pnl, 2),
                stake_settled=round(acc.stake_settled, 2),
                sum_ev_consensus=round(acc.sum_ev_consensus, 6),
                n_ev_consensus=acc.n_ev_consensus,
                sum_ev_model=round(acc.sum_ev_model, 6),
                n_ev_model=acc.n_ev_model,
                days=[
                    DayBucket(
                        date=day,
                        settled_n=int(bucket["settled_n"]),
                        won_n=int(bucket["won_n"]),
                        decided_n=int(bucket["decided_n"]),
                        pnl=round(bucket["pnl"], 2),
                        sum_ev_consensus=round(bucket["evc"], 6),
                        n_ev_consensus=int(bucket["nec"]),
                        sum_ev_model=round(bucket["evm"], 6),
                        n_ev_model=int(bucket["nem"]),
                    )
                    for day, bucket in sorted(acc.days.items())
                ],
            )
        )
    return faces
