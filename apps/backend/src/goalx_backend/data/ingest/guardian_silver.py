"""
NewsShock P0 抽取器（model-iterate 票 04，spec S4；research/26 P0 落地）。

卫报 bronze（news_article，21.4 万篇）→ silver ``guardian/news_event``
结构化事件表：场次关联（sid）、事件类型（伤停/停赛/主帅更迭/轮换线索/
其他）、队伍侧、置信度、抽取模型与版本。

管线（有界试点：单联赛 × 双赛季）：

1. **试点场次索引**：fixture_universe 过滤（联赛+赛季）；
2. **英文链接**：运行面 hist_matches（英文队名）经 corpus_gate
   match_fixtures（v3 名字身份轮，与 gold 同机）配到 sid——卫报正文
   是英文队名，中文语料名不直接匹配；
3. **候选文章预筛**（纯代码零成本）：标题词边界命中英文队名 × 发布日
   在 [match_date−3d, kickoff) 窗（PIT：发布必须早于开球，赛后稿
   计数剔除）；
4. **LLM 抽取**（scout 档零牌价，走 llm/client 熔断+记账）：约束
   JSON schema，违例 fail-loud 计数不静默丢；
5. **落盘**：parquet + _meta（版本戳+bronze digest+试点范围钉死），
   article_sid 幂等去重。

全量回填不进本模块（票 04 裁决门：试点信号成立另裁）。
"""

from __future__ import annotations

# pyarrow/duckdb 存根不全（gold 同例放宽）
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import pyarrow as pa
from loguru import logger
from openai import BadRequestError
from openai.types.chat import ChatCompletionMessageParam

from goalx_backend.config import Settings
from goalx_backend.data.corpus_gate import FixtureRow, HistRow, match_fixtures
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.leagues import FD_TO_LEAGUE
from goalx_backend.data.silver import write_dataset_meta, write_partition
from goalx_backend.llm.client import ModelTier, Purpose, glm_chat

PROVIDER = "guardian"
BRONZE_DATASET = "news_article"
EVENT_DATASET = "news_event"
EVENT_VERSION = "1"

EVENT_TYPES = ("injury", "suspension", "manager_change", "rotation_hint", "other")
VALID_SIDES = ("home", "away")
LINK_WINDOW_DAYS = 3  # 发布日窗：[match_date−3d, kickoff)
MAX_BODY_CHARS = 6000  # 抽取输入截断（成本护栏）

_SYSTEM_PROMPT = (
    "You extract structured football team-news events from a news article "
    "for a quant pipeline. Given the fixture (home team vs away team) and "
    "the article text, output STRICT JSON only:\n"
    '{"relevant": bool, "events": [{"type": "injury|suspension|'
    'manager_change|rotation_hint|other", "side": "home|away", '
    '"player": string-or-null, "confidence": 0.0-1.0}]}\n'
    "Rules: only events affecting the two teams' lineup/management for THIS "
    "fixture; relevant=false if the article is not about team news for this "
    "fixture (post-match reports, transfers unrelated to availability, "
    "league-wide news). No prose outside JSON."
)


@dataclass(frozen=True)
class _Fixture:
    sid: str
    league_cn: str
    kickoff_day: str
    kickoff_iso: str  # 真实开球时刻（PIT 判定用，非日粒度）
    season: str
    home_cn: str
    away_cn: str
    goals_home: int
    goals_away: int


@dataclass(frozen=True)
class _Link:
    sid: str
    kickoff_day: str
    kickoff_iso: str
    en_home: str
    en_away: str


@dataclass(frozen=True)
class _Candidate:
    article_sid: str
    published_at: str
    title: str
    body: str
    link: _Link
    side: str  # home/away（标题命中的队名侧）


@dataclass
class GuardianEventReport:
    """一次试点构建的报告。"""

    articles_scanned: int = 0
    candidates: int = 0
    extracted_articles: int = 0
    relevant_articles: int = 0
    events: int = 0
    schema_violations: int = 0
    content_filtered: int = 0
    post_kickoff_dropped: int = 0
    linked_sids: int = 0
    unlinked_hist_rows: int = 0
    model: str = ""
    llm_calls: int = 0
    estimated_cny: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """报告字段平铺（_meta 落盘）。"""
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def fixture_index(
    duck_con: duckdb.DuckDBPyConnection, competition: str, seasons: tuple[str, ...]
) -> list[_Fixture]:
    """试点场次（fixture_universe 过滤联赛码+赛季）。"""
    league = FD_TO_LEAGUE.get(competition)
    if league is None:
        raise ValueError(f"未知联赛码 {competition!r}")
    rows = duck_con.execute(
        """
        SELECT sid, league, strftime(CAST(kickoff AS DATE), '%Y-%m-%d'),
               CAST(kickoff AS VARCHAR), season, home, away,
               home_goals, away_goals
        FROM fixture_universe
        WHERE league = ? AND season IN (SELECT unnest(?))
          AND kickoff IS NOT NULL
        ORDER BY kickoff, sid
        """,
        [league, list(seasons)],
    ).fetchall()
    return [
        _Fixture(
            sid=str(r[0]),
            league_cn=str(r[1]),
            kickoff_day=str(r[2]),
            kickoff_iso=str(r[3]),
            season=str(r[4]),
            home_cn=str(r[5]),
            away_cn=str(r[6]),
            goals_home=int(r[7] or 0),
            goals_away=int(r[8] or 0),
        )
        for r in rows
    ]


def english_links(
    duck_con: duckdb.DuckDBPyConnection, fixtures: list[_Fixture], competition: str
) -> tuple[dict[str, _Link], int]:
    """
    英文队名链接：hist_matches × match_fixtures（v3 名字轮）→ sid。

    返回 (sid→link, 未配对 hist 行数)。同一机制服务 gold 配对（复用
    不另造）——名字身份+日期锚。
    """
    if not fixtures:
        return {}, 0
    fixture_rows = [
        FixtureRow(
            sid=f.sid,
            league=f.league_cn,
            kickoff_day=f.kickoff_day,
            goals_home=f.goals_home,
            goals_away=f.goals_away,
            home=f.home_cn,
            away=f.away_cn,
        )
        for f in fixtures
    ]
    day_min = min(f.kickoff_day for f in fixtures)
    day_max = max(f.kickoff_day for f in fixtures)
    rows = duck_con.execute(
        """
        SELECT competition, match_date, home_team, away_team, fthg, ftag
        FROM goalx.hist_matches
        WHERE competition = ? AND match_date BETWEEN ? AND ?
        ORDER BY match_date, home_team
        """,
        [competition, day_min, day_max],
    ).fetchall()
    hists = [
        HistRow(
            league_key=str(r[0]),
            match_date=str(r[1])[:10],
            home=str(r[2]),
            away=str(r[3]),
            goals_home=int(r[4] or 0),
            goals_away=int(r[5] or 0),
            psc_home=None,
            psc_draw=None,
            psc_away=None,
        )
        for r in rows
    ]
    matched = match_fixtures(fixture_rows, hists)
    pilot_by_sid = {f.sid: f for f in fixtures}
    links = {
        pair.sid: _Link(
            sid=pair.sid,
            kickoff_day=pair.kickoff_day,
            kickoff_iso=pilot_by_sid[pair.sid].kickoff_iso,
            en_home=hist.home,
            en_away=hist.away,
        )
        for pair, hist in matched.pairs
    }
    return links, len(matched.gaps)


def _kickoff_instant(link: _Link) -> datetime | None:
    """真实开球时刻（北京墙钟 naive → UTC aware：−8h；PIT 判定）。"""
    try:
        naive = datetime.fromisoformat(link.kickoff_iso.replace(" ", "T"))
    except ValueError:
        return None
    return naive.replace(tzinfo=UTC) - timedelta(hours=8)


def _word_boundary(name: str, text: str) -> bool:
    """词边界队名命中（Man City 不命中 Manchester City 的子串误报）。"""
    return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text) is not None


def candidate_articles(
    store: CorpusStore,
    links: dict[str, _Link],
    *,
    window_days: int = LINK_WINDOW_DAYS,
) -> tuple[list[_Candidate], int, int]:
    """
    文章预筛（纯代码）：标题词边界命中队名 × 发布窗 [match−3d, kickoff)。

    返回（候选, 扫描数, 赛后稿计数）。PIT：published_at ≥ kickoff 的
    命中稿计数剔除（事件表逐行断言发布早于开球）。
    """
    by_name: dict[str, list[tuple[_Link, str]]] = {}
    for link in links.values():
        by_name.setdefault(link.en_home, []).append((link, "home"))
        by_name.setdefault(link.en_away, []).append((link, "away"))

    candidates: list[_Candidate] = []
    scanned = 0
    post_kickoff = 0
    seen_articles: set[str] = set()
    for line in store.iter_bronze_lines(PROVIDER, BRONZE_DATASET):
        if not line:
            continue
        row = json.loads(line)
        payload = row.get("payload") or {}
        article_sid = str(row.get("sid") or payload.get("id") or "")
        if not article_sid or article_sid in seen_articles:
            continue
        seen_articles.add(article_sid)
        scanned += 1
        title = str(payload.get("webTitle") or "")
        published = str(payload.get("webPublicationDate") or "")
        if not title or not published:
            continue
        found, late = _link_article(
            article_sid,
            title,
            published,
            str(payload.get("bodyText") or ""),
            by_name,
            window_days,
        )
        candidates.extend(found)
        post_kickoff += late
    return candidates, scanned, post_kickoff


def _link_article(
    article_sid: str,
    title: str,
    published: str,
    body: str,
    by_name: dict[str, list[tuple[_Link, str]]],
    window_days: int,
) -> tuple[list[_Candidate], int]:
    """一篇文章 × 队名索引 → （窗内候选, 赛后稿命中数）。"""
    found: list[_Candidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    post_kickoff = 0
    pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
    for name, entries in by_name.items():
        if not _word_boundary(name, title):
            continue
        for link, side in entries:
            kickoff = _kickoff_instant(link)
            if kickoff is None or pub >= kickoff:
                post_kickoff += 1
                continue
            if pub < kickoff - timedelta(days=window_days + 1):
                continue
            key = (article_sid, link.sid)
            if key in seen_pairs:
                continue  # 同文同场只抽一次（提示词含两队，side 不重复计）
            seen_pairs.add(key)
            found.append(
                _Candidate(
                    article_sid=article_sid,
                    published_at=published,
                    title=title,
                    body=body,
                    link=link,
                    side=side,
                )
            )
    return found, post_kickoff


def parse_extraction(content: str) -> dict[str, Any] | None:
    """
    LLM 响应 → 结构 dict（schema 校验；违例返回 None 由调用方计数）。

    容忍 markdown 代码围栏（模型惯例）；字段越界（类型/侧/置信度范围）
    一律 None——fail-loud 计数不静默修。
    """
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    relevant = parsed.get("relevant")
    events = parsed.get("events")
    if not isinstance(relevant, bool) or not isinstance(events, list):
        return None  # 顶层 schema 违例
    cleaned_pairs = [_clean_event(e if isinstance(e, dict) else {}) for e in events]
    if not all(ok for ok, _ in cleaned_pairs):
        return None
    return {
        "relevant": relevant,
        "events": [cleaned for _, cleaned in cleaned_pairs],
    }


def _clean_event(event: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """单事件 schema 校验（False=违例，调用方 fail-loud 计数）。"""
    etype = event.get("type")
    side = event.get("side")
    confidence = event.get("confidence")
    player = event.get("player")
    if etype not in EVENT_TYPES or side not in VALID_SIDES:
        return False, {}
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return False, {}
    if not 0.0 <= confidence <= 1.0:
        return False, {}
    if player is not None and not isinstance(player, str):
        return False, {}
    return True, {
        "type": str(etype),
        "side": str(side),
        "player": player,
        "confidence": round(float(confidence), 3),
    }


class ContentFiltered(RuntimeError):
    """供应商内容过滤拦截（[1301]；真树实测个别博彩相关稿件触发）。"""


def _extract_with_llm(
    conn: sqlite3.Connection, settings: Settings, candidate: _Candidate
) -> tuple[dict[str, Any] | None, str, float]:
    """单篇 LLM 抽取（scout 档）；返回 (结构 dict|None, model, 估算成本)。"""
    link = candidate.link
    user_prompt = (
        f"Fixture: {link.en_home} (home) vs {link.en_away} (away), "
        f"date {link.kickoff_day}.\n"
        f"Article title: {candidate.title}\n"
        f"Article body:\n{candidate.body[:MAX_BODY_CHARS]}"
    )
    messages: list[ChatCompletionMessageParam] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    try:
        return _glm_once(conn, settings, messages)
    except ContentFiltered:
        raise
    except Exception:  # 网络瞬断等——重试一次
        return _glm_once(conn, settings, messages, temperature=0.2)


def _glm_once(
    conn: sqlite3.Connection,
    settings: Settings,
    messages: list[ChatCompletionMessageParam],
    *,
    temperature: float = 0.0,
) -> tuple[dict[str, Any] | None, str, float]:
    try:
        result = glm_chat(
            conn,
            settings,
            ModelTier.SCOUT,
            messages,
            purpose=Purpose.SCOUT,
            max_tokens=512,
            temperature=temperature,
        )
    except BadRequestError as exc:  # [1301] contentFilter——个别稿触发，跳过不崩
        if "1301" in str(exc) or "contentFilter" in str(exc):
            raise ContentFiltered(str(exc)) from exc
        raise
    return parse_extraction(result.content), result.model, result.estimated_cny


Extractor = Callable[[_Candidate], tuple[dict[str, Any] | None, str, float]]


@dataclass(frozen=True)
class NewsContext:
    """试点构建的运行面句柄捆（duck 桥 + 运行库 + LLM 设置）。"""

    duck_con: duckdb.DuckDBPyConnection
    conn: sqlite3.Connection
    settings: Settings


def build_news_events(
    ctx: NewsContext,
    store: CorpusStore,
    *,
    competition: str = "E0",
    seasons: tuple[str, ...] = ("2023-24", "2024-25"),
    article_limit: int = 400,
    extractor: Extractor | None = None,
) -> GuardianEventReport:
    """
    试点构建：链接 → 预筛 → 抽取 → silver 落盘（幂等覆盖）。

    extractor 注入点供测试（缺省真 LLM scout 档）。
    """
    fixtures = fixture_index(ctx.duck_con, competition, seasons)
    links, unlinked = english_links(ctx.duck_con, fixtures, competition)
    candidates, scanned, post_kickoff = candidate_articles(store, links)
    season_of = {f.sid: _season_of(fixtures, f.sid) for f in fixtures}

    report = GuardianEventReport(
        articles_scanned=scanned,
        candidates=len(candidates),
        post_kickoff_dropped=post_kickoff,
        linked_sids=len(links),
        unlinked_hist_rows=unlinked,
    )

    def default_extract(
        candidate: _Candidate,
    ) -> tuple[dict[str, Any] | None, str, float]:
        return _extract_with_llm(ctx.conn, ctx.settings, candidate)

    extract = extractor or default_extract
    event_rows: list[dict[str, Any]] = []
    for candidate in candidates[:article_limit]:
        try:
            parsed, model, cost = extract(candidate)
        except ContentFiltered:
            report.content_filtered += 1
            report.llm_calls += 1
            continue
        report.llm_calls += 1
        report.estimated_cny += cost
        report.model = model
        if parsed is None:
            report.schema_violations += 1
            continue
        report.extracted_articles += 1
        if not parsed["relevant"]:
            continue
        report.relevant_articles += 1
        for event in parsed["events"]:
            report.events += 1
            event_rows.append(
                {
                    "article_sid": candidate.article_sid,
                    "sid": candidate.link.sid,
                    "league": competition,
                    "season": season_of.get(candidate.link.sid, ""),
                    "published_at": candidate.published_at,
                    "kickoff_day": candidate.link.kickoff_day,
                    "kickoff_iso": candidate.link.kickoff_iso,
                    "event_type": event["type"],
                    "side": event["side"],
                    "player": event["player"],
                    "confidence": event["confidence"],
                    "model": model,
                    "version": EVENT_VERSION,
                }
            )
    _write_silver(store, event_rows, competition, seasons, report)
    return report


def _season_of(fixtures: list[_Fixture], sid: str) -> str:
    """Sid → 赛季（缺=空串如实入档）。"""
    return next((f.season for f in fixtures if f.sid == sid), "")


def _write_silver(
    store: CorpusStore,
    event_rows: list[dict[str, Any]],
    competition: str,
    seasons: tuple[str, ...],
    report: GuardianEventReport,
) -> None:
    """Parquet 覆盖 + _meta（bronze digest + 试点范围钉死）。"""
    bronze_path = store.bronze_path(PROVIDER, BRONZE_DATASET)
    digest = ""
    if bronze_path.exists():
        hasher = hashlib.sha256()
        with bronze_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()[:16]
    # PIT 终检：逐行断言发布时刻早于真实开球时刻（与候选过滤同口径）
    for row in event_rows:
        naive = datetime.fromisoformat(row["kickoff_iso"].replace(" ", "T"))
        kickoff = naive.replace(tzinfo=UTC) - timedelta(hours=8)
        pub = datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))
        if pub >= kickoff:
            raise ValueError(f"PIT 违例：{row['article_sid']} 发布不早于开球")
    root = store.silver_path(PROVIDER, EVENT_DATASET)
    write_partition(root / "all", event_rows, _event_schema())
    write_dataset_meta(
        root,
        {
            "version": EVENT_VERSION,
            "built_at": datetime.now().isoformat(timespec="seconds"),
            "rows": len(event_rows),
            "bronze_sha256_16": digest,
            "pilot": {"competition": competition, "seasons": list(seasons)},
            "report": report.as_dict(),
        },
    )
    logger.info(
        "guardian/news_event 试点：{} 场链接 / {} 候选 / {} 事件"
        + "（schema 违例 {}，赛后稿 {}）",
        report.linked_sids,
        report.candidates,
        report.events,
        report.schema_violations,
        report.post_kickoff_dropped,
    )


def _event_schema() -> pa.Schema:
    """事件表 schema。"""
    return pa.schema(
        [
            pa.field("article_sid", pa.string()),
            pa.field("sid", pa.string()),
            pa.field("league", pa.string()),
            pa.field("season", pa.string()),
            pa.field("published_at", pa.string()),
            pa.field("kickoff_day", pa.string()),
            pa.field("kickoff_iso", pa.string()),
            pa.field("event_type", pa.string()),
            pa.field("side", pa.string()),
            pa.field("player", pa.string()),
            pa.field("confidence", pa.float64()),
            pa.field("model", pa.string()),
            pa.field("version", pa.string()),
        ]
    )
