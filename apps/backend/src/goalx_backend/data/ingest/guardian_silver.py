"""
NewsShock P0 抽取器（model-iterate 票 04，spec S4；research/26 P0 落地）。

卫报 bronze（news_article，21.4 万篇）→ silver ``guardian/news_event``
结构化事件表：场次关联（sid）、事件类型（伤停/停赛/主帅更迭/轮换线索/
其他）、队伍侧、置信度、抽取模型与版本。

分层（ADR-0008）：本模块=语料管线+SQL（住所 data.ingest）；LLM 调用
胶水住 ``llm/guardian_extract``（llm 层在本层之上），经 ``Extractor``
缝注入——data.ingest 不许上翻 import llm（caliber-loop 票 01 归位，
修复 model-iterate 票 04 遗留的 main 红 CI）。

管线（有界试点：单联赛 × 双赛季）：

1. **试点场次索引**：fixture_universe 过滤（联赛+赛季）；
2. **英文链接**：运行面 hist_matches（英文队名）经 corpus_gate
   match_fixtures（v3 名字身份轮，与 gold 同机）配到 sid——卫报正文
   是英文队名，中文语料名不直接匹配；
3. **候选文章预筛**（纯代码零成本）：标题词边界命中英文队名 × 发布日
   在 [kickoff−3d, kickoff) 窗（时刻锚定）（PIT：发布必须早于开球，赛后稿
   计数剔除）；
4. **LLM 抽取**（Extractor 缝注入，生产胶水=llm/guardian_extract，
   scout 档零牌价，走 llm/client 熔断+记账）：约束 JSON schema，
   违例 fail-loud 计数不静默丢；
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_gate import FixtureRow, HistRow, match_fixtures
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.fixtures import beijing_naive_to_utc
from goalx_backend.data.leagues import FD_TO_LEAGUE
from goalx_backend.data.silver import (
    PIT_STRICT,
    PIT_UNPROVEN,
    write_dataset_meta,
    write_partition,
)

PROVIDER = "guardian"
BRONZE_DATASET = "news_article"
EVENT_DATASET = "news_event"
EVENT_VERSION = "2"  # 票 02：checkpoint 复合键+pit_group 列

EVENT_TYPES = ("injury", "suspension", "manager_change", "rotation_hint", "other")
VALID_SIDES = ("home", "away")
LINK_WINDOW_DAYS = 3  # 发布窗：[kickoff−3d, kickoff)（时刻锚定）


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
class Candidate:
    """抽取候选（公共名：Extractor 注入缝入参，llm 胶水引用）。"""

    article_sid: str
    published_at: str
    title: str
    body: str
    link: _Link
    side: str  # home/away（标题命中的队名侧）
    pit_group: str  # strict_pit/unproven（lastModified 版本组，票 02）


@dataclass
class GuardianEventReport:
    """一次试点构建的报告。"""

    articles_scanned: int = 0
    candidates: int = 0
    extracted_articles: int = 0
    relevant_articles: int = 0
    events: int = 0
    schema_violations: int = 0
    empty_responses: int = 0
    content_filtered: int = 0
    post_kickoff_hits: int = 0
    limit_reached: bool = False
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


def _parse_instant(value: str) -> datetime | None:
    """
    ISO 时间戳（Z/偏移）→ aware datetime；naive/非法 → None。

    tz-strict（票 02 correctness）：naive 返回 None 而非带 naive 对象——
    与 aware kickoff 比较会 TypeError 崩预筛（bronze append-only 毒丸行
    致重放永久卡死）。调用点全部保守降级：_pit_group→unproven、
    候选窗→不产候选、PIT 终检→清晰违例。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _kickoff_instant(link: _Link) -> datetime | None:
    """真实开球时刻（PIT 判定；转换=data.fixtures.beijing_naive_to_utc 单一真相源）。"""
    return beijing_naive_to_utc(link.kickoff_iso)


def _word_boundary(name: str, text: str) -> bool:
    """词边界队名命中（Man City 不命中 Manchester City 的子串误报）。"""
    return re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text) is not None


def candidate_articles(
    store: CorpusStore,
    links: dict[str, _Link],
    *,
    window_days: int = LINK_WINDOW_DAYS,
) -> tuple[list[Candidate], int, int]:
    """
    文章预筛（纯代码）：标题词边界命中队名 × 发布窗 [match−3d, kickoff)。

    返回（候选, 扫描数, 赛后稿计数）。PIT：published_at ≥ kickoff 的
    命中稿计数剔除（事件表逐行断言发布早于开球）。
    """
    by_name: dict[str, list[tuple[_Link, str]]] = {}
    for link in links.values():
        by_name.setdefault(link.en_home, []).append((link, "home"))
        by_name.setdefault(link.en_away, []).append((link, "away"))

    candidates: list[Candidate] = []
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
            str(payload.get("lastModified") or ""),
            by_name,
            window_days,
        )
        candidates.extend(found)
        post_kickoff += late
    return candidates, scanned, post_kickoff


def _pit_group(last_modified: str, kickoff: datetime) -> str:
    """
    LastModified 版本组：终版早于开球=strict_pit，否则/缺失=unproven。

    webPublicationDate PIT 门槛不变（候选过滤另行判）；lastModified 只
    加严不放宽——分组标记供报告侧不混合下结论（票 02）。
    """
    lm = _parse_instant(last_modified)
    if lm is not None and lm < kickoff:
        return PIT_STRICT
    return PIT_UNPROVEN


def _link_article(
    article_sid: str,
    title: str,
    published: str,
    body: str,
    last_modified: str,
    by_name: dict[str, list[tuple[_Link, str]]],
    window_days: int,
) -> tuple[list[Candidate], int]:
    """一篇文章 × 队名索引 → （窗内候选, 赛后稿命中数）。"""
    found: list[Candidate] = []
    seen_pairs: set[tuple[str, str]] = set()
    post_kickoff = 0
    pub = _parse_instant(published)
    if pub is None:
        return found, post_kickoff  # 非法时间戳：不产候选
    for name, entries in by_name.items():
        if not _word_boundary(name, title):
            continue
        for link, side in entries:
            kickoff = _kickoff_instant(link)
            if kickoff is None or pub >= kickoff:
                post_kickoff += 1
                continue
            if pub < kickoff - timedelta(days=window_days):
                continue
            key = (article_sid, link.sid)
            if key in seen_pairs:
                continue  # 同文同场只抽一次（提示词含两队，side 不重复计）
            seen_pairs.add(key)
            found.append(
                Candidate(
                    article_sid=article_sid,
                    published_at=published,
                    title=title,
                    body=body,
                    link=link,
                    side=side,
                    pit_group=_pit_group(last_modified, kickoff),
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


# 抽取缝（ADR-0008 依赖倒置）：LLM 调用住 llm/guardian_extract（llm 层
# 在本层之上，不可上翻 import）；返回 checkpoint 条目体（filtered/parsed/
# model/cost/empty[/error]），ContentFiltered 等供应商分账在胶水内消化。
Extractor = Callable[[Candidate], dict[str, Any]]


def _body_hash(candidate: Candidate) -> str:
    """候选正文 sha256 前 16 位（键与条目共用一处，防口径漂移）。"""
    return hashlib.sha256(candidate.body.encode()).hexdigest()[:16]


def checkpoint_key(candidate: Candidate) -> str:
    """
    复合键=article|fixture|body_hash|extractor 版本（票 02）。

    一稿命中多场是链接层设计（各对独立抽取）；复合键消除断点重跑时
    A 场结果记到 B 场的交叉污染。键内含版本与 hash=条目自描述，可从
    文件直接判断可否复用。
    """
    return "|".join(
        (
            candidate.article_sid,
            candidate.link.sid,
            _body_hash(candidate),
            f"v{EVENT_VERSION}",
        )
    )


def _extract_one(extract: Extractor, candidate: Candidate) -> dict[str, Any]:
    """单篇抽取（缝注入）→ 自描述 checkpoint 条目（复合键+身份字段）。"""
    return {
        "key": checkpoint_key(candidate),
        "article_sid": candidate.article_sid,
        "sid": candidate.link.sid,
        "body_hash": _body_hash(candidate),
        "extractor_version": EVENT_VERSION,
        **extract(candidate),
    }


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
    extractor: Extractor,
) -> GuardianEventReport:
    """
    试点构建：链接 → 预筛 → 抽取 → silver 落盘（幂等覆盖）。

    extractor 必注入（生产=llm.guardian_extract.make_extractor；测试=假件）。
    """
    fixtures = fixture_index(ctx.duck_con, competition, seasons)
    links, unlinked = english_links(ctx.duck_con, fixtures, competition)
    candidates, scanned, post_kickoff = candidate_articles(store, links)
    season_of = {f.sid: f.season for f in fixtures}

    report = GuardianEventReport(
        articles_scanned=scanned,
        candidates=len(candidates),
        post_kickoff_hits=post_kickoff,
        linked_sids=len(links),
        unlinked_hist_rows=unlinked,
    )

    selected = candidates[:article_limit]
    report.limit_reached = len(candidates) > article_limit
    checkpoint = _checkpoint_path(store)
    done = _load_checkpoint(checkpoint)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)  # 首跑建目录
    event_rows: list[dict[str, Any]] = []
    for candidate in selected:
        entry = done.get(checkpoint_key(candidate))
        if entry is None:
            entry = _extract_one(extractor, candidate)
            with checkpoint.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _tally_entry(entry, candidate, report, season_of, event_rows, competition)
    _write_silver(store, event_rows, competition, seasons, report)
    checkpoint.unlink(missing_ok=True)  # 全量落盘成功——checkpoint 使命完成
    return report


def _load_checkpoint(checkpoint: Path) -> dict[str, dict[str, Any]]:
    """
    断点续跑：按复合键复用（熔断/中断后重跑跳过）。

    旧格式行（article_sid 键，无 ``key``）不迁移、直接跳过——按新键
    自然重抽（票 02 裁决，存量 90 篇随版本变更触发）。
    """
    done: dict[str, dict[str, Any]] = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entry = json.loads(line)
            key = entry.get("key")
            if key:
                done[str(key)] = entry
    return done


def _tally_entry(
    entry: dict[str, Any],
    candidate: Candidate,
    report: GuardianEventReport,
    season_of: dict[str, str],
    event_rows: list[dict[str, Any]],
    competition: str,
) -> None:
    """一条抽取结果入计数器与事件行集。"""
    report.llm_calls += 1
    if entry.get("filtered"):
        report.content_filtered += 1
        return
    report.estimated_cny += float(entry.get("cost", 0.0))
    report.model = str(entry.get("model", ""))
    parsed = entry.get("parsed")
    if parsed is None:
        if entry.get("empty"):
            report.empty_responses += 1
        else:
            report.schema_violations += 1
        return
    report.extracted_articles += 1
    if not parsed["relevant"]:
        return
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
                "pit_group": candidate.pit_group,
                "model": str(entry.get("model", "")),
                "version": EVENT_VERSION,
            }
        )


def _checkpoint_path(store: CorpusStore) -> Path:
    """断点续跑文件（silver 数据集根 _checkpoint.jsonl）。"""
    return store.silver_path(PROVIDER, EVENT_DATASET) / "_checkpoint.jsonl"


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
        link = _Link(
            sid=row["sid"],
            kickoff_day=row["kickoff_day"],
            kickoff_iso=row["kickoff_iso"],
            en_home="",
            en_away="",
        )
        kickoff = _kickoff_instant(link)
        pub = _parse_instant(row["published_at"])
        if kickoff is None or pub is None or pub >= kickoff:
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
        report.post_kickoff_hits,
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
            pa.field("pit_group", pa.string()),
            pa.field("model", pa.string()),
            pa.field("version", pa.string()),
        ]
    )
