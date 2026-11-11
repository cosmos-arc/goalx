"""
市场基准同窗冻结（auto-paper-20261106 票 01，prereg A 节）。

决策锚拍点（12:30/16:30/21:00 北京时，与策略层 auto-v2 共用锚集）对
当日全部竞彩场次 write-once 冻结两条 had 基准：

- jc_official：竞彩官方 SP 三向（原始赔率），取有效观测时刻
  （effective_observed_at 语义）≤ cutoff 的最新行——晚收的价不得凭倒填
  的源调盘时刻（sporttery captured_at）混进锚，观测时刻未知的旧行不进
  （未知不伪造）；只落完整三向，缺口不伪造。prereg「归一化」是评分侧
  比较口径，由原始赔率确定性派生，快照存原始值。
- eu_consensus：欧赔完整三向共识 Shin 概率（评分基准口径，与
  forward_validation.market_baseline_asof 同源同窗）。

「各玩法可得者」落地：主库竞彩捕获面只有 had（sporttery 源），hhad/
ttg/crs/hafu 无捕获行可读——基准按可得者=had 冻结，缺口由评分侧
unknown 差值语义兜住（票 02 不变量），不伪造空基准。

market_baseline_snapshots 表归本模块（ADR-0008）；UNIQUE 键吸收同锚重跑
（write-once：重跑只补缺席行，不改既有值）。快照是证据非缓存——
cutoff_at 记录真实读数时点，评分侧以它为同窗口径；锚后到价不倒填
（cutoff 由任务层封顶在锚+ANCHOR_GRACE 窗口内，晚到补跑不扩窗）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

# prereg B 节冻结锚（改动=新协议版本，历史快照永不改写）
ANCHOR_LABELS: tuple[str, ...] = ("12:30", "16:30", "21:00")
# 锚快照读数窗上限：定拍在锚后 3 分钟，窗留 30 分钟吸收抖动/晚到重跑；
# 补跑再晚也只读到锚+30min 的可见价，锚语义不随补跑时刻漂移
ANCHOR_GRACE = timedelta(minutes=30)
BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass
class BaselineFreezeStats:
    """一次锚冻结的计数面（晨检/日志用）。"""

    business_date: str
    anchor_label: str
    fixtures: int = 0
    jc_frozen: int = 0
    eu_frozen: int = 0
    jc_absent: int = 0  # JC 三向不全（该锚可见价不完整，不伪造）
    eu_absent: int = 0  # 无完整共识（未链/书数不足/降级行源空）
    duplicates: int = 0  # 同锚重跑被 UNIQUE 吸收的行数


def anchor_label_at(now: datetime | None = None) -> str | None:
    """当前北京时之前最近的锚标签；早于首锚（12:30）返回 None。"""
    current = now or datetime.now(BEIJING)
    minutes = current.hour * 60 + current.minute
    label: str | None = None
    for anchor in ANCHOR_LABELS:
        hour, minute = (int(part) for part in anchor.split(":"))
        if hour * 60 + minute <= minutes:
            label = anchor
    return label


def effective_cutoff(
    anchor_label: str, business_date: str, now: datetime | None = None
) -> str:
    """
    锚快照读数截止：min(当下, 锚时刻+30min 窗)。

    晚到补跑不把补跑时刻当截止——锚后 >30min 才到的价不进该锚
    （write-once 只防改写不防晚到首冻，窗口封顶补这个洞）。
    """
    hour, minute = (int(part) for part in anchor_label.split(":"))
    anchor_at = datetime.fromisoformat(f"{business_date}T00:00:00").replace(
        tzinfo=BEIJING
    ) + timedelta(hours=hour, minutes=minute)
    current = now or datetime.now(BEIJING)
    cutoff = min(current, anchor_at + ANCHOR_GRACE)
    return cutoff.astimezone(UTC).isoformat(timespec="seconds")


def _insert_snapshot(  # noqa: PLR0913 证据列随 source 解释累加（observed/sale_state）
    conn: sqlite3.Connection,
    stats: BaselineFreezeStats,
    *,
    fixture_id: int,
    cutoff_at: str,
    source: str,
    prices: dict[str, Any],
    observed_at: dict[str, str] | None,
    sale_state: str | None,
) -> bool:
    """write-once 落一行基准；UNIQUE 吸收返回 False。"""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO market_baseline_snapshots
        (business_date, fixture_id, anchor_label, cutoff_at, source, market,
         prices, observed_at, sale_state, created_at)
        VALUES (?, ?, ?, ?, ?, 'had', ?, ?, ?, ?)
        """,
        (
            stats.business_date,
            fixture_id,
            stats.anchor_label,
            cutoff_at,
            source,
            json.dumps(prices, ensure_ascii=False, sort_keys=True),
            (
                json.dumps(observed_at, ensure_ascii=False, sort_keys=True)
                if observed_at is not None
                else None
            ),
            sale_state,
            utc_now_iso(),
        ),
    )
    return cur.rowcount > 0


def baseline_probs_asof(
    conn: sqlite3.Connection, fixture_id: int, source: str, as_of: str
) -> tuple[str, tuple[float, float, float]] | None:
    """
    as_of 前最近锚的冻结基准（评分侧同窗口径，auto-paper 票 02）。

    prices 按 source 解释：jc_official=原始 SP（归一化由评分侧确定性派生，
    与冻结语义一致）；eu_consensus=Shin 概率。取该场 cutoff_at ≤ as_of 的
    最新锚行；无行/缺腿 → None（评分侧记 unknown 差值，不删行）。
    """
    row = conn.execute(
        """
        SELECT anchor_label, prices FROM market_baseline_snapshots
        WHERE fixture_id = ? AND source = ? AND market = 'had' AND cutoff_at <= ?
        ORDER BY cutoff_at DESC, id DESC LIMIT 1
        """,
        (fixture_id, source, as_of),
    ).fetchone()
    if row is None:
        return None
    prices = json.loads(str(row["prices"]))
    try:
        raw = tuple(float(prices[sel]) for sel in SELECTIONS)
    except (KeyError, TypeError, ValueError):
        return None
    if any(value <= 0 for value in raw):
        return None
    if source == "jc_official":
        probs: tuple[float, float, float] = cast(
            "tuple[float, float, float]", om.normalized_implied(raw)
        )
    elif source == "eu_consensus":
        probs = cast("tuple[float, float, float]", raw)
    else:
        return None
    return str(row["anchor_label"]), probs


def freeze_anchor_baselines(
    conn: sqlite3.Connection,
    *,
    anchor_label: str,
    cutoff_at: str,
    business_date: str | None = None,
    duck_con: quote_evidence.DuckCon | None = None,
) -> BaselineFreezeStats:
    """
    对一个业务日的全部竞彩场次冻结一个锚的 JC SP + 欧赔共识基准。

    纯读无采集副作用：cutoff_at 由调用方给（定拍任务=当下；补跑=显式），
    欧赔行源走 corpus 批量预取（duck_con 缺席降级 legacy 行源，通常为空
    → eu_absent 计数，不阻断 JC 面）。sale_state 附锚时最新观测（可缺），
    供评分/策略侧按可购过滤，不在本层裁决。
    """
    if anchor_label not in ANCHOR_LABELS:
        raise ValueError(f"未知锚 {anchor_label!r}（冻结锚集 {ANCHOR_LABELS}）")
    stats = BaselineFreezeStats(
        business_date=business_date or fx_store.beijing_business_date(),
        anchor_label=anchor_label,
    )
    fixtures = fx_store.fixtures_for_business_date(conn, stats.business_date)
    stats.fixtures = len(fixtures)
    sid_by_fixture = {
        int(f["id"]): mapping.srct_sid_for_fixture(conn, int(f["id"])) for f in fixtures
    }
    prefetched = (
        quote_evidence.srct_snapshot_rows_bulk(
            duck_con,
            sorted({s for s in sid_by_fixture.values() if s}),
            cutoff_at,
        )
        if duck_con is not None
        else {}
    )
    for fixture in fixtures:
        fixture_id = int(fixture["id"])
        sale_row = fx_store.latest_sale_status_asof(conn, fixture_id, "had", cutoff_at)
        sale_state = str(sale_row["sale_state"]) if sale_row is not None else None
        jc = fx_store.latest_odds_by_selection_asof(
            conn, fixture_id, "had", "sporttery", cutoff_at
        )
        if all(sel in jc for sel in SELECTIONS):
            frozen = _insert_snapshot(
                conn,
                stats,
                fixture_id=fixture_id,
                cutoff_at=cutoff_at,
                source="jc_official",
                prices={sel: jc[sel][0] for sel in SELECTIONS},
                observed_at={sel: jc[sel][1] for sel in SELECTIONS},
                sale_state=sale_state,
            )
            if frozen:
                stats.jc_frozen += 1
            else:
                stats.duplicates += 1
        else:
            stats.jc_absent += 1
        sid = sid_by_fixture[fixture_id]
        probs = quote_evidence.eu_consensus_asof(
            conn,
            fixture_id,
            cutoff_at,
            duck_con=duck_con,
            sid=sid,
            rows=prefetched.get(sid) if sid is not None else None,
        )
        if probs is not None:
            frozen = _insert_snapshot(
                conn,
                stats,
                fixture_id=fixture_id,
                cutoff_at=cutoff_at,
                source="eu_consensus",
                prices=probs,
                observed_at=None,  # 共识是派生态：窗口=cutoff_at，无逐源时刻可记
                sale_state=sale_state,
            )
            if frozen:
                stats.eu_frozen += 1
            else:
                stats.duplicates += 1
        else:
            stats.eu_absent += 1
    conn.commit()
    return stats
