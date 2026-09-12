"""
sporttery 竞彩采集（票 19）：getMatchCalculatorV1.qry 产品化。

网络层只做一件薄事（带 Referer 的 GET）；解析与入库是纯函数/纯存储，
便于离线测试。调盘时点（updateDate/updateTime）随快照落库（ADR 0001）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.models import MatchCodeInput, SnapshotInput, Tier
from goalx_backend.store import fixtures as fx_store

CST = timezone(timedelta(hours=8))  # 竞彩官方时区：北京时间
POOL_CODES = ("had", "hhad", "crs", "ttg", "hafu")

# 竞彩联赛缩写 → (Odds API sport key, tier)（票 17：Tier 1=五大+欧冠+欧联）
LEAGUE_MAP: dict[str, tuple[str, Tier]] = {
    "英超": ("soccer_epl", Tier.TIER1),
    "西甲": ("soccer_spain_la_liga", Tier.TIER1),
    "意甲": ("soccer_italy_serie_a", Tier.TIER1),
    "德甲": ("soccer_germany_bundesliga", Tier.TIER1),
    "法甲": ("soccer_france_ligue_one", Tier.TIER1),
    "欧冠": ("soccer_uefa_champs_league", Tier.TIER1),
    "欧联": ("soccer_uefa_europa_league", Tier.TIER1),
    "荷甲": ("soccer_netherlands_eredivisie", Tier.TIER2),
    "欧协联": ("soccer_uefa_europa_conference_league", Tier.TIER2),
    "法乙": ("soccer_france_ligue_two", Tier.TIER2),
    "西乙": ("soccer_spain_segunda_division", Tier.TIER2),
    "意乙": ("soccer_italy_serie_b", Tier.TIER2),
    "德乙": ("soccer_germany_bundesliga2", Tier.TIER2),
    "德国杯": ("soccer_germany_dfb_pokal", Tier.TIER2),
}


@dataclass
class MarketQuote:
    """一个市场在某时点的全部报价。"""

    market_code: str
    captured_at: str
    goal_line: str | None = None
    prices: dict[str, float] = field(default_factory=dict)


@dataclass
class ParsedMatch:
    """一场竞彩比赛的采集结果。"""

    source_match_id: str
    business_date: str
    code: str
    league: str
    league_all_name: str
    home_team: str
    away_team: str
    kickoff_utc: str
    is_single: bool
    markets: list[MarketQuote]


@dataclass
class IngestStats:
    """一次采集入库的统计。"""

    matches: int = 0
    snapshots: int = 0
    duplicate_snapshots: int = 0


def fetch_calculator_payload(
    settings: Settings, client: httpx.Client
) -> dict[str, Any]:
    """拉取全部 5 玩法的竞彩计算器数据（须 Referer 头，票 01 实测）。"""
    response = client.get(
        settings.sporttery_calculator_url,
        params={"poolCode": ",".join(POOL_CODES), "channel": "c"},
        headers={
            "Referer": settings.sporttery_referer,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/126.0"
            ),
            "Accept": "application/json",
        },
        timeout=25.0,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    if payload.get("errorCode") not in (0, "0"):
        raise ValueError(f"sporttery errorCode: {payload.get('errorCode')}")
    return payload


_CRS_OTHER_KEYS = {"s1sh": "h_other", "s1sd": "d_other", "s1sa": "a_other"}
_CRS_KEY_LENGTH = 6


def _cst_to_utc(date_str: str, time_str: str) -> str:
    """把官方北京时间字段转成 UTC ISO 串。"""
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=CST).astimezone(UTC).isoformat()


def _parse_crs_key(key: str) -> str | None:
    """Crs 键 → 规范选项编码（s02s01→2:1；s1sh→h_other）。"""
    if key in _CRS_OTHER_KEYS:
        return _CRS_OTHER_KEYS[key]
    if len(key) == _CRS_KEY_LENGTH and key.startswith("s") and "s" in key[1:]:
        home, away = key[1:].split("s")
        if home.isdigit() and away.isdigit():
            return f"{int(home)}:{int(away)}"
    return None


def _fill_had_prices(quote: MarketQuote, market_code: str, raw: dict[str, Any]) -> None:
    """胜平负/让球胜平负的 h/d/a 报价。"""
    if market_code in ("had", "hhad"):
        for selection in ("h", "d", "a"):
            _add_price(quote.prices, selection, raw.get(selection))


def _fill_grid_prices(
    quote: MarketQuote, market_code: str, raw: dict[str, Any]
) -> None:
    """ttg（s0..s7）与 hafu（两字母）的网格报价。"""
    if market_code == "ttg":
        for n in range(8):
            _add_price(quote.prices, str(n), raw.get(f"s{n}"))
    elif market_code == "hafu":
        for half in ("h", "d", "a"):
            for full in ("h", "d", "a"):
                _add_price(quote.prices, f"{half}{full}", raw.get(f"{half}{full}"))


def _parse_market(market_code: str, raw: dict[str, Any]) -> MarketQuote | None:
    """解析单个玩法块；无有效报价返回 None。"""
    captured = _cst_to_utc(str(raw["updateDate"]), str(raw["updateTime"]))
    quote = MarketQuote(market_code=market_code, captured_at=captured)
    if market_code == "crs":
        for key, value in raw.items():
            if key.endswith("f") or key in ("goalLine", "goalLineValue", "id"):
                continue
            selection = _parse_crs_key(str(key))
            if selection is None:
                continue
            _add_price(quote.prices, selection, value)
    else:
        _fill_had_prices(quote, market_code, raw)
        _fill_grid_prices(quote, market_code, raw)
    if market_code == "hhad":
        quote.goal_line = raw.get("goalLine") or None
    return quote if quote.prices else None


def _add_price(prices: dict[str, float], selection: str, value: object) -> None:
    """把官方字符串赔率转 float 落入 prices（无效值忽略）。"""
    if value is None:
        return
    try:
        odds = float(str(value))
    except ValueError:
        return
    if odds > 1.0:
        prices[selection] = odds


def parse_matches(payload: dict[str, Any]) -> list[ParsedMatch]:
    """解析计算器 payload → ParsedMatch 列表（纯函数）。"""
    parsed: list[ParsedMatch] = []
    value: dict[str, Any] = payload.get("value", {})
    for day in value.get("matchInfoList", []):
        for m in day.get("subMatchList", []):
            markets: list[MarketQuote] = []
            for code in POOL_CODES:
                raw = m.get(code)
                if not raw:
                    continue
                quote = _parse_market(code, raw)
                if quote is not None:
                    markets.append(quote)
            parsed.append(
                ParsedMatch(
                    source_match_id=str(m["matchId"]),
                    business_date=day.get("businessDate", ""),
                    code=m.get("matchNumStr", ""),
                    league=m.get("leagueAbbName", ""),
                    league_all_name=m.get("leagueAllName", ""),
                    home_team=m.get("homeTeamAllName", ""),
                    away_team=m.get("awayTeamAllName", ""),
                    kickoff_utc=_cst_to_utc(m["matchDate"], m["matchTime"]),
                    is_single=bool(m.get("bettingSingle")),
                    markets=markets,
                )
            )
    return parsed


def store_matches(conn: sqlite3.Connection, matches: list[ParsedMatch]) -> IngestStats:
    """把解析结果幂等入库（fixtures/match_codes/odds_snapshots）。"""
    stats = IngestStats()
    for match in matches:
        sport_key, tier = LEAGUE_MAP.get(match.league, (None, Tier.TIER2))
        competition = fx_store.upsert_competition(
            conn, match.league, tier=tier, odds_api_sport_key=sport_key
        )
        home = fx_store.upsert_team(conn, match.home_team)
        away = fx_store.upsert_team(conn, match.away_team)
        fixture = fx_store.upsert_fixture(
            conn, competition, match.kickoff_utc, home, away
        )
        fx_store.upsert_match_code(
            conn,
            MatchCodeInput(
                fixture_id=fixture,
                kind="jingcai",
                business_date=match.business_date,
                code=match.code,
                source_match_id=match.source_match_id,
                is_single=match.is_single,
            ),
        )
        stats.matches += 1
        for quote in match.markets:
            for selection, odds in quote.prices.items():
                meta = {"goal_line": quote.goal_line} if quote.goal_line else None
                snapshot_id = fx_store.insert_odds_snapshot(
                    conn,
                    SnapshotInput(
                        fixture_id=fixture,
                        market_code=quote.market_code,
                        selection_code=selection,
                        source="sporttery",
                        odds=odds,
                        captured_at=quote.captured_at,
                        meta=meta,
                    ),
                )
                if snapshot_id is None:
                    stats.duplicate_snapshots += 1
                else:
                    stats.snapshots += 1
    conn.commit()
    return stats
