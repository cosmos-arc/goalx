"""
源T（srct）轨迹语料采集：CorpusStore 上的按日闭环（规格 v2，票 59 起）。

单命令按日闭环：Over 日页（GB18030）发现 CorpusScope 场次 sid → 逐场按
端点注册表拉页 → 每响应 gzip+sha256 落 CorpusStore raw/ → checkpoint
断点可续。夜班调度/预算/熔断的编排层在 srct_night.py（本模块持有
NightBudget 语义，避免反向依赖）。

**端点集数据驱动（票 59）**：SPEC_ENDPOINTS 是
`.scratch/goalx-quant/collection-spec.md` §一规格表的机器面——先改表再
改码，新端点=一行注册表 + 一对抓取/解析函数；测试 test_ingest_srct_spec
钉死注册表与规格一致（防规格再漂移）。changeDetail 单书亚盘轨迹已按
规格 v2 撤采（历史两端点多庄页即得、当期轨迹由当期班快照自建），数据集
常量留档供存量 silver 口径引用。

口径沿 zucai/srcb 模式：网络薄（固定桌面 UA+对应 Referer）、解析纯函数、
实测样本裁剪单测（书商名一律打码，代称红线）。HTTP 访问套件（用户裁定
2026-09-23）：httpx 客户端 + tenacity 重试 + limits 滑动窗口限流
（MemoryStorage，不依赖外部存储）。防封基线（research/20 §九 定案 4）：
3s±1s 抖动（间距）、20/分钟滑动窗口（硬顶）、传输失败指数退避重试、
单页失败不炸整跑。端点 URL 模板从 config 注入（代称红线：实名/路径不落
码库，真值进本地 .env）。

三时间口径：源页时间→published_at 属 silver 层；采集只记抓取时刻
fetched_at（observed_at 语义）。行不带 fixture_id——身份绑定后置（定则 1）。
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
from functools import partial
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
ASIANODDS_DATASET = "asian_odds"  # 亚盘多庄页（规格 v2 端点 3；初/即时/终三组）
OVERDOWN_DATASET = "over_down"  # 大小球多庄页（规格 v2 端点 4；与亚盘多庄同构）
DETAIL_DATASET = (
    "match_detail"  # 详情页（规格 v2 端点 5；xG/阵容/事件/场地，票 66 撤 stats 切此）
)
ANALYSIS_DATASET = "match_analysis"  # 分析页（规格 v2 端点 6；特征面 bronze-only）
STATS_DATASET = "match_stats"  # 47 键技术统计（含 xG，键集逐年演进；票 66 撤切 detail）
# 撤采留档（票 59，规格 v2 裁决）：changeDetail 单书亚盘轨迹不再采集；数据集
# 常量与版本保留——silver odds_change_event 的 ah 面与历史对账仍引用该口径
HANDICAP_DATASET = "asian_handicap"
# 每解析器独立版本（spec story 8：记录可追溯到确切解析器版本；修一个只
# 重物化其数据集）。day_page=PARSE_VERSION（srct_day_v1）：一行=一日
# CorpusScope 完场清单，silver fixture_universe 的唯一输入（切片 14）
BRONZE_VERSIONS: dict[str, str] = {
    DAY_DATASET: PARSE_VERSION,
    ODDS_DATASET: "srct_odds_v1",
    ASIANODDS_DATASET: "srct_ah_multi_v1",
    OVERDOWN_DATASET: "srct_ou_multi_v1",
    DETAIL_DATASET: "srct_detail_v1",
    ANALYSIS_DATASET: "srct_analysis_v1",
    STATS_DATASET: "srct_stats_v1",
    HANDICAP_DATASET: "srct_hdp_v1",
}

# CorpusScope 15 项（ADR-0010 定案 1）：日页联赛名按字面量精确匹配。
# 欧战两词为 2026-09-23 实测钉死的站点字面量（欧冠杯/欧罗巴杯——资格赛与
# 正赛同名随行，正赛区分已落切片 14 silver fixture_universe 的 stage 列，
# 按开球月窗口派生）；欧会杯（Conference）/欧联U19 等由精确匹配天然排除。
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

# 夜班预算（切片 13，research/20 §九 定案 4 + spec story 3/5）：~8K 请求/日
# 上限与连败 5 场熔断；编排（窗口/台账/摘要）在 srct_night.py
NIGHT_REQUEST_CAP = 8000
FAILURE_STREAK_CAP = 5

# 采集深度（票 18 老季分层，Phase2/3 扩展批）：全深=每场全端点；
# 浅深=只 日页+1x2 轨迹 两请求（浅深成员见 SPEC_ENDPOINTS.in_shallow——
# checkpoint 不记跳过端点，升深重跑天然只补深端点）。判定/持久/探针
# 编排在 srct_night.py
DEPTH_FULL = "full"
DEPTH_SHALLOW = "shallow"

# 伪 200/坏响应按内容判别的标记
_CONTENT_404_MARKER = "error_404.gif"
_STATS_MARKER = "var jsonData"

_SID_RE = re.compile(r"analysis\((\d+)\)")
_ROW_FIELDS_RE = re.compile(r"\|?[^|]*\|([^|]+)\|(\d+日\d+:\d+)\|完\|")
_TEAMS_RE = re.compile(r"\|([^|]{2,20})\|(\d+)\|-\|(\d+)\|([^|]{2,20})\|")
# 排名前缀两种形态：联赛内 [17]、跨联赛排名 [西甲8]（欧战行）
_RANK_RE = re.compile(r"\[[^\]]{1,10}\]")


# —— 端点规格注册表（票 59）：collection-spec.md §一 的机器面 ——
# 先改表再改码：新端点=本表一行 + _ENDPOINT_WIRING 一对抓取/解析；
# test_ingest_srct_spec 钉死本表与规格表一致（防漂移契约）。
#
# 状态：active=采集面；retired=撤采留档（数据集常量供存量 silver 引用，
# 不再进采集循环/升深补抓）。票 66 落地后 match_stats 转 retired、
# 四新端点（票 60/61/62）转 active——本表是唯一改动点。
@dataclass(frozen=True)
class SpecEndpoint:
    """规格表一行：数据集 / raw 扩展名 / 深度与状态成员资格。"""

    dataset: str
    ext: str
    per_day: bool = False  # 日页=按日一键（不进每场端点循环）
    in_shallow: bool = False  # 浅深（老季两请求层）是否包含
    status: str = "active"


SPEC_ENDPOINTS: tuple[SpecEndpoint, ...] = (
    SpecEndpoint(DAY_DATASET, ".htm", per_day=True),
    SpecEndpoint(ODDS_DATASET, ".js", in_shallow=True),
    SpecEndpoint(ASIANODDS_DATASET, ".html"),
    SpecEndpoint(OVERDOWN_DATASET, ".html"),
    SpecEndpoint(DETAIL_DATASET, ".html"),
    SpecEndpoint(ANALYSIS_DATASET, ".html"),
    SpecEndpoint(STATS_DATASET, ".html"),
    SpecEndpoint(HANDICAP_DATASET, ".html", status="retired"),
)


def match_endpoint_datasets(depth: str = DEPTH_FULL) -> tuple[str, ...]:
    """该深度的每场端点数据集（注册表序；浅深只含 in_shallow 成员）。"""
    return tuple(
        spec.dataset
        for spec in SPEC_ENDPOINTS
        if spec.status == "active"
        and not spec.per_day
        and (depth == DEPTH_FULL or spec.in_shallow)
    )


def deep_endpoint_datasets() -> tuple[str, ...]:
    """全深独占的每场端点（浅深跳过）：升深补抓判据与中断证据重放集。"""
    shallow = set(match_endpoint_datasets(DEPTH_SHALLOW))
    return tuple(d for d in match_endpoint_datasets() if d not in shallow)


def retired_endpoint_datasets() -> tuple[str, ...]:
    """撤采留档数据集（不进采集循环；常量供存量 silver/对账引用）。"""
    return tuple(spec.dataset for spec in SPEC_ENDPOINTS if spec.status == "retired")


# 多庄对比页（亚盘/大小球同构，2026-09-25 实测）：每数据行自带 changeDetail
# 链接（companyID=cid，大小写混见）；页题标记用于真页判别（空表≠坏页，
# 定则 4）；数据行 ≥12 格：勾选/名/盘序 + 初/即时/终三组 + 详情
_MULTI_BOOK_CID_RE = re.compile(r"companyID=(\d+)", re.I)
_ASIANODDS_TITLE_MARKER = "亚指指数"
_OVERDOWN_TITLE_MARKER = "大小指数"
_MULTI_BOOK_MIN_CELLS = 12


def _js_var(text: str, name: str) -> str | None:
    """`var name="v"` / `var name='v'` / `var name=数值` 取值（缺变量 None）。"""
    found = re.search(rf'var {name}\s*=\s*("([^"]*)"|\'([^\']*)\'|[^;]*)', text)
    if found is None:
        return None
    for group in (found.group(2), found.group(3)):
        if group is not None:
            return group
    return found.group(1).strip() or None


def _js_array_rows(text: str, name: str) -> list[str] | None:
    """`name=Array("r1","r2",…);` → 原始行串数组（无此数组 None）。"""
    block = re.search(rf"{name}=Array\((.*?)\);", text, re.S)
    if block is None:
        return None
    return re.findall(r'"([^"]*)"', block.group(1))


class SrctContentError(Exception):
    """内容判别失败（伪 200/标记缺失）——确定性坏响应，不退避重试。"""


class NightStop(Exception):
    """夜班停机信号（预算触顶/熔断）——进度已落库，次夜断点续传。"""

    reason: str = "night_stop"


class BudgetExhausted(NightStop):
    """请求预算触顶（当晚收手；越界误差 ≤ 一场请求串）。"""

    reason = "budget"


class CircuitOpen(NightStop):
    """连败熔断（当晚收手留痕，不再消耗预算试探源端）。"""

    reason = "circuit"


@dataclass
class NightBudget:
    """
    夜班双重护栏：请求预算硬顶 + 连败熔断（spec story 3/5）。

    charge 在闸门内逐请求计（重试也计）；note 记连续失败事件——场级（一场
    任一端点传输失败）与日页级（传输失败/伪 200）共用同一连败计数，连续
    FAILURE_STREAK_CAP 次熔断（源端软封锁最常见的两种形态都落在护栏内），
    任一成功事件清零。
    """

    request_cap: int = NIGHT_REQUEST_CAP
    failure_streak_cap: int = FAILURE_STREAK_CAP
    requests: int = 0
    failure_streak: int = 0

    def charge(self, count: int = 1) -> None:
        """计线上请求；触顶抛 BudgetExhausted（当次请求不再发出）。"""
        self.requests += count
        if self.requests >= self.request_cap:
            raise BudgetExhausted(f"预算触顶 {self.requests}/{self.request_cap}")

    def note(self, failed: bool) -> None:
        """按场记成败；连续失败达 cap 抛 CircuitOpen。"""
        if not failed:
            self.failure_streak = 0
            return
        self.failure_streak += 1
        if self.failure_streak >= self.failure_streak_cap:
            raise CircuitOpen(f"连败 {self.failure_streak} 场熔断")


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
    skipped: int = 0  # 全端点 raw 缓存的场次
    parsed_ok: int = 0
    parse_failed: dict[str, str] = field(default_factory=dict)
    xg_matches: int = 0  # 统计页解析成功且含 xG 的场数（coverage 摘要）
    stats_nonempty: int = 0  # 统计页 stats 非空场数（票 18 老季深度探针证据）
    asian_odds_nonempty: int = 0  # 亚盘多庄页有报价行场数（同上，票 59 起接管）
    asian_odds_books: int = 0  # 亚盘多庄页逐盘行累计（书商×多盘；CLI 摘要）
    over_down_nonempty: int = 0  # 大小球多庄页有报价行场数（票 60 起）
    over_down_books: int = 0  # 大小球多庄页逐盘行累计（CLI 摘要）
    detail_nonempty: int = 0  # 详情页有技统/首发场数（票 61 起）
    analysis_nonempty: int = 0  # 分析页有特征数据场数（票 62 起）
    bronze_repaired: int = 0  # raw 有而 bronze 缺的本地重解析回补数
    failed: dict[str, str] = field(default_factory=dict)
    stopped: str | None = None  # 夜班停机原因（budget/circuit；None=干净跑完）


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


def fetch_asianodds_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场亚盘多庄页原始字节（UTF-8；实测免 Referer，2026-09-25）。"""
    response = client.get(
        settings.srct_asianodds_url.format(sid=sid),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def fetch_overdown_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场大小球多庄页原始字节（UTF-8；实测免 Referer，2026-09-25）。"""
    response = client.get(
        settings.srct_overdown_url.format(sid=sid),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def fetch_detail_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场详情页原始字节（live 主机，UTF-8；实测免 Referer，2026-09-25）。"""
    response = client.get(
        settings.srct_detail_url.format(sid=sid),
        headers={"User-Agent": DESKTOP_UA},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.content


def fetch_analysis_page(client: httpx.Client, settings: Settings, sid: str) -> bytes:
    """拉一场分析页原始字节（zq 主机，UTF-8；实测免 Referer，2026-09-25）。"""
    response = client.get(
        settings.srct_analysis_url.format(sid=sid),
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


def _quote_triple(cells: list[str], start: int) -> dict[str, str | None]:
    """水|线|水 三格 → 贴源报价组（值归一归 silver；空串为源页空格）。"""
    keys = ("home_water", "line", "away_water")
    return {keys[i]: cells[start + i].strip() or None for i in range(3)}


def _parse_multi_book_page(
    body: bytes, title_marker: str, label: str
) -> list[dict[str, object]]:
    """
    多庄对比页公共行解析（亚盘 AsianOdds_n / 大小球 OverDown_n 同构）。

    行结构（2026-09-25 实测，两页同 13 格）：勾选|公司名+状态|盘序
    |初(水线水)|即时(水线水)|终(水线水)|详情，每行一 changeDetail 链接
    =一书一盘。三组贴源存原文——
    - initial=初盘（页方口径，可能与 changeDetail 首行水位差一拍：后者有截断先例）；
    - latest=抓取时点最新价（完场后=场内末价，2026-09-25 实测与存档 92' 临场行一致）；
    - close=终盘（完场后=盘前末价，实测与 changeDetail 盘前末行逐值一致）。

    内容判别（定则 4 空≠无）：页题标记在而零书商行=合法空表（老场无报价，
    照常返回 []）；标记缺/伪 200 图=坏响应抛 SrctContentError。公司名原串
    入 bronze（语料数据面；repo 侧一律代称/打码）。
    """
    text = body.decode("utf-8-sig", errors="replace")
    if title_marker not in text or is_content_404(text):
        msg = f"{label}: 非多庄对比页（伪 200 或改版）"
        raise SrctContentError(msg)
    books: list[dict[str, object]] = []
    for chunk in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S | re.I):
        cid = _MULTI_BOOK_CID_RE.search(chunk)
        if cid is None:
            continue
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip()
            for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", chunk, re.S | re.I)
        ]
        if len(cells) < _MULTI_BOOK_MIN_CELLS:
            continue
        books.append(
            {
                "cid": cid.group(1),
                "name_raw": cells[1],  # 公司名+封/即状态原串（silver 层代称化）
                "multi": cells[2] or "盘1",  # 多盘标记（盘2/盘3/…；空=主盘）
                "initial": _quote_triple(cells, 3),
                "latest": _quote_triple(cells, 6),
                "close": _quote_triple(cells, 9),
            }
        )
    return books


def parse_asianodds_page(body: bytes) -> dict[str, object]:
    """亚盘多庄页（UTF-8）→ {"books": [...]}（行语义见 _parse_multi_book_page）。"""
    return {"books": _parse_multi_book_page(body, _ASIANODDS_TITLE_MARKER, "asianodds")}


def parse_overdown_page(body: bytes) -> dict[str, object]:
    """
    大小球多庄页（UTF-8）→ {"books": [...]}。

    与亚盘多庄同构（行语义见 _parse_multi_book_page），差异仅线值语义：
    line=进球数盘口线（"2.5/3" 等），水=大球/小球水位（票面口径：大球/进球数）。
    """
    return {"books": _parse_multi_book_page(body, _OVERDOWN_TITLE_MARKER, "overdown")}


# detail 详情页（live 主机，UTF-8，规格 v2 端点 5）：页题标记=现场分析。
# 分区结构（2026-09-25 实测历史页 2025-05）：技统条 li.lists>div.data 三
# span（主/名/客，当期页含 xG 行）；事件 eventtable>li；阵容 homeN/guestN
# 标题（队名+阵型+教练）+ plays>home/guest 首发块（em.num+名）+ 替补块
# （name>i 号）；头部 场地/天气/温度。xG 历史页可缺（老页真无，非坏页）。
_DETAIL_TITLE_MARKER = "现场分析"
_DETAIL_TECH_MIN_SPANS = 3  # 技统条行最少 span 数（主/名/客）
_DETAIL_KICKOFF_RE = re.compile(r"var strTime = '([^']+)'")
_DETAIL_VENUE_RE = re.compile(r"场地：\s*(.+?)\s*天气：\s*(.+?)\s*温度：\s*(\S+)")
_DETAIL_REFEREE_RE = re.compile(r"主裁判[:：]\s*([^\s<]+)")
_DETAIL_FORMATION_RE = re.compile(r"\d+(?:-\d+)+")
_DETAIL_COACH_RE = re.compile(r"主教练[:：]\s*([^)<]+)")


def _clean_html(fragment: str) -> str:
    """去标签压空白（事件/头部等贴源清洗共用）。"""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _detail_tech(text: str) -> list[dict[str, str]]:
    """技统条 li.lists → (home, name, away) 三元组（值贴源字符串）。"""
    rows: list[dict[str, str]] = []
    for data in re.findall(
        r"<li class='lists'>.*?<div class='data'>(.*?)</div>", text, re.S
    ):
        cells = [
            _clean_html(c) for c in re.findall(r"<span[^>]*>(.*?)</span>", data, re.S)
        ]
        cells = [c for c in cells if c]
        if len(cells) >= _DETAIL_TECH_MIN_SPANS:
            rows.append({"home": cells[0], "name": cells[1], "away": cells[2]})
    return rows


def _detail_lineup(text: str) -> dict[str, object]:
    """
    阵容区：首发（em.num 号 + 球员名 + pid + 队长标）与替补（name>i 号）。

    主客归属=该条目之前最近一次 class="home"/"guest" 容器标记（骨架实测：
    plays>home 首发块→guest 首发块→替补 home/guest 块顺序稳定）。pid 取
    setImgUrl(pid) 的球员 id；队长标=紧邻条目前的 captain div。
    """
    start = text.find("首发阵容")
    seg = text[start:] if start >= 0 else ""
    markers = sorted(
        (m.start(), cls)
        for cls in ("home", "guest")
        for m in re.finditer(rf'class="{cls}"', seg)
    )
    starters: dict[str, list[dict[str, object]]] = {"home": [], "guest": []}
    bench: dict[str, list[dict[str, object]]] = {"home": [], "guest": []}
    for m in re.finditer(r"<div class='play'[^>]*>(.*?)</span>", seg, re.S):
        chunk = m.group(0)
        side = next((cls for pos, cls in reversed(markers) if pos <= m.start()), "home")
        entry: dict[str, object] = {
            "pid": None,
            # 队长标在 play 块内部（<div class="captain"> 先于球员 span）
            "captain": 'class="captain"' in chunk,
        }
        pid = re.search(r"setImgUrl\((\d+)\)", chunk)
        if pid is not None:
            entry["pid"] = pid.group(1)
        num = re.search(r'<em class="num">\s*(\d+)\s*</em>', chunk)
        if num is not None:
            name = re.search(r"class='name'><a[^>]*>([^<]+)</a>", chunk)
            entry.update(num=num.group(1), name=name.group(1) if name else None)
            starters[side].append(entry)
            continue
        bench_num = re.search(
            r"<div class='name'><i>\s*(\d+)\s*</i><a[^>]*>([^<]+)</a>", chunk
        )
        if bench_num is not None:
            entry.update(num=bench_num.group(1), name=bench_num.group(2))
            bench[side].append(entry)
    return {
        "home_starters": starters["home"],
        "away_starters": starters["guest"],
        "home_bench": bench["home"],
        "away_bench": bench["guest"],
    }


def parse_detail_page(body: bytes) -> dict[str, object]:
    """
    Detail 详情页（UTF-8）→ meta/tech/events/lineup 分区（bronze 贴源）。

    分区缺=空（老页无 xG/裁判等，空≠无，定则 4）；页题标记缺/伪 200 图
    =坏响应抛 SrctContentError。与旧 47 键 stats 端点并存（票 61 expand：
    独立数据集 match_detail，撤切在票 66）。事件保留清洗串（进球/助攻/
    换人原文），字段级解读归 silver。
    """
    text = body.decode("utf-8-sig", errors="replace")
    if _DETAIL_TITLE_MARKER not in text or is_content_404(text):
        msg = "detail: 非详情分析页（伪 200 或改版）"
        raise SrctContentError(msg)
    head_window = _clean_html(
        text[: text.find("首发阵容") if "首发阵容" in text else 4000]
    )
    venue = _DETAIL_VENUE_RE.search(head_window)
    referee = _DETAIL_REFEREE_RE.search(head_window)
    formations: dict[str, str | None] = {}
    coaches: dict[str, str | None] = {}
    for side, cls in (("home", "homeN"), ("away", "guestN")):
        block = re.search(rf'class="{cls}"[^>]*>(.*?)</div>', text, re.S)
        if block is not None:
            seg = block.group(1)
            fmt = _DETAIL_FORMATION_RE.search(_clean_html(seg))
            coach = _DETAIL_COACH_RE.search(seg)
            formations[side] = fmt.group(0) if fmt else None
            coaches[side] = coach.group(1).strip() if coach else None
    event_start = text.find("eventtable")
    events = (
        [
            _clean_html(li)
            for li in re.findall(
                r"<li[^>]*>(.*?)</li>", text[event_start : event_start + 12000], re.S
            )
            if _clean_html(li)
        ]
        if event_start >= 0
        else []
    )
    tech = _detail_tech(text)
    kickoff = _DETAIL_KICKOFF_RE.search(text)
    return {
        "meta": {
            "home": _js_var(text, "homeTeamName"),
            "away": _js_var(text, "guestTeamName"),
            "kickoff": kickoff.group(1) if kickoff else None,
            "venue": venue.group(1).strip() if venue else None,
            "weather": venue.group(2).strip() if venue else None,
            "temperature": venue.group(3) if venue else None,
            "referee": referee.group(1) if referee else None,
            "home_formation": formations.get("home"),
            "away_formation": formations.get("away"),
            "home_coach": coaches.get("home"),
            "away_coach": coaches.get("away"),
        },
        "tech": tech,
        "has_xg": any(
            "xg" in r["name"].lower() or "预期进球" in r["name"] for r in tech
        ),
        "events": events,
        "lineup": _detail_lineup(text),
    }


# 分析页（zq 主机，UTF-8，规格 v2 端点 6）：特征面，bronze-only（票 62——
# silver 消费按 YAGNI 挂起，特征线开票时再接）。数据层=JS 数组 var（近况/
# 交战/盘路对比/积分榜），未来五场在 HTML 表（单格行=队名切换主客块）。
_ANALYSIS_TITLE_MARKER = "数据分析"
_ANALYSIS_ARRAY_VARS: tuple[tuple[str, str], ...] = (
    # (payload 键, 源 var 名)——键贴源 var 名，零转译；silver 层再语义化
    ("h_data", "h_data"),  # 主队近况（近 47 行）
    ("a_data", "a_data"),  # 客队近况
    ("h2_data", "h2_data"),  # 主队近况（主客拆分口径）
    ("a2_data", "a2_data"),
    ("v_data", "v_data"),  # 交战历史（h2h）
    ("Vs_hOdds", "Vs_hOdds"),  # 盘路对比（逐书，行首 scheduleId+cid）
    ("Vs_eOdds", "Vs_eOdds"),  # 欧赔对比（逐书）
    ("homeScoreStr", "homeScoreStr"),  # 主队积分榜
    ("guestScoreStr", "guestScoreStr"),  # 客队积分榜
)
_ANALYSIS_FUTURE_DATE_RE = re.compile(r"\d{2}-\d{2}")
_ANALYSIS_FUTURE_HEADER = frozenset(("时间", "赛事", "对阵", "分析", "直播", "相隔"))
_ANALYSIS_HOME_BLOCK = 1  # 单格行（队名块头）计数：1=主队块，>1=客队块


def _analysis_array_rows(text: str, name: str) -> list[str]:
    """`var name=[[..],[..]];` → 顶层数组行原文串列表（贴源；缺 var 空）。"""
    m = re.search(rf"var {name}\s*=\s*(\[.*?\]);", text, re.S)
    if m is None:
        return []
    return [
        row.strip()
        for row in re.split(r"\],\s*\[", m.group(1).strip()[1:-1])
        if row.strip()
    ]


def _analysis_future_fixtures(text: str) -> dict[str, list[list[str]]]:
    """未来五场：单格行（队名）切换主客块，含日期形态的行=赛程行。"""
    start = text.find("未来五场")
    seg = text[start:] if start >= 0 else ""
    sides: dict[str, list[list[str]]] = {"home": [], "away": []}
    block = 0  # 单格行（队名）计数：1=主队块，2=客队块
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S | re.I):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        ]
        cells = [c for c in cells if c and c != "&nbsp;"]
        while cells and cells[0] in _ANALYSIS_FUTURE_HEADER:  # 行内残留表头格剥掉
            cells.pop(0)
        if not cells:
            continue
        if len(cells) == 1:
            block += 1
            continue
        has_date = any(_ANALYSIS_FUTURE_DATE_RE.search(c) for c in cells)
        if block == _ANALYSIS_HOME_BLOCK and has_date:
            sides["home"].append(cells)
        elif block > _ANALYSIS_HOME_BLOCK and has_date:
            sides["away"].append(cells)
    return sides


def parse_analysis_page(body: bytes) -> dict[str, object]:
    """
    分析页（UTF-8）→ 特征面分区（票 62 bronze-only）。

    bronze 贴源：近况/交战/盘路对比/积分/未来五场。页题标记"数据分析"
    判别真页；分区缺=空（老页空≠无）。silver 消费按 YAGNI 挂起。
    """
    text = body.decode("utf-8-sig", errors="replace")
    if _ANALYSIS_TITLE_MARKER not in text or is_content_404(text):
        msg = "analysis: 非数据分析页（伪 200 或改版）"
        raise SrctContentError(msg)
    kickoff = _DETAIL_KICKOFF_RE.search(text)
    return {
        "meta": {
            "home": _js_var(text, "hometeam"),
            "away": _js_var(text, "guestteam"),
            "kickoff": kickoff.group(1) if kickoff else None,
        },
        "arrays": {
            key: _analysis_array_rows(text, var) for key, var in _ANALYSIS_ARRAY_VARS
        },
        "future_fixtures": _analysis_future_fixtures(text),
    }


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


# 活跃端点 → 必配 settings 字段（URL 模板；1x2 轨迹另需 Referer）
_ENDPOINT_SETTINGS: dict[str, tuple[str, ...]] = {
    DAY_DATASET: ("srct_day_url",),
    ODDS_DATASET: ("srct_odds_url", "srct_odds_referer"),
    ASIANODDS_DATASET: ("srct_asianodds_url",),
    OVERDOWN_DATASET: ("srct_overdown_url",),
    DETAIL_DATASET: ("srct_detail_url",),
    ANALYSIS_DATASET: ("srct_analysis_url",),
    STATS_DATASET: ("srct_stats_url",),
}


def _require_endpoints(settings: Settings) -> None:
    required = [
        field
        for spec in SPEC_ENDPOINTS
        if spec.status == "active"
        for field in _ENDPOINT_SETTINGS[spec.dataset]
    ]
    missing = [f"GOALX_{f.upper()}" for f in required if not getattr(settings, f)]
    if missing:
        msg = (
            f"srct 端点未配置：{' / '.join(missing)}"
            "（真值见 research/20 §二，进本地 .env）"
        )
        raise RuntimeError(msg)


_FetchFn = Callable[[httpx.Client, Settings, str], bytes]
_ParseFn = Callable[[bytes], dict[str, object]]


@dataclass(frozen=True)
class _EndpointSpec:
    """一场一个端点：数据集名 / raw 扩展名 / 拉取 / 解析。"""

    dataset: str
    ext: str
    fetch: Callable[[str], bytes]
    parse: _ParseFn


# 每场端点接线表：数据集 → (抓取, 解析)。新端点入列 = 注册表一行 + 此一对
# （retired 数据集不接线——撤采后永不回采集循环）
_ENDPOINT_WIRING: dict[str, tuple[_FetchFn, _ParseFn]] = {
    ODDS_DATASET: (fetch_odds_js, parse_odds_page),
    ASIANODDS_DATASET: (fetch_asianodds_page, parse_asianodds_page),
    OVERDOWN_DATASET: (fetch_overdown_page, parse_overdown_page),
    DETAIL_DATASET: (fetch_detail_page, parse_detail_page),
    ANALYSIS_DATASET: (fetch_analysis_page, parse_analysis_page),
    STATS_DATASET: (fetch_stats_page, parse_stats_page),
}


def _endpoint_specs(
    client: httpx.Client, settings: Settings, *, depth: str = DEPTH_FULL
) -> tuple[_EndpointSpec, ...]:
    """
    注册表驱动的每场端点集（票 59 数据驱动化）。

    全深=全部 active 每场端点；浅深=仅 in_shallow 成员（老季两请求层）；
    retired 永不进；日页 per_day 不进（按日单独走 `_load_day_page`）。
    """
    specs: list[_EndpointSpec] = []
    for spec in SPEC_ENDPOINTS:
        wiring = _ENDPOINT_WIRING.get(spec.dataset)
        if (
            wiring is None
            or spec.status != "active"
            or spec.per_day
            or (depth != DEPTH_FULL and not spec.in_shallow)
        ):
            continue
        specs.append(
            _EndpointSpec(
                dataset=spec.dataset,
                ext=spec.ext,
                fetch=partial(wiring[0], client, settings),
                parse=wiring[1],
            )
        )
    return tuple(specs)


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


def _gated(
    fetch: Callable[[], bytes],
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: NightBudget | None,
    wire: list[int],
) -> Callable[[], bytes]:
    """闸门包裹：滑窗 → 计数 → 预算计费 → 拉取（日页/端点共用形状）。"""

    def gated() -> bytes:
        throttle(limiter, _REQUEST_WINDOW, sleeper)
        wire[0] += 1
        if budget is not None:
            budget.charge(1)  # 逐请求计（重试也计）；触顶时本次请求不发出
        return fetch()

    return gated


def _attempt_fetch(
    spec: _EndpointSpec,
    sid: str,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: NightBudget | None = None,
) -> tuple[bytes | None, int, str | None]:
    """闸门+计数+重试拉一页；失败返回 (None, 线上请求数, 错误摘要)。"""
    wire = [0]
    gated = _gated(lambda: spec.fetch(sid), limiter, sleeper, budget, wire)
    try:
        return _retrying(sleeper)(gated), wire[0], None
    except httpx.HTTPError as exc:
        logger.warning("srct fetch {}:{} failed ({})", sid, spec.dataset, exc)
        return None, wire[0], str(exc)[:120]


def _day_payload(body: bytes) -> dict[str, object]:
    """日页 → bronze payload：CorpusScope 完赛场清单（fixture_universe 输入）。"""
    return {
        "matches": [
            {
                "sid": m.sid,
                "league": m.league,
                "kickoff_label": m.kickoff_label,
                "home": m.home,
                "away": m.away,
                "score": m.score,
            }
            for m in filter_scope(parse_over_page(decode_day_page(body)))
        ]
    }


def _note_evidence(
    dataset: str, payload: dict[str, object], stats: SrctCollectStats
) -> None:
    """票 18 探针证据计数：统计/亚盘多庄/大小球多庄有内容（新旧两路径共用）。"""
    if dataset == STATS_DATASET and payload.get("stats"):
        stats.stats_nonempty += 1
    elif dataset == ASIANODDS_DATASET and payload.get("books"):
        stats.asian_odds_nonempty += 1
        stats.asian_odds_books += len(cast("list[object]", payload["books"]))
    elif dataset == OVERDOWN_DATASET and payload.get("books"):
        stats.over_down_nonempty += 1
        stats.over_down_books += len(cast("list[object]", payload["books"]))
    elif dataset == DETAIL_DATASET and (
        payload.get("tech")
        or cast("dict[str, object]", payload.get("lineup") or {}).get("home_starters")
    ):
        stats.detail_nonempty += 1
    elif dataset == ANALYSIS_DATASET and any(
        cast("dict[str, list[object]]", payload.get("arrays") or {}).values()
    ):
        stats.analysis_nonempty += 1


def _bronze_append(
    store: CorpusStore,
    dataset: str,
    parse: Callable[[bytes], dict[str, object]],
    key: str,
    page: bytes,
    fetched_at: str,
    stats: SrctCollectStats,
) -> bool:
    """解析并追加 bronze 行；解析失败只计数不写行（raw 已留档）。"""
    try:
        payload = parse(page)
    except SrctContentError as exc:
        stats.parse_failed[f"{key}:{dataset}"] = str(exc)[:120]
        logger.warning("srct parse {}:{} failed ({})", key, dataset, exc)
        return False
    store.append_bronze(
        SRCT_PROVIDER,
        dataset,
        [_bronze_row(key, dataset, _page_sha(page), fetched_at, payload)],
    )
    stats.parsed_ok += 1
    if dataset == STATS_DATASET and payload.get("has_xg"):
        stats.xg_matches += 1
    _note_evidence(dataset, payload, stats)
    return True


def _handle_cached(
    store: CorpusStore,
    dataset: str,
    ext: str,
    parse: Callable[[bytes], dict[str, object]],
    key: str,
    in_bronze: bool,
    stats: SrctCollectStats,
) -> None:
    """Raw 在缓存：bronze 齐则纯跳过；缺（中断窗口）则本地重解析回补，零重抓。"""
    if in_bronze:
        if dataset in deep_endpoint_datasets():
            # 票 18：中断探针日续传时，已落库深端点的证据要从缓存重放
            # （宁可错升不错漏——漏=数据永久缺口）；xg_matches 不重复记
            _note_evidence(
                dataset,
                parse(store.read_raw(SRCT_PROVIDER, dataset, key, ext=ext)),
                stats,
            )
        return
    page = store.read_raw(SRCT_PROVIDER, dataset, key, ext=ext)
    if _bronze_append(store, dataset, parse, key, page, utc_now_iso(), stats):
        stats.bronze_repaired += 1


def day_page_sids(payload: dict[str, object]) -> list[str]:
    """日页 bronze payload → CorpusScope sid 清单（票 18 升深补抓完整性判据）。"""
    matches = cast("list[dict[str, object]]", payload.get("matches", []))
    return [str(m["sid"]) for m in matches if m.get("sid") is not None]


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


def _load_day_page(  # noqa: PLR0913 与 collect_day 同集接缝（防封/预算/统计）
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    date: str,
    limiter: MovingWindowRateLimiter,
    sleeper: Callable[[float], None],
    budget: NightBudget | None,
    stats: SrctCollectStats,
) -> bytes | None:
    """
    日页字节：缓存命中本地读，否则闸门+重试+预算计费+落 raw。

    None = 夜班停机（预算触顶），stats.stopped 已记，进度已落库。
    """
    if store.has(SRCT_PROVIDER, DAY_DATASET, date):
        stats.day_page_cached = True
        body = store.read_raw(SRCT_PROVIDER, DAY_DATASET, date, ext=".htm")
        # 回补：raw 在而 bronze 缺（切片 14 前存量/中断窗口）——本地重解析
        _handle_cached(
            store,
            DAY_DATASET,
            ".htm",
            _day_payload,
            date,
            date in _bronze_sids(store, DAY_DATASET),
            stats,
        )
        return body
    day_wire = [0]
    fetch_day = _gated(
        lambda: fetch_day_page(client, settings, date),
        limiter,
        sleeper,
        budget,
        day_wire,
    )
    try:
        body = _retrying(sleeper)(fetch_day)
    except NightStop as stop:
        stats.stopped = stop.reason
        return None
    stats.requests += day_wire[0]
    store.ingest_raw(SRCT_PROVIDER, DAY_DATASET, date, body, ext=".htm")
    # 日页也进 bronze（切片 14）：sid=date，一行=一日 CorpusScope 完场清单
    _bronze_append(store, DAY_DATASET, _day_payload, date, body, utc_now_iso(), stats)
    return body


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
    budget: NightBudget | None = None,
    depth: str = DEPTH_FULL,
) -> SrctCollectStats:
    """
    一日闭环：日页发现 sid → 每场按注册表端点集 → raw+bronze。

    断点续传：日页与每场每端点以 (provider, dataset, key) 查 checkpoint，
    已完成零重抓（日页从 raw 本地重解析）。rate_limiter 缺省每次运行新建
    滑动窗口（夜班应注入跨日期共享的 limiter）。

    budget（夜班注入）：触顶/熔断不抛出——记 stats.stopped 后正常返回，
    中断点前的进度已全部落库，次夜按 checkpoint 续传。

    depth（票 18）：shallow 只打 日页+1x2 轨迹（老季浅深）；跳过端点不记
    checkpoint——升深重跑按缓存只补深端点，零重抓（端点集见 SPEC_ENDPOINTS）。
    """
    datetime.strptime(date, "%Y-%m-%d")  # 键格式确定性
    _require_endpoints(settings)
    store.ensure_tree()  # 目录树随首条命令落地（gold/duckdb 先空占位）
    limiter = rate_limiter if rate_limiter is not None else default_limiter()
    stats = SrctCollectStats(date=date)
    body = _load_day_page(
        store, settings, client, date, limiter, sleeper, budget, stats
    )
    if body is None:
        return stats
    scope = filter_scope(parse_over_page(decode_day_page(body)))
    stats.scope_sids = [m.sid for m in scope]
    jitter_rng = random.Random() if rng is None else rng  # noqa: S311 抖动非加密用途
    specs = _endpoint_specs(client, settings, depth=depth)
    bronze_sids = {spec.dataset: _bronze_sids(store, spec.dataset) for spec in specs}
    for match in scope:
        cached = 0
        failed_before = len(stats.failed)
        try:
            for spec in specs:
                sid, dataset = match.sid, spec.dataset
                if store.has(SRCT_PROVIDER, dataset, sid):
                    cached += 1
                    _handle_cached(
                        store,
                        dataset,
                        spec.ext,
                        spec.parse,
                        sid,
                        sid in bronze_sids[dataset],
                        stats,
                    )
                    continue
                page, wire, error = _attempt_fetch(spec, sid, limiter, sleeper, budget)
                stats.requests += wire
                # 抖动对成败两态都生效——失败连发后立即打下一场同样扰站
                if jitter is not None:
                    sleeper(jitter_rng.uniform(*jitter))
                if page is None:
                    stats.failed[f"{sid}:{dataset}"] = error or "unknown"
                    continue
                store.ingest_raw(SRCT_PROVIDER, dataset, sid, page, ext=spec.ext)
                stats.raw_new += 1
                _bronze_append(
                    store, dataset, spec.parse, sid, page, utc_now_iso(), stats
                )
            if cached == len(specs):
                stats.skipped += 1  # 全端点 raw 在缓存（回补不重抓）
            if budget is not None:
                budget.note(len(stats.failed) > failed_before)
        except NightStop as stop:
            stats.stopped = stop.reason  # 进度已落库；次夜按 checkpoint 续传
            return stats
    return stats
