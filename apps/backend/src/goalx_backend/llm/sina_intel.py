"""
新浪 AI 网关情报采集（票 09 补六/补七调研，2026-09-20 落地）。

纯 JSON 网关 ``mix.lottery.sina.com.cn/gateway/index/entry``，curl 直连
（参数约定从 AI 页 JS bundle 逆出：``format=json&__caller__=web&__version__
=1.0.0&__verno__=1``）。两接口：

- ``jczqOnSellMatches&gameTypes=spf``：竞彩在售场次（matchNo 竞彩编号 +
  matchId 新浪 id + 队名）——映射走 matchNo 一跳（比按日+队名宽松匹配稳）；
- ``footballMatchTeamInjury&matchId=``：伤停（伤因/位置/缺席场次/起止），
  与澳客 formation 互补互校验（澳客有身价+影响总额，新浪有伤因+缺席场次）。

定位：辅助源（伤停主力仍为澳客 formation，PR #34）。同内容双源存证
自然发生（collector 不同 → raw_hash 不同 → 各自一条 intel）。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import httpx
from limits import RateLimitItemPerMinute
from loguru import logger

from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.llm.store import IntelDraft, insert_intel_observation
from goalx_backend.rate_limit import default_limiter, throttle

_BASE = "https://mix.lottery.sina.com.cn/gateway/index/entry"
_QUERY = "format=json&__caller__=web&__version__=1.0.0&__verno__=1"
_COLLECTOR = "sina-injury"
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
    "Referer": "https://lottery.sina.com.cn/ai/",
}


@dataclass(frozen=True)
class SinaPlayer:
    """一名伤停/存疑球员（typeCn=受伤/停赛/出战成疑…）。"""

    name: str
    position: str  # 前锋/后卫/门将/中场
    type_cn: str
    reason: str
    missed_matches: str
    start_time: str
    end_time: str


@dataclass(frozen=True)
class SinaInjuries:
    """一场的新浪伤停全貌（主客两侧）。"""

    team1: list[SinaPlayer] = field(default_factory=list)
    team2: list[SinaPlayer] = field(default_factory=list)

    def is_empty(self) -> bool:
        """两侧均无名单（网关无数据=不产情报）。"""
        return not self.team1 and not self.team2


@dataclass
class SinaStats:
    """一次新浪采集的计数（幂等重跑 inserted=0 属正常）。"""

    on_sell: int = 0
    mapped: int = 0
    inserted: int = 0
    skipped_known: int = 0
    fetch_failed: int = 0


def _parse_player(row: dict[str, object]) -> SinaPlayer:
    """网关球员行 → 结构（get 兜底：字段缺失不炸整个采集）。"""

    def s(key: str) -> str:
        value = row.get(key, "")
        return str(value) if value is not None else ""

    return SinaPlayer(
        name=s("playerName"),
        position=s("positionCn"),
        type_cn=s("typeCn"),
        reason=s("reason"),
        missed_matches=s("missedMatches"),
        start_time=s("startTime"),
        end_time=s("endTime"),
    )


def parse_injuries(payload: dict[str, object]) -> SinaInjuries:
    """FootballMatchTeamInjury 完整响应 → 结构（data 缺失=无伤停数据）。"""
    result = cast(dict[str, object] | None, payload.get("result"))
    if result is None:
        return SinaInjuries()
    data = cast(dict[str, object] | None, result.get("data"))
    if data is None:
        return SinaInjuries()
    team1 = cast(list[object] | None, data.get("team1")) or []
    team2 = cast(list[object] | None, data.get("team2")) or []
    return SinaInjuries(
        team1=[
            _parse_player(cast(dict[str, object], r))
            for r in team1
            if isinstance(r, dict)
        ],
        team2=[
            _parse_player(cast(dict[str, object], r))
            for r in team2
            if isinstance(r, dict)
        ],
    )


def parse_on_sell(payload: dict[str, object]) -> list[dict[str, str]]:
    """
    JczqOnSellMatches 响应 → [{matchNo, matchId, team1, team2, kickoff}]。

    kickoff 由 matchTime（epoch 秒，新浪口径=北京时间）转 UTC ISO——与
    fixtures.kickoff_utc 同存续口径。
    """
    result = cast(dict[str, object] | None, payload.get("result"))
    rows = cast(list[object] | None, result.get("data") if result else None) or []
    out: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        row = cast(dict[str, object], item)
        if not row.get("matchNo"):
            continue  # 非竞彩编号场次（列表混有北单等）——不参与映射
        epoch = cast(str | int | None, row.get("matchTime"))
        try:
            kickoff = (
                datetime.fromtimestamp(int(epoch or 0), tz=UTC).isoformat(
                    timespec="seconds"
                )
                if epoch
                else ""
            )
        except (TypeError, ValueError):
            kickoff = ""
        out.append(
            {
                "matchNo": str(row.get("matchNo", "")),
                "matchId": str(row.get("matchId", "")),
                "team1": str(row.get("team1", "")),
                "team2": str(row.get("team2", "")),
                "kickoff": kickoff,
            }
        )
    return out


def _match_no_fixture_id(conn: sqlite3.Connection, match_no: str) -> int | None:
    """竞彩编号（周日002）→ fixture_id（fixtures 仓储一跳，ADR-0008）。"""
    return fx_store.fixture_id_for_match_code(conn, match_no)


def _side_text(label: str, players: list[SinaPlayer]) -> str:
    if not players:
        return f"{label}：无伤停名单"
    names = "、".join(
        f"{p.type_cn}{p.position}{p.name}({p.reason}"
        + (f"，缺{p.missed_matches}场" if p.missed_matches not in ("", "0") else "")
        + ")"
        for p in players[:_MAX_LISTED]
    )
    suffix = " 等" if len(players) > _MAX_LISTED else ""
    return f"{label}：{names}{suffix}"


def _draft_for(
    injuries: SinaInjuries, match: dict[str, str], moment: str
) -> IntelDraft:
    return IntelDraft(
        kind=_KIND,
        text="；".join(
            [
                _side_text(f"{match['team1']}伤停", injuries.team1),
                _side_text(f"{match['team2']}伤停", injuries.team2),
            ]
        ),
        source="sina.com/gateway",
        collected_at=moment,
        collector=_COLLECTOR,
        raw_payload={
            "sina_match_id": match["matchId"],
            "match_no": match["matchNo"],
            "team1": [p.__dict__ for p in injuries.team1],
            "team2": [p.__dict__ for p in injuries.team2],
        },
    )


def _gw_json(client: httpx.Client, cat1: str, **params: str) -> dict[str, object]:
    """一次网关调用 → JSON dict（非 200/坏 JSON 抛异常由调用方计数）。"""
    query = "&".join([_QUERY, f"cat1={cat1}", *(f"{k}={v}" for k, v in params.items())])
    response = client.get(
        f"{_BASE}?{query}", headers=_HEADERS, timeout=20.0, follow_redirects=True
    )
    response.raise_for_status()
    payload: dict[str, object] = response.json()
    status = cast(dict[str, object] | None, payload.get("status"))
    if status is not None and status.get("code") not in (0, None, "0"):
        raise ValueError(f"sina gateway status: {status}")
    return payload


def collect_sina_injury_intel(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    now: str | None = None,
) -> SinaStats:
    """竞彩在售场次的新浪伤停情报（幂等；单场失败跳过不阻塞）。"""
    stats = SinaStats()
    moment = now or utc_now_iso()
    limiter = default_limiter()
    try:
        throttle(limiter, _RATE_CEILING)
        on_sell_payload = _gw_json(client, "jczqOnSellMatches", gameTypes="spf")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("新浪在售列表拉取失败: {}", exc)
        stats.fetch_failed += 1
        return stats
    matches = parse_on_sell(on_sell_payload)
    stats.on_sell = len(matches)
    drafts: list[tuple[int, IntelDraft]] = []
    with atomic(conn):
        for match in matches:
            fixture_id = _match_no_fixture_id(conn, match["matchNo"])
            if fixture_id is None:
                continue  # 本库无此竞彩编号（未入库/已下架）——无从挂情报
            stats.mapped += 1
            try:
                throttle(limiter, _RATE_CEILING)
                injury_payload = _gw_json(
                    client, "footballMatchTeamInjury", matchId=match["matchId"]
                )
            except (httpx.HTTPError, ValueError) as exc:
                stats.fetch_failed += 1
                logger.warning("新浪伤停拉取失败 {}: {}", match["matchNo"], exc)
                continue
            time.sleep(_SLEEP_SECONDS)
            injuries = parse_injuries(injury_payload)
            if injuries.is_empty():
                continue  # 网关无该场伤停数据——不产情报（不虚构）
            drafts.append((fixture_id, _draft_for(injuries, match, moment)))
        for fixture_id, draft in drafts:
            if insert_intel_observation(conn, fixture_id, draft):
                stats.inserted += 1
            else:
                stats.skipped_known += 1
    return stats


def sina_stats_dict(stats: SinaStats) -> dict[str, object]:
    """统计转字典（flow 日志/CLI 输出用）。"""
    return {
        "on_sell": stats.on_sell,
        "mapped": stats.mapped,
        "inserted": stats.inserted,
        "skipped_known": stats.skipped_known,
        "fetch_failed": stats.fetch_failed,
    }
