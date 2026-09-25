"""
源B 传统足彩采集（票 43）：期次/对阵/人气分布（域名见配置 zucai_base_url）。

**判死留档（2026-09-25 深夜用户裁决，票 68 官方化接替）**：源B 采集面随
反爬升级死亡，传统足彩全转体彩官方（见 zucai_official.py）；本模块代码
留档不删不调——pool_snapshot 已切官方源，人气份额（源B 用户投票）无
替代源（官方 published 注数已覆盖彩果面）。

数据源实证（2026-09-18，详见票 43 Answer 的四源对比）：
- 期次发现：传统足彩索引页链接给出近期/在售期次（含预售下一期）；
- 期次页 ``/zucai/<期号>/``：200（GB18030、浏览器 UA），服务端渲染 14 场
  ——场序（tr<N>）/联赛/开赛时间（title 属性，北京时间）/主客队缩写名/
  三向"99 家平均欧指"/源内部场次 id；页头销售截止时间；任九与胜负彩
  同期次同 14 场（玩法层差异），ingest 一份对阵两玩法共用；
- 人气分布接口（期次页 JS 调用）：需 Referer（无 → 405）；percent=百分比、
  number=票数；在售与已完场期次均可回溯。键语义经欧赔交叉验证 14/14：
  **源B 键 1=主胜 2=平 3=客胜**，与官方池码（3=胜 1=平 0=负）不同，
  解析层完成映射；
- 该分布是源B 用户人气投票（公众分布代理信号，非官方池份额）→ 入库
  origin='estimated'；官方销量四源直接 GET 全不可得 → pool_states 走
  AI 代采入口（api/pool.py，source='agent'），本模块不碰。

口径守则：网络层薄（带 UA/Referer 的 GET）；解析纯函数（fixture 为实测页
裁剪样本）；入库经 data/pool.py（期次/对阵当前态刷新 + 份额 append-only）。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store

SOURCE = "okooo.com"
PARSE_VERSION = "zucai_v1"
CST = timezone(timedelta(hours=8))  # 源页面时间均为北京时间
# 任九与胜负彩共用期次/对阵（实证同期同 14 场）；库内以 ttt14 名义存一份
PERIOD_MARKET = "ttt14"
# 同步默认取最新 N 期（在售 + 预售/近完场），其余历史期次按需显式传参回补
DEFAULT_PERIOD_WINDOW = 2

# 源B 人气键 → 官方池码（3=胜 1=平 0=负；实证：源B 1=主胜 2=平 3=客胜）
_POP_KEY_TO_POOL_CODE = {"1": "3", "2": "1", "3": "0"}

_TR_RE = re.compile(r'<tr id="tr(?P<seq>\d+)"[^>]*>(?P<body>.*?)</tr>', re.S)
_LEAGUE_RE = re.compile(r'class="ls\s+jsLeagueName"[^>]*>(?P<league>[^<]+)</a>')
_KICKOFF_RE = re.compile(r"比赛时间:\s*(?P<dt>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_HOME_RE = re.compile(
    r'name="c1".*?<em class="pltxt"\s*>(?P<odds>\d+\.\d+)</em>.*?'
    + r'<span class="homenameobj homename"[^>]*>(?P<team>[^<]+)</span>',
    re.S,
)
_DRAW_RE = re.compile(r'name="c3".*?<em class="pltxt"\s*>(?P<odds>\d+\.\d+)</em>', re.S)
_AWAY_RE = re.compile(
    r'name="c5".*?<em class="pltxt"\s*>(?P<odds>\d+\.\d+)</em>.*?'
    + r'<span class="awaynameobj awayname"[^>]*>(?P<team>[^<]+)</span>',
    re.S,
)
_MATCH_ID_RE = re.compile(r"/soccer/match/(?P<mid>\d+)/")
_DEADLINE_RE = re.compile(
    r'<div class="overTime">截止时间:<em>\s*(?P<md>\d{2}-\d{2})\([^)]*\)\s*'
    + r"(?P<hm>\d{2}:\d{2})</em>"
)
_PERIOD_LINK_RE = re.compile(r'href="/zucai/(\d{5})/"')


@dataclass
class ParsedMatch:
    """一期一场对阵（源B 页面值；kickoff/deadline 已转 UTC ISO）。"""

    match_seq: int
    source_match_id: str | None
    league: str
    kickoff_utc: str
    home_team: str
    away_team: str
    euro_odds: tuple[float | None, float | None, float | None]


@dataclass
class ParsedIssue:
    """一个期次页的解析结果。"""

    period_no: str
    sales_deadline_utc: str | None
    matches: list[ParsedMatch] = field(default_factory=list)


@dataclass
class FetchedPage:
    """一次页面拉取（解码后文本 + 原始字节）。"""

    url: str
    text: str
    raw: bytes


@dataclass
class PoolSyncStats(pool_store.PoolSyncStats):
    """一次彩池同步统计（复用仓储形状，附来源常量与解析版本默认值）。"""

    source: str = SOURCE
    parse_version: str = PARSE_VERSION


def _headers(settings: Settings, period_no: str | None) -> dict[str, str]:
    """访问限制：浏览器 UA + 期次页 Referer（人气接口无 Referer → 405）。"""
    base = settings.zucai_base_url.rstrip("/")
    referer = f"{base}/zucai/{period_no}/" if period_no else f"{base}/zucai/"
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Referer": referer,
    }


def _cst_to_utc_iso(naive_cst: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """北京时间字符串 → UTC ISO（源页面无时区标记，业务时区既定 CST）。"""
    return (
        datetime.strptime(naive_cst, fmt).replace(tzinfo=CST).astimezone(UTC)
    ).isoformat(timespec="seconds")


def fetch_index_page(client: httpx.Client, settings: Settings) -> FetchedPage:
    """拉取传统足彩索引页（期次发现）。"""
    url = f"{settings.zucai_base_url.rstrip('/')}/zucai/"
    response = client.get(url, headers=_headers(settings, None), timeout=25.0)
    response.raise_for_status()
    return FetchedPage(
        url=url,
        text=response.content.decode("gb18030", errors="replace"),
        raw=response.content,
    )


def fetch_issue_page(
    client: httpx.Client, settings: Settings, period_no: str
) -> FetchedPage:
    """拉取一个期次页（对阵 + 截止时间）。"""
    url = f"{settings.zucai_base_url.rstrip('/')}/zucai/{period_no}/"
    response = client.get(url, headers=_headers(settings, period_no), timeout=25.0)
    response.raise_for_status()
    return FetchedPage(
        url=url,
        text=response.content.decode("gb18030", errors="replace"),
        raw=response.content,
    )


def fetch_popularity(
    client: httpx.Client, settings: Settings, period_no: str, *, kind: str = "percent"
) -> dict[str, Any]:
    """拉取一期人气分布（kind=percent 百分比 / number 票数）。"""
    base = settings.zucai_base_url.rstrip("/")
    response = client.get(
        f"{base}/ajax/",
        params={
            "method": "data.statistic.popularity",
            "format": "json",
            "LotteryNo": period_no,
            "LotteryType": "ToTo",
            "Type": kind,
            "Sort": "1",
        },
        headers=_headers(settings, period_no),
        timeout=25.0,
    )
    response.raise_for_status()
    return json.loads(response.content.decode("utf-8", errors="replace"))


def parse_index_periods(page: FetchedPage) -> list[str]:
    """索引页 → 期号列表（升序去重）。"""
    return sorted(set(_PERIOD_LINK_RE.findall(page.text)))


def parse_issue_page(page: FetchedPage, period_no: str) -> ParsedIssue:
    """期次页 → 对阵 + 截止时间（纯函数；不完整行直接跳过，宁缺勿错）。"""
    issue = ParsedIssue(period_no=period_no, sales_deadline_utc=None)
    deadline = _DEADLINE_RE.search(page.text)
    if deadline is not None:
        # 截止时间无年份：取页内最早开赛日的年份（期次与开赛同年）
        years = sorted(set(re.findall(r"比赛时间:\s*(\d{4})-", page.text)))
        year = years[0] if years else f"{datetime.now(CST).year}"
        try:
            issue.sales_deadline_utc = _cst_to_utc_iso(
                f"{year}-{deadline.group('md')} {deadline.group('hm')}:00"
            )
        except ValueError:
            issue.sales_deadline_utc = None
    for match in _TR_RE.finditer(page.text):
        seq = int(match.group("seq"))
        body = match.group("body")
        kickoff = _KICKOFF_RE.search(body)
        home = _HOME_RE.search(body)
        draw = _DRAW_RE.search(body)
        away = _AWAY_RE.search(body)
        if kickoff is None or home is None or away is None:
            continue  # 结构不完整的行不入（对阵缺失宁缺勿错）
        league = _LEAGUE_RE.search(body)
        mid = _MATCH_ID_RE.search(body)
        issue.matches.append(
            ParsedMatch(
                match_seq=seq,
                source_match_id=mid.group("mid") if mid else None,
                league=league.group("league").strip() if league else "",
                kickoff_utc=_cst_to_utc_iso(kickoff.group("dt")),
                home_team=home.group("team").strip(),
                away_team=away.group("team").strip(),
                euro_odds=(
                    float(home.group("odds")),
                    float(draw.group("odds")) if draw else None,
                    float(away.group("odds")),
                ),
            )
        )
    issue.matches.sort(key=lambda m: m.match_seq)
    return issue


_POP_TOTAL_MIN = 0.9  # 三向和下界（页面百分比口径的健全性护栏）
_POP_TOTAL_MAX = 1.1


def _map_pop_triple(triple: dict[str, Any], scale: float) -> dict[str, float]:
    """源B 一场三元组 → 官方池码数值（坏值跳过）。"""
    out: dict[str, float] = {}
    for key, value in triple.items():
        code = _POP_KEY_TO_POOL_CODE.get(str(key))
        if code is None:
            continue
        try:
            out[code] = float(value) * scale
        except (TypeError, ValueError):
            continue
    return out


def parse_popularity(
    percent: dict[str, Any], votes: dict[str, Any] | None = None
) -> dict[int, pool_store.PoolShareInput]:
    """
    人气接口 → 每场三向份额（0-1，官方池码）+ 可选票数（纯函数）。

    - 键映射：源B 1/2/3 → 官方 3（胜）/1（平）/0（负）；
    - 三向不全或和明显异常的场次整场丢弃（宁缺勿错）；
    - votes 同键映射（scale=1）。
    """
    raw = percent.get("statistic_popularity_response")
    if not isinstance(raw, dict):
        return {}
    response = cast("dict[str, Any]", raw)
    votes_response = (
        votes.get("statistic_popularity_response") if votes is not None else None
    )
    out: dict[int, pool_store.PoolShareInput] = {}
    for seq_raw, triple in response.items():
        try:
            seq = int(seq_raw)
        except ValueError:
            continue
        if not isinstance(triple, dict):
            continue
        shares = _map_pop_triple(cast("dict[str, Any]", triple), scale=0.01)
        total = sum(shares.values())
        if len(shares) != len(_POP_KEY_TO_POOL_CODE) or not (
            _POP_TOTAL_MIN <= total <= _POP_TOTAL_MAX
        ):
            continue
        vote_counts: dict[str, int] = {}
        if votes_response is not None:
            vtriple = votes_response.get(seq_raw)
            if isinstance(vtriple, dict):
                vote_counts = {
                    code: int(count)
                    for code, count in _map_pop_triple(
                        cast("dict[str, Any]", vtriple), scale=1.0
                    ).items()
                }
        out[seq] = pool_store.PoolShareInput(
            match_seq=seq, shares=shares, votes=vote_counts or None
        )
    return out


def sync_pool_data(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    period_nos: list[str] | None = None,
    window: int = DEFAULT_PERIOD_WINDOW,
    now: datetime | None = None,
) -> PoolSyncStats:
    """
    同步入口：期次发现 → 期次页 + 人气分布 → 入库（幂等可重放）。

    - period_nos 缺省 = 索引页发现的最新 ``window`` 期（升序）；
    - 对阵当前态刷新（replace）、份额追加快照（captured_at = 观测时点）；
    - 元信息写 pool_sync_runs（append-only）。
    """
    now_dt = now or datetime.now(UTC)
    stats = PoolSyncStats(observed_at=now_dt.isoformat(timespec="seconds"))
    if period_nos is None:
        index = fetch_index_page(client, settings)
        stats.pages += 1
        discovered = parse_index_periods(index)
        period_nos = discovered[-window:] if window > 0 else discovered
    stats.period_nos = list(period_nos)
    for period_no in period_nos:
        page = fetch_issue_page(client, settings, period_no)
        stats.pages += 1
        issue = parse_issue_page(page, period_no)
        pool_period_id = pool_store.upsert_pool_period(
            conn, PERIOD_MARKET, period_no, issue.sales_deadline_utc
        )
        if issue.matches:
            stats.matches += pool_store.replace_pool_matches(
                conn,
                pool_period_id,
                [
                    pool_store.PoolMatchInput(
                        match_seq=m.match_seq,
                        source_match_id=m.source_match_id,
                        league=m.league,
                        kickoff_utc=m.kickoff_utc,
                        home_team=m.home_team,
                        away_team=m.away_team,
                        euro_odds=m.euro_odds,
                    )
                    for m in issue.matches
                ],
            )
        percent = fetch_popularity(client, settings, period_no, kind="percent")
        votes = fetch_popularity(client, settings, period_no, kind="number")
        stats.pages += 2
        shares = parse_popularity(percent, votes)
        if shares:
            stats.share_rows += pool_store.insert_public_shares(
                conn,
                pool_period_id,
                sorted(shares.values(), key=lambda s: s.match_seq),
                origin="estimated",
                source=SOURCE,
                captured_at=stats.observed_at,
            )
        stats.missing_shares += len(issue.matches) - len(shares)
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
