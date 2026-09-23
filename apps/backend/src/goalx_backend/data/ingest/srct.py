"""
源T（srct）轨迹语料采集·切片 11：CorpusStore 骨架上的第一夜最小闭环。

单命令按日闭环：Over 日页（GB18030）发现 CorpusScope 场次 sid → 逐场拉
1x2d 轨迹 → 每响应 gzip+sha256 落 CorpusStore raw/ → checkpoint 断点可续。
bronze 解析层与另两端点在切片 12；夜班调度/预算/熔断在切片 13。

口径沿 zucai/srcb 模式：网络薄（固定桌面 UA+对应 Referer）、解析纯函数、
实测样本裁剪单测。HTTP 访问套件（用户裁定 2026-09-23）：httpx 客户端 +
tenacity 重试 + limits 滑动窗口限流（MemoryStorage，不依赖外部存储）。
防封基线（research/20 §九 定案 4）：3s±1s 抖动（间距）、20/分钟滑动窗口
（硬顶）、传输失败指数退避重试、单页失败不炸整跑。端点 URL 模板从 config
注入（代称红线：实名/路径不落码库，真值进本地 .env）。

三时间口径：changeTime→published_at 属解析层（切片 12）；本切片只记
抓取时刻 fetched_at（observed_at 语义）。行不带 fixture_id——身份绑定
后置（定则 1）。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import cast

import httpx
from limits import RateLimitItemPerMinute
from limits.strategies import MovingWindowRateLimiter
from loguru import logger
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso
from goalx_backend.rate_limit import default_limiter, throttle

PARSE_VERSION = "srct_day_v1"
SRCT_PROVIDER = "srct"
DAY_DATASET = "day_page"
ODDS_DATASET = "odds_1x2d"
HANDICAP_DATASET = "asian_handicap"  # 亚盘变化表（锚定书商 cid 走 URL 模板）
STATS_DATASET = "match_stats"  # 47 键技术统计（含 xG，键集逐年演进）
# 每解析器独立版本（spec story 8：记录可追溯到确切解析器版本；修一个只
# 重物化其数据集）
BRONZE_VERSIONS: dict[str, str] = {
    ODDS_DATASET: "srct_odds_v1",
    HANDICAP_DATASET: "srct_hdp_v1",
    STATS_DATASET: "srct_stats_v1",
}

# CorpusScope 15 项（ADR-0010 定案 1）：日页联赛名按字面量精确匹配。
# 欧战两词为 2026-09-23 实测钉死的站点字面量（欧冠杯/欧罗巴杯——资格赛与
# 正赛同名随行，正赛区分留给切片 14 fixture_universe 按开球日期窗口）；
# 欧会杯（Conference）/欧联U19 等由精确匹配天然排除。
CORPUS_SCOPE: tuple[str, ...] = (
    "英超",
    "西甲",
    "德甲",
    "意甲",
    "法甲",
    "英冠",
    "荷甲",
    "葡超",
    "土超",
    "比甲",
    "苏超",
    "瑞超",
    "挪超",
    "欧冠杯",
    "欧罗巴杯",
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# 防封参数（research/20 §九 定案 4）：请求间隔 3s±1s 均匀抖动（间距）+
# 滑动窗口硬顶 20/分钟（jitter 均值 ≈20/min，窗口只加顶不改间距）
JITTER_RANGE: tuple[float, float] = (2.0, 4.0)
RATE_LIMIT_PER_MINUTE = 20
_REQUEST_WINDOW = RateLimitItemPerMinute(RATE_LIMIT_PER_MINUTE)
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
MAX_RETRIES = 3

# 伪 200/坏响应按内容判别的标记
_CONTENT_404_MARKER = "error_404.gif"
_HANDICAP_TITLE_MARKER = "亚赔变化表"
_STATS_MARKER = "var jsonData"

_SID_RE = re.compile(r"analysis\((\d+)\)")
_ROW_FIELDS_RE = re.compile(r"\|?[^|]*\|([^|]+)\|(\d+日\d+:\d+)\|完\|")
_TEAMS_RE = re.compile(r"\|([^|]{2,20})\|(\d+)\|-\|(\d+)\|([^|]{2,20})\|")
# 排名前缀两种形态：联赛内 [17]、跨联赛排名 [西甲8]（欧战行）
_RANK_RE = re.compile(r"\[[^\]]{1,10}\]")


_TIME_CELL_RE = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}")
_SCORE_CELL_RE = re.compile(r"\d+-\d+")
# 亚盘行列位语义（实测三形态：临场 6/7 格、早盘 5 格、封盘 4 格）
_TIME_IDX_LIVE = 5  # 临场行时间列
_TIME_IDX_EARLY = 3  # 早盘/封盘行时间列
_MIN_EARLY_CELLS = 4  # 封盘最少格数（分/比分/封/时间）
_LIVE_STATUS_CELLS = 7  # 临场行含状态列的格数
_EARLY_STATUS_CELLS = 5  # 早盘行含状态列的格数


def _js_var(text: str, name: str) -> str | None:
    """`var name="value"` 或 `var name=数值` 取值（缺变量 None）。"""
    found = re.search(rf'var {name}=("([^"]*)"|[^;]*);', text)
    if found is None:
        return None
    return found.group(2) if found.group(2) is not None else found.group(1).strip()


def _js_array_rows(text: str, name: str) -> list[str] | None:
    """`name=Array("r1","r2",…);` → 原始行串数组（无此数组 None）。"""
    block = re.search(rf"{name}=Array\((.*?)\);", text, re.S)
    if block is None:
        return None
    return re.findall(r'"([^"]*)"', block.group(1))


def _handicap_row(cells: list[str]) -> dict[str, object] | None:
    """按时间列位置归一一行（临场 6/7 格、早盘 5 格、封盘 4 格；未识别 None）。"""
    time_idx = next(
        (i for i, cell in enumerate(cells) if _TIME_CELL_RE.fullmatch(cell)), None
    )
    if time_idx == _TIME_IDX_LIVE:  # 临场：分/比分/水/盘/水/时间(/状态)
        return {
            "minute": cells[0],
            "score": cells[1],
            "home_water": cells[2],
            "line": cells[3],
            "away_water": cells[4],
            "change_time": cells[_TIME_IDX_LIVE],
            "status": cells[6] if len(cells) >= _LIVE_STATUS_CELLS else None,
        }
    if (
        time_idx == _TIME_IDX_EARLY and len(cells) >= _MIN_EARLY_CELLS
    ):  # 早盘（水/盘/水/时间/早）或封盘（分/比分/封/时间）
        if _SCORE_CELL_RE.fullmatch(cells[1]):
            return {
                "minute": cells[0],
                "score": cells[1],
                "home_water": None,
                "line": None,
                "away_water": None,
                "change_time": cells[_TIME_IDX_EARLY],
                "status": cells[2],  # 封（暂停报价时点）
            }
        return {
            "minute": None,
            "score": None,
            "home_water": cells[0],
            "line": cells[1],
            "away_water": cells[2],
            "change_time": cells[_TIME_IDX_EARLY],
            "status": cells[4] if len(cells) >= _EARLY_STATUS_CELLS else None,
        }
    return None


class SrctContentError(Exception):
    """内容判别失败（伪 200/标记缺失）——确定性坏响应，不退避重试。"""


@dataclass(frozen=True)
class DayMatch:
    """日页一行完赛场次（sid 全站唯一主键）。"""

    sid: str
    league: str
    kickoff_label: str
    home: str
    away: str
    score: str


@dataclass
class SrctCollectStats:
    """一次按日采集的统计（CLI/夜班摘要口径：请求/新增/解析/失败）。"""

    date: str
    scope_sids: list[str] = field(default_factory=list)
    day_page_cached: bool = False
    requests: int = 0  # 全部线上请求（含重试）
    raw_new: int = 0
    skipped: int = 0  # 三端点全缓存的场次
    parsed_ok: int = 0
    parse_failed: dict[str, str] = field(default_factory=dict)
    xg_matches: int = 0  # 统计页解析成功且含 xG 的场数（coverage 摘要）
    bronze_repaired: int = 0  # raw 有而 bronze 缺的本地重解析回补数
    failed: dict[str, str] = field(default_factory=dict)


def parse_over_page(html: str) -> list[DayMatch]:
    """Over 日页 → 完赛场次行（纯函数；非完赛/无 sid 行跳过）。"""
    matches: list[DayMatch] = []
    for chunk in re.split(r"<tr", html):
        sid = _SID_RE.search(chunk)
        if sid is None:
            continue
        text = re.sub(r"<[^>]+>", "|", chunk)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"\|+", "|", text)
        fields = _ROW_FIELDS_RE.match(text)
        teams = _TEAMS_RE.search(text)
        if fields is None or teams is None:
            continue
        matches.append(
            DayMatch(
                sid=sid.group(1),
                league=fields.group(1).strip(),
                kickoff_label=fields.group(2),
                home=_RANK_RE.sub("", teams.group(1)).strip(),
                away=_RANK_RE.sub("", teams.group(4)).strip(),
                score=f"{teams.group(2)}-{teams.group(3)}",
            )
        )
    return matches


def filter_scope(
    matches: list[DayMatch], scope: tuple[str, ...] = CORPUS_SCOPE
) -> list[DayMatch]:
    """按 CorpusScope 联赛字面量精确匹配过滤。"""
    return [m for m in matches if m.league in scope]


def is_content_404(text: str) -> bool:
    """GBK 404 伪 200 按内容判别（HTTP 200 但页面是 404 图）。"""
    return _CONTENT_404_MARKER in text


def decode_day_page(body: bytes) -> str:
    """日页 GB18030 解码（容忍替换，raw 字节已另存）。"""
    return body.decode("gb18030", errors="replace")


def decode_odds_js(body: bytes) -> str:
    """1x2d 轨迹 JS 解码（UTF-8+BOM）。"""
    return body.decode("utf-8-sig", errors="replace")


def fetch_day_page(client: httpx.Client, settings: Settings, date: str) -> bytes:
    """拉一日 Over 页原始字节；伪 200 抛 SrctContentError。"""
    response = client.get(
        settings.srct_day_url.format(date=date.replace("-", "")),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    text = decode_day_page(response.content)
    if is_content_404(text):
        msg = f"day page {date}: 伪 200（404 内容）"
        raise SrctContentError(msg)
    return response.content


def fetch_odds_js(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场 1x2d 轨迹原始字节（内容判别在解析层）。"""
    response = client.get(
        settings.srct_odds_url.format(sid=sid),
        headers={
            "User-Agent": DESKTOP_UA,
            "Referer": settings.srct_odds_referer.format(sid=sid),
        },
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def fetch_handicap_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场亚盘变化表原始字节（GBK；锚定书商 cid 在 URL 模板内）。"""
    response = client.get(
        settings.srct_handicap_url.format(sid=sid),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def fetch_stats_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场 47 键统计页原始字节（UTF-8；实测免 Referer）。"""
    response = client.get(
        settings.srct_stats_url.format(sid=sid),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def parse_odds_page(body: bytes) -> dict[str, object]:
    """
    1x2d 轨迹页（UTF-8+BOM）→ 元数据 + game/gameDetail 原始行数组。

    行保留 | 分隔原串（bronze 贴源；字段规范化归 silver 层）。缺 game
    数组（伪 200/改版）抛 SrctContentError。
    """
    text = decode_odds_js(body)
    game = _js_array_rows(text, "game")
    if game is None:
        msg = "odds: 缺 game 数组（伪 200 或坏响应）"
        raise SrctContentError(msg)
    game_detail = _js_array_rows(text, "gameDetail") or []
    return {
        "meta": {
            "league": _js_var(text, "matchname_cn"),
            "home": _js_var(text, "hometeam_cn"),
            "away": _js_var(text, "guestteam_cn"),
            "match_time": _js_var(text, "MatchTime"),
            "schedule_id": _js_var(text, "ScheduleID"),
        },
        "game": game,
        "game_detail": game_detail,
    }


def parse_handicap_page(body: bytes) -> dict[str, object]:
    """
    亚盘变化表（GB18030）→ 归一行数组（临场/早盘/封盘三形态）。

    行字段：minute/score/home_water/line/away_water/change_time/status，
    缺列 None（值保留原串，浮点归一归 silver）。书商无数据=0 行（有效）。
    """
    text = body.decode("gb18030", errors="replace")
    if _HANDICAP_TITLE_MARKER not in text:
        msg = "handicap: 非亚赔变化表页（伪 200 或改版）"
        raise SrctContentError(msg)
    rows: list[dict[str, object]] = []
    for chunk in re.findall(r"<TR align=center[^>]*>(.*?)</TR>", text, re.S):
        cells = [
            re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(r"<TD[^>]*>(.*?)</TD>", chunk, re.S)
        ]
        row = _handicap_row(cells)
        if row is not None:
            rows.append(row)
    return {"rows": rows}


def parse_stats_page(body: bytes) -> dict[str, object]:
    """
    47 键统计页（UTF-8）→ 键值数组 + xG coverage；缺键容忍（定则 4）。

    键集逐年演进（2018 页 24 键无 xG、2025 页 47 键含 xG），缺 xG 照常
    返回（has_xg=False），缺 jsonData 块（伪 200/改版）抛 SrctContentError。
    """
    text = body.decode("utf-8-sig", errors="replace")
    block = re.search(r"var jsonData\s*=\s*(\{.*)", text, re.S)
    if block is None:
        msg = "stats: 缺 jsonData 块（伪 200 或坏响应）"
        raise SrctContentError(msg)
    try:
        decoded, _ = json.JSONDecoder().raw_decode(block.group(1))
    except ValueError as exc:
        msg = f"stats: jsonData 非法 JSON（{exc}）"
        raise SrctContentError(msg) from exc
    obj = cast(dict[str, object], decoded)
    tech = cast(dict[str, object], obj.get("techStat") or {})
    items = cast(list[dict[str, object]], tech.get("itemList") or [])
    stats: list[dict[str, object]] = [
        {
            "kind": item.get("kind"),
            "name": item.get("name"),
            "home_value": cast(dict[str, object], item.get("home") or {}).get("value"),
            "away_value": cast(dict[str, object], item.get("away") or {}).get("value"),
        }
        for item in items
    ]
    kinds = {str(item["kind"]) for item in stats if item["kind"]}
    return {
        "stats": stats,
        "has_xg": any(k.startswith("XG") or k == "EXPECTED_GOALS" for k in kinds),
    }


def _retrying(sleeper: Callable[[float], None]) -> Retrying:
    """
    Tenacity 重试策略（用户裁定套件，不手搓退避循环）。

    传输类失败（httpx.HTTPError）指数退避 2/4/8s（封顶 60s）重试 3 次；
    内容错误（SrctContentError）不匹配谓词直抛。
    """
    return Retrying(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(MAX_RETRIES + 1),
        wait=wait_exponential(
            multiplier=BACKOFF_BASE_SECONDS, exp_base=2, max=BACKOFF_CAP_SECONDS
        ),
        sleep=sleeper,
        reraise=True,
    )


def _require_endpoints(settings: Settings) -> None:
    required = (
        "srct_day_url",
        "srct_odds_url",
        "srct_odds_referer",
        "srct_handicap_url",
        "srct_stats_url",
    )
    missing = [f"GOALX_{f.upper()}" for f in required if not getattr(settings, f)]
    if missing:
        msg = (
            f"srct 端点未配置：{' / '.join(missing)}"
            "（真值见 research/20 §二，进本地 .env）"
        )
        raise RuntimeError(msg)


@dataclass(frozen=True)
class _EndpointSpec:
    """一场一个端点：数据集名 / raw 扩展名 / 拉取 / 解析。"""

    dataset: str
    ext: str
    fetch: Callable[[str], bytes]
    parse: Callable[[bytes], dict[str, object]]


def _endpoint_specs(
    client: httpx.Client, settings: Settings
) -> tuple[_EndpointSpec, ...]:
    """ADR-0010 定案 2：每场三请求（轨迹 / 亚盘 / 47 键统计）。"""
    return (
        _EndpointSpec(
            ODDS_DATASET,
            ".js",
            lambda sid: fetch_odds_js(client, settings, sid),
            parse_odds_page,
        ),
        _EndpointSpec(
            HANDICAP_DATASET,
            ".html",
            lambda sid: fetch_handicap_page(client, settings, sid),
            parse_handicap_page,
        ),
        _EndpointSpec(
            STATS_DATASET,
            ".html",
            lambda sid: fetch_stats_page(client, settings, sid),
            parse_stats_page,
        ),
    )


def _bronze_row(
    sid: str,
    dataset: str,
    raw_sha: str,
    fetched_at: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Bronze 行信封（ADR-0011 决策 1）：六字段 + payload，可回溯 raw 件。"""
    return {
        "provider": SRCT_PROVIDER,
        "dataset": dataset,
        "sid": sid,
        "fetched_at": fetched_at,  # 抓取时刻（observed_at 语义，非解析时刻）
        "parser_version": BRONZE_VERSIONS[dataset],
        "raw_sha": raw_sha,
        "payload": payload,
    }


def _attempt_fetch(
    spec: _EndpointSpec,
    sid: str,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
) -> tuple[bytes | None, int, str | None]:
    """闸门+计数+重试拉一页；失败返回 (None, 线上请求数, 错误摘要)。"""
    wire = [0]

    def gated() -> bytes:
        throttle(limiter, _REQUEST_WINDOW, sleeper)
        wire[0] += 1
        return spec.fetch(sid)

    try:
        return _retrying(sleeper)(gated), wire[0], None
    except httpx.HTTPError as exc:
        logger.warning("srct fetch {}:{} failed ({})", sid, spec.dataset, exc)
        return None, wire[0], str(exc)[:120]


def _bronze_append(
    store: CorpusStore,
    spec: _EndpointSpec,
    sid: str,
    page: bytes,
    fetched_at: str,
    stats: SrctCollectStats,
) -> bool:
    """解析并追加 bronze 行；解析失败只计数不写行（raw 已留档）。"""
    try:
        payload = spec.parse(page)
    except SrctContentError as exc:
        stats.parse_failed[f"{sid}:{spec.dataset}"] = str(exc)[:120]
        logger.warning("srct parse {}:{} failed ({})", sid, spec.dataset, exc)
        return False
    store.append_bronze(
        SRCT_PROVIDER,
        spec.dataset,
        [_bronze_row(sid, spec.dataset, _page_sha(page), fetched_at, payload)],
    )
    stats.parsed_ok += 1
    if spec.dataset == STATS_DATASET and payload.get("has_xg"):
        stats.xg_matches += 1
    return True


def _handle_cached(
    store: CorpusStore,
    spec: _EndpointSpec,
    sid: str,
    in_bronze: bool,
    stats: SrctCollectStats,
) -> None:
    """Raw 在缓存：bronze 齐则纯跳过；缺（中断窗口）则本地重解析回补，零重抓。"""
    if in_bronze:
        return
    page = store.read_raw(SRCT_PROVIDER, spec.dataset, sid, ext=spec.ext)
    if _bronze_append(store, spec, sid, page, utc_now_iso(), stats):
        stats.bronze_repaired += 1


def _bronze_sids(store: CorpusStore, dataset: str) -> set[str]:
    """该数据集当前解析器版本已落 bronze 的 sid 集（防重/回补判断）。"""
    return {
        str(row["sid"])
        for row in store.read_bronze(SRCT_PROVIDER, dataset)
        if row.get("parser_version") == BRONZE_VERSIONS[dataset]
    }


def _page_sha(page: bytes) -> str:
    """Bronze 信封的 raw_sha（与 corpus_store 落盘同口径）。"""
    return hashlib.sha256(page).hexdigest()


def collect_day(  # noqa: PLR0913 防封/测试接缝参数随切片累加，切片 13 归并预算面
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    date: str,
    jitter: tuple[float, float] | None = JITTER_RANGE,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    rate_limiter: MovingWindowRateLimiter | None = None,
) -> SrctCollectStats:
    """
    一日闭环：日页发现 sid → 每场三端点（轨迹/亚盘/统计）→ raw+bronze。

    断点续传：日页与每场每端点以 (provider, dataset, key) 查 checkpoint，
    已完成零重抓（日页从 raw 本地重解析）。rate_limiter 缺省每次运行新建
    滑动窗口（跨夜预算限流归切片 13，届时注入长生命周期 limiter）。
    """
    datetime.strptime(date, "%Y-%m-%d")  # 键格式确定性
    _require_endpoints(settings)
    store.ensure_tree()  # 目录树随首条命令落地（gold/duckdb 先空占位）
    limiter = rate_limiter if rate_limiter is not None else default_limiter()
    stats = SrctCollectStats(date=date)
    if store.has(SRCT_PROVIDER, DAY_DATASET, date):
        body = store.read_raw(SRCT_PROVIDER, DAY_DATASET, date, ext=".htm")
        stats.day_page_cached = True
    else:
        day_wire = [0]

        def fetch_day() -> bytes:
            throttle(limiter, _REQUEST_WINDOW, sleeper)
            day_wire[0] += 1
            return fetch_day_page(client, settings, date)

        body = _retrying(sleeper)(fetch_day)
        stats.requests += day_wire[0]
        store.ingest_raw(SRCT_PROVIDER, DAY_DATASET, date, body, ext=".htm")
    scope = filter_scope(parse_over_page(decode_day_page(body)))
    stats.scope_sids = [m.sid for m in scope]
    jitter_rng = random.Random() if rng is None else rng  # noqa: S311 抖动非加密用途
    specs = _endpoint_specs(client, settings)
    bronze_sids = {spec.dataset: _bronze_sids(store, spec.dataset) for spec in specs}
    for match in scope:
        cached = 0
        for spec in specs:
            sid, dataset = match.sid, spec.dataset
            if store.has(SRCT_PROVIDER, dataset, sid):
                cached += 1
                _handle_cached(store, spec, sid, sid in bronze_sids[dataset], stats)
                continue
            page, wire, error = _attempt_fetch(spec, sid, limiter, sleeper)
            stats.requests += wire
            # 抖动对成败两态都生效——失败连发后立即打下一场同样扰站
            if jitter is not None:
                sleeper(jitter_rng.uniform(*jitter))
            if page is None:
                stats.failed[f"{sid}:{dataset}"] = error or "unknown"
                continue
            store.ingest_raw(SRCT_PROVIDER, dataset, sid, page, ext=spec.ext)
            stats.raw_new += 1
            _bronze_append(store, spec, sid, page, utc_now_iso(), stats)
        if cached == len(specs):
            stats.skipped += 1  # 三端点 raw 全在缓存（回补不重抓）
    return stats
