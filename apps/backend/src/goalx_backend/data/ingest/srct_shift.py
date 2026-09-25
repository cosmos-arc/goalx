"""
源T 当期班（票 65）：竞彩在售 + 当日完赛场次的盘前四类拍。

替代已停的源B 双拍与欧赔聚合的实时职能（collection-spec §二）。四类拍：

- **开售拍（open）**：sid 首次出现在在售清单 → 拉 1x2d（顺带取联赛/主客/
  开球建档；CorpusScope 资格在此判定）；
- **每日两拍（daily am/pm）**：墙钟 10/22 点窗各一拍 1x2d（中程轨迹）；
- **临场拍（close）**：开球前自适应窗（凌晨/早场 12h、晚场 2h——凌晨场
  停售提前 3-12h 的已知约束）→ 1x2d + 亚盘 + 大小三请求（喂陈盘/早锁）；
- **完场收口拍**：不在本模块——夜班 collect_day 按日页全量收口（既有
  管线，Over 页隔日稳态）。四拍组合即"当期轨迹 append-only 自建"
  （规格 v2 撤 changeDetail 的裁决）。

**入口（票 65 实测票 d 结论，2026-09-26）**：live 主机
``/vbsxml/Ballpub/BaSID.js``（采集器客户端桌面 UA 直通）——
``var Ba_Soccer="sid,…"`` ≈410 场 ±2 日窗，**含未开赛在售场**。bf 主机
index.htm=857B 桩、data 主机 soccer_scheduleid.js=SPA 壳（死）、
zq ``/default/getScheduleInfo``=联赛选择状态非清单。

**开球口径**：1x2d meta MatchTime（``YYYY,MM-k,D,H,M,S`` JS Date 参数串，
第二段 "MM-1"=0-based 月编码；**UTC**——2026-09-26 与日页标签实证差 8h）。

**落库**：拍快照进既有 bronze 数据集（odds_1x2d/asian_odds/over_down），
raw 键加拍后缀（``{sid}@open`` / ``@daily-{date}{slot}`` / ``@close``）——
与夜班收口键（裸 sid）互不占位；bronze 行 sid 仍为裸 sid，silver
latest-per-sid 语义天然收敛到收口行（在售期为最新拍，收口后为终版页）。
sid 建档表 srct_shift_matches（corpus_store）；拍去重=checkpoint has()。

防封与夜班共用口径：20/min 滑窗硬顶、3s±1s 抖动、请求预算（当次运行
上限，缺省 600）；01:00-08:00 家宽夜窗让位夜班不跑。拍失败不重试本轮
（无 raw 键即下轮自动重试——拍天然幂等自愈）。
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import httpx
from limits.strategies import MovingWindowRateLimiter
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct
from goalx_backend.rate_limit import default_limiter, throttle

if TYPE_CHECKING:
    from goalx_backend.data.ingest import jc_shift

BEIJING = timezone(timedelta(hours=8))  # 拍时刻表/停售墙钟的业务时区
# 入口清单数据集（raw-only：BaSID 源页留档审计，无 bronze 消费链）
SHIFT_ENTRY_DATASET = "shift_entry"
# 当次运行请求上限：一场全周期 6-10 请求 × 日量 100-300 场分摊到 32 拍/日
SHIFT_REQUEST_CAP = 600
# 夜窗让位（01:00-08:00 与 srct-night 同家宽窗互斥；上限 08:00 收口收早场拍）
SHIFT_INACTIVE_HOURS: frozenset[int] = frozenset(range(1, 8))
# 每日两拍时刻表（墙钟小时窗 → 槽位；与源B 时代 10:40/22:40 拍习惯对齐）
DAILY_SLOTS: dict[str, tuple[int, ...]] = {"am": (10, 11), "pm": (22, 23)}
# 临场拍自适应窗（票 58/27 号矩阵约束）：北京午前开球=凌晨/早场，停售提前
CLOSING_EARLY_KICKOFF_HOUR = 12
_MATCH_TIME_MAX_MONTH = 12  # 月编码越界=坏串（如 "13-1"）
CLOSING_EARLY_LEAD = timedelta(hours=12)
CLOSING_LATE_LEAD = timedelta(hours=2)

_BA_SOCCER_RE = re.compile(r'var Ba_Soccer="([^"]*)"')
# MatchTime 形态 'YYYY,MM-k,D,H,M,S'（JS Date 参数串；第二段 0-based 月编码）
_MATCH_TIME_RE = re.compile(
    r"^(\d{4}),(\d{1,2})-(\d{1,2}),(\d{1,2}),(\d{2}),(\d{2}),(\d{2})$"
)


@dataclass
class ShiftStats:
    """一次当期班运行的统计（CLI/flow 摘要口径）。"""

    run_at: str = ""
    discovered: int = 0  # 在售清单 sid 总数
    known: int = 0  # 已建档 sid 数
    final_collected: int = 0  # 夜班已收口跳过数（裸 sid raw 在）
    open_beats: int = 0
    daily_beats: int = 0
    close_beats: int = 0
    out_scope: int = 0  # 非 CorpusScope 建档数（本轮新见）
    missing_kickoff: int = 0  # MatchTime 不可解建档数（无拍可调度留痕）
    requests: int = 0
    raw_new: int = 0
    parsed_ok: int = 0
    failed: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None  # budget/window_closed；None=清单跑完
    # JC 官方拍相位（票 71；jc: 前缀区分于源T 层）
    jc_discovered: int = 0
    jc_open_beats: int = 0
    jc_daily_beats: int = 0
    jc_close_beats: int = 0
    jc_finalized: int = 0


def parse_basid(body: bytes) -> list[str]:
    """BaSID.js → 足球在售 sid 清单（保序去重；缺 var 空）。"""
    found = _BA_SOCCER_RE.search(body.decode("utf-8", errors="replace"))
    if found is None:
        return []
    seen: dict[str, None] = {}
    for raw in found.group(1).split(","):
        if raw.strip():
            seen.setdefault(raw.strip(), None)
    return list(seen)


def parse_match_time(raw: object) -> datetime | None:
    """
    1x2d meta MatchTime（JS Date 参数串）→ 开球 UTC 时刻；不可解 None。

    '2026,09-1,26,03,00,00' = new Date(2026, 9-1, 26, 3, 0, 0) =
    2026-09-26 03:00 UTC（0-based 月：第二段右数=减项；UTC 与日页标签
    差 8h 实证，见模块 docstring）。
    """
    found = _MATCH_TIME_RE.match(str(raw or "").strip())
    if found is None:
        return None
    year, month, month_sub, day, hour, minute, second = (int(g) for g in found.groups())
    # JS 0-based 月编码："09-1" 求值=索引 8 → 日历 9 月（与日页标签实证）
    month = month - month_sub + 1
    if not 1 <= month <= _MATCH_TIME_MAX_MONTH:
        return None
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=UTC)
    except ValueError:
        return None


def open_key(sid: str) -> str:
    """开售拍 raw 键。"""
    return f"{sid}@open"


def daily_key(sid: str, day: str, slot: str) -> str:
    """每日拍 raw 键（一日一槽一键）。"""
    return f"{sid}@daily-{day}{slot}"


def close_key(sid: str, dataset: str) -> str:
    """临场拍 raw 键（数据集后缀防跨端点撞键）。"""
    return f"{sid}@close-{dataset}"


def slot_for_hour(hour: int) -> str | None:
    """墙钟小时 → 当日槽位（10/11=am，22/23=pm；其余 None 不拍 daily）。"""
    for slot, hours in DAILY_SLOTS.items():
        if hour in hours:
            return slot
    return None


def closing_lead(kickoff_utc: datetime) -> timedelta:
    """临场拍提前量：北京午前开球（凌晨/早场）12h，其余 2h。"""
    beijing_hour = kickoff_utc.astimezone(BEIJING).hour
    if beijing_hour < CLOSING_EARLY_KICKOFF_HOUR:
        return CLOSING_EARLY_LEAD
    return CLOSING_LATE_LEAD


def _beat_fetchers(
    client: httpx.Client, settings: Settings
) -> dict[str, Callable[[str], bytes]]:
    """拍端点集：数据集 → 抓取函数（收口口径与夜班同端点同头）。"""
    return {
        srct.ODDS_DATASET: lambda sid: srct.fetch_odds_js(client, settings, sid),
        srct.ASIANODDS_DATASET: lambda sid: srct.fetch_asianodds_page(
            client, settings, sid
        ),
        srct.OVERDOWN_DATASET: lambda sid: srct.fetch_overdown_page(
            client, settings, sid
        ),
    }


def _beat_ext(dataset: str) -> str:
    """拍的 raw 扩展名（与注册表口径一致）。"""
    return next(spec.ext for spec in srct.SPEC_ENDPOINTS if spec.dataset == dataset)


def _parse_fn(dataset: str) -> Callable[[bytes], dict[str, object]]:
    """拍的解析函数（与夜班同解析器——bronze 版本同轨）。"""
    return srct.endpoint_wiring(dataset)[1]


def _run_beat(  # noqa: PLR0913 同 collect_day 接缝集（防封/预算/统计）
    store: CorpusStore,
    sid: str,
    dataset: str,
    key: str,
    fetch: Callable[[str], bytes],
    stats: ShiftStats,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: srct.NightBudget | None,
) -> dict[str, object] | None:
    """
    一拍：闸门 → 预算 → 拉取 → raw+bronze（键=拍键，行 sid=裸 sid）。

    解析失败不落 raw（坏页不占拍键——下轮自动重试，幂等自愈）；传输/
    内容失败只计数。返回解析 payload（开售拍建档用），失败 None。
    """
    throttle(limiter, srct.REQUEST_WINDOW, sleeper)
    if budget is not None:
        budget.charge(1)
    stats.requests += 1
    try:
        page = fetch(sid)
        payload = _parse_fn(dataset)(page)
    except (httpx.HTTPError, srct.SrctContentError) as exc:
        stats.failed[f"{key}"] = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("srct shift {} failed ({})", key, exc)
        return None
    store.ingest_raw(srct.SRCT_PROVIDER, dataset, key, page, ext=_beat_ext(dataset))
    stats.raw_new += 1
    _append_beat_bronze(store, sid, dataset, key, payload, stats)
    return payload


def _append_beat_bronze(
    store: CorpusStore,
    sid: str,
    dataset: str,
    key: str,
    payload: dict[str, object],
    stats: ShiftStats,
) -> None:
    """拍快照进 bronze（行 sid=裸 sid；raw_sha 回指拍键工件）。"""
    store.append_bronze(
        srct.SRCT_PROVIDER,
        dataset,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": dataset,
                "sid": sid,
                "fetched_at": srct.utc_now_iso(),
                "parser_version": srct.BRONZE_VERSIONS[dataset],
                "raw_sha": store.raw_sha(srct.SRCT_PROVIDER, dataset, key),
                "payload": payload,
            }
        ],
    )
    stats.parsed_ok += 1


def _upsert_match(
    store: CorpusStore,
    sid: str,
    payload: dict[str, object],
    *,
    kickoff: datetime | None,
    beat: bool = True,
) -> None:
    """开售拍建档/拍后刷新（联赛/主客/开球/scope 资格）。"""
    meta = cast("dict[str, object]", payload.get("meta") or {})
    league = str(meta.get("league") or "")
    store.upsert_shift_match(
        {
            "sid": sid,
            "league": league,
            "home": str(meta.get("home") or ""),
            "away": str(meta.get("away") or ""),
            "kickoff_utc": kickoff.isoformat() if kickoff else None,
            "in_scope": int(league in srct.CORPUS_SCOPE),
            "beats": 1 if beat else 0,
            "last_beat_at": srct.utc_now_iso() if beat else None,
        }
    )


def _bump_beats(
    store: CorpusStore, row: dict[str, object], count: int
) -> dict[str, object]:
    """拍后计数刷新（beats/last_beat_at）；返回刷新行供同轮续拍累计。"""
    row = dict(row)
    row["beats"] = int(cast("int", row.get("beats") or 0)) + count
    row["last_beat_at"] = srct.utc_now_iso()
    store.upsert_shift_match(row)
    return row


def run_shift(  # noqa: PLR0911, PLR0913 接缝参数随防封/预算/窗口累加（同 collect_day 先例）
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    now_fn: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    jitter: tuple[float, float] | None = srct.JITTER_RANGE,
    request_cap: int = SHIFT_REQUEST_CAP,
    rate_limiter: MovingWindowRateLimiter | None = None,
    budget: srct.NightBudget | None = None,
    enforce_window: bool = True,
) -> ShiftStats:
    """
    推进一次当期班：在售清单 → 逐 sid 四类拍决策 → raw+bronze append。

    幂等：每拍以 raw 键查 checkpoint，已拍零重抓；失败拍无键下轮自愈。
    budget 触顶记 stopped 正常返回（进度已落库）。01:00-08:00 夜窗让位
    （enforce_window=False 供冒烟/手工回补绕过）。
    """
    now = now_fn() if now_fn is not None else datetime.now(UTC)
    stats = ShiftStats(run_at=now.isoformat(timespec="seconds"))
    if enforce_window and now.astimezone(BEIJING).hour in SHIFT_INACTIVE_HOURS:
        stats.stopped = "window_closed"
        return stats
    if not settings.srct_basid_url:
        msg = "srct_basid_url 未配置（当期班入口，票 65 实测真值见 .env）"
        raise RuntimeError(msg)
    budget = budget if budget is not None else srct.NightBudget(request_cap=request_cap)
    limiter = rate_limiter if rate_limiter is not None else default_limiter()
    jitter_rng = random.Random() if rng is None else rng  # noqa: S311 抖动非加密用途
    fetchers = _beat_fetchers(client, settings)
    known = store.shift_matches()
    # 入口清单（1 请求，闸门+预算内；raw 留档审计）。live 主机同站防爬：
    # 须带 r 缓存参数与 live 根 Referer（2026-09-26 实测缺则 200 空体）
    entry_key = f"{now.astimezone(BEIJING):%Y%m%d%H%M}"
    live_root = re.match(r"https?://[^/]+", settings.srct_detail_url or "")
    try:
        throttle(limiter, srct.REQUEST_WINDOW, sleeper)
        budget.charge(1)
        stats.requests += 1
        response = client.get(
            settings.srct_basid_url,
            params={"r": "007"},
            headers={
                "User-Agent": srct.DESKTOP_UA,
                **({"Referer": live_root.group(0) + "/"} if live_root else {}),
            },
            timeout=20.0,
        )
        response.raise_for_status()
        body = response.content
        if not body:
            msg = "_entry: BaSID 响应空体（缺 Referer/r 参数或源端异常）"
            stats.failed["_entry"] = msg
            logger.warning("srct shift {}", msg)
            return stats
    except srct.NightStop as stop:
        stats.stopped = stop.reason
        return stats
    except httpx.HTTPError as exc:
        stats.failed["_entry"] = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("srct shift 入口清单拉取失败（{}）", exc)
        return stats
    store.ingest_raw(
        srct.SRCT_PROVIDER, SHIFT_ENTRY_DATASET, entry_key, body, ext=".js"
    )
    sids = parse_basid(body)
    stats.discovered = len(sids)
    stats.known = sum(1 for sid in sids if sid in known)
    for sid in sids:
        row = known.get(sid)
        try:
            row = _advance_sid(
                store,
                settings,
                sid,
                row,
                now,
                fetchers,
                stats,
                limiter,
                sleeper,
                budget,
                jitter_rng,
                jitter,
            )
        except srct.NightStop as stop:
            stats.stopped = stop.reason  # 进度已落库，下轮拍键续
            return stats
        if row is not None:
            known[sid] = row
    # JC 官方拍（票 71）：同窗同预算殿后（源T 优先；发现失败只留痕）。
    # 局部导入防环（jc_shift 复用本模块的 BEIJING/slot/lead 决策件）
    from goalx_backend.data.ingest import jc_shift  # noqa: PLC0415

    try:
        jc_stats = jc_shift.run_jc_beats(
            store,
            settings,
            client,
            now=now,
            sleeper=sleeper,
            budget=budget,
        )
    except srct.NightStop as stop:
        stats.stopped = stop.reason
        return stats
    _absorb_jc(stats, jc_stats)
    return stats


def _absorb_jc(stats: ShiftStats, jc_stats: jc_shift.JcShiftStats) -> None:
    """JC 拍统计并进当期班摘要（键前缀 jc: 区分两层）。"""
    stats.jc_discovered = jc_stats.discovered
    stats.jc_open_beats = jc_stats.open_beats
    stats.jc_daily_beats = jc_stats.daily_beats
    stats.jc_close_beats = jc_stats.close_beats
    stats.jc_finalized = jc_stats.finalized
    stats.requests += jc_stats.requests
    stats.raw_new += jc_stats.raw_new
    stats.failed.update({f"jc:{k}": v for k, v in jc_stats.failed.items()})


def _open_beat(
    store: CorpusStore,
    sid: str,
    fetch: Callable[[str], bytes],
    stats: ShiftStats,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: srct.NightBudget,
) -> dict[str, object] | None:
    """开售拍抓取：raw 落 @open 键；解析 payload 返回（bronze 归调方裁）。"""
    throttle(limiter, srct.REQUEST_WINDOW, sleeper)
    budget.charge(1)
    stats.requests += 1
    try:
        page = fetch(sid)
        payload = _parse_fn(srct.ODDS_DATASET)(page)
    except (httpx.HTTPError, srct.SrctContentError) as exc:
        stats.failed[open_key(sid)] = f"{type(exc).__name__}: {exc}"[:120]
        logger.warning("srct shift {} failed ({})", open_key(sid), exc)
        return None
    store.ingest_raw(
        srct.SRCT_PROVIDER,
        srct.ODDS_DATASET,
        open_key(sid),
        page,
        ext=_beat_ext(srct.ODDS_DATASET),
    )
    return payload


def _advance_sid(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915, C901 拍决策接缝集随四类拍累加
    store: CorpusStore,
    settings: Settings,
    sid: str,
    row: dict[str, object] | None,
    now: datetime,
    fetchers: dict[str, Callable[[str], bytes]],
    stats: ShiftStats,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: srct.NightBudget,
    jitter_rng: random.Random,
    jitter: tuple[float, float] | None,
) -> dict[str, object] | None:
    """一个 sid 的拍决策（开售/每日/临场；返回刷新后的建档行）。"""
    # 夜班已收口（裸 sid raw 在）→ 全生命周期完结，无需任何拍
    if store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, sid):
        stats.final_collected += 1
        return row
    if row is not None and row.get("kickoff_utc") is None:
        return row  # 已建档但 MatchTime 不可解：无拍可调度（missing_kickoff 已留痕）
    if row is None:
        # 开售拍：建档（联赛/开球/scope 首见即定）；@open 已拍但建档缺 → 本地回补。
        # raw 一律落（拍键去重+审计），**bronze 只进 CorpusScope 场**——非 scope
        # 场轨迹不进语料 silver 面（罗丙/友谊赛等非 JC 联赛混在在售清单里）
        if store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, open_key(sid)):
            page = store.read_raw(
                srct.SRCT_PROVIDER,
                srct.ODDS_DATASET,
                open_key(sid),
                ext=_beat_ext(srct.ODDS_DATASET),
            )
            try:
                payload = _parse_fn(srct.ODDS_DATASET)(page)
            except srct.SrctContentError:
                return row  # 解析器升版不兼容：留待下轮重拍（键在但行废）
            new_raw = False
        else:
            payload = _open_beat(
                store,
                sid,
                fetchers[srct.ODDS_DATASET],
                stats,
                limiter,
                sleeper,
                budget,
            )
            if jitter is not None:
                sleeper(jitter_rng.uniform(*jitter))
            new_raw = payload is not None
        if payload is None:
            return row
        kickoff = parse_match_time(
            cast("dict[str, object]", payload.get("meta") or {}).get("match_time")
        )
        if kickoff is None:
            stats.missing_kickoff += 1
        _upsert_match(store, sid, payload, kickoff=kickoff)
        stats.open_beats += 1
        row = store.shift_matches().get(sid, {"sid": sid})
        if kickoff is None or not row.get("in_scope"):
            if row.get("league"):
                stats.out_scope += 1
            if new_raw:
                # 非 scope 场 @open raw 已作拍键落盘（去重+审计），bronze 不进
                stats.raw_new += 1
            return row
        if new_raw:
            stats.raw_new += 1
            _append_beat_bronze(
                store, sid, srct.ODDS_DATASET, open_key(sid), payload, stats
            )
    if not row.get("in_scope"):
        return row  # 非 CorpusScope：开售拍后不再拍（语料面范围裁定）
    kickoff = datetime.fromisoformat(str(row["kickoff_utc"]))
    if kickoff <= now:
        return row  # 已开球：收口归夜班，当期班生命周期完
    lead = closing_lead(kickoff)
    if kickoff - now <= lead:
        # 临场拍：三端点全拍（陈盘/早锁信号源）；一键一数据集
        for dataset in (
            srct.ODDS_DATASET,
            srct.ASIANODDS_DATASET,
            srct.OVERDOWN_DATASET,
        ):
            key = close_key(sid, dataset)
            if store.has(srct.SRCT_PROVIDER, dataset, key):
                continue
            if (
                _run_beat(
                    store,
                    sid,
                    dataset,
                    key,
                    fetchers[dataset],
                    stats,
                    limiter,
                    sleeper,
                    budget,
                )
                is not None
            ):
                stats.close_beats += 1
                row = _bump_beats(store, row, 1)
            if jitter is not None:
                sleeper(jitter_rng.uniform(*jitter))
        return row
    # 每日两拍：当日槽位内一拍 1x2d（中程轨迹；临场窗内不重复）
    slot = slot_for_hour(now.astimezone(BEIJING).hour)
    if slot is not None:
        key = daily_key(sid, now.astimezone(BEIJING).date().isoformat(), slot)
        if not store.has(srct.SRCT_PROVIDER, srct.ODDS_DATASET, key):
            if (
                _run_beat(
                    store,
                    sid,
                    srct.ODDS_DATASET,
                    key,
                    fetchers[srct.ODDS_DATASET],
                    stats,
                    limiter,
                    sleeper,
                    budget,
                )
                is not None
            ):
                stats.daily_beats += 1
                row = _bump_beats(store, row, 1)
            if jitter is not None:
                sleeper(jitter_rng.uniform(*jitter))
    return row


def stats_dict(stats: ShiftStats) -> dict[str, object]:
    """统计 → 可 JSON 化 dict（flow 返回值/日志口径）。"""
    return asdict(stats)
