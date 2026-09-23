"""
源T（srct）轨迹语料采集·切片 11：CorpusStore 骨架上的第一夜最小闭环。

单命令按日闭环：Over 日页（GB18030）发现 CorpusScope 场次 sid → 逐场拉
1x2d 轨迹 → 每响应 gzip+sha256 落 CorpusStore raw/ → checkpoint 断点可续。
bronze 解析层与另两端点在切片 12；夜班调度/预算/熔断在切片 13。

口径沿 zucai/srcb 模式：网络薄（固定桌面 UA+对应 Referer）、解析纯函数、
实测样本裁剪单测。防封基线（research/20 §九 定案 4）：3s±1s 抖动、
传输失败指数退避重试、单页失败不炸整跑。端点 URL 模板从 config 注入
（代称红线：实名/路径不落码库，真值进本地 .env）。

三时间口径：changeTime→published_at 属解析层（切片 12）；本切片只记
抓取时刻 fetched_at（observed_at 语义）。行不带 fixture_id——身份绑定
后置（定则 1）。
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore

PARSE_VERSION = "srct_day_v1"
SRCT_PROVIDER = "srct"
DAY_DATASET = "day_page"
ODDS_DATASET = "odds_1x2d"

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
# 防封参数（research/20 §九 定案 4）：请求间隔 3s±1s 均匀抖动
JITTER_RANGE: tuple[float, float] = (2.0, 4.0)
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
MAX_RETRIES = 3

# 伪 200/坏响应按内容判别的标记
_CONTENT_404_MARKER = "error_404.gif"
_ODDS_MARKER = "game=Array"

_SID_RE = re.compile(r"analysis\((\d+)\)")
_ROW_FIELDS_RE = re.compile(r"\|?[^|]*\|([^|]+)\|(\d+日\d+:\d+)\|完\|")
_TEAMS_RE = re.compile(r"\|([^|]{2,20})\|(\d+)\|-\|(\d+)\|([^|]{2,20})\|")
# 排名前缀两种形态：联赛内 [17]、跨联赛排名 [西甲8]（欧战行）
_RANK_RE = re.compile(r"\[[^\]]{1,10}\]")


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
    """一次按日采集的统计（CLI/夜班摘要口径）。"""

    date: str
    scope_sids: list[str] = field(default_factory=list)
    day_page_cached: bool = False
    requests: int = 0
    raw_new: int = 0
    skipped: int = 0
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
    """拉一场 1x2d 轨迹原始字节；缺 game 标记抛 SrctContentError。"""
    response = client.get(
        settings.srct_odds_url.format(sid=sid),
        headers={
            "User-Agent": DESKTOP_UA,
            "Referer": settings.srct_odds_referer.format(sid=sid),
        },
        timeout=30.0,
    )
    response.raise_for_status()
    if _ODDS_MARKER not in decode_odds_js(response.content):
        msg = f"odds {sid}: 缺轨迹标记（伪 200 或坏响应）"
        raise SrctContentError(msg)
    return response.content


def backoff_seconds(attempt: int) -> float:
    """指数退避时长（attempt 从 0 起，封顶 60s）。"""
    return min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)


def _counted(fetch: Callable[[], bytes], counter: list[int]) -> Callable[[], bytes]:
    """包一层线上请求计数（重试也是真请求，预算记账要数全）。"""

    def wrapped() -> bytes:
        counter[0] += 1
        return fetch()

    return wrapped


def _fetch_with_retry(
    fetch: Callable[[], bytes], sleeper: Callable[[float], None]
) -> bytes:
    """传输类失败（HTTP 状态/网络）指数退避重试；内容错误不重试。"""
    attempt = 0
    while True:
        try:
            return fetch()
        except httpx.HTTPError:
            if attempt >= MAX_RETRIES:
                raise
            sleeper(backoff_seconds(attempt))
            attempt += 1


def _require_endpoints(settings: Settings) -> None:
    if not (
        settings.srct_day_url and settings.srct_odds_url and settings.srct_odds_referer
    ):
        msg = (
            "srct 端点未配置：GOALX_SRCT_DAY_URL / GOALX_SRCT_ODDS_URL / "
            "GOALX_SRCT_ODDS_REFERER（真值见 research/20 §二，进本地 .env）"
        )
        raise RuntimeError(msg)


def collect_day(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    *,
    date: str,
    jitter: tuple[float, float] | None = JITTER_RANGE,
    sleeper: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> SrctCollectStats:
    """
    一日闭环：日页发现 sid → 逐场 1x2d → raw+checkpoint。

    断点续传：日页与每场轨迹以 (provider, dataset, key) 查 checkpoint，
    已完成零重抓（日页从 raw 本地重解析）。
    """
    datetime.strptime(date, "%Y-%m-%d")  # 键格式确定性
    _require_endpoints(settings)
    store.ensure_tree()  # 目录树随首条命令落地（gold/duckdb 先空占位）
    stats = SrctCollectStats(date=date)
    if store.has(SRCT_PROVIDER, DAY_DATASET, date):
        body = store.read_raw(SRCT_PROVIDER, DAY_DATASET, date, ext=".htm")
        stats.day_page_cached = True
    else:
        wire: list[int] = [0]
        body = _fetch_with_retry(
            _counted(lambda: fetch_day_page(client, settings, date), wire), sleeper
        )
        stats.requests += wire[0]
        store.ingest_raw(SRCT_PROVIDER, DAY_DATASET, date, body, ext=".htm")
    scope = filter_scope(parse_over_page(decode_day_page(body)))
    stats.scope_sids = [m.sid for m in scope]
    jitter_rng = random.Random() if rng is None else rng  # noqa: S311 抖动非加密用途
    for match in scope:
        if store.has(SRCT_PROVIDER, ODDS_DATASET, match.sid):
            stats.skipped += 1
            continue
        wire = [0]
        try:
            odds = _fetch_with_retry(
                _counted(lambda m=match: fetch_odds_js(client, settings, m.sid), wire),
                sleeper,
            )
        except (httpx.HTTPError, SrctContentError) as exc:
            stats.failed[match.sid] = str(exc)[:120]
            logger.warning("srct odds {} failed ({})", match.sid, exc)
        else:
            store.ingest_raw(SRCT_PROVIDER, ODDS_DATASET, match.sid, odds, ext=".js")
            stats.raw_new += 1
        stats.requests += wire[0]
        # 抖动对成败两态都生效——失败连发后立即打下一场同样扰站
        if jitter is not None:
            sleeper(jitter_rng.uniform(*jitter))
    return stats
