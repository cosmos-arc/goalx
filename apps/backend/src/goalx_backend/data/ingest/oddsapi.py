"""
The Odds API 欧赔采集与竞彩↔欧赔 join（票 20；票 35 补观测证据与预算记账）。

- sport key 动态发现（命名会变，勿硬编码——票 08 坑位备忘）。
- credit 逐请求记账（票 35）：发送前在 IMMEDIATE 事务内预留并检查
  日/月预算（并发安全），请求失败退回负数行，成功行保留——部分失败
  也不会漏记已耗额度。
- 时间语义（票 35）：observed_at = 本机收到响应时间；source_updated_at =
  事件 last_update（源自报）；captured_at 对新行 = 源时间（源时间未知时
  = observed_at，此时报价只观察不进正式候选）。旧数据 captured_at 按
  本源解释为观测时间。
- 同公司完整三向才可比较（票 35）：缺任一向的 bookmaker 整体丢弃，
  不产生部分可信报价。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import observations
from goalx_backend.data import results as rs_store
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling import team_align
from goalx_backend.models import (
    ObservationInput,
    ObservationPurpose,
    SnapshotInput,
    SnapshotPurpose,
)

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
PARSE_VERSION = "oddsapi_h2h_v2"
ALLOWED_MARKETS = ("h2h",)


class CreditBudgetExceeded(RuntimeError):
    """Raised when the odds-api credit guard would blow the budget."""


@dataclass
class ParsedEvent:
    """一个欧赔事件（h2h 各 bookmaker 完整三向报价）。"""

    event_id: str
    sport_key: str
    home_team: str
    away_team: str
    commence_utc: str
    books: dict[str, dict[str, float]]  # book key -> {"h": .., "d": .., "a": ..}
    last_update_utc: str | None = None
    observed_at: str | None = None
    observation_id: int | None = None


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


def reserve_credits(
    conn: sqlite3.Connection, settings: Settings, count: int, note: str
) -> int:
    """
    预留 credits 并检查预算（票 35 验收 6）。

    IMMEDIATE 事务把「查用量 + 记账」原子化：并发/重复任务不会双双
    通过检查；预留行立即计入用量（保守——中途崩溃宁可高估不漏记）。
    月预算不因日限额调整而放宽（两道独立上限都检查）。
    """
    now = datetime.now(UTC)
    # 先冲刷本连接待写事务，避免与显式 BEGIN IMMEDIATE 冲突
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        month_used = rs_store.credit_usage(conn, month_start_utc(now))
        month_budget = settings.odds_api_monthly_credit_budget
        if month_used + count > month_budget:
            raise CreditBudgetExceeded(
                f"monthly credits {month_used}+{count} > {month_budget}"
            )
        day_used = rs_store.credit_usage(conn, day_start_utc(now))
        day_budget = settings.odds_api_daily_credit_budget
        if day_used + count > day_budget:
            raise CreditBudgetExceeded(
                f"daily credits {day_used}+{count} > {day_budget}"
            )
        row_id = rs_store.record_cost(
            conn,
            "odds_api_credit",
            units=float(count),
            note=note,
            occurred_at=now.isoformat(timespec="seconds"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row_id


def refund_credits(conn: sqlite3.Connection, count: int, note: str) -> None:
    """退回未消耗的预留（请求未发出/失败）：负数行抵扣，账目可审计。"""
    rs_store.record_cost(
        conn,
        "odds_api_credit",
        units=-float(count),
        note=f"refund: {note}",
        occurred_at=utc_now_iso(),
    )
    conn.commit()


def record_credits(conn: sqlite3.Connection, credits_used: int, note: str) -> None:
    """把本次消耗记入 CostLedger（不带预算检查；预留给 reserve_credits）。"""
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


def _unix_to_iso(seconds: object) -> str | None:
    """The Odds API unix 秒 → ISO UTC；缺失/非法返回 None（不伪造）。"""
    if not isinstance(seconds, int | float | str):
        return None
    try:
        return datetime.fromtimestamp(int(seconds), tz=UTC).isoformat(
            timespec="seconds"
        )
    except (ValueError, OSError):
        return None


def _outcome_prices(event: dict[str, Any]) -> dict[str, dict[str, float]]:
    """h2h 报价 → book -> {h/d/a}；只保留三向完整的 book（票 35）。"""
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
        if set(prices) == {"h", "d", "a"}:
            books[bookmaker["key"]] = prices
    return books


def parse_events(
    sport_key: str,
    events: list[dict[str, Any]],
    *,
    observed_at: str | None = None,
    observation_id: int | None = None,
) -> list[ParsedEvent]:
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
                last_update_utc=_unix_to_iso(event.get("last_update")),
                observed_at=observed_at,
                observation_id=observation_id,
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
    """
    欧赔快照 append-only 入库（只写已 join 的 fixture）+ 事件英文名回填别名。

    captured_at = 源 last_update（未知时 = observed_at，此时仅观察用途）；
    报价未变的重复观测被唯一键吸收，观测证据保留在 quote_observations。
    """
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
        captured = event.last_update_utc or event.observed_at
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
                        captured_at=captured or utc_now_iso(),
                        observed_at=event.observed_at,
                        source_updated_at=event.last_update_utc,
                        observation_id=event.observation_id,
                        purpose=purpose,
                    ),
                )
                if snapshot_id is None:
                    stats.duplicate_snapshots += 1
                else:
                    stats.snapshots += 1
    conn.commit()
    return stats


def _fetch_sport_odds(  # noqa: PLR0913 — 采集上下文逐项传参，聚合对象反而更难读
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    sport_key: str,
    *,
    markets: tuple[str, ...],
    raw_root: Path | None,
    now: datetime,
    extra_params: dict[str, str] | None = None,
) -> list[ParsedEvent]:
    """
    拉一个 sport 的 h2h 报价：预留 credit → 请求（失败退回）→ 证据 → 解析。

    每个 /odds 请求恰好消耗 1 credit（1 market × 1 region）。
    """
    reserve_credits(conn, settings, len(markets), f"sport={sport_key}")
    try:
        params = {
            "apiKey": settings.odds_api_key,
            "regions": "eu",
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
        }
        if extra_params:
            params.update(extra_params)
        response = client.get(
            f"{settings.odds_api_base_url}/sports/{sport_key}/odds",
            params=params,
            timeout=25.0,
        )
        response.raise_for_status()
        raw_events = response.json()
    except Exception:
        # 请求未成功：API 未计费，退回预留（票 35 验收 6）
        refund_credits(conn, len(markets), f"sport={sport_key} request failed")
        raise
    observed = now.isoformat(timespec="seconds")
    sha, raw_ref = (
        observations.save_raw(raw_root, "odds_api", response.content)
        if raw_root
        else (observations.sha256_hex(response.content), None)
    )
    remaining = response.headers.get("x-requests-remaining")
    observation_id = fx_store.record_quote_observation(
        conn,
        ObservationInput(
            source="odds_api",
            purpose=ObservationPurpose.LIVE,
            observed_at=observed,
            endpoint=f"/sports/{sport_key}/odds",
            parse_version=PARSE_VERSION,
            raw_sha256=sha,
            raw_ref=raw_ref,
            summary=f"events={len(raw_events)}"
            + (f" remaining={remaining}" if remaining else ""),
        ),
    )
    return parse_events(
        sport_key,
        raw_events,
        observed_at=observed,
        observation_id=observation_id,
    )


def _assert_allowed_markets(markets: tuple[str, ...]) -> None:
    """票 35：本票只采 had 同义的 h2h；totals 等付费市场付费后丢弃=浪费额度。"""
    blocked = [m for m in markets if m not in ALLOWED_MARKETS]
    if blocked:
        raise ValueError(f"市场 {blocked} 不在允许清单 {ALLOWED_MARKETS} (票 35)")


def fetch_and_store_odds(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    markets: tuple[str, ...] = ("h2h",),
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> OddsIngestStats:
    """
    完整欧赔采集：逐请求预算预留→发现 sport keys→拉取→join→入库。

    credits 逐请求记账：部分失败时已成功请求的消耗均已入账（票 35）。
    """
    _assert_allowed_markets(markets)
    moment = now or datetime.now(UTC)
    check_credit_budget(conn, settings)
    sport_keys = discover_sport_keys(settings, client)
    all_events: list[ParsedEvent] = []
    for sport_key in sport_keys:
        all_events.extend(
            _fetch_sport_odds(
                conn,
                settings,
                client,
                sport_key,
                markets=markets,
                raw_root=raw_root,
                now=moment,
            )
        )
    stats = OddsIngestStats(
        events=len(all_events), credits_used=len(sport_keys) * len(markets)
    )
    join_report = join_fixtures(conn, all_events)
    stored = store_events(conn, all_events)
    stats.snapshots = stored.snapshots
    stats.duplicate_snapshots = stored.duplicate_snapshots
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
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> OddsIngestStats:
    """
    收盘窗口尽力快照（票 32）：kickoff 前 window_minutes 内开球的已 join 场次。

    只拉窗口内事件（commence_time_from/to），purpose=closing 入库；与常规
    采集共享月预算（同一 reserve_credits 路径，票 35）。
    """
    _assert_allowed_markets(markets)
    moment = now or datetime.now(UTC)
    check_credit_budget(conn, settings)
    window_end = moment + timedelta(minutes=window_minutes)
    sport_keys = discover_sport_keys(settings, client)
    all_events: list[ParsedEvent] = []
    for sport_key in sport_keys:
        all_events.extend(
            _fetch_sport_odds(
                conn,
                settings,
                client,
                sport_key,
                markets=markets,
                raw_root=raw_root,
                now=moment,
                extra_params={
                    "commence_time_from": moment.isoformat(),
                    "commence_time_to": window_end.isoformat(),
                },
            )
        )
    stats = OddsIngestStats(
        events=len(all_events), credits_used=len(sport_keys) * len(markets)
    )
    stored = store_events(conn, all_events, purpose=SnapshotPurpose.CLOSING)
    stats.snapshots = stored.snapshots
    stats.duplicate_snapshots = stored.duplicate_snapshots
    conn.commit()
    return stats


def _known_odds_api_aliases(conn: sqlite3.Connection, team_id: int) -> set[str]:
    """该 canonical 队已知的 Odds API 侧英文名集合。"""
    return team_align.odds_api_aliases_for_team(conn, team_id)


def join_fixtures(conn: sqlite3.Connection, events: list[ParsedEvent]) -> JoinReport:
    """
    联赛 + 开球时间窗冷启动 join；结果持久化（残余人工映射表补齐）。

    时间匹配仅是候选（票 35）：已知主客队英文名时必须交叉核对——
    主客互换/队名冲突一律拒绝，歧义不自动认定。
    """
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
        sport_events = by_sport.get(sport_key, [])
        in_window = sorted(
            (
                event
                for event in sport_events
                if abs(_event_commence_utc(event) - kickoff) <= window
            ),
            key=lambda e: abs(_event_commence_utc(e) - kickoff),
        )
        candidates = [event for event in in_window if event.event_id not in used_events]
        if not candidates:
            reason = "no_free_event_in_window" if in_window else "no_event_in_window"
            report.unmatched.append({"fixture_id": str(row["id"]), "reason": reason})
            continue
        # 已知队名 → 身份交叉核对（票 35）：时间命中但队名对不上即拒绝。
        home_aliases = _known_odds_api_aliases(conn, int(row["home_team_id"]))
        away_aliases = _known_odds_api_aliases(conn, int(row["away_team_id"]))
        if home_aliases and away_aliases:
            matching = [
                e
                for e in candidates
                if e.home_team in home_aliases and e.away_team in away_aliases
            ]
            swapped = [
                e
                for e in candidates
                if e.home_team in away_aliases and e.away_team in home_aliases
            ]
            if not matching:
                reason = "team_swap_mismatch" if swapped else "team_name_conflict"
                report.unmatched.append(
                    {"fixture_id": str(row["id"]), "reason": reason}
                )
                continue
            candidates = matching
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
            team_align.record_odds_api_alias(conn, team_id, alias)
        report.joined += 1
        if is_tier1:
            report.tier1_joined += 1
    conn.commit()
    return report
