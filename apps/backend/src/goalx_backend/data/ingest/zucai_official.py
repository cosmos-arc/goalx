"""
传统足彩官方采集（票 68）：lottery 族端点 → pool 域表（任9/14场/4场进球+彩果）。

替代判死的源B zucai（2026-09-25 深夜用户裁决：全转官方含彩果；源B 判死
弃用留档）。端点实测（2026-09-26 凌晨，票 68 落地前复查）：

- **当期** ``getFootBallMatchV1.qry``（``param=90,0&sellStatus=0&termLimits=10``；
  Referer=传足计算器页）——一响应含 sfc/bqc/jqc 三玩法在售对阵
  （``{game}Match.matchList``）+ 上期彩果 ``lastPoolDraw``/``lastPoolDrawRj``
  （任9）。**lotteryDrawNum 参数实测被端点忽略**（三种组合同响应）——
  历史回填不在此端点。
- **历史** ``getFootBallDrawInfoByDrawNumV2.qry``（``isVerify=1&lotteryGameNum={g}``
  ``&lotteryDrawNum={n}``；Referer=开奖公告页 + **Origin 头必须**——CORS 闸）
  ——逐期完整彩果：
  14 场对阵（含比分/开奖码 ``lotteryDrawResult``）+ 奖级注数/奖金 +
  销量/奖池（14场与任9 双份）。**存档深至 2007（07001 实测有数据）**。
- 游戏号族谱：90=胜负游戏（14场，任9 同期同场次沿 zucai 先例 ttt14 一份
  对阵）；94=4场进球（goals4 首采）；98=6场半全场（留档不消费——V1 响应
  原文自带，不建消费链）。

口径：

- ``infohubMatchId`` 与 uniform 赛果 ``match_id`` 同族（204xxxx 实测同段）——
  入 ``pool_matches.source_match_id``，官方层身份可精确 join（spec 69
  story 18）。
- ``startTime`` 官方只供日期（源B 时代有完整时刻）——kickoff 落 CST 当日
  00:00 的 UTC ISO（日期精度，消费端 fixture 名+时间匹配降级为可容忍）。
- Q8 published 注数落点：``prize_tiers`` json = ``{"sfc": {...}, "rj": {...}}``
  （奖级 stakeCount/stakeAmount/prizeAmount + 销量/奖池），替代 AI 代采
  与源B 人气份额（人气随源B 死亡无源，官方口径已覆盖 published 面）。
- 幂等：period/matches 当前态刷新、states 同值跳过异值覆盖；历史回填按
  "该期已有官方 prize_tiers" 跳过。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store

SOURCE = "sporttery-official"
PARSE_VERSION = "zucai_official_v1"
CURRENT_URL = "https://webapi.sporttery.cn/gateway/lottery/getFootBallMatchV1.qry"
DRAW_INFO_URL = (
    "https://webapi.sporttery.cn/gateway/lottery/getFootBallDrawInfoByDrawNumV2.qry"
)
CURRENT_REFERER = "https://www.sporttery.cn/ctzc/jsq/index.html"
DRAW_REFERER = "https://www.sporttery.cn/ctzc/kjgg/"
CST = timezone(timedelta(hours=8))
# 14场=任9 同期同场次（zucai 先例 ttt14 一份对阵）；4场进球独立市场
MARKET_SFC = "ttt14"
MARKET_JQC = "goals4"
GAME_SFC = "90"
GAME_JQC = "94"
# 历史回填请求间距=20/min 等效（spec 69 story 12：官方采集同 20/min 护栏）
BACKFILL_SLEEP_SECONDS = 3.2


def _headers(referer: str) -> dict[str, str]:
    """同域同头直通（Origin=开奖族 CORS 闸，实测缺则空响应）。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "Referer": referer,
        "Origin": "https://www.sporttery.cn",
        "Accept": "application/json, text/plain, */*",
    }


def fetch_current(client: httpx.Client, settings: Settings) -> dict[str, Any]:
    """当期响应（在售对阵 + 上期彩果）；success=false 抛 RuntimeError。"""
    response = client.get(
        settings.zucai_official_current_url or CURRENT_URL,
        params={
            "param": "90,0",
            "lotteryDrawNum": "",
            "sellStatus": "0",
            "termLimits": "10",
        },
        headers=_headers(CURRENT_REFERER),
        timeout=25.0,
    )
    response.raise_for_status()
    payload = cast("dict[str, Any]", response.json())
    if not payload.get("success"):
        msg = f"足彩当期端点返回失败: {payload.get('errorMessage')}"
        raise RuntimeError(msg)
    return cast("dict[str, Any]", payload.get("value") or {})


def fetch_draw_info(
    client: httpx.Client, settings: Settings, game_num: str, draw_num: str
) -> dict[str, Any]:
    """一期历史彩果响应（对阵+比分+奖级+销量/奖池）。"""
    response = client.get(
        settings.zucai_official_draw_url or DRAW_INFO_URL,
        params={
            "isVerify": "1",
            "lotteryGameNum": game_num,
            "lotteryDrawNum": draw_num,
        },
        headers=_headers(DRAW_REFERER),
        timeout=25.0,
    )
    response.raise_for_status()
    payload = cast("dict[str, Any]", response.json())
    if not payload.get("success"):
        msg = (
            f"足彩历史端点返回失败({game_num}/{draw_num}):"
            f" {payload.get('errorMessage')}"
        )
        raise RuntimeError(msg)
    return cast("dict[str, Any]", payload.get("value") or {})


def _money(raw: object) -> float | None:
    """'25,162,994' → 25162994.0；空/坏值 None。"""
    if raw is None:
        return None
    text = str(raw).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cst_naive_to_utc(text: object) -> str | None:
    """'YYYY-MM-DD[ HH:MM:SS]'（北京钟面）→ UTC ISO；日期精度落当日 00:00。"""
    if not isinstance(text, str) or not text.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return (
                datetime.strptime(text.strip(), fmt)
                .replace(tzinfo=CST)
                .astimezone(UTC)
                .isoformat(timespec="seconds")
            )
        except ValueError:
            continue
    return None


def _tier_rows(prize_list: object) -> list[dict[str, object]]:
    """奖级数组 → 归一行（level/stake_count/stake_amount/prize_amount）。"""
    rows: list[dict[str, object]] = []
    if not isinstance(prize_list, list):
        return rows
    for tier in cast("list[dict[str, Any]]", prize_list):
        rows.append(
            {
                "level": str(tier.get("prizeLevel") or ""),
                "stake_count": _money(tier.get("stakeCount")),
                "stake_amount": _money(
                    tier.get("stakeAmountFormat") or tier.get("stakeAmount")
                ),
                "prize_amount": _money(tier.get("totalPrizeamount")),
            }
        )
    return rows


def _game_state(
    game: dict[str, Any], *, market: str = MARKET_SFC, rj: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """一玩法彩果面 → prize_tiers json 内容（键随市场；ttt14 另并任9 面）。"""
    tiers = _tier_rows(game.get("prizeLevelList"))
    if not tiers:
        return None
    face = "jqc" if market == MARKET_JQC else "sfc"
    state: dict[str, Any] = {
        face: {
            "total_sale": _money(game.get("totalSaleAmount")),
            "pool_after": _money(
                game.get("poolBalanceAfterDraw") or game.get("poolBalanceAfterdraw")
            ),
            "tiers": tiers,
        }
    }
    if rj is not None:
        rj_tiers = _tier_rows(rj.get("prizeLevelList"))
        if rj_tiers:
            state["rj"] = {
                "total_sale": _money(rj.get("totalSaleAmount")),
                "pool_after": _money(
                    rj.get("poolBalanceAfterDraw") or rj.get("poolBalanceAfterdraw")
                ),
                "tiers": rj_tiers,
            }
    return state


@dataclass
class OfficialPeriod:
    """一期官方口径数据（market 维度归一；matches 可缺=仅彩果面）。"""

    market: str
    period_no: str
    sales_deadline_utc: str | None = None
    published_at: str | None = None
    matches: list[pool_store.PoolMatchInput] = field(default_factory=list)
    sales_amount: float | None = None
    rollover_in: float | None = None
    prize_tiers: dict[str, Any] | None = None


def parse_match_list(match_list: object) -> list[pool_store.PoolMatchInput]:
    """在售/历史 matchList → 对阵行（infohubMatchId=uniform 同族身份）。"""
    rows: list[pool_store.PoolMatchInput] = []
    if not isinstance(match_list, list):
        return rows
    for row in cast("list[dict[str, Any]]", match_list):
        seq = row.get("matchNum")
        home = row.get("masterTeamAllName") or row.get("masterTeamName")
        away = row.get("guestTeamAllName") or row.get("guestTeamName")
        if seq is None or not home or not away:
            continue
        rows.append(
            pool_store.PoolMatchInput(
                match_seq=int(seq),
                source_match_id=(
                    str(row["infohubMatchId"]) if row.get("infohubMatchId") else None
                ),
                league=str(row.get("matchName") or ""),
                kickoff_utc=_cst_naive_to_utc(row.get("startTime"))
                or datetime.now(UTC).isoformat(timespec="seconds"),
                home_team=str(home),
                away_team=str(away),
                euro_odds=(
                    None,
                    None,
                    None,
                ),  # 官方无欧指（源B 时代的页面值随判死消失）
            )
        )
    rows.sort(key=lambda m: m.match_seq)
    return rows


def parse_current(value: dict[str, Any]) -> list[OfficialPeriod]:
    """
    当期响应 → 期次清单（纯函数）。

    sfc 在售（有对阵）+ jqc 在售（开售发现）+ 上期彩果（lastPoolDraw 并
    lastPoolDrawRj 任9 面）。
    """
    periods: list[OfficialPeriod] = []
    sfc = cast("dict[str, Any]", value.get("sfcMatch") or {})
    if sfc.get("matchList"):
        periods.append(_period_from_game(MARKET_SFC, sfc))
    jqc = cast("dict[str, Any]", value.get("jqcMatch") or {})
    if jqc.get("matchList"):
        periods.append(_period_from_game(MARKET_JQC, jqc))
    last = cast("dict[str, Any]", sfc.get("lastPoolDraw") or {})
    if last.get("lotteryDrawNum"):
        periods.append(
            _period_from_game(MARKET_SFC, last, rj=sfc.get("lastPoolDrawRj") or None)
        )
    return periods


def _period_from_game(
    market: str, game: dict[str, Any], *, rj: dict[str, Any] | None = None
) -> OfficialPeriod:
    """一玩法 game 对象（V1 当期/上期或 V2 历史形态）→ OfficialPeriod。"""
    period = OfficialPeriod(
        market=market,
        period_no=str(game.get("lotteryDrawNum") or ""),
        sales_deadline_utc=_cst_naive_to_utc(game.get("lotterySaleEndtime")),
        published_at=_cst_naive_to_utc(game.get("lotteryDrawTime")),
        matches=parse_match_list(game.get("matchList")),
    )
    face = "jqc" if market == MARKET_JQC else "sfc"
    tiers = _game_state(game, market=market, rj=rj)
    if tiers is not None:
        period.prize_tiers = tiers
        period.sales_amount = tiers[face]["total_sale"]
        period.rollover_in = tiers[face]["pool_after"]
    return period


def parse_draw_info(market: str, value: dict[str, Any]) -> OfficialPeriod | None:
    """V2 历史响应 → OfficialPeriod（空期/无对阵 None——jqc 大多数期无数据）。"""
    if not value.get("lotteryDrawNum") or not value.get("matchList"):
        return None
    period = _period_from_game(market, value)
    # V2 的任9 面只有金额无奖级（totalSaleAmountRj 等）——并进 sfc 面留档
    if period.prize_tiers is not None:
        rj_sale = _money(value.get("totalSaleAmountRj"))
        if rj_sale is not None:
            period.prize_tiers["rj_amounts"] = {
                "total_sale": rj_sale,
                "pool_after": _money(value.get("poolBalanceAfterdrawRj")),
            }
    return period


@dataclass
class PoolSyncStats(pool_store.PoolSyncStats):
    """一次官方同步统计（复用仓储形状，附来源常量与解析版本默认值）。"""

    source: str = SOURCE
    parse_version: str = PARSE_VERSION


def _ingest_period(
    conn: sqlite3.Connection, period: OfficialPeriod, stats: PoolSyncStats
) -> None:
    """一期 → pool 三表（期次 deadline 保留既有值——彩果面常无销售窗）。"""
    deadline = period.sales_deadline_utc
    if deadline is None:
        existing = pool_store.pool_period_id(conn, period.market, period.period_no)
        if existing is not None:
            deadline = pool_store.pool_period_deadline(conn, existing)
    pool_period_id = pool_store.upsert_pool_period(
        conn, period.market, period.period_no, deadline
    )
    if period.matches:
        stats.matches += pool_store.replace_pool_matches(
            conn, pool_period_id, period.matches
        )
    if period.prize_tiers is not None:
        pool_store.upsert_pool_state(
            conn,
            pool_period_id,
            sales_amount=period.sales_amount,
            rollover_in=period.rollover_in,
            prize_tiers=period.prize_tiers,
            published_at=period.published_at,
            source=SOURCE,
        )
    if period.period_no not in stats.period_nos:
        stats.period_nos.append(period.period_no)


def _has_official_state(conn: sqlite3.Connection, market: str, period_no: str) -> bool:
    """该期是否已有官方 prize_tiers（回填跳过判据；AI 代采无 tiers 不算）。"""
    pool_period_id = pool_store.pool_period_id(conn, market, period_no)
    if pool_period_id is None:
        return False
    state = pool_store.pool_state_for_period(conn, pool_period_id)
    return state is not None and state["prize_tiers"] is not None


def sync_current(
    conn: sqlite3.Connection, settings: Settings, client: httpx.Client
) -> PoolSyncStats:
    """当期同步：在售对阵刷新 + 上期彩果落 state（幂等；三拍调度入口）。"""
    stats = PoolSyncStats(observed_at=datetime.now(UTC).isoformat(timespec="seconds"))
    value = fetch_current(client, settings)
    stats.pages += 1
    for period in parse_current(value):
        _ingest_period(conn, period, stats)
    pool_store.record_pool_sync_run(conn, stats)
    return stats


def backfill_draws(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    periods: int = 20,
    start: str | None = None,
    games: tuple[str, ...] = (GAME_SFC, GAME_JQC),
    sleeper: float = BACKFILL_SLEEP_SECONDS,
) -> PoolSyncStats:
    """
    历史彩果回填：从当期期号（或 start）逐期倒查 V2，落官方 state。

    已有官方 prize_tiers 的期跳过（幂等重跑零请求耗在已落期）；jqc 大多数
    期无数据（空响应跳过留痕于计数）。一次坐十年 ≈1000 期 ×2 玩法 ≈2000
    请求，官方域低频礼貌推进。
    """
    stats = PoolSyncStats(observed_at=datetime.now(UTC).isoformat(timespec="seconds"))
    start_no = start
    if start_no is None:
        value = fetch_current(client, settings)
        stats.pages += 1
        start_no = str(
            (cast("dict[str, Any]", value.get("sfcMatch") or {})).get("lotteryDrawNum")
            or ""
        )
        if not start_no:
            msg = "当期响应无期号，回填起点未知"
            raise RuntimeError(msg)
    market_of = {GAME_SFC: MARKET_SFC, GAME_JQC: MARKET_JQC}
    empty_periods = 0
    for offset in range(periods):
        period_no = f"{int(start_no) - offset:05d}"
        got_any = False
        for game in games:
            market = market_of[game]
            if _has_official_state(conn, market, period_no):
                continue
            value = fetch_draw_info(client, settings, game, period_no)
            stats.pages += 1
            period = parse_draw_info(market, value)
            if period is None:
                continue
            _ingest_period(conn, period, stats)
            got_any = True
            if sleeper:
                time.sleep(sleeper)
        if not got_any:
            empty_periods += 1
    if empty_periods:
        logger.info("zucai official 回填：{} 期无数据（老期/无 jqc）", empty_periods)
    pool_store.record_pool_sync_run(conn, stats)
    return stats


def stats_dict(stats: PoolSyncStats) -> dict[str, Any]:
    """统计 → 可 JSON 化 dict（flow 返回值/日志用）。"""
    return {
        "source": stats.source,
        "observed_at": stats.observed_at,
        "period_nos": stats.period_nos,
        "pages": stats.pages,
        "matches": stats.matches,
        "share_rows": stats.share_rows,
        "missing_shares": stats.missing_shares,
    }
