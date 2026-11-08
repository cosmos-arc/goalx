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
    """一个模式（paper/live）的全量聚合行（总量=日桶归约，单一记账源）。"""

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
class _DayAcc:
    """日桶累计器（可变）；ModeSummary 的全部日桶口径字段由此归约。"""

    settled_n: int = 0
    won_n: int = 0
    decided_n: int = 0
    pnl: float = 0.0
    sum_ev_consensus: float = 0.0
    n_ev_consensus: int = 0
    sum_ev_model: float = 0.0
    n_ev_model: int = 0

    def add(self, row: sqlite3.Row) -> None:
        """一行已结算注入桶（won/decided 命中率口径与历史页一致：void 不入分母）。"""
        status = str(row["status"])
        self.settled_n += 1
        if status == "won":
            self.won_n += 1
        if status != "void":
            self.decided_n += 1
        self.pnl += float(row["profit"] or 0.0)
        if row["snap_ev_consensus"] is not None:
            self.sum_ev_consensus += float(row["snap_ev_consensus"])
            self.n_ev_consensus += 1
        if row["snap_ev_model"] is not None:
            self.sum_ev_model += float(row["snap_ev_model"])
            self.n_ev_model += 1


@dataclass
class _ModeAcc:
    """模式级累计器：bets_n/open_n/stake 全量记，结算行只进日桶（单源）。"""

    mode: str
    bets_n: int = 0
    open_n: int = 0
    stake_settled: float = 0.0
    days: dict[str, _DayAcc] = field(default_factory=dict)


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
            acc.stake_settled += float(
                row["actual_stake"] if row["actual_stake"] is not None else row["stake"]
            )
            day = beijing_business_date(datetime.fromisoformat(str(row["settled_at"])))
            acc.days.setdefault(day, _DayAcc()).add(row)
    faces: list[ModeSummary] = []
    for mode in BetMode:
        acc = accs[mode.value]
        buckets = [
            DayBucket(
                date=day,
                settled_n=bucket.settled_n,
                won_n=bucket.won_n,
                decided_n=bucket.decided_n,
                pnl=round(bucket.pnl, 2),
                sum_ev_consensus=round(bucket.sum_ev_consensus, 6),
                n_ev_consensus=bucket.n_ev_consensus,
                sum_ev_model=round(bucket.sum_ev_model, 6),
                n_ev_model=bucket.n_ev_model,
            )
            for day, bucket in sorted(acc.days.items())
        ]
        faces.append(
            ModeSummary(
                mode=acc.mode,
                bets_n=acc.bets_n,
                open_n=acc.open_n,
                settled_n=sum(b.settled_n for b in buckets),
                won_n=sum(b.won_n for b in buckets),
                decided_n=sum(b.decided_n for b in buckets),
                total_pnl=round(sum(b.pnl for b in buckets), 2),
                stake_settled=round(acc.stake_settled, 2),
                sum_ev_consensus=round(sum(b.sum_ev_consensus for b in buckets), 6),
                n_ev_consensus=sum(b.n_ev_consensus for b in buckets),
                sum_ev_model=round(sum(b.sum_ev_model for b in buckets), 6),
                n_ev_model=sum(b.n_ev_model for b in buckets),
                days=buckets,
            )
        )
    return faces
