"""
sporttery 竞彩采集（票 19；票 35 补观测证据）：getMatchCalculatorV1.qry 产品化。

网络层只做一件薄事（带 Referer 的 GET）；解析与入库是纯函数/纯存储，
便于离线测试。

时间语义（票 35，随快照落库）：
- captured_at = source_updated_at = 各市场块自报的 updateDate/updateTime
  （源调盘时间，CST→UTC）；
- observed_at = 本机收到 HTTP 响应的时间（由调用方传入，测试用固定时钟）；
- 旧数据只有 captured_at，按本源解释为源调盘时间；当时是否已知
  observed_at 不可证明，不倒填。

销售状态（票 35）：
- 计算器端点只返回在售场次，payload 缺 sellStatus 字段时按 on_sale 记录
  （端点语义，非伪造）；出现但值未知 → unknown，保守不进正式候选；
- 单固资格：had 块的 single 字段优先（"1"/"0"），缺失时若比赛级
  bettingSingle=0 则该场无任何单关（False），否则未知（None）。

进球类单固（票 wb-04）：ttg/crs 市场块实测恒缺 single 字段，单固资格取
matchInfo 的 poolList 各池 single（1/0，实测为唯一可靠来源）；poolList 也
缺失时同 had 的比赛级否决规则。had 口径不动（票 35 语义）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import observations
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import GOALS_MARKETS
from goalx_backend.models import (
    MatchCodeInput,
    ObservationInput,
    ObservationPurpose,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

CST = timezone(timedelta(hours=8))  # 竞彩官方时区：北京时间
POOL_CODES = ("had", "hhad", "crs", "ttg", "hafu")
PARSE_VERSION = "sporttery_calculator_v2"

# sellStatus 原始值 → 内部状态；未列出值视为未知（保守拒绝，票 35）。
SELL_STATUS_MAP: dict[str, str] = {"0": "on_sale", "1": "stopped"}


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
    single: bool | None = None
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
    sell_status_raw: str | None = None
    pool_single: dict[str, bool | None] = field(default_factory=dict)

    @property
    def sale_state(self) -> str:
        """销售状态（端点只列在售场次，缺字段=on_sale，未知值保守 unknown）。"""
        if self.sell_status_raw is None:
            return "on_sale"
        return SELL_STATUS_MAP.get(self.sell_status_raw, "unknown")

    def had_single_eligible(self) -> bool | None:
        """Had 单固资格：市场级 single 优先；比赛级否决；否则未知。"""
        for quote in self.markets:
            if quote.market_code == "had" and quote.single is not None:
                return quote.single
        return None if self.is_single else False

    def goals_single_eligible(self, market_code: str) -> bool | None:
        """
        进球类（ttg/crs）单固资格（票 wb-04）。

        市场块 single 优先（实测恒缺，防御性保留）→ poolList 各池 single
        （实测唯一可靠来源）→ 比赛级否决（bettingSingle=0 → False）→ None。
        """
        if market_code not in GOALS_MARKETS:
            raise ValueError(f"非进球类玩法: {market_code}")
        for quote in self.markets:
            if quote.market_code == market_code and quote.single is not None:
                return quote.single
        if market_code in self.pool_single:
            return self.pool_single[market_code]
        return None if self.is_single else False


@dataclass
class FetchedCalculator:
    """一次计算器响应：解析后的 payload 与原始字节（证据用）。"""

    payload: dict[str, Any]
    raw: bytes


@dataclass
class IngestStats:
    """一次采集入库的统计。"""

    matches: int = 0
    snapshots: int = 0
    duplicate_snapshots: int = 0
    observation_id: int | None = None


def fetch_calculator(settings: Settings, client: httpx.Client) -> FetchedCalculator:
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
    return FetchedCalculator(payload=payload, raw=response.content)


def fetch_calculator_payload(
    settings: Settings, client: httpx.Client
) -> dict[str, Any]:
    """兼容入口：只要 payload（测试/诊断用）。"""
    return fetch_calculator(settings, client).payload


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
    single_raw = str(raw.get("single"))
    if single_raw == "1":
        quote.single = True
    elif single_raw == "0":
        quote.single = False
    # 缺失/未知值 → None：单固资格未知，不伪造（票 35）
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


def _parse_pool_single(sub_match: dict[str, Any]) -> dict[str, bool | None]:
    """
    各池（poolList）单固资格 → ``{pool_code: True/False/None}``（票 wb-04）。

    实测 payload 的市场块（had/ttg/crs/...）恒缺 ``single`` 字段，
    poolList 的 ``single``（int 1/0）是单固资格唯一可靠来源；未列出的
    池不进字典（调用方继续走比赛级否决规则）。
    """
    out: dict[str, bool | None] = {}
    pools: list[dict[str, Any]] = sub_match.get("poolList") or []
    for pool in pools:
        code = str(pool.get("poolCode", "")).lower()
        if code not in (*GOALS_MARKETS, "had", "hhad", "hafu"):
            continue
        raw: object = pool.get("single")
        if raw in (1, "1"):
            out[code] = True
        elif raw in (0, "0"):
            out[code] = False
        else:
            out[code] = None
    return out


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
                    sell_status_raw=(
                        str(m["sellStatus"])
                        if m.get("sellStatus") is not None
                        else None
                    ),
                    pool_single=_parse_pool_single(m),
                )
            )
    return parsed


def _resolve_fixture(
    conn: sqlite3.Connection,
    match: ParsedMatch,
    competition_id: int,
    home: int,
    away: int,
) -> int:
    """定位 fixture：source_match_id 命中优先（改期沿用原场次），否则 upsert。"""
    existing = fx_store.find_fixture_by_source_match(
        conn, "jingcai", match.source_match_id
    )
    if existing is not None:
        fixture_id = int(existing["id"])
        if str(existing["kickoff_utc"]) != match.kickoff_utc:
            fx_store.update_fixture_kickoff(conn, fixture_id, match.kickoff_utc)
        return fixture_id
    return fx_store.upsert_fixture(conn, competition_id, match.kickoff_utc, home, away)


def store_matches(
    conn: sqlite3.Connection,
    matches: list[ParsedMatch],
    *,
    observed_at: str | None = None,
    observation_id: int | None = None,
) -> IngestStats:
    """把解析结果幂等入库（fixtures/match_codes/odds_snapshots/sale_statuses）。"""
    stats = IngestStats(observation_id=observation_id)
    observed = observed_at or utc_now_iso()
    for match in matches:
        sport_key, tier = LEAGUE_MAP.get(match.league, (None, Tier.TIER2))
        competition = fx_store.upsert_competition(
            conn, match.league, tier=tier, odds_api_sport_key=sport_key
        )
        home = fx_store.upsert_team(conn, match.home_team)
        away = fx_store.upsert_team(conn, match.away_team)
        fixture = _resolve_fixture(conn, match, competition, home, away)
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
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture,
                market_code=None,
                sale_state=match.sale_state,
                observed_at=observed,
                observation_id=observation_id,
            ),
        )
        for quote in match.markets:
            if quote.market_code == "had":
                fx_store.append_sale_status(
                    conn,
                    SaleStatusInput(
                        fixture_id=fixture,
                        market_code="had",
                        sale_state=match.sale_state,
                        single_eligible=match.had_single_eligible(),
                        observed_at=observed,
                        observation_id=observation_id,
                    ),
                )
            elif quote.market_code in GOALS_MARKETS:
                # 进球类单固（票 wb-04）：poolList 池级 single（had 口径不动）
                fx_store.append_sale_status(
                    conn,
                    SaleStatusInput(
                        fixture_id=fixture,
                        market_code=quote.market_code,
                        sale_state=match.sale_state,
                        single_eligible=match.goals_single_eligible(quote.market_code),
                        observed_at=observed,
                        observation_id=observation_id,
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
                        observed_at=observed,
                        source_updated_at=quote.captured_at,
                        observation_id=observation_id,
                        meta=meta,
                    ),
                )
                if snapshot_id is None:
                    stats.duplicate_snapshots += 1
                else:
                    stats.snapshots += 1
    conn.commit()
    return stats


def capture_jingcai(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> IngestStats:
    """
    采集入口（票 35 证据链）：fetch → 原始证据 → 观测行 → 解析入库。

    observed_at 用本机收到响应的时间（测试传固定时钟）；原始响应 gzip
    落盘（raw_root 可空=只记哈希）。
    """
    observed = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    fetched = fetch_calculator(settings, client)
    matches = parse_matches(fetched.payload)
    sha, raw_ref = (
        observations.save_raw(raw_root, "sporttery", fetched.raw)
        if raw_root
        else (observations.sha256_hex(fetched.raw), None)
    )
    observation_id = fx_store.record_quote_observation(
        conn,
        ObservationInput(
            source="sporttery",
            purpose=ObservationPurpose.LIVE,
            observed_at=observed,
            endpoint="getMatchCalculatorV1.qry",
            parse_version=PARSE_VERSION,
            raw_sha256=sha,
            raw_ref=raw_ref,
            summary=f"matches={len(matches)}",
        ),
    )
    return store_matches(
        conn, matches, observed_at=observed, observation_id=observation_id
    )
