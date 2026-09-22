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

销售状态（票 35，had 部分被票 38 细化，见下节）：
- 计算器端点只返回在售场次，payload 缺 sellStatus 字段时按 on_sale 记录
  （端点语义，非伪造）；出现但值未知 → unknown，保守不进正式候选；
- 原票 35 口径：had 块 single 优先，缺失时比赛级 bettingSingle=0 →
  无单关（False），否则未知（None）。

had 单固（票 38）：市场块实测恒缺 single 字段（票 wb-04 实证），同
ttg/crs 接入 matchInfo 的 poolList 各池 single（1/0）——解析与入库机制
与进球类同构；裁决差异一处：poolList 池级与市场块字段冲突时以 poolList
为准（票 38 人裁决项，实证市场块恒缺、冲突为未观测理论分支），故 had
链为 poolList 池级 → 市场块 single（防御保留）→ 比赛级否决。had_quote
判定语义不动（票 35 口径），本模块只修正 sale_statuses 数据口径。

进球类单固（票 wb-04）：ttg/crs 市场块实测恒缺 single 字段，单固资格取
matchInfo 的 poolList 各池 single（1/0，实测为唯一可靠来源）；poolList 也
缺失时同 had 的比赛级否决规则。
"""

from __future__ import annotations

import json
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
# v3（票 38）：had 单固补 poolList 池级解析（v2 会把 poolList single=1 的
# 场次误记为非单固）；重解析历史证据时以本版本为准。
PARSE_VERSION = "sporttery_calculator_v3"

# sellStatus 原始值 → 内部状态；未列出值视为未知（保守拒绝，票 35）。
SELL_STATUS_MAP: dict[str, str] = {"0": "on_sale", "1": "stopped"}


# 竞彩联赛缩写 → (Odds API sport key, tier)（票 17：Tier 1=五大+欧冠+欧联）
# "欧罗巴"=JC 实际上架名（主库 competitions 实证；"欧联"键保留兼容别名——
# 2026-09-22 前单一"欧联"键从未命中，票 17 声明的欧联 Tier1 覆盖一直失效）
LEAGUE_MAP: dict[str, tuple[str, Tier]] = {
    "英超": ("soccer_epl", Tier.TIER1),
    "西甲": ("soccer_spain_la_liga", Tier.TIER1),
    "意甲": ("soccer_italy_serie_a", Tier.TIER1),
    "德甲": ("soccer_germany_bundesliga", Tier.TIER1),
    "法甲": ("soccer_france_ligue_one", Tier.TIER1),
    "欧冠": ("soccer_uefa_champs_league", Tier.TIER1),
    "欧联": ("soccer_uefa_europa_league", Tier.TIER1),
    "欧罗巴": ("soccer_uefa_europa_league", Tier.TIER1),
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
        """
        Had 单固资格（票 38）。

        poolList 池级 single 优先（实测唯一可靠来源，与市场块冲突时以其
        为准——票 38 裁决）→ 市场块 single（防御保留，实测恒缺）→
        比赛级否决（bettingSingle=0 → False）→ None。
        """
        if "had" in self.pool_single:
            return self.pool_single["had"]
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


@dataclass
class ReprocessStats:
    """一次证据重解析重放的统计（票 38）。"""

    observations: int = 0  # 库中 sporttery 观测行总数
    reparsed: int = 0  # 成功重解析重放的观测数
    skipped_no_raw: int = 0  # raw_ref 为空（只记了哈希）无法重解析的观测数
    matches: int = 0
    snapshots: int = 0
    duplicate_snapshots: int = 0


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
    池不进字典（调用方继续走市场块/比赛级否决规则）。ttg/crs 与 had
    （票 38）共用本解析。
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


def _append_sale_status_once(conn: sqlite3.Connection, status: SaleStatusInput) -> bool:
    """
    幂等追加销售状态行（票 38）。

    证据身份（observation_id）相同的重复行跳过——同一证据重解析可安全
    重跑；返回是否真的追加了。
    """
    if status.observation_id is not None and fx_store.sale_status_row_exists(
        conn, status
    ):
        return False
    fx_store.append_sale_status(conn, status)
    return True


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
        _append_sale_status_once(
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
                # had 单固（票 38）：poolList 池级 single 接入解析链
                _append_sale_status_once(
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
                # 进球类单固（票 wb-04）：poolList 池级 single
                _append_sale_status_once(
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


def _capture_pipeline(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    raw_root: Path | None,
    now: datetime | None,
) -> tuple[IngestStats, list[ParsedMatch], int]:
    """Fetch → 原始证据 → 观测行 → 解析入库（capture_jingcai/探测共用）。"""
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
    stats = store_matches(
        conn, matches, observed_at=observed, observation_id=observation_id
    )
    return stats, matches, observation_id


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
    stats, _matches, _obs_id = _capture_pipeline(
        conn, settings, client, raw_root=raw_root, now=now
    )
    return stats


# 停售探测两形态（票 47 设计修正）：晚间场临场停售（开球前几十分钟）与
# 凌晨场前夜墙钟停售（北京 ~22-24 点统一停售，距开球 3-12h+）。
_PROBE_SOON_MINUTES = 180  # 开球前 3h 内恒探测
_PROBE_EVENING_HOUR = 19  # 北京 ≥19 点起，探测次日内（凌晨场）停售迁移
_PROBE_HORIZON_MINUTES = 16 * 60  # 粗筛上限（北京 19 点覆盖到次日上午开球）


@dataclass
class SaleStopProbe:
    """一次停售探测的结果（票 47 决策锚触发器）。"""

    candidates: int = 0
    stopped: list[sqlite3.Row] = field(default_factory=list)  # 本轮检出停售的候选行
    observation_id: int | None = None
    captured: IngestStats | None = None


def probe_sale_stops(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    raw_root: Path | None = None,
    now: datetime | None = None,
) -> SaleStopProbe:
    """
    停售加密探测（票 47 双锚之决策锚）。

    候选 = 已 join、未 stopped、「开球 ≤3h 或 北京 ≥19 点的 16h 内场次」。

    无候选零请求（探测载体不空耗）；有候选走全链路采集（证据/快照/销售
    状态幂等吸收），随后判定停售迁移：payload 显式 sellStatus=1（store_matches
    已落 stopped 行）或**从在售列表消失**（本函数补显式 stopped 行——端点
    只列在售场次，消失即停售；不补行则迁移时刻不可重建）。返回检出停售的
    候选行（含 odds_api_event_id/sport_key，供决策锚定向拉取）。
    """
    moment = now or datetime.now(UTC)
    now_iso = moment.isoformat(timespec="seconds")
    horizon = (moment + timedelta(minutes=_PROBE_HORIZON_MINUTES)).isoformat(
        timespec="seconds"
    )
    rows = fx_store.sale_stop_probe_candidates(conn, now_iso, horizon)
    soon = timedelta(minutes=_PROBE_SOON_MINUTES)
    candidates = [
        row
        for row in rows
        if str(row["latest_state"]) != "stopped"
        and (
            datetime.fromisoformat(str(row["kickoff_utc"])) - moment <= soon
            or moment.astimezone(CST).hour >= _PROBE_EVENING_HOUR
        )
    ]
    probe = SaleStopProbe(candidates=len(candidates))
    if not candidates:
        return probe
    stats, matches, observation_id = _capture_pipeline(
        conn, settings, client, raw_root=raw_root, now=moment
    )
    probe.captured = stats
    probe.observation_id = observation_id
    listed = {m.source_match_id for m in matches}
    code_by_fixture = {
        int(code_row["fixture_id"]): str(code_row["source_match_id"])
        for code_row in conn.execute(
            """
            SELECT fixture_id, source_match_id FROM match_codes
            WHERE kind = 'jingcai' AND source_match_id IS NOT NULL
            """
        ).fetchall()
    }
    for row in candidates:
        fixture_id = int(row["id"])
        latest = conn.execute(
            """
            SELECT sale_state FROM sale_statuses WHERE fixture_id = ?
            ORDER BY observed_at DESC, id DESC LIMIT 1
            """,
            (fixture_id,),
        ).fetchone()
        if latest is not None and str(latest["sale_state"]) == "stopped":
            probe.stopped.append(row)  # payload 显式停售（store_matches 已落行）
            continue
        source_match_id = code_by_fixture.get(fixture_id)
        if (
            source_match_id is not None
            and source_match_id not in listed
            # 消失判停售仅对确证 on_sale 的场次：unknown（从未观测在售，
            # 如手工 join）可能只是不在当期列表，不冒充迁移时刻
            and str(row["latest_state"]) == "on_sale"
            and latest is not None
            and str(latest["sale_state"]) == "on_sale"
        ):
            # 从在售列表消失 = 停售；补显式行保迁移时刻（append-only）
            _append_sale_status_once(
                conn,
                SaleStatusInput(
                    fixture_id=fixture_id,
                    market_code=None,
                    sale_state="stopped",
                    observed_at=now_iso,
                    observation_id=observation_id,
                ),
            )
            conn.commit()
            probe.stopped.append(row)
    return probe


def reprocess_observations(conn: sqlite3.Connection, raw_root: Path) -> ReprocessStats:
    """
    重解析全部 sporttery 原始证据并重放入库（票 38 存量单固修正）。

    计算器端点只返回在售场次，误记历史无法靠重拉修复；票 35 的原始
    证据存档（gzip + 哈希）可离线重解析。对每个观测行：校验 sha256 →
    以当前解析版本重放 ``store_matches``（快照 INSERT OR IGNORE、销售
    状态行按证据身份判重，可重复执行）。

    时间语义：重放行的 observed_at/observation_id 沿用原观测——修正内容
    在原证据时间线上即已可知（当时误解析），不倒填也不推迟；旧行保留
    （append-only 证据设计），同 observed_at 下 as-of 判定取新行（id 大）。
    哈希不符视为证据损坏，立即失败（fail-closed，不产生部分修正）。
    """
    stats = ReprocessStats()
    rows = conn.execute(
        "SELECT * FROM quote_observations WHERE source = 'sporttery' ORDER BY id"
    ).fetchall()
    stats.observations = len(rows)
    for row in rows:
        raw_ref = row["raw_ref"]
        if raw_ref is None:
            stats.skipped_no_raw += 1
            continue
        raw = observations.read_raw(raw_root, str(raw_ref))
        if observations.sha256_hex(raw) != str(row["raw_sha256"]):
            raise ValueError(f"raw evidence hash mismatch for observation {row['id']}")
        replayed = store_matches(
            conn,
            parse_matches(json.loads(raw)),
            observed_at=str(row["observed_at"]),
            observation_id=int(row["id"]),
        )
        stats.reparsed += 1
        stats.matches += replayed.matches
        stats.snapshots += replayed.snapshots
        stats.duplicate_snapshots += replayed.duplicate_snapshots
    conn.commit()
    return stats
