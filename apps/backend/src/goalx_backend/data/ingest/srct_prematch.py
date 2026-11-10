"""
源T 赛前拍点（autoresearch 票 14）——JC 在售场 detail 页赛前快照。

−150/−90/−60/−30min 四档（E-15 阵容线前向真值供给）。

E-15 阵容线冻结的重开条件=名单首次公开时刻的**前向真值**——库内 detail
捕获 100% 赛后（2026-09 以来 420 条赛前=0，2026-11-10 只读实测），本模块
从今晚起供给该真值：starters 从空→11 人齐的最早 observed_at 即首次公开
时刻。冻结词不动（禁事后子集挖掘、无真值不翻案）——本票只供前向读数，
判决词按账本如实落。

**候选面**：JC 业务日板上场（今日页在售足球场）已链源T sid 的场
（jc_prematch_candidates，主库 fixtures/match_codes/mapping 归属函数拼装）。

**拍窗**：四档各占 (n−30, n] 分钟窗（档间 30min 空档是离散拍点设计，
非连续采样）；当期班 */30 cadence 下每档恰一拍。01:00-08:00 家宽夜窗随
当期班让位（凌晨场的 −90/−60/−30 可能结构性缺席——读数按可观测面如实，
晚间场公布窗 −75±30min 全程在拍）。

**落库**：复用既有 match_detail bronze 数据集 + detail 抓取/解析器
（fetch_detail_page + parse_detail_dispatch，WAF 白名单客户端与 30/min
滑窗共用当期班同一进程实例）；raw 键 `{sid}@pre-{n}`（与夜班裸键、既有
@open/@daily/@close 拍键互不占位），bronze 行 sid=裸 sid、fetched_at=
抓取时刻（observed_at PIT 语义，晚到不回灌、不倒填名义档时刻）。夜班
collect_day 赛后收口裸键行晚于一切赛前拍落库——latest-per-sid 收敛到
终版页，赛前拍行原序保留供读数。拍失败不占键（下轮自愈）；夜班已收口
（裸键在）或已开球的候选跳过。失败不阻塞当期班其余相位（隔离见
srct_shift.run_shift）。

预算：≈5–8 场/日 × 4 档 ≈ 32 请求/日，与当期班共用 NightBudget 与滑窗
（同一 run_shift 调用内顺序执行，无跨进程叠加）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
from limits.strategies import MovingWindowRateLimiter
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct
from goalx_backend.rate_limit import throttle

# 四档拍点（分钟）：−150 首档窗上界；E-15 公布窗先验 −75±30min 落在
# −90/−60 档邻域，−30 档兜公布后残差
PREMATCH_LEADS: tuple[int, ...] = (150, 90, 60, 30)
# 每档拍窗宽度（分钟）：窗 (n−30, n]，*/30 cadence 下恰一拍
PREMATCH_WINDOW_MINUTES = 30
# 拍键中缀（corpus_store.prematch_snapshot_face 的 LIKE 模式与此同源钉死）
PREMATCH_KEY_INFIX = "@pre-"
# 首发齐判（足球队首发 11 人；两侧同齐才算「名单已公开」的保守读数）
_STARTERS_COMPLETE = 11


def prematch_key(sid: str, lead_minutes: int) -> str:
    """赛前拍 raw 键（一档一键；与裸键/@open/@daily/@close 互不占位）。"""
    return f"{sid}{PREMATCH_KEY_INFIX}{lead_minutes}"


def due_prematch_lead(kickoff: datetime, now: datetime) -> int | None:
    """
    当前时刻命中的拍档（分钟）；无命中 None。

    档窗 (n−30, n]（含上界、不含下界）：边界不双中；开球已过/超首档窗
    不拍。naive 输入按 UTC 解释（运行面 kickoff 恒带偏移，防御同
    fx_store.beijing_naive 惯例）。
    """
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    lead = (kickoff - now).total_seconds() / 60
    if lead <= 0:
        return None
    for n in PREMATCH_LEADS:
        if n - PREMATCH_WINDOW_MINUTES < lead <= n:
            return n
    return None


def lineup_complete(payload: Mapping[str, object]) -> bool:
    """两侧首发各满 11（快照读数的「名单已公开」保守判据）。"""
    lineup = cast("dict[str, list[object]]", payload.get("lineup") or {})
    return (
        len(lineup.get("home_starters") or []) >= _STARTERS_COMPLETE
        and len(lineup.get("away_starters") or []) >= _STARTERS_COMPLETE
    )


@dataclass
class PrematchStats:
    """一次赛前拍运行的统计（并入当期班摘要口径）。"""

    candidates: int = 0  # 候选场数（JC 已链、未开球）
    due: int = 0  # 落在拍窗内的候选场数
    beats: int = 0  # 本轮落库快照数
    full_xi: int = 0  # 快照中两侧首发已齐数（首次公开时刻读数的日频面）
    skipped_final: int = 0  # 夜班已收口（裸键在）跳过数
    requests: int = 0
    raw_new: int = 0
    parsed_ok: int = 0
    failed: dict[str, str] = field(default_factory=dict)


def run_prematch_beats(  # noqa: PLR0913 接缝集与当期班拍同构（防封/预算/统计一路传参）
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    candidates: Sequence[tuple[str, datetime]],
    *,
    now: datetime,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: srct.NightBudget | None = None,
) -> PrematchStats:
    """
    推进一轮赛前拍：逐候选判档 → 未拍即抓 → raw+bronze append。

    幂等：每档以 raw 键查 checkpoint，已拍零重抓；失败拍无键下轮自愈
    （拍窗未过即可补）。budget 触顶抛 NightStop（进度已落库，调方统一接）。
    observed_at=抓取时刻（PIT），不倒填名义档时刻。
    """
    stats = PrematchStats(candidates=len(candidates))
    for sid, kickoff in candidates:
        if store.has(srct.SRCT_PROVIDER, srct.DETAIL_DATASET, sid):
            stats.skipped_final += 1  # 夜班已收口终版页：赛前拍无增量
            continue
        lead = due_prematch_lead(kickoff, now)
        if lead is None:
            continue
        stats.due += 1
        key = prematch_key(sid, lead)
        if store.has(srct.SRCT_PROVIDER, srct.DETAIL_DATASET, key):
            continue
        throttle(limiter, srct.REQUEST_WINDOW, sleeper)
        if budget is not None:
            budget.charge(1)
        stats.requests += 1
        try:
            page = srct.fetch_detail_page(client, settings, sid)
            payload = srct.parse_detail_dispatch(page)
        except (httpx.HTTPError, srct.SrctContentError) as exc:
            stats.failed[key] = f"{type(exc).__name__}: {exc}"[:120]
            logger.warning("srct prematch {} failed ({})", key, exc)
            continue
        observed_at = srct.utc_now_iso()
        store.ingest_raw(
            srct.SRCT_PROVIDER, srct.DETAIL_DATASET, key, page, ext=".html"
        )
        stats.raw_new += 1
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.DETAIL_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.DETAIL_DATASET,
                    "sid": sid,
                    "fetched_at": observed_at,
                    "parser_version": srct.BRONZE_VERSIONS[srct.DETAIL_DATASET],
                    "raw_sha": store.raw_sha(
                        srct.SRCT_PROVIDER, srct.DETAIL_DATASET, key
                    ),
                    "payload": payload,
                }
            ],
        )
        stats.parsed_ok += 1
        stats.beats += 1
        if lineup_complete(payload):
            stats.full_xi += 1
    return stats


def jc_prematch_candidates(
    conn: sqlite3.Connection, now: datetime
) -> list[tuple[str, datetime]]:
    """
    JC 板上场次的已链源T候选（票 14 拍面）：[now, now+150min] 开球场。

    主库归属函数拼装（fixtures/match_codes/mapping，ADR-0008——无内联
    SQL）；未链场不进候选（无 sid 可拍，等映射同步自愈）。含已停售场
    （停售时刻配对是离线读数，拍点只管真值供给）。
    """
    horizon_from = now.isoformat(timespec="seconds")
    horizon_to = (now + timedelta(minutes=max(PREMATCH_LEADS))).isoformat(
        timespec="seconds"
    )
    business_dates = tuple(
        fx_store.beijing_business_date(now + timedelta(days=offset))
        for offset in (-1, 0, 1)
    )
    rows = fx_store.jc_board_rows_in_horizon(
        conn,
        business_dates=business_dates,
        kickoff_from_iso=horizon_from,
        kickoff_to_iso=horizon_to,
    )
    sids = mapping.srct_sids_for_fixtures(conn, [int(row["fid"]) for row in rows])
    out: list[tuple[str, datetime]] = []
    for row in rows:
        sid = sids.get(int(row["fid"]))
        if sid is None:
            continue
        kickoff = datetime.fromisoformat(str(row["kickoff"]))
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)
        out.append((sid, kickoff))
    return out
