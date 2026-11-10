"""
新闻/伤停 PIT 前向夜拍（autoresearch 票 17，账本 §4.1#11）。

账本红线的代码化：**每晚不开=永久缺口**——本模块是唯一不可回填资产的
前向开拍面；**PIT 纪律：observed_at≤决策时点准入，晚到不回灌**。

双轨（票面「回填存量清单+增量通道」）：

- **存量清单轨** = 语料卫报同步（票 79 ``guardian-sync``，09:40 调度）：
  1999 起回填 + 日增量，本模块不改其状态机；
- **增量通道轨** = 本模块夜拍：每晚定拍（挂 daily-wrap 23:30 既有
  deployment，零新增 cron）拉当日窗（缝起点=语料轨完成日−1，保守回看）
  → 落**专表** ``news_article_pit``（match_detail_prematch 分表先例，
  PR#295 独立复核 W1：不混 ``news_article``，防 sid 索引掩蔽语料轨语义）。

拍窗与防洞（增量通道的完整性论证）：

- 窗=[完整日+1, 拍日]，oldest-first（尾增不扰动前页）；完成只标记
  ``last_full_day=拍日−1``——拍日是进行中日，其晚间发布的文章由**明晚
  窗重含拍日**兜住（每晚窗重含昨日=保守完成标记的推论，联合覆盖无洞）。
- 同夜已 ok 重跑=零请求心跳（幂等）；断点=窗起+页游标持久，次夜续页
  不重翻；预算/单拍上限/HTTP 失败诚实停（拍行如实记 stopped/failed）。

PIT 语义：观测纪元取**逐页拉取时刻**（行 fetched_at=该页拉取时刻，
独立复核 F1：拍班共享纪元会把尾页文章记早=前瞻准入，完成时刻共享纪元
则需缓冲 bronze 或放宽崩溃窗，逐页两者皆免且永不早于页内任何
webPublicationDate）；同一文章多拍多行=多观测纪元（append-only，读侧
按 sid 取最早 observed_at 为首次准入时刻）。晚到（发布早于观测）如实
保留 published_at 原样——不倒填、不回灌早期拍。准入判据单一真相源=
``pit_admitted``。非末页短计数（截断/漂移嫌疑）诚实停保留游标
（独立复核 F3——照常翻页会让未达文章永久漏出后续窗）；400 深页帽按
guardian 签名从响应体识别、如实记 ``paging_cap`` 停（独立复核 F2——
实际不可达，运维重播种窗起后自续）。

E-12 扩样面预留（读数面 stub，采集是长跑）：事件→JC 调盘配对读面以
``E12_PAIRING_NOTES`` 钉住口径（n≈785 独立场次才够 55% 功效门），
配对语义=夜拍准入纪元 × jc_shift 调盘拍时刻（observed_at≤调盘时点才算
「可先知」），实现待采集积累后另票。

直播吧/虎扑源：在案评估（票面），不属本模块实现面。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import guardian
from goalx_backend.db import utc_now_iso

PROVIDER = guardian.PROVIDER
# 深翻页帽载体状态码（独立复核 F2；与 guardian._HTTP_BAD_REQUEST 同值——
# 跨模块私有常量不引用，此处钉字面并注释同源性）
_PAGING_CAP_HTTP_STATUS = 400
_PAGING_CAP_BODY_MARK = "paging this far"
# 夜拍专表（分表先例：match_detail_prematch 不混 match_detail）
PIT_DATASET = "news_article_pit"
PIT_RAW_DATASET = "search_pit"
PIT_BRONZE_VERSION = "guardian_news_pit_v1"
# 单拍请求上限（安全阀：缝夜多页也吃不满 500/日账本；稳态 1-2 页/晚）
BEAT_REQUEST_CAP = 50
# 语料轨（票 79）回填总量实测（2026-09-27）：双轨报告的进度估计面常数
EXPECTED_BACKFILL_TOTAL = 203_080
# 拍行状态值域（news_pit_beats.status）
STATUS_OK = "ok"
STATUS_DONE_TODAY = "done_today"
STATUS_NO_KEY = "no_key"
# E-12 扩样面预留（读面 stub：事件→JC 调盘配对的口径钉住，实现待采集
# 积累后另票——n≈785 独立场次才够 55% 功效门，采集是长跑）
E12_PAIRING_NOTES: dict[str, object] = {
    "status": "reserved",
    "pairing": (
        "news_article_pit 首次准入纪元（sid 最早 observed_at）× "
        "jc_shift 调盘拍时刻（beat 时刻序列）——事件先于调盘可观测才算"
        "「可先知」，配对键=fixture×侧"
    ),
    "admission": "observed_at ≤ 决策时点（pit_admitted 单一真相源）",
    "power_gate_n": 785,
    "serving": ("E-12 扩样（缺口 10.6×）/E-15 探针底座/S6（账本 §4.1#11）"),
    "note": "晚到不回灌：晚于决策时点的观测不进该决策的输入面",
}

State = dict[str, object]


def pit_admitted(observed_at: str, asof: datetime) -> bool:
    """
    PIT 准入判据（账本红线单一真相源）：``observed_at ≤ asof`` 准入。

    tz 显式归一：两端必须 aware（Z/偏移原样解析）；naive/非法时间戳
    不可证 → 不准入（与 guardian_silver._parse_instant 同防御口径）。
    调用方传 aware ``asof``（naive asof 按 UTC 解释——运行面拍点恒 aware）。
    """
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed <= asof


@dataclass
class NewsPitBeatStats:
    """一次夜拍的统计（诚实停/跳过均置位；拍行入 news_pit_beats）。"""

    observed_at: str = ""
    beat_date: str = ""
    window_from: str = ""
    window_to: str = ""
    requests: int = 0
    pages: int = 0
    articles: int = 0
    completed: bool = False
    stopped: str | None = None  # cap|budget|http_failure|paging_cap|no_key|done_today
    skipped_done_today: bool = False
    skipped_no_key: bool = False


def stats_dict(stats: NewsPitBeatStats) -> dict[str, object]:
    """统计 → 日志/CLI 字典。"""
    return dict(stats.__dict__)


def _as_str(value: object) -> str:
    """Object → str（checkpoint 行值取型）。"""
    return str(cast("str", value))


def _as_optional_str(value: object) -> str | None:
    """可空值取型（object → str | None）。"""
    return None if value is None else _as_str(value)


def _safe_error(exc: Exception) -> str:
    """异常文本脱敏（URL query 含 api-key——问号后一律截断；guardian 同型）。"""
    return str(exc).split("?")[0]


def seam_start(store: CorpusStore, today: date) -> date:
    """
    首拍窗起点：语料轨覆盖水平线−1 日。

    保守回看：缝夜重叠无害（append-only 读侧按最早 observed_at 去重）；
    无语料轨=今−1。
    """
    corpus = store.guardian_state()
    horizon = _as_optional_str(corpus.get("last_completed_date")) or _as_optional_str(
        corpus.get("to_date")
    )
    if horizon:
        start = date.fromisoformat(horizon) - timedelta(days=1)
        return min(start, today - timedelta(days=1))
    return today - timedelta(days=1)


def _raw_key(beat_date: str, window_from: str, page: int) -> str:
    """夜拍 raw 键：带拍日——每拍 raw 页为不可变观测证据（跨拍不覆写）。"""
    return f"{beat_date}_{window_from}_p{page:04d}"


def _beat_window(state: State) -> tuple[str, int]:
    """当前态 → (窗起, 页游标)：续跑用持久窗起与页码，不从 1 重翻。"""
    window_from = _as_str(state.get("window_from") or "")
    page = int(cast("int", state.get("page") or 1))
    return window_from, page


@dataclass(frozen=True)
class _BeatPagePull:
    """一次成功拉页的结果（失败不产生本类型——游标不进）。"""

    total_pages: int
    articles: int


def _pull_beat_page(  # 接缝集与 guardian._pull_page 同构（防封/预算/统计一路传参）
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    stats: NewsPitBeatStats,
    *,
    page: int,
    budget_day: str,
    clock: Callable[[], str],
) -> _BeatPagePull | str:
    """
    拉一页并落 raw+bronze+账（成功 → 计数入 stats）。

    失败返回停因字符串（``http_failure``/``paging_cap``/``short_page``，
    已记账、stats.requests 已加）由调用方诚实停；成功返回页面对象。
    行 ``fetched_at``=**该页拉取时刻**（逐页观测纪元，独立复核 F1：
    拍班共享纪元会把尾页文章记早=前瞻准入；完成时刻共享纪元则需缓冲
    bronze 或放宽崩溃窗——逐页取时刻两者皆免且永不早于页内发布）。
    """
    try:
        payload = guardian.fetch_search_page(
            client,
            settings,
            from_date=stats.window_from,
            to_date=stats.window_to,
            page=page,
            oldest_first=True,
        )
    except httpx.HTTPStatusError as exc:
        store.increment_guardian_requests(budget_day)
        stats.requests += 1
        # 深翻页帽（独立复核 F2）：fetch_search_page 走 raise_for_status，
        # 只会抛 HTTPStatusError——按 guardian 同签名（400 + "paging this
        # far" 文案）从响应体识别。单窗超 ~190 页（≈5 年窗）实际不可达，
        # 但如实记 paging_cap（晨检黄面可见）、cursor 保留，运维重播种
        # 窗起（手工改 checkpoint news_pit_state.window_from）后自续。
        if (
            exc.response.status_code == _PAGING_CAP_HTTP_STATUS
            and _PAGING_CAP_BODY_MARK in exc.response.text
        ):
            return "paging_cap"
        logger.warning("news-pit page {} failed: {}", page, _safe_error(exc))
        return "http_failure"
    except httpx.HTTPError as exc:
        store.increment_guardian_requests(budget_day)
        stats.requests += 1
        logger.warning("news-pit page {} failed: {}", page, _safe_error(exc))
        return "http_failure"
    store.increment_guardian_requests(budget_day)
    stats.requests += 1
    response = cast("dict[str, object]", payload.get("response") or {})
    total_pages = int(cast("int", response.get("pages") or 0))
    results = cast("list[object]", response.get("results") or [])
    # 截断防御（独立复核 F3）：非末页行数必须满页（page-size=200）——
    # 短计数=截断/漂移，若照常翻页会让未达文章永久漏出所有后续窗
    # （关窗即不可回补）。诚实停、cursor 保留、次夜重拉该页自愈。
    if page < total_pages and len(results) < guardian.PAGE_SIZE:
        logger.warning(
            "news-pit page {}/{} 短计数 {}/{}（截断/漂移嫌疑）——诚实停保留游标",
            page,
            total_pages,
            len(results),
            guardian.PAGE_SIZE,
        )
        return "short_page"
    page_observed_at = clock()  # 逐页观测纪元（≥该页任何 webPublicationDate）
    body = json.dumps(payload, ensure_ascii=False).encode()
    raw = store.ingest_raw(
        PROVIDER,
        PIT_RAW_DATASET,
        _raw_key(stats.beat_date, stats.window_from, page),
        body,
    )
    articles = guardian.parse_articles(payload)
    rows: list[dict[str, object]] = [
        {
            "provider": PROVIDER,
            "dataset": PIT_DATASET,
            "sid": _as_str(article["id"]),
            "fetched_at": page_observed_at,
            "parser_version": PIT_BRONZE_VERSION,
            "raw_sha": raw.sha256,
            "payload": article,
        }
        for article in articles
    ]
    store.append_bronze(PROVIDER, PIT_DATASET, rows)
    return _BeatPagePull(
        total_pages=total_pages,
        articles=len(rows),
    )


def run_news_pit_beat(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    now: datetime | None = None,
    request_cap: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], str] = utc_now_iso,
) -> NewsPitBeatStats:
    """
    推进一次夜拍：窗口翻页 → raw+bronze append → 状态/拍行落账。

    幂等：同夜已 ok 重跑零请求心跳（守卫先于 no_key——独立复核 F4：
    no_key 先落行会覆写 ok 状态致次日假黄）；失败拍游标不进（次夜/
    重试自续）。预算任一触顶或 HTTP/截断失败诚实停（stopped 置因）。
    clock 注入口只服务测试接缝（逐页观测纪元的时间源）。
    """
    now_dt = now or datetime.now(UTC)
    today = now_dt.date()
    stats = NewsPitBeatStats(
        observed_at=clock(),
        beat_date=today.isoformat(),
        window_to=today.isoformat(),
    )
    tonight = store.news_pit_beat(stats.beat_date)
    if tonight is not None and _as_str(tonight["status"]) == STATUS_OK:
        # 同夜 ok 心跳：零请求原样返回（不落新拍行——ok 行即该夜记录；
        # 覆写成 done_today/no_key 会让晨检读到非 ok 假降级）
        stats.skipped_done_today = True
        stats.stopped = "done_today"
        return stats
    if not settings.guardian_api_key:
        stats.stopped = "no_key"
        stats.skipped_no_key = True
        _record_beat(store, stats, STATUS_NO_KEY)
        logger.warning("news-pit beat skipped: api key 未配置")
        return stats
    state = store.news_pit_state()
    window_from, page = _beat_window(state)
    stats.window_from = window_from or seam_start(store, today).isoformat()
    page = page if window_from else 1
    cap = BEAT_REQUEST_CAP if request_cap is None else request_cap
    budget_day = today.isoformat()
    status = STATUS_OK
    while True:
        if (
            store.guardian_requests(budget_day)
            >= settings.guardian_daily_request_budget
        ):
            stats.stopped = stats.stopped or "budget"
            status = stats.stopped
            break
        if stats.requests >= cap:
            stats.stopped = stats.stopped or "cap"
            status = stats.stopped
            break
        pull = _pull_beat_page(
            store,
            settings,
            client,
            stats,
            page=page,
            budget_day=budget_day,
            clock=clock,
        )
        if isinstance(pull, str):
            stats.stopped = pull
            status = pull if pull != "http_failure" else "failed"
            break
        stats.pages += 1
        stats.articles += pull.articles
        # 增量按本页行数（stats.articles 是本拍累计——直接加会跨页重复计）
        total_so_far = (
            int(cast("int", state.get("total_articles") or 0)) + pull.articles
        )
        if page >= pull.total_pages:
            stats.completed = True
            # 保守完成标记：拍日进行中，只记到拍日−1（明晚窗重含拍日兜尾）
            state = {
                "window_from": today.isoformat(),
                "page": 1,
                "last_full_day": (today - timedelta(days=1)).isoformat(),
                "total_articles": total_so_far,
            }
            break
        page += 1
        state = {
            "window_from": stats.window_from,
            "page": page,
            "last_full_day": state.get("last_full_day"),
            "total_articles": total_so_far,
        }
        store.upsert_news_pit_state(state)
        sleeper(guardian.REQUEST_GAP_SECONDS)
    store.upsert_news_pit_state(state)
    _record_beat(store, stats, status)
    return stats


def _record_beat(store: CorpusStore, stats: NewsPitBeatStats, status: str) -> None:
    """拍行落账（同夜重试按 beat_date 累加计数、状态以最新为准）。"""
    store.record_news_pit_beat(
        {
            "beat_date": stats.beat_date,
            "observed_at": stats.observed_at,
            "window_from": stats.window_from,
            "window_to": stats.window_to,
            "pages": stats.pages,
            "requests": stats.requests,
            "articles": stats.articles,
            "status": status,
        }
    )


def dual_track_face(
    store: CorpusStore,
    settings: Settings | None = None,
    *,
    today: date | None = None,
) -> dict[str, object]:
    """
    卫报双轨报告（票 17 票面验收面）：回填存量清单 + 增量通道双轨只读面。

    - **存量清单轨**（语料 guardian-sync，票 79）：游标/当日预算/进度
      估计（ingested ÷ 实测 203,080 常数——估计面，非精确承诺）；
    - **增量通道轨**（本模块夜拍）：最近 7 夜拍连续性（ok 数/缺夜清单
      ——每晚不开=永久缺口，缺夜可见性优先）+ 累计观测行数；
    - ``pit_rule``：PIT 纪律一行（准入判据挂靠 pit_admitted 单一真相源）。
    """
    today_d = today or datetime.now(UTC).date()
    corpus = store.guardian_state()
    ingested = int(cast("int", corpus.get("total_articles") or 0))
    backfill: dict[str, object] = {
        "phase": _as_str(corpus["phase"]),
        "cursor": {
            "from_date": corpus.get("from_date"),
            "to_date": corpus.get("to_date"),
            "page": corpus.get("page"),
        },
        "last_completed_date": corpus.get("last_completed_date"),
        "total_articles": ingested,
        "requests_today": store.guardian_requests(today_d.isoformat()),
        "budget": (
            settings.guardian_daily_request_budget
            if settings is not None
            else 500  # Settings 缺省（只读面不强制传）
        ),
        "estimated_progress": {
            "ingested": ingested,
            "expected_total": EXPECTED_BACKFILL_TOTAL,
            "fraction": round(ingested / EXPECTED_BACKFILL_TOTAL, 4),
        },
    }
    pit_state = store.news_pit_state()
    beat_dates = store.news_pit_beat_dates()
    window_start = today_d - timedelta(days=6)
    expected_start = (
        max(window_start, date.fromisoformat(min(beat_dates)))
        if beat_dates
        else window_start
    )
    expected_nights = {
        (expected_start + timedelta(days=offset)).isoformat()
        for offset in range((today_d - expected_start).days)
    }
    recent_rows = [
        row
        for row in store.news_pit_beats(limit=14)
        if str(row["beat_date"]) >= window_start.isoformat()
    ]
    forward: dict[str, object] = {
        "last_full_day": pit_state.get("last_full_day"),
        "window_from": pit_state.get("window_from"),
        "page": pit_state.get("page"),
        "last_beat": store.news_pit_beat(max(beat_dates)) if beat_dates else None,
        "beats_recent_7d": len(recent_rows),
        "ok_beats_recent_7d": sum(
            1 for row in recent_rows if row["status"] == STATUS_OK
        ),
        "gap_nights_recent_7d": sorted(expected_nights - beat_dates),
        "total_observations": int(cast("int", pit_state.get("total_articles") or 0)),
    }
    return {
        "as_of": today_d.isoformat(),
        "backfill": backfill,
        "forward_pit": forward,
        "pit_rule": {
            "admission": "observed_at ≤ 决策时点准入，晚到不回灌",
            "predicate": "goalx_backend.data.ingest.news_pit.pit_admitted",
            "dataset": f"{PROVIDER}/{PIT_DATASET}",
            "beat_host": "daily-wrap 23:30（既有 deployment，零新增 cron）",
        },
    }
