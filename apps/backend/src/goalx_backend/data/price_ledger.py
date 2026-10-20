"""
同步报价账本（measure-integrity 票 07，主线 A 仪器）。

同场/同玩法/同结算口径/同决策时点下配对保存：JC 报价（值+observed_at+
source_updated_at，溯源 odds_snapshots.id）× 参考锚（源T Pinnacle
fair probs，as-of observed_at——sharp_probs_asof 同口径）× 最新销售态
× 单关资格。只记录不判定不下注；候选过程完整保存，不做任何
"只存过阈值"过滤。

- 触发：JC 摄取新快照落库时（sporttery 摄取路径调用）——同刻配对现算；
- 回填：参考当时不可得 → 诚实 NULL + 日终 sweep 按 observed_at 现算
  （as-of 语义保证事后计算不引入前视）；回填只填 NULL 行不改已填行
  （写一次），但会更新 reference_backfilled_at 标记——由 sweep 的
  UPDATE 走触发器豁免列（触发器放行仅该列的回填 UPDATE）。
- append-only：行不可删改（触发器防线）；UNIQUE(snapshot_id) 幂等。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.quote_evidence import sharp_probs_asof
from goalx_backend.models import SnapshotInput


def record_jc_observation(
    conn: sqlite3.Connection,
    snap: SnapshotInput,
    *,
    snapshot_id: int,
) -> bool:
    """
    JC 新观测落账本一行（新快照路径专用；重复调用被 UNIQUE 吸收）。

    snap 即摄取路径刚落库的快照（fixture/玩法/选向/赔率/时刻全取自它）；
    observed_at 缺（旧行为快照）不配对——时间语义未知不冒充。
    参考锚 as-of=observed_at 现算（缺=诚实 NULL 留给 sweep）；销售态
    as-of=observed_at。返回是否新落行。
    """
    if snap.observed_at is None:
        return False
    sharp = sharp_probs_asof(conn, snap.fixture_id, snap.observed_at, anchor_only=True)
    sale = fx_store.latest_sale_status_asof(
        conn, snap.fixture_id, snap.market_code, snap.observed_at
    )
    probs = sharp[0] if sharp is not None else {}
    # 无自有事务壳：随摄取批同事务提交（中途 commit 会碎批次原子性）
    cur = conn.execute(
        " ".join(
            (
                "INSERT OR IGNORE INTO price_deviation_ledger",
                "(fixture_id, market_code, selection_code, jc_odds,",
                "observed_at, source_updated_at, snapshot_id,",
                "p_sharp_h, p_sharp_d, p_sharp_a, anchor_ref,",
                "sale_state, single_eligible, created_at)",
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            )
        ),
        (
            snap.fixture_id,
            snap.market_code,
            snap.selection_code,
            float(snap.odds),
            snap.observed_at,
            snap.source_updated_at,
            snapshot_id,
            probs.get("h"),
            probs.get("d"),
            probs.get("a"),
            sharp[1] if sharp is not None else None,
            str(sale["sale_state"]) if sale is not None else None,
            (
                int(sale["single_eligible"])
                if sale is not None and sale["single_eligible"] is not None
                else None
            ),
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    return cur.rowcount > 0


def backfill_missing_reference(conn: sqlite3.Connection) -> int:
    """
    日终 sweep：参考锚 NULL 的行按 observed_at 现算回填。

    as-of 语义保证事后计算无前视；只处理 p_sharp_h IS NULL 的行，
    已填行不动。触发器放行仅参考列的回填 UPDATE（见迁移 v30）。
    """
    moment = datetime.now(UTC).isoformat(timespec="seconds")
    pending = conn.execute(
        " ".join(
            (
                "SELECT id, fixture_id, observed_at FROM price_deviation_ledger",
                "WHERE p_sharp_h IS NULL",
            )
        )
    ).fetchall()
    filled = 0
    for row in pending:
        sharp = sharp_probs_asof(
            conn, int(row["fixture_id"]), str(row["observed_at"]), anchor_only=True
        )
        if sharp is None:
            continue
        probs, anchor_ref = sharp
        conn.execute(
            " ".join(
                (
                    "UPDATE price_deviation_ledger SET",
                    "p_sharp_h = ?, p_sharp_d = ?, p_sharp_a = ?, anchor_ref = ?,",
                    "reference_backfilled_at = ? WHERE id = ? AND p_sharp_h IS NULL",
                )
            ),
            (
                probs.get("h"),
                probs.get("d"),
                probs.get("a"),
                anchor_ref,
                moment,
                int(row["id"]),
            ),
        )
        filled += 1
    conn.commit()
    return filled


def ledger_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """账本面汇总（日终报告/判活用）。"""
    row = conn.execute(
        " ".join(
            (
                "SELECT COUNT(*) n,",
                "SUM(CASE WHEN p_sharp_h IS NULL THEN 1 ELSE 0 END) missing_ref,",
                "SUM(CASE WHEN reference_backfilled_at IS NOT NULL",
                "THEN 1 ELSE 0 END) backfilled",
                "FROM price_deviation_ledger",
            )
        )
    ).fetchone()
    return {
        "rows": int(row[0] or 0),
        "missing_reference": int(row[1] or 0),
        "reference_backfilled": int(row[2] or 0),
    }
