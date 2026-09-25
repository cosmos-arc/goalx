"""
JC 当期实时拍（票 71）：在售轮询 getFixedBonusV1，挂当期班四类拍框架。

**发现**：竞彩计算器生产端点（``getMatchCalculatorV1``，复用生产
fetch_calculator）——``value.matchInfoList[].subMatchList[]`` = 在售场次
（matchId 与 uniform/fixedBonus 同族，matchDate+matchTime 北京钟面）。

**四类拍（与源T 当期班同 deployment 同窗调度，spec 69 story 5/12）**：

- **开售拍**：matchId 首见 → fixedBonus 拍键 ``{mid}@open``；
- **每日两拍**：10/22 槽 → ``{mid}@daily-{date}{slot}``；
- **临场拍**：开球前自适应窗（凌晨/早场 12h、晚场 2h——凌晨场停售提前
  3-12h 的已知约束，与源T 同规则）→ ``{mid}@close``；
- **完场收口拍**：开球已过而裸键 raw 缺 → 裸键全量收口（fixedBonus 存档
  永在，SP 冻结于停售，赛后任意时点等价）。

**幂等自愈**（story 6）：oddsHistory 全量返回——连续两拍间隔有 SP 变化
时第二拍拉全量历史零丢失；漏拍/断拍重拉即补，拍天然无缺口语义。

**护栏**：与源T 拍共用同一 NightBudget 与 20/min 滑窗（同一 run_shift
调用内顺序执行；源T 优先，JC 用剩余预算，断拍下轮自愈）。

落库：jc provider（官方权威层，独立数据集）；bronze 行 sid=matchId
（latest-per-sid 收敛到收口行）；raw 拍键与裸键互不占位。建档表
jc_shift_matches（kickoff 取自 calculator；finalized 收口旗）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, srct, srct_shift
from goalx_backend.data.ingest.sporttery import fetch_calculator

# 收口判定缓冲：开球后 30 分钟再收（足球 90'+，SP 冻结于停售故任意时点
# 等价，缓冲只避开赛中请求的无意义消耗）
FINALIZE_GRACE = 30 * 60


@dataclass
class JcShiftStats:
    """一次 JC 拍的统计（并入当期班 run_shift 摘要口径）。"""

    discovered: int = 0  # calculator 在售 matchId 总数
    open_beats: int = 0
    daily_beats: int = 0
    close_beats: int = 0
    finalized: int = 0
    requests: int = 0
    raw_new: int = 0
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JcOnSaleMatch:
    """calculator 在售行（拍决策所需的归一字段）。"""

    match_id: str
    kickoff_utc: str
    league: str
    home: str
    away: str


def parse_on_sale_matches(payload: dict[str, Any]) -> dict[str, JcOnSaleMatch]:
    """Calculator payload → 在售 matchId → 归一行（纯函数；坏行跳过）。"""
    value = cast("dict[str, Any]", payload.get("value") or {})
    out: dict[str, JcOnSaleMatch] = {}
    for day in cast("list[dict[str, Any]]", value.get("matchInfoList") or []):
        for row in cast("list[dict[str, Any]]", day.get("subMatchList") or []):
            match_id = row.get("matchId")
            date_raw, time_raw = row.get("matchDate"), row.get("matchTime")
            if match_id is None or not date_raw or not time_raw:
                continue
            try:
                kickoff = datetime.strptime(
                    f"{date_raw} {time_raw}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=srct_shift.BEIJING)
            except ValueError:
                continue
            out[str(match_id)] = JcOnSaleMatch(
                match_id=str(match_id),
                kickoff_utc=kickoff.astimezone(UTC).isoformat(timespec="seconds"),
                league=str(row.get("leagueAbbName") or ""),
                home=str(row.get("homeTeamAbbName") or ""),
                away=str(row.get("awayTeamAbbName") or ""),
            )
    return out


def open_key(match_id: str) -> str:
    """JC 开售拍 raw 键。"""
    return f"{match_id}@open"


def daily_key(match_id: str, day: str, slot: str) -> str:
    """JC 每日拍 raw 键。"""
    return f"{match_id}@daily-{day}{slot}"


def close_key(match_id: str) -> str:
    """JC 临场拍 raw 键。"""
    return f"{match_id}@close"


def _beat(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    match_id: str,
    key: str,
    stats: JcShiftStats,
    budget: srct.NightBudget | None = None,
) -> bool:
    """一拍：预算计费 → jc.collect_match 带拍键；成功 True。"""
    if budget is not None:
        budget.charge(1)  # 触顶抛 NightStop——run_shift 统一接（进度已落库）
    before = jc.JcCollectStats()
    jc.collect_match(store, settings, client, match_id, stats=before, key=key)
    stats.requests += before.requests
    stats.raw_new += before.raw_new
    for fail_key, message in before.failed.items():
        stats.failed[fail_key] = message
    return not before.failed


def _bump(store: CorpusStore, row: dict[str, object], count: int) -> dict[str, object]:
    """拍后计数刷新；返回刷新行。"""
    row = dict(row)
    row["beats"] = int(cast("int", row.get("beats") or 0)) + count
    row["last_seen_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    store.upsert_jc_shift_match(row)
    return row


def run_jc_beats(  # noqa: C901, PLR0912, PLR0915 拍决策分支随四类拍累加（同 run_night 先例）
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    now: datetime,
    sleeper: Callable[[float], None],
    budget: srct.NightBudget | None = None,
) -> JcShiftStats:
    """
    推进一次 JC 拍：calculator 发现 → 四类拍决策 → jc provider append。

    budget 与源T 共用（同一 NightBudget；源T 优先 JC 殿后）。发现失败
    （calculator 不可用）只留痕不炸整跑——源T 拍已落，JC 下轮补。
    """
    stats = JcShiftStats()
    try:
        if budget is not None:
            budget.charge(1)  # 发现也进共享预算（spec 69 story 12 口径）
        fetched = fetch_calculator(settings, client)
        stats.requests += 1
    except srct.NightStop as stop:
        stats.failed["jc:discovery"] = f"budget: {stop.reason}"
        return stats
    except (httpx.HTTPError, ValueError) as exc:
        stats.failed["jc:discovery"] = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("jc shift 发现失败（{}）", exc)
        return stats
    on_sale = parse_on_sale_matches(fetched.payload)
    stats.discovered = len(on_sale)
    known = store.jc_shift_matches()
    beijing_now = now.astimezone(srct_shift.BEIJING)
    slot = srct_shift.slot_for_hour(beijing_now.hour)
    # 在售拍决策（open/daily/close）
    for match_id, row_on_sale in on_sale.items():
        kickoff = datetime.fromisoformat(row_on_sale.kickoff_utc)
        row = known.get(match_id) or {
            "match_id": match_id,
            "league": row_on_sale.league,
            "home": row_on_sale.home,
            "away": row_on_sale.away,
            "kickoff_utc": row_on_sale.kickoff_utc,
            "beats": 0,
            "finalized": 0,
        }
        row["league"] = row_on_sale.league
        row["kickoff_utc"] = row_on_sale.kickoff_utc
        row["last_seen_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        store.upsert_jc_shift_match(row)
        if not store.has(jc.JC_PROVIDER, jc.SP_DATASET, open_key(match_id)):
            if _beat(
                store, settings, client, match_id, open_key(match_id), stats, budget
            ):
                stats.open_beats += 1
                row = _bump(store, row, 1)
            sleeper(3.2)
            continue  # 开售拍轮不叠拍
        if kickoff <= now:
            continue  # 已开球：等收口
        lead = srct_shift.closing_lead(kickoff)
        if kickoff - now <= lead:
            if not store.has(jc.JC_PROVIDER, jc.SP_DATASET, close_key(match_id)):
                if _beat(
                    store,
                    settings,
                    client,
                    match_id,
                    close_key(match_id),
                    stats,
                    budget,
                ):
                    stats.close_beats += 1
                    row = _bump(store, row, 1)
                sleeper(3.2)
            continue
        if slot is not None:
            key = daily_key(match_id, beijing_now.date().isoformat(), slot)
            if not store.has(jc.JC_PROVIDER, jc.SP_DATASET, key):
                if _beat(store, settings, client, match_id, key, stats, budget):
                    stats.daily_beats += 1
                    row = _bump(store, row, 1)
                sleeper(3.2)
    # 完场收口：开球+缓冲已过、裸键缺、未收口 → 全量收口（fixedBonus 存档在）
    for match_id, row in known.items():
        kickoff_raw = row.get("kickoff_utc")
        if not kickoff_raw or row.get("finalized"):
            continue
        if store.has(jc.JC_PROVIDER, jc.SP_DATASET, match_id):
            # 裸键已在（手工收口/票 70 先采）：只补旗不计数不重抓
            store.upsert_jc_shift_match({**row, "finalized": 1})
            continue
        kickoff = datetime.fromisoformat(str(kickoff_raw))
        if now.timestamp() - kickoff.timestamp() < FINALIZE_GRACE:
            continue
        if _beat(store, settings, client, match_id, match_id, stats, budget):
            stats.finalized += 1
            store.upsert_jc_shift_match({**row, "finalized": 1})
            sleeper(3.2)
    return stats
