"""
卫报新闻语料采集（票 79）：LLM 信息层 P0.5 地基（用户已批，抽取器挂起）。

open-platform 免费 developer 档：~500 请求/日、~1 rps、非商用。football 段
全量 203,080 篇（2026-09-27 实测），page-size 200 ≈1,016 页 → 500/日预算
约 2 日拉完；之后日增量 1-2 请求。直连可用（fake-ip TUN 放行，无需代理
env），与源T 体系不同主机零共享限流。

分页稳定性：搜索默认新→旧排序，回填期间新文插入会把后续页整体前推
（漂移=缺页/重页）——回填态锚定 ``to-date``（首跑冻结）+ ``order-by=oldest``
（旧→新），新增内容落在锚之后不扰动；日增量态窗口 1-2 页无漂移问题，
新→旧直取。

- raw-first：响应 JSON gzip+sha（``raw/guardian/search/``，许可=key 持有者
  自用、raw 本地、禁再分发——同源T 政策不进 repo）；
- bronze ``news_article``（``guardian_news_v1``，一行一文）：id/
  webPublicationDate/lastModified/webTitle/webUrl/section/bodyText——发布
  与修改时间双存原样（定则 2/3：修订兜底在评测集消费层，采集不裁决）；
- 节流：请求间距 1s（票面 1 rps）+ 500/日预算账本（404 也记 1）；
- checkpoint 游标=(phase, from/to-date, page, last_completed)——断点续跑
  幂等，崩溃点页重抓 raw 同内容无害，bronze 后行胜出（信封惯例）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso

PROVIDER = "guardian"
RAW_DATASET = "search"
DATASET = "news_article"
BRONZE_VERSION = "guardian_news_v1"

SECTION = "football"
PAGE_SIZE = 200
REQUEST_GAP_SECONDS = 1.0  # 票面 1 rps 礼貌间距
PHASE_BACKFILL = "backfill"
PHASE_DAILY = "daily"

State = dict[str, object]


@dataclass
class GuardianSyncStats:
    """一次同步的统计（budget_exhausted/完成日跳过均诚实置位）。"""

    observed_at: str = ""
    phase: str = PHASE_BACKFILL
    requests: int = 0
    pages: int = 0
    articles: int = 0
    total_articles: int = 0
    budget_exhausted: bool = False
    request_capped: bool = False
    completed: bool = False
    skipped_done_today: bool = False
    raw_bytes: int = 0
    state: dict[str, object] = field(default_factory=dict)


def stats_dict(stats: GuardianSyncStats) -> dict[str, object]:
    """统计 → 日志/CLI 字典。"""
    return dict(stats.__dict__)


def _as_int(value: object) -> int:
    """Object → int（checkpoint 行值取型）。"""
    return int(cast("int", value))


def _as_str(value: object) -> str:
    """Object → str。"""
    return str(cast("str", value))


def fetch_search_page(
    client: httpx.Client,
    settings: Settings,
    *,
    from_date: str,
    to_date: str,
    page: int,
    oldest_first: bool,
) -> dict[str, object]:
    """拉一页搜索响应（非 2xx 抛错由调用方停；key 不进任何日志/异常文本）。"""
    params: dict[str, str | int] = {
        "section": SECTION,
        "from-date": from_date,
        "to-date": to_date,
        "page-size": PAGE_SIZE,
        "page": page,
        "show-fields": "bodyText,lastModified",
        "api-key": settings.guardian_api_key,
    }
    if oldest_first:
        params["order-by"] = "oldest"
    response = client.get(
        f"{settings.guardian_base_url}/search", params=params, timeout=30.0
    )
    response.raise_for_status()
    return cast("dict[str, object]", response.json())


def parse_articles(payload: dict[str, object]) -> list[dict[str, object]]:
    """
    响应 → 文章行（guardian_news_v1 字段面；缺 bodyText 的行诚实置 None）。

    发布时间（webPublicationDate）与修改时间（fields.lastModified）双存
    原样——修订兜底是评测集消费层的事（定则 2/3）。
    """
    response = cast("dict[str, object]", payload.get("response") or {})
    results = cast("list[object]", response.get("results") or [])
    rows: list[dict[str, object]] = []
    for item_raw in results:
        if not isinstance(item_raw, dict):
            continue
        item = cast("dict[str, object]", item_raw)
        fields = cast("dict[str, object]", item.get("fields") or {})
        article_id = _as_str(item.get("id") or "")
        if not article_id:
            continue
        rows.append(
            {
                "id": article_id,
                "webPublicationDate": _as_str(item.get("webPublicationDate") or ""),
                "lastModified": _as_str(fields.get("lastModified") or ""),
                "webTitle": _as_str(item.get("webTitle") or ""),
                "webUrl": _as_str(item.get("webUrl") or ""),
                "section": _as_str(item.get("sectionName") or SECTION),
                "bodyText": fields.get("bodyText"),
            }
        )
    return rows


def _raw_key(from_date: str, page: int) -> str:
    return f"{from_date}_p{page:04d}"


def _daily_window(state: State, today: date) -> tuple[str, str]:
    """日增量窗口：上次完成日 + 1 → 今日（缺口日一次补齐）。"""
    last = state.get("last_completed_date")
    start = date.fromisoformat(_as_str(last)) + timedelta(days=1) if last else today
    start = min(start, today)
    return start.isoformat(), today.isoformat()


def _window(state: State, today: date, phase: str) -> tuple[str, str, int, bool]:
    """当前态 → (from, to, page, oldest_first)。回填态锚定 to-date 防漂移。"""
    if phase == PHASE_BACKFILL:
        to_date = _as_str(state["to_date"]) or today.isoformat()
        return _as_str(state["from_date"]), to_date, _as_int(state["page"]), True
    from_date, to_date = _daily_window(state, today)
    page = _as_int(state["page"]) if _as_str(state["from_date"]) == from_date else 1
    return from_date, to_date, page, False


@dataclass(frozen=True)
class _PagePull:
    """一次成功拉页的结果（失败不产生本类型——游标不进）。"""

    total_pages: int
    articles: int
    raw_bytes: int


def _pull_page(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    window: tuple[str, str, int, bool],
    observed_at: str,
    budget_day: str,
) -> _PagePull | None:
    """拉一页并落 raw+bronze+记账；HTTP 失败记账返回 None（断点原地续）。"""
    from_date, to_date, page, oldest_first = window
    try:
        payload = fetch_search_page(
            client,
            settings,
            from_date=from_date,
            to_date=to_date,
            page=page,
            oldest_first=oldest_first,
        )
    except httpx.HTTPError as exc:
        store.increment_guardian_requests(budget_day)
        logger.warning("guardian page fetch failed at page {}: {}", page, exc)
        return None
    store.increment_guardian_requests(budget_day)
    body = json.dumps(payload, ensure_ascii=False).encode()
    raw = store.ingest_raw(PROVIDER, RAW_DATASET, _raw_key(from_date, page), body)
    rows: list[dict[str, object]] = [
        {
            "provider": PROVIDER,
            "dataset": DATASET,
            "sid": _as_str(article["id"]),
            "fetched_at": observed_at,
            "parser_version": BRONZE_VERSION,
            "raw_sha": raw.sha256,
            "payload": article,
        }
        for article in parse_articles(payload)
    ]
    store.append_bronze(PROVIDER, DATASET, rows)
    response = cast("dict[str, object]", payload.get("response") or {})
    return _PagePull(
        total_pages=_as_int(response.get("pages") or 0),
        articles=len(rows),
        raw_bytes=raw.byte_size,
    )


def sync_guardian(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    now: datetime | None = None,
    request_cap: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> GuardianSyncStats:
    """
    顺序翻页同步（断点续跑幂等；预算/单次上限任一触顶即停，游标已进）。

    - 回填态：from=1999-01-01、to=首跑锚定日、oldest-first 深翻页；
      到尾页转 daily（last_completed=锚定日）。
    - 日增量态：窗口=上次完成日+1→今日，新→旧 1-2 页；当日已完成则
      零请求跳过（零成本心跳，与赛果同步同模式）。
    每请求先过预算账本（当日已用 ≥500 → budget_exhausted 停），间距 1s。
    """
    now_dt = now or datetime.now(UTC)
    today = now_dt.date()
    budget_day = today.isoformat()
    stats = GuardianSyncStats(observed_at=utc_now_iso())
    state = store.guardian_state()
    stats.phase = _as_str(state["phase"])
    if not settings.guardian_api_key:
        stats.state = state
        logger.warning("guardian-sync skipped: api key 未配置")
        return stats
    done_today = state.get("last_completed_date") == today.isoformat()
    if stats.phase == PHASE_DAILY and done_today:
        stats.skipped_done_today = True
        stats.state = state
        return stats
    if stats.phase == PHASE_BACKFILL and not state["to_date"]:
        # 锚定日：回填期冻结（新文落在锚后，oldest-first 翻页不漂移）
        state = {**state, "to_date": today.isoformat()}
    while True:
        used = store.guardian_requests(budget_day)
        if used >= settings.guardian_daily_request_budget:
            stats.budget_exhausted = True
            break
        if request_cap is not None and stats.requests >= request_cap:
            stats.request_capped = True
            break
        window = _window(state, today, stats.phase)
        pull = _pull_page(
            store, settings, client, window, stats.observed_at, budget_day
        )
        if pull is None:
            break
        from_date, to_date, page, _ = window
        stats.requests += 1
        stats.pages += 1
        stats.articles += pull.articles
        stats.raw_bytes += pull.raw_bytes
        total = _as_int(state["total_articles"]) + pull.articles
        if page >= pull.total_pages:
            # 回填到尾（锚定日收口）或日增量窗口拉完——都收敛进 daily 形态
            state = {
                "phase": PHASE_DAILY,
                "from_date": from_date,
                "to_date": to_date,
                "page": 1,
                "last_completed_date": to_date,
                "total_articles": total,
            }
            stats.completed = True
            stats.phase = PHASE_DAILY
            break
        state = {
            "phase": stats.phase,
            "from_date": from_date,
            "to_date": to_date,
            "page": page + 1,
            "last_completed_date": state.get("last_completed_date"),
            "total_articles": total,
        }
        store.upsert_guardian_state(state)
        if pull.total_pages > 1:
            sleeper(REQUEST_GAP_SECONDS)
    store.upsert_guardian_state(state)
    stats.total_articles = _as_int(state["total_articles"])
    stats.state = {
        key: state[key]
        for key in ("phase", "from_date", "to_date", "page", "last_completed_date")
    }
    return stats
