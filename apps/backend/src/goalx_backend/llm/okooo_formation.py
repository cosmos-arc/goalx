"""
澳客阵容页伤停采集（票 09 定案主力源，2026-09-19 用户提供路径实测）。

``/soccer/match/{source_match_id}/formation/`` 服务端渲染伤停段，curl
直连可得（同站 qingbao/analysis 子路径被 WAF 拦而 formation 开放）。
场次 id **零映射**：``pool_matches.source_match_id`` 即澳客场次 id
（源B 期次页同源采集）。三源评估（formation vs qiumibao vs 500.com
JS 挑战）见票 09 Comments。

数据形状：主/客队伤停影响总额 + 每名球员（伤/停、位置【卫门中锋】、
身价、出场、进球）。一场一请求，日两拍 × ~28 场 ≈ 56 请求/日。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field

import httpx
from limits import RateLimitItemPerMinute
from loguru import logger

from goalx_backend.data import pool as pool_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.rate_limit import default_limiter, throttle

_BASE = "https://www.okooo.com"
_FORMATION_PATH = "/soccer/match/{match_id}/formation/"
_COLLECTOR = "okooo-formation"
_KIND = "伤停"
_SLEEP_SECONDS = 0.3
# 滑动窗口加顶（票 57）：0.3s 间距≈200/min 持续，300/min 纯加顶零行为变化
_RATE_CEILING = RateLimitItemPerMinute(300)
_MAX_LISTED = 6  # 单侧摘要最多列出的伤停人数

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.okooo.com/zucai/",
}


@dataclass(frozen=True)
class PlayerInjury:
    """一名伤停球员（state=伤/停；position=卫/门/中/锋）。"""

    state: str
    position: str
    name: str
    price: str
    detail: str  # "出场:0 进球:0"


@dataclass(frozen=True)
class FormationInjuries:
    """一场的伤停全貌（主客两侧 + 影响总额）。"""

    home_total: str
    away_total: str
    home: list[PlayerInjury] = field(default_factory=list)
    away: list[PlayerInjury] = field(default_factory=list)

    def is_empty(self) -> bool:
        """两侧均无名单（页面存在但无伤停——也是有效情报）。"""
        return not self.home and not self.away


@dataclass
class InjuryStats:
    """一次伤停采集的计数（幂等重跑 inserted=0 属正常）。"""

    matches_seen: int = 0
    with_source_id: int = 0
    inserted: int = 0
    skipped_known: int = 0
    fetch_failed: int = 0


_ITEM_RE = re.compile(
    r"""
    <div\s+class="item-state\s*(?P<cls>shang|ting)">(?P<state>伤|停)</div>
    \s*<div\s+class="item-text">(?P<pos>【[^】]*】)</div>
    .*?<a[^>]*class="name[^"]*"[^>]*>(?P<name>[^<]+)</a>
    \s*<div\s+class="price">(?P<price>[^<]*)</div>
    \s*<div\s+class="text">(?P<detail>[^<]*)</div>
    """,
    re.S | re.X,
)
_TOTAL_HOME_RE = re.compile(
    r'<div class="price">(?P<total>[^<]*)</div>\s*<div class="desc">主队伤停'
)
_TOTAL_AWAY_RE = re.compile(
    r'<div class="price">(?P<total>[^<]*)</div>\s*<div class="desc">客队伤停'
)


def fetch_formation_page(client: httpx.Client, source_match_id: str) -> str:
    """拉取阵容页 HTML（gbk 解码；非 200 抛 HTTPStatusError）。"""
    response = client.get(
        _BASE + _FORMATION_PATH.format(match_id=source_match_id),
        headers=_HEADERS,
        timeout=25.0,
    )
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


def parse_formation_injuries(html: str) -> FormationInjuries | None:
    """阵容页 → 伤停结构（页面无伤停段返回 None=不适用，空名单返回空结构）。"""
    home_marker = html.find("主队伤停")
    if home_marker < 0:
        return None
    away_marker = html.find("客队伤停")
    if away_marker < 0:
        return None
    home_block = html[
        home_marker : away_marker if away_marker > home_marker else len(html)
    ]
    away_block = html[away_marker : away_marker + 8000]

    def _parse_items(block: str) -> list[PlayerInjury]:
        return [
            PlayerInjury(
                state=m.group("state"),
                position=m.group("pos"),
                name=m.group("name").strip(),
                price=m.group("price").strip(),
                detail=m.group("detail").strip(),
            )
            for m in _ITEM_RE.finditer(block)
        ]

    home_total = _TOTAL_HOME_RE.search(html[home_marker - 200 : home_marker + 20])
    away_total = _TOTAL_AWAY_RE.search(html[away_marker - 200 : away_marker + 20])
    return FormationInjuries(
        home_total=home_total.group("total") if home_total else "",
        away_total=away_total.group("total") if away_total else "",
        home=_parse_items(home_block),
        away=_parse_items(away_block),
    )


def _side_text(label: str, total: str, players: list[PlayerInjury]) -> str:
    """一侧伤停摘要（空名单显式说明）。"""
    if not players:
        return f"{label}（影响{total or '未知'}）：无在伤名单"
    names = "、".join(
        f"{p.state}{p.position}{p.name}({p.price},{p.detail})"
        for p in players[:_MAX_LISTED]
    )
    suffix = " 等" if len(players) > _MAX_LISTED else ""
    return f"{label}（影响{total}）：{names}{suffix}"


def _draft_for(injuries: FormationInjuries, moment: str) -> IntelDraft:
    """一场伤停 → 存证情报（主客合并一条）。"""
    return IntelDraft(
        kind=_KIND,
        text="；".join(
            [
                _side_text("主队伤停", injuries.home_total, injuries.home),
                _side_text("客队伤停", injuries.away_total, injuries.away),
            ]
        ),
        source="okooo.com/formation",
        collected_at=moment,
        collector=_COLLECTOR,
        raw_payload={
            "home_total": injuries.home_total,
            "away_total": injuries.away_total,
            "home": [p.__dict__ for p in injuries.home],
            "away": [p.__dict__ for p in injuries.away],
        },
    )


def collect_injury_intel(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    now: str | None = None,
) -> InjuryStats:
    """当期在售彩池场次的伤停情报（幂等；单页失败告警跳过不阻塞）。"""
    stats = InjuryStats()
    moment = now or utc_now_iso()
    limiter = default_limiter()
    drafts: list[tuple[int, IntelDraft]] = []
    with atomic(conn):
        for market_code in ("ttt14", "pick9"):
            for period in pool_store.list_pool_periods(conn, market_code):
                deadline = period["sales_deadline"]
                if deadline is None or str(deadline) < moment:
                    continue
                for row in pool_store.pool_matches_for_period(conn, int(period["id"])):
                    stats.matches_seen += 1
                    fixture_id = pool_store.match_fixture_id(
                        conn,
                        str(row["kickoff_utc"]),
                        str(row["home_team"]),
                        str(row["away_team"]),
                    )
                    source_id = row["source_match_id"]
                    if fixture_id is None or not source_id:
                        continue  # 无桥接或无源场次 id——无从拉取
                    stats.with_source_id += 1
                    try:
                        throttle(limiter, _RATE_CEILING)
                        html = fetch_formation_page(client, str(source_id))
                    except httpx.HTTPError as exc:
                        stats.fetch_failed += 1
                        logger.warning("formation 拉取失败 {}: {}", source_id, exc)
                        continue
                    time.sleep(_SLEEP_SECONDS)
                    injuries = parse_formation_injuries(html)
                    if injuries is None:
                        continue  # 页面无伤停段（历史场次等）——不产情报
                    drafts.append((fixture_id, _draft_for(injuries, moment)))
        for fixture_id, draft in drafts:
            if insert_intel_observation(conn, fixture_id, draft):
                stats.inserted += 1
            else:
                stats.skipped_known += 1
    return stats


def injury_stats_dict(stats: InjuryStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "matches_seen": stats.matches_seen,
        "with_source_id": stats.with_source_id,
        "inserted": stats.inserted,
        "skipped_known": stats.skipped_known,
        "fetch_failed": stats.fetch_failed,
    }
