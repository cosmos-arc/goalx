"""
The Odds API 欧赔采集与竞彩↔欧赔 join（票 20）。

- sport key 动态发现（命名会变，勿硬编码——票 08 坑位备忘）。
- 每次调用按 sport×region×market 记 1 credit 进 CostLedger，
  日/月预算护栏（免费档 500 credits/月）。
- 冷启动 join：联赛（sport key）+ 开球时间窗 ±20 分钟（原型实测 90%），
  join 结果持久化在 fixtures.odds_api_event_id。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from goalx_backend import team_align
from goalx_backend.config import Settings
from goalx_backend.db import utc_now_iso
from goalx_backend.models import SnapshotInput, SnapshotPurpose
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store

# 想要覆盖的赛事前缀（Tier 1 全量 + 常见 Tier 2；_winner 类排除）
WANTED_PREFIXES = (
    "soccer_epl",
    "soccer_spain",
    "soccer_italy",
    "soccer_germany",
    "soccer_france",
    "soccer_uefa_champ",
    "soccer_uefa_europa",
    "soccer_netherlands",
)
JOIN_WINDOW_MINUTES = 20


class CreditBudgetExceeded(RuntimeError):
    """Raised when the odds-api credit guard would blow the budget."""


@dataclass
class ParsedEvent:
    """一个欧赔事件（h2h 各 bookmaker 报价）。"""

    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_utc: str
    books: dict[str, dict[str, float]]  # book key -> {"h": .., "d": .., "a": ..}


@dataclass
class OddsIngestStats:
    """一次欧赔采集的统计。"""

    events: int = 0
    snapshots: int = 0
    duplicate_snapshots: int = 0
    credits_used: int = 0
    unmatched: int = 0


@dataclass
class JoinReport:
    """一次 join 的统计与缺口清单。"""

    joined: int = 0
    tier1_joined: int = 0
    tier1_total: int = 0
    unmatched: list[dict[str, str]] = field(default_factory=list)


def month_start_utc(now: datetime | None = None) -> str:
    """本月 1 号 00:00 UTC（credit 月预算统计起点）。"""
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def day_start_utc(now: datetime | None = None) -> str:
    """今日 00:00 UTC（credit 日预算统计起点）。"""
    moment = now or datetime.now(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def check_credit_budget(conn: sqlite3.Connection, settings: Settings) -> None:
    """预算护栏：超日/月预算抛 CreditBudgetExceeded（票 20 验收）。"""
    month_used = rs_store.credit_usage(conn, month_start_utc())
    if month_used >= settings.odds_api_monthly_credit_budget:
        raise CreditBudgetExceeded(
            f"monthly credits {month_used} >= {settings.odds_api_monthly_credit_budget}"
        )
    day_used = rs_store.credit_usage(conn, day_start_utc())
    if day_used >= settings.odds_api_daily_credit_budget:
        raise CreditBudgetExceeded(
            f"daily credits {day_used} >= {settings.odds_api_daily_credit_budget}"
        )


def record_credits(conn: sqlite3.Connection, credits_used: int, note: str) -> None:
    """把本次消耗记入 CostLedger。"""
    rs_store.record_cost(
        conn,
        "odds_api_credit",
        units=float(credits_used),
        note=note,
        occurred_at=utc_now_iso(),
    )


def polite_client() -> httpx.Client:
    """带连接级重试与限速的采集客户端（票 19 重试+礼貌限速）。"""
    return httpx.Client(
        transport=httpx.HTTPTransport(retries=3),
        limits=httpx.Limits(max_connections=2),
    )


def discover_sport_keys(settings: Settings, client: httpx.Client) -> list[str]:
    """动态发现足球 sport key（过滤前缀与 _winner 盘）。"""
    response = client.get(
        f"{settings.odds_api_base_url}/sports",
        params={"apiKey": settings.odds_api_key},
        timeout=25.0,
    )
    response.raise_for_status()
    sports = response.json()
    return sorted(
        s["key"]
        for s in sports
        if s["key"].startswith(WANTED_PREFIXES) and not s["key"].endswith("_winner")
    )


def _outcome_prices(event: dict[str, Any]) -> dict[str, dict[str, float]]:
    """h2h 报价 → book -> {h/d/a}（按事件主客队名匹配 outcome 名）。"""
    books: dict[str, dict[str, float]] = {}
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    for bookmaker in event.get("bookmakers", []):
        prices: dict[str, float] = {}
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if not isinstance(price, int | float) or price <= 1:
                    continue
                if name == home:
                    prices["h"] = float(price)
                elif name == away:
                    prices["a"] = float(price)
                elif name == "Draw":
                    prices["d"] = float(price)
        if {"h", "a"} <= prices.keys():
            books[bookmaker["key"]] = prices
    return books


def parse_events(sport_key: str, events: list[dict[str, Any]]) -> list[ParsedEvent]:
    """把 Odds API odds 端点响应解析为 ParsedEvent 列表（纯函数）。"""
    parsed: list[ParsedEvent] = []
    for event in events:
        books = _outcome_prices(event)
        if not books:
            continue
        parsed.append(
            ParsedEvent(
                event_id=str(event["id"]),
                sport_key=sport_key,
                home_team=event["home_team"],
                away_team=event["away_team"],
                commence_utc=event["commence_time"],
                books=books,
            )
        )
    return parsed


def _event_commence_utc(event: ParsedEvent) -> datetime:
    """事件的 kickoff datetime（Odds API 返回 ISO UTC）。"""
    return datetime.fromisoformat(event.commence_utc.replace("Z", "+00:00"))


def store_events(
    conn: sqlite3.Connection,
    events: list[ParsedEvent],
    *,
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE,
) -> OddsIngestStats:
    """欧赔快照 append-only 入库（只写已 join 的 fixture）+ 事件英文名回填别名。"""
    stats = OddsIngestStats(events=len(events))
    by_event = {event.event_id: event for event in events}
    team_align.backfill_aliases_from_events(
        conn,
        [(event.event_id, event.home_team, event.away_team) for event in events],
    )
    joined_fixtures = conn.execute(
        "SELECT id, odds_api_event_id FROM fixtures WHERE odds_api_event_id IS NOT NULL"
    ).fetchall()
    for row in joined_fixtures:
        event = by_event.get(str(row["odds_api_event_id"]))
        if event is None:
            continue
        for book, prices in event.books.items():
            for selection, odds in prices.items():
                snapshot_id = fx_store.insert_odds_snapshot(
                    conn,
                    SnapshotInput(
                        fixture_id=int(row["id"]),
                        market_code="had",
                        selection_code=selection,
                        source=f"odds_api:{book}",
                        odds=odds,
                        captured_at=utc_now_iso(),
                        purpose=purpose,
                    ),
                )
                if snapshot_id is None:
                    stats.duplicate_snapshots += 1
                else:
                    stats.snapshots += 1
    conn.commit()
    return stats


def fetch_and_store_odds(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    markets: tuple[str, ...] = ("h2h",),
) -> OddsIngestStats:
    """
    完整欧赔采集：预算检查→发现 sport keys→拉取→join→入库→记账。

    markets 追加 totals 会按 sport 翻倍 credit 消耗（1 credit/market/sport），
    CLV 跟踪需要时再开启。
    """
    check_credit_budget(conn, settings)
    sport_keys = discover_sport_keys(settings, client)
    all_events: list[ParsedEvent] = []
    for sport_key in sport_keys:
        response = client.get(
            f"{settings.odds_api_base_url}/sports/{sport_key}/odds",
            params={
                "apiKey": settings.odds_api_key,
                "regions": "eu",
                "markets": ",".join(markets),
                "oddsFormat": "decimal",
            },
            timeout=25.0,
        )
        response.raise_for_status()
        all_events.extend(parse_events(sport_key, response.json()))
    stats = OddsIngestStats(
        events=len(all_events), credits_used=len(sport_keys) * len(markets)
    )
    join_report = join_fixtures(conn, all_events)
    stored = store_events(conn, all_events)
    stats.snapshots = stored.snapshots
    stats.duplicate_snapshots = stored.duplicate_snapshots
    record_credits(conn, stats.credits_used, f"sports={len(sport_keys)}")
    conn.commit()
    stats.unmatched = len(join_report.unmatched)
    return stats


def fetch_closing_window(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    window_minutes: int = 35,
    markets: tuple[str, ...] = ("h2h",),
) -> OddsIngestStats:
    """
    收盘窗口尽力快照（票 32）：kickoff 前 window_minutes 内开球的已 join 场次。

    只拉窗口内事件（commence_time_from/to），purpose=closing 入库；部署侧
    cron 在 −30/−10/−1min 附近多次触发（credit 按 sport×market 计）。
    """
    check_credit_budget(conn, settings)
    now = datetime.now(UTC)
    window_end = now + timedelta(minutes=window_minutes)
    sport_keys = discover_sport_keys(settings, client)
    all_events: list[ParsedEvent] = []
    for sport_key in sport_keys:
        response = client.get(
            f"{settings.odds_api_base_url}/sports/{sport_key}/odds",
            params={
                "apiKey": settings.odds_api_key,
                "regions": "eu",
                "markets": ",".join(markets),
                "oddsFormat": "decimal",
                "commence_time_from": now.isoformat(),
                "commence_time_to": window_end.isoformat(),
            },
            timeout=25.0,
        )
        response.raise_for_status()
        all_events.extend(parse_events(sport_key, response.json()))
    stats = OddsIngestStats(
        events=len(all_events), credits_used=len(sport_keys) * len(markets)
    )
    stored = store_events(conn, all_events, purpose=SnapshotPurpose.CLOSING)
    stats.snapshots = stored.snapshots
    stats.duplicate_snapshots = stored.duplicate_snapshots
    record_credits(conn, stats.credits_used, f"closing:sports={len(sport_keys)}")
    conn.commit()
    return stats


def join_fixtures(conn: sqlite3.Connection, events: list[ParsedEvent]) -> JoinReport:
    """联赛 + 开球时间窗冷启动 join；结果持久化（残余人工映射表补齐）。"""
    report = JoinReport()
    rows = conn.execute(
        """
        SELECT f.id, f.kickoff_utc, f.odds_api_event_id, f.home_team_id,
               f.away_team_id, c.odds_api_sport_key, c.tier
        FROM fixtures f
        JOIN match_codes mc ON mc.fixture_id = f.id AND mc.kind = 'jingcai'
        JOIN competitions c ON c.id = f.competition_id
        WHERE f.odds_api_event_id IS NULL AND c.odds_api_sport_key IS NOT NULL
        """
    ).fetchall()
    by_sport: dict[str, list[ParsedEvent]] = {}
    for event in events:
        by_sport.setdefault(event.sport_key, []).append(event)
    # 事件 1:1 不可复用：同刻多场（如两场意甲同 16:30 开球）时，
    # 已被其他 fixture 占用的事件不能再作为候选（否则别名映射会被污染）。
    used_events = {
        str(row["odds_api_event_id"])
        for row in conn.execute(
            "SELECT odds_api_event_id FROM fixtures WHERE odds_api_event_id IS NOT NULL"
        ).fetchall()
    }
    for row in rows:
        sport_key = str(row["odds_api_sport_key"])
        is_tier1 = row["tier"] == "tier1"
        if is_tier1:
            report.tier1_total += 1
        kickoff = datetime.fromisoformat(str(row["kickoff_utc"]))
        window = timedelta(minutes=JOIN_WINDOW_MINUTES)
        candidates = sorted(
            (
                event
                for event in by_sport.get(sport_key, [])
                if abs(_event_commence_utc(event) - kickoff) <= window
                and event.event_id not in used_events
            ),
            key=lambda e: abs(_event_commence_utc(e) - kickoff),
        )
        if not candidates:
            report.unmatched.append(
                {"fixture_id": str(row["id"]), "reason": "no_event_in_window"}
            )
            continue
        best = candidates[0]
        ambiguous = len(candidates) > 1 and _event_commence_utc(
            candidates[1]
        ) == _event_commence_utc(best)
        if ambiguous:
            # 歧义不落库：留待人工映射（set_odds_api_join method='manual'）
            report.unmatched.append(
                {"fixture_id": str(row["id"]), "reason": "ambiguous_time_window"}
            )
            continue
        fx_store.set_odds_api_join(
            conn, int(row["id"]), best.event_id, sport_key, "time_window"
        )
        used_events.add(best.event_id)
        # join 时同步持久化事件英文名（训练域↔预测域对齐输入，票 25）
        for team_id, alias in (
            (int(row["home_team_id"]), best.home_team),
            (int(row["away_team_id"]), best.away_team),
        ):
            conn.execute(
                """
                INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
                VALUES (?, 'odds_api', ?)
                """,
                (team_id, alias),
            )
        report.joined += 1
        if is_tier1:
            report.tier1_joined += 1
    conn.commit()
    return report
