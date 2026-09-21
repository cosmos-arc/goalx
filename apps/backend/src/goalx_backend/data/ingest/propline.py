"""
PropLine 免费层欧赔采集与竞彩↔PropLine join（票 50；research/19 选型）。

与 oddsapi.py 并存的第二欧赔源（互备/切换/交叉验证），不替换 The Odds
API；join 列独立（fixtures.propline_event_id），一场竞彩可同时挂两源。

实测差异（2026-09-21 票 50 验收，对照 the-odds-api）：
- 价格为美式赔率（``oddsFormat=decimal`` 被忽略）→ american_to_decimal；
- outcome 队名不归一到事件 home/away（冷门队各书拼写不同，如 FC Sabah /
  Sabah FK / Sabah Baku）→ Draw 精确 + 归一化等值 + 消去法（两非平局
  之一已定则余者定），仍不可判则整书丢弃（票 35 纪律：宁缺勿错配）；
- 事件 last_update 为 ISO 字符串（非 unix 秒）；
- sport key 接受 the-odds-api 别名并原样回显 → 直接用
  competitions.odds_api_sport_key 请求，join 命名空间天然对齐；无对应
  联赛（如 soccer_france_ligue_two）返回结构化 404 unknown_sport，
  逐 sport 降级记录不中断整轮采集；
- 计费=请求数/天（免费层 1,000）：响应即记 1（含 404；5xx 不记），
  X-Daily-Remaining 低于余量阈值即停（省出安全边际）。

时间语义沿用票 35：observed_at = 本机收到响应时间；source_updated_at =
事件 last_update（源自报）；captured_at 对新行 = 源时间（未知时 =
observed_at，此时报价只观察不进正式候选）。同公司完整三向才可比较。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import observations
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest.oddsapi import (
    JoinReport,
    OddsIngestStats,
    ParsedEvent,
    day_start_utc,
)
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling import team_align
from goalx_backend.models import (
    ObservationInput,
    ObservationPurpose,
    SnapshotInput,
    SnapshotPurpose,
)

PARSE_VERSION = "propline_h2h_v1"
ALLOWED_MARKETS = ("h2h",)
COST_CATEGORY = "propline_request"
JOIN_WINDOW_MINUTES = 20

# 队名归一化时剥离的通用俱乐部词（纯语法归一，不做模糊打分）
CLUB_TOKENS = frozenset(
    [
        "fc",
        "cf",
        "afc",
        "fk",
        "sc",
        "ac",
        "bk",
        "sk",
        "cd",
        "ca",
        "sd",
        "ud",
        "us",
        "ss",
        "if",
    ]
)

_HTTP_NOT_FOUND = 404
_HTTP_TOO_MANY_REQUESTS = 429


class ProplineBudgetExceeded(RuntimeError):
    """Raised when the PropLine daily request guard would blow the budget."""


def american_to_decimal(price: float) -> float | None:
    """美式赔率 → 小数赔率；0/非法返回 None（无价不伪造）。"""
    if price > 0:
        return round(1 + price / 100, 4)
    if price < 0:
        return round(1 + 100 / abs(price), 4)
    return None


def _normalize_name(name: str) -> str:
    """队名归一：剥音符、casefold、去通用俱乐部词（FC Sabah → sabah）。"""
    folded = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode()
        .casefold()
    )
    return " ".join(t for t in folded.split() if t not in CLUB_TOKENS)


def _iso_to_seconds(value: object) -> str | None:
    """PropLine ISO 时间戳（含微秒/Z）→ 秒级 ISO UTC；缺失/非法返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat(timespec="seconds")
        )
    except ValueError:
        return None


def _outcome_role(name_n: str, home_n: str, away_n: str) -> str | None:
    """归一化 outcome 名 → "h"/"d"/"a"；不可判返回 None（含空名守卫）。"""
    if name_n == "draw":
        return "d"
    if name_n == home_n and name_n != away_n:
        return "h"
    if name_n == away_n and name_n != home_n:
        return "a"
    return None


def _finalize_book(
    prices: dict[str, float], unassigned: list[float], conflict: bool
) -> dict[str, float] | None:
    """消去法补侧 + 三向完整性裁决；不完整/歧义返回 None。"""
    if not conflict and len(unassigned) == 1:
        missing = {"h", "a"} - prices.keys()
        if "d" in prices and len(missing) == 1:
            prices[missing.pop()] = unassigned[0]
            unassigned.clear()
    if prices.keys() == {"h", "d", "a"} and not unassigned and not conflict:
        return prices
    return None


def _book_prices(
    bookmaker: dict[str, Any], home_n: str, away_n: str
) -> dict[str, float] | None:
    """
    一个 bookmaker 的 h2h → {h/d/a}；三向不全或映射歧义返回 None。

    映射规则（票 50）：``Draw``（casefold 等值）→ d；outcome 名与事件
    home/away 精确或归一化等值 → h/a；两非平局之一已定且仅剩一个未定
    → 消去法定另一侧（h2h 只有两队，结构性安全）；同名两 outcome 映射
    到同一侧（归一化冲突）或两侧均无法判定 → 整书丢弃（票 35 纪律）。
    """
    prices: dict[str, float] = {}
    unassigned: list[float] = []
    conflict = False
    for market in bookmaker.get("markets", []):
        if market.get("key") != "h2h":
            continue
        for outcome in market.get("outcomes", []):
            price = american_to_decimal(outcome.get("price"))
            if price is None or price <= 1:
                continue
            role = _outcome_role(
                _normalize_name(str(outcome.get("name", ""))), home_n, away_n
            )
            if role is None:
                unassigned.append(price)
                continue
            if role in prices:
                conflict = True  # 两 outcome 映到同一侧：整书不可信
                break
            prices[role] = price
        if conflict:
            break
    return _finalize_book(prices, unassigned, conflict)


def _outcome_prices(event: dict[str, Any]) -> dict[str, dict[str, float]]:
    """h2h 报价 → book -> {h/d/a}；只保留三向完整且映射无歧义的 book。"""
    books: dict[str, dict[str, float]] = {}
    home = str(event.get("home_team", ""))
    away = str(event.get("away_team", ""))
    home_n, away_n = _normalize_name(home), _normalize_name(away)
    for bookmaker in event.get("bookmakers", []):
        prices = _book_prices(bookmaker, home_n, away_n)
        if prices is not None:
            books[str(bookmaker["key"])] = prices
    return books


def parse_events(
    sport_key: str,
    events: list[dict[str, Any]],
    *,
    observed_at: str | None = None,
    observation_id: int | None = None,
) -> list[ParsedEvent]:
    """把 PropLine /odds 响应解析为 ParsedEvent 列表（纯函数）。"""
    parsed: list[ParsedEvent] = []
    for event in events:
        books = _outcome_prices(event)
        if not books:
            continue
        parsed.append(
            ParsedEvent(
                event_id=str(event["id"]),
                sport_key=str(event.get("sport_key", sport_key)),
                home_team=str(event["home_team"]),
                away_team=str(event["away_team"]),
                commence_utc=str(event["commence_time"]),
                books=books,
                last_update_utc=_iso_to_seconds(event.get("last_update")),
                observed_at=observed_at,
                observation_id=observation_id,
            )
        )
    return parsed


def _request_usage(conn: sqlite3.Connection, since_utc: str) -> int:
    """本源当日已记请求数（ledger 口径；credit_usage 是 odds_api 专表项）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(units), 0) AS used FROM cost_ledger
        WHERE category = ? AND occurred_at >= ?
        """,
        (COST_CATEGORY, since_utc),
    ).fetchone()
    return int(row["used"])


def check_request_budget(conn: sqlite3.Connection, settings: Settings) -> None:
    """预算护栏：当日请求数超线抛 ProplineBudgetExceeded（fail-closed）。"""
    used = _request_usage(conn, day_start_utc())
    if used >= settings.propline_daily_request_budget:
        raise ProplineBudgetExceeded(
            f"daily requests {used} >= {settings.propline_daily_request_budget}"
        )


def record_request(conn: sqlite3.Connection, note: str) -> None:
    """把一次已达服务端的请求记入 CostLedger（404 也计；5xx 不计）。"""
    rs_store.record_cost(
        conn,
        COST_CATEGORY,
        occurred_at=utc_now_iso(),
        note=note,
    )
    conn.commit()


def discover_sport_keys(conn: sqlite3.Connection, settings: Settings) -> list[str]:
    """
    冻结范围优先；否则取库内竞彩赛事表的 sport key（别名直用，零请求）。

    sport key 来自 competitions.odds_api_sport_key（the-odds-api 命名），
    PropLine 接受其作别名并在响应原样回显——join 命名空间因此对齐，
    无需维护静态映射表。
    """
    if settings.propline_sport_scope:
        return [
            key.strip()
            for key in settings.propline_sport_scope.split(",")
            if key.strip()
        ]
    rows = conn.execute(
        """
        SELECT DISTINCT odds_api_sport_key FROM competitions
        WHERE odds_api_sport_key IS NOT NULL ORDER BY 1
        """
    ).fetchall()
    return [str(row["odds_api_sport_key"]) for row in rows]


def fetch_freshness(settings: Settings, client: httpx.Client) -> dict[str, Any]:
    """免鉴权健康检查（单人运营兜底）：per-book staleness 全景。"""
    response = client.get(f"{settings.propline_base_url}/freshness", timeout=15.0)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


def _error_code(response: httpx.Response) -> str | None:
    """PropLine 结构化错误码（{"detail": {"error": ...}}）；非结构化为 None。"""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    body = cast("dict[str, Any]", payload)
    detail = body.get("detail")
    if isinstance(detail, dict):
        error = cast("dict[str, Any]", detail).get("error")
        return str(error) if error else None
    return None


@dataclass
class FetchSportResult:
    """一个 sport 的拉取结果（unavailable=结构化 404，无对应联赛）。"""

    events: list[ParsedEvent] = field(default_factory=list)
    remaining: int | None = None
    unavailable: bool = False


def _fetch_sport_odds(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    sport_key: str,
    *,
    raw_root: Path | None,
    now: datetime,
) -> FetchSportResult:
    """
    拉一个 sport 的 h2h 报价：预算检查 → 请求 → 记账 → 证据 → 解析。

    404 unknown_sport 降级为 unavailable（无对应联赛，如
    soccer_france_ligue_two）；429 daily_limit_exceeded 转为
    ProplineBudgetExceeded fail-closed。
    """
    check_request_budget(conn, settings)
    response = client.get(
        f"{settings.propline_base_url}/sports/{sport_key}/odds",
        params={"markets": ",".join(ALLOWED_MARKETS)},
        headers={"X-API-Key": settings.propline_api_key},
        timeout=25.0,
    )
    code = _error_code(response)
    if response.status_code == _HTTP_NOT_FOUND and code == "unknown_sport":
        record_request(conn, f"sport={sport_key} unavailable")
        return FetchSportResult(remaining=_remaining(response), unavailable=True)
    if (
        response.status_code == _HTTP_TOO_MANY_REQUESTS
        and code == "daily_limit_exceeded"
    ):
        record_request(conn, f"sport={sport_key} daily_limit")
        raise ProplineBudgetExceeded(f"provider daily limit hit at {sport_key}")
    response.raise_for_status()
    record_request(conn, f"sport={sport_key}")
    result = FetchSportResult(remaining=_remaining(response))
    raw_events = response.json()
    observed = now.isoformat(timespec="seconds")
    sha, raw_ref = (
        observations.save_raw(raw_root, "propline", response.content)
        if raw_root
        else (observations.sha256_hex(response.content), None)
    )
    observation_id = fx_store.record_quote_observation(
        conn,
        ObservationInput(
            source="propline",
            purpose=ObservationPurpose.LIVE,
            observed_at=observed,
            endpoint=f"/sports/{sport_key}/odds",
            parse_version=PARSE_VERSION,
            raw_sha256=sha,
            raw_ref=raw_ref,
            summary=f"events={len(raw_events)}"
            + (
                f" remaining={result.remaining}" if result.remaining is not None else ""
            ),
        ),
    )
    conn.commit()
    result.events = parse_events(
        sport_key,
        raw_events,
        observed_at=observed,
        observation_id=observation_id,
    )
    return result


def _remaining(response: httpx.Response) -> int | None:
    """X-Daily-Remaining 头 → int（provider 侧配额真值）；缺失 None。"""
    value = response.headers.get("x-daily-remaining")
    if value is None or not value.isdigit():
        return None
    return int(value)


def store_events(
    conn: sqlite3.Connection,
    events: list[ParsedEvent],
    *,
    purpose: SnapshotPurpose = SnapshotPurpose.LIVE_CAPTURE,
    meta: dict[str, str] | None = None,
) -> OddsIngestStats:
    """PropLine 快照 append-only 入库（只写已 join 的 fixture；镜像票 35 语义）。"""
    stats = OddsIngestStats(events=len(events))
    by_event = {event.event_id: event for event in events}
    joined_fixtures = conn.execute(
        "SELECT id, propline_event_id FROM fixtures WHERE propline_event_id IS NOT NULL"
    ).fetchall()
    for row in joined_fixtures:
        event = by_event.get(str(row["propline_event_id"]))
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
                        source=f"propline:{book}",
                        odds=odds,
                        captured_at=captured or utc_now_iso(),
                        observed_at=event.observed_at,
                        source_updated_at=event.last_update_utc,
                        observation_id=event.observation_id,
                        purpose=purpose,
                        meta=meta,
                    ),
                )
                if snapshot_id is None:
                    stats.duplicate_snapshots += 1
                else:
                    stats.snapshots += 1
    conn.commit()
    return stats


def _event_commence_utc(event: ParsedEvent) -> datetime:
    """事件的 kickoff datetime（PropLine 返回 ISO UTC）。"""
    return datetime.fromisoformat(event.commence_utc.replace("Z", "+00:00"))


def join_fixtures(conn: sqlite3.Connection, events: list[ParsedEvent]) -> JoinReport:
    """
    联赛 + 开球时间窗冷启动 join（镜像 oddsapi.join_fixtures，落 propline 列）。

    时间匹配仅是候选：已知主客队英文名时交叉核对，主客互换/冲突/歧义
    一律拒绝，留待人工映射。
    """
    report = JoinReport()
    rows = conn.execute(
        """
        SELECT f.id, f.kickoff_utc, f.propline_event_id, f.home_team_id,
               f.away_team_id, c.odds_api_sport_key, c.tier
        FROM fixtures f
        JOIN match_codes mc ON mc.fixture_id = f.id AND mc.kind = 'jingcai'
        JOIN competitions c ON c.id = f.competition_id
        WHERE f.propline_event_id IS NULL AND c.odds_api_sport_key IS NOT NULL
        """
    ).fetchall()
    by_sport: dict[str, list[ParsedEvent]] = {}
    for event in events:
        by_sport.setdefault(event.sport_key, []).append(event)
    used_events = {
        str(row["propline_event_id"])
        for row in conn.execute(
            "SELECT propline_event_id FROM fixtures WHERE propline_event_id IS NOT NULL"
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
        home_aliases = team_align.english_aliases_for_team(
            conn, int(row["home_team_id"])
        )
        away_aliases = team_align.english_aliases_for_team(
            conn, int(row["away_team_id"])
        )
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
            report.unmatched.append(
                {"fixture_id": str(row["id"]), "reason": "ambiguous_time_window"}
            )
            continue
        conn.execute(
            """
            UPDATE fixtures SET propline_event_id = ?, propline_sport_key = ?
            WHERE id = ?
            """,
            (best.event_id, best.sport_key, int(row["id"])),
        )
        used_events.add(best.event_id)
        for team_id, alias in (
            (int(row["home_team_id"]), best.home_team),
            (int(row["away_team_id"]), best.away_team),
        ):
            team_align.record_propline_alias(conn, team_id, alias)
        report.joined += 1
        if is_tier1:
            report.tier1_joined += 1
    conn.commit()
    return report


def _assert_allowed_markets() -> None:
    """票 50：只采 h2h（与 oddsapi 票 35 同口径；totals 等后续票另议）。"""
    if ALLOWED_MARKETS != ("h2h",):  # pragma: no cover - 常量防护，防手改漂移
        raise ValueError(f"市场清单 {ALLOWED_MARKETS} 漂移（票 50 只允许 h2h）")


@dataclass
class ProplineRunStats:
    """一轮 PropLine 采集的统计（credits_used 语义=请求数）。"""

    ingest: OddsIngestStats = field(default_factory=OddsIngestStats)
    requests_used: int = 0
    daily_remaining: int | None = None
    unavailable_sports: list[str] = field(default_factory=list)
    unmatched: int = 0


def fetch_and_store_odds(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> ProplineRunStats:
    """
    完整采集：预算检查 → 逐 sport 拉取（unknown_sport 降级）→ join → 入库。

    X-Daily-Remaining 低于 propline_remaining_floor 即停止后续 sport
    （本sport数据已入库不回滚）；请求级 fail-closed 由
    check_request_budget/ProplineBudgetExceeded 保证。
    """
    _assert_allowed_markets()
    moment = now or datetime.now(UTC)
    stats = ProplineRunStats()
    for sport_key in discover_sport_keys(conn, settings):
        if (
            stats.daily_remaining is not None
            and stats.daily_remaining < settings.propline_remaining_floor
        ):
            break
        result = _fetch_sport_odds(
            conn, settings, client, sport_key, raw_root=raw_root, now=moment
        )
        stats.requests_used += 1
        stats.daily_remaining = result.remaining
        if result.unavailable:
            stats.unavailable_sports.append(sport_key)
        stats.ingest.events += len(result.events)
        join_report = join_fixtures(conn, result.events)
        stats.unmatched += len(join_report.unmatched)
        stored = store_events(conn, result.events)
        stats.ingest.snapshots += stored.snapshots
        stats.ingest.duplicate_snapshots += stored.duplicate_snapshots
    stats.ingest.credits_used = stats.requests_used
    return stats
