"""
Understat xG 特征层采集（票 45）：五大联赛 getLeagueData 纯 JSON AJAX。

合规注记：understat.com robots.txt 全站 Disallow——本模块为个人研究低频
使用（默认每日 1 首页 + 5 联赛文件 = 6 请求，票面上限 10；不碰逐射门/
球员级数据）。端点形态（2026-09-20 实测，research/17 §一）：联赛页 RC4
内嵌已撤，改 ``GET /getLeagueData/{league}/{season}``（首页暖 PHPSESSID +
浏览器 UA + Referer/XHR 头，~260-530KB 明文，无 Cloudflare）。

载荷结构：``{"teams": {id: {id, title, history[]}}, "dates": [...]}``——
dates 逐场（id/isResult/goals/xG/forecast{w,d,l}，数值一律字符串），
teams.history 逐场 xG/xGA/npxG/npxGA（去点球口径，与 dates 按队×时刻对齐）。
forecast 仅已赛场次携带（源把它随赛果存回）——定位是历史对标基准，
不是实时基准（票面"进评测基准族"）。

防前视红线（定则 2）：getLeagueData 赛后滚动更新，prior_* 列只累计
**开球日严格早于本场**的本季已完场 npxG/npxGA；同日场次互不可见
（同日错峰早场在晚场决策时点常未完场，保守按日截断）。
fixture join 走确定性键（开球日 ±1 + 双方队名经 team_aliases 解析唯一
命中），多候选/未解析一律 NULL 不硬配（定则 1，禁自信合并）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.reconcile import upsert_source_coverage
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling.team_align import NameIndex

SOURCE = "understat"
PARSE_VERSION = "understat_v1"
# fd 历史底座代码 → understat slug（票面覆盖=五大；俄超 rfpl 按需追加）
LEAGUES: dict[str, str] = {
    "E0": "epl",
    "SP1": "la_liga",
    "I1": "serie_a",
    "D1": "bundesliga",
    "F1": "ligue_1",
}
DEFAULT_LEAGUES: tuple[str, ...] = tuple(LEAGUES.values())
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
}
_SEASON_START_MONTH = 7  # 跨年赛季分界（7 月起属新季，与 openfootball 同口径）
_NOT_COVERED = 404


@dataclass
class UnderstatMatch:
    """一条 understat 场次（解析 + 派生特征；prior_* 由 attach_prior 填充）。"""

    match_id: str
    league: str
    season: str
    datetime_utc: str  # 源口径 UTC，秒精度 ISO
    home_team_id: str
    home_team: str
    away_team_id: str
    away_team: str
    is_result: bool
    goals_home: int | None
    goals_away: int | None
    xg_home: float | None  # dates 口径（含点球 xG）
    xg_away: float | None
    npxg_home: float | None  # teams.history 口径（去点球）
    npxg_away: float | None
    forecast_w: float | None  # 源自带胜平负概率（仅已赛场次携带）
    forecast_d: float | None
    forecast_l: float | None
    prior_npxg_home: float | None = None  # 本季严格早于本场开球的累计
    prior_npxga_home: float | None = None
    prior_matches_home: int | None = None
    prior_npxg_away: float | None = None
    prior_npxga_away: float | None = None
    prior_matches_away: int | None = None


@dataclass
class UnderstatSyncStats:
    """一次同步的统计（leagues 逐联赛×赛季，最新 run 行即状态）。"""

    observed_at: str = ""
    leagues: list[dict[str, Any]] = field(default_factory=list)
    matches: int = 0
    results: int = 0
    joined: int = 0
    unmatched: int = 0  # 未 join 到竞彩场次的行（全季 380 场大多无场次号，信息位）


def season_start_year(day: date) -> str:
    """日期 → 起始年（"2026" = 2026/27；7 月起属新季）。"""
    year = day.year if day.month >= _SEASON_START_MONTH else day.year - 1
    return str(year)


def _num(value: object) -> float | None:
    """载荷数值列：字符串/数值 → float；null/异常 → None。"""
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _int(value: object) -> int | None:
    num = _num(value)
    return int(num) if num is not None else None


def _iso(datetime_raw: object) -> str:
    """源时刻串（空格分隔）→ ISO（T 分隔）。"""
    return str(datetime_raw or "").replace(" ", "T")


def parse_league(
    doc: dict[str, Any], *, league: str, season: str
) -> list[UnderstatMatch]:
    """
    GetLeagueData 载荷 → 场次列表（纯函数）。

    npxG 从 teams.history 按（队 id × 时刻）对齐；history 只含已赛场次且
    与 dates 的 isResult 集合一致（2026-09-20 实测 2×关系成立）。
    """
    history: dict[tuple[str, str], float] = {}
    for raw_team in cast("dict[str, Any]", doc.get("teams") or {}).values():
        team_id = str(raw_team.get("id") or "")
        for entry in cast("list[dict[str, Any]]", raw_team.get("history") or []):
            npxg = _num(entry.get("npxG"))
            # history 时刻键为 date（dates[] 用 datetime；实测 2026-09 载荷）
            moment = _iso(entry.get("date") or entry.get("datetime"))
            if team_id and npxg is not None and moment:
                history[(team_id, moment)] = npxg

    matches: list[UnderstatMatch] = []
    for raw in cast("list[dict[str, Any]]", doc.get("dates") or []):
        dt = _iso(raw.get("datetime"))
        if not dt:
            continue
        home = cast("dict[str, Any]", raw.get("h") or {})
        away = cast("dict[str, Any]", raw.get("a") or {})
        forecast = cast("dict[str, Any]", raw.get("forecast") or {})
        matches.append(
            UnderstatMatch(
                match_id=str(raw.get("id") or ""),
                league=league,
                season=season,
                datetime_utc=dt,
                home_team_id=str(home.get("id") or ""),
                home_team=str(home.get("title") or ""),
                away_team_id=str(away.get("id") or ""),
                away_team=str(away.get("title") or ""),
                is_result=bool(raw.get("isResult")),
                goals_home=_int(
                    cast("dict[str, Any]", raw.get("goals") or {}).get("h")
                ),
                goals_away=_int(
                    cast("dict[str, Any]", raw.get("goals") or {}).get("a")
                ),
                xg_home=_num(cast("dict[str, Any]", raw.get("xG") or {}).get("h")),
                xg_away=_num(cast("dict[str, Any]", raw.get("xG") or {}).get("a")),
                npxg_home=history.get((str(home.get("id") or ""), dt)),
                npxg_away=history.get((str(away.get("id") or ""), dt)),
                forecast_w=_num(forecast.get("w")),
                forecast_d=_num(forecast.get("d")),
                forecast_l=_num(forecast.get("l")),
            )
        )
    return [m for m in matches if m.match_id]


def attach_prior(matches: list[UnderstatMatch]) -> None:
    """
    原地填充 prior_*：按比赛日分组滚动累计，**同日场次互不可见**。

    红线（定则 2）：累计只含 is_result 且双方 npxG 齐备、**开球日严格早于
    本场**的场次。跨日而非跨时刻截断是保守选择：同日错峰早场（如 19:45/
    20:00）在晚场决策时点常未完场，其赛后才定的最终 npxG 不得泄入（评审
    BLOCKER 修正）；代价是同日早场已完赛的少量信息被放弃，累计特征按日
    粒度更新（每日 09:20 同步在当日开球前，天然无损）。缺 npxG 的完场场
    跳过（不按 0 计）。未赛场次同样携带 prior（其开球前已完场的累计）。
    """
    running: dict[str, tuple[float, float, int]] = {}  # team_id → (npxg, npxga, n)
    ordered = sorted(matches, key=lambda m: m.datetime_utc)
    i = 0
    while i < len(ordered):
        # 同比赛日一组：先统一取 prior，再统一入账
        day = ordered[i].datetime_utc[:10]
        group: list[UnderstatMatch] = []
        while i < len(ordered) and ordered[i].datetime_utc[:10] == day:
            group.append(ordered[i])
            i += 1
        for m in group:
            m.prior_npxg_home, m.prior_npxga_home, m.prior_matches_home = running.get(
                m.home_team_id, (0.0, 0.0, 0)
            )
            m.prior_npxg_away, m.prior_npxga_away, m.prior_matches_away = running.get(
                m.away_team_id, (0.0, 0.0, 0)
            )
        for m in group:
            if m.is_result and m.npxg_home is not None and m.npxg_away is not None:
                for team_id, attack, concede in (
                    (m.home_team_id, m.npxg_home, m.npxg_away),
                    (m.away_team_id, m.npxg_away, m.npxg_home),
                ):
                    npxg, npxga, n = running.get(team_id, (0.0, 0.0, 0))
                    running[team_id] = (npxg + attack, npxga + concede, n + 1)


def warm_session(client: httpx.Client, settings: Settings) -> None:
    """访问首页取 PHPSESSID（AJAX 端点要求会话 cookie；失败向上抛）。"""
    response = client.get(settings.understat_base_url, headers=_HEADERS, timeout=25.0)
    response.raise_for_status()


def fetch_league(
    client: httpx.Client, settings: Settings, league: str, season: str
) -> dict[str, Any] | None:
    """拉一个联赛赛季载荷；404 → None（无此覆盖），其余异常向上抛。"""
    response = client.get(
        f"{settings.understat_base_url}/getLeagueData/{league}/{season}",
        headers={
            **_HEADERS,
            "Referer": f"{settings.understat_base_url}/league/{league}",
        },
        timeout=40.0,
    )
    if response.status_code == _NOT_COVERED:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"understat {league}/{season} 载荷非对象: {type(payload)}")
    return cast("dict[str, Any]", payload)


def _fixture_join_index(
    conn: sqlite3.Connection,
) -> dict[tuple[int, int, str], int | None]:
    """(主队 id, 客队 id, 开球日) → fixture_id；同键多场次 → None（歧义标记）。"""
    index: dict[tuple[int, int, str], int | None] = {}
    for row in conn.execute(
        "SELECT id, kickoff_utc, home_team_id, away_team_id FROM fixtures"
    ).fetchall():
        key = (
            int(row["home_team_id"]),
            int(row["away_team_id"]),
            str(row["kickoff_utc"])[:10],
        )
        if key in index and index[key] != int(row["id"]):
            index[key] = None
        else:
            index[key] = int(row["id"])
    return index


def _join_fixture(
    match: UnderstatMatch,
    index: dict[tuple[int, int, str], int | None],
    alias: NameIndex,
    seen: dict[str, int | None],
) -> int | None:
    """
    确定性 join：双方队名解析 → 同 id 且开球日 ±1 内唯一候选才落。

    窗口内任一日出现歧义键（同对阵同日多 fixture）即整体放弃——歧义日
    不能被邻日的唯一候选"救回"（禁自信合并，评审修正）。
    """
    home_id = seen.setdefault(match.home_team, alias.resolve(match.home_team))
    away_id = seen.setdefault(match.away_team, alias.resolve(match.away_team))
    if home_id is None or away_id is None:
        return None
    day = date.fromisoformat(match.datetime_utc[:10])
    candidates: set[int] = set()
    for delta in (-1, 0, 1):
        key = (home_id, away_id, (day + timedelta(days=delta)).isoformat())
        if key not in index:  # 该日无候选
            continue
        fixture_id = index[key]
        if fixture_id is None:  # 该日歧义 → 窗口作废
            return None
        candidates.add(fixture_id)
    return next(iter(candidates)) if len(candidates) == 1 else None


def upsert_matches(
    conn: sqlite3.Connection,
    matches: list[UnderstatMatch],
    *,
    alias: NameIndex,
    observed_at: str,
) -> list[int | None]:
    """
    场次行 UPSERT（现态表）；返回逐场 join 结果（供 run 行计数，口径同载荷）。

    完场态不回撤（is_result 取 MAX）、数值列新值优先但不回删（COALESCE）；
    prior_* 与 fixture_id 每次按最新解析重算（派生特征，可随源修订回移——
    与完场态的粘滞语义不对称，属设计取舍：prior 无生产消费者）。
    """
    index = _fixture_join_index(conn)
    resolved: dict[str, int | None] = {}
    joined: list[int | None] = []
    for m in matches:
        fixture_id = _join_fixture(m, index, alias, resolved)
        joined.append(fixture_id)
        conn.execute(
            """
            INSERT INTO understat_matches
                (match_id, league, season, datetime_utc, home_team_id, home_team,
                 away_team_id, away_team, is_result, goals_home, goals_away,
                 xg_home, xg_away, npxg_home, npxg_away,
                 prior_npxg_home, prior_npxga_home, prior_matches_home,
                 prior_npxg_away, prior_npxga_away, prior_matches_away,
                 forecast_w, forecast_d, forecast_l, fixture_id,
                 first_seen_at, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
            ON CONFLICT (match_id) DO UPDATE SET
                datetime_utc = excluded.datetime_utc,
                is_result = MAX(understat_matches.is_result, excluded.is_result),
                goals_home = COALESCE(
                    excluded.goals_home, understat_matches.goals_home
                ),
                goals_away = COALESCE(
                    excluded.goals_away, understat_matches.goals_away
                ),
                xg_home = COALESCE(excluded.xg_home, understat_matches.xg_home),
                xg_away = COALESCE(excluded.xg_away, understat_matches.xg_away),
                npxg_home = COALESCE(
                    excluded.npxg_home, understat_matches.npxg_home
                ),
                npxg_away = COALESCE(
                    excluded.npxg_away, understat_matches.npxg_away
                ),
                prior_npxg_home = excluded.prior_npxg_home,
                prior_npxga_home = excluded.prior_npxga_home,
                prior_matches_home = excluded.prior_matches_home,
                prior_npxg_away = excluded.prior_npxg_away,
                prior_npxga_away = excluded.prior_npxga_away,
                prior_matches_away = excluded.prior_matches_away,
                forecast_w = COALESCE(
                    excluded.forecast_w, understat_matches.forecast_w
                ),
                forecast_d = COALESCE(
                    excluded.forecast_d, understat_matches.forecast_d
                ),
                forecast_l = COALESCE(
                    excluded.forecast_l, understat_matches.forecast_l
                ),
                fixture_id = excluded.fixture_id,
                observed_at = excluded.observed_at
            """,
            (
                m.match_id,
                m.league,
                m.season,
                m.datetime_utc,
                m.home_team_id,
                m.home_team,
                m.away_team_id,
                m.away_team,
                int(m.is_result),
                m.goals_home,
                m.goals_away,
                m.xg_home,
                m.xg_away,
                m.npxg_home,
                m.npxg_away,
                m.prior_npxg_home,
                m.prior_npxga_home,
                m.prior_matches_home,
                m.prior_npxg_away,
                m.prior_npxga_away,
                m.prior_matches_away,
                m.forecast_w,
                m.forecast_d,
                m.forecast_l,
                fixture_id,
                observed_at,
                observed_at,
            ),
        )
    return joined


def sync_understat(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    alias: NameIndex,
    leagues: tuple[str, ...] = DEFAULT_LEAGUES,
    seasons: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> UnderstatSyncStats:
    """
    采集入口：暖会话 → 逐联赛拉取解析 → upsert + coverage + run 行。

    单联赛失败（网络/解析）计数跳过不炸整跑；404 记 not_covered。
    seasons 缺省为当前赛季（回填显式传年份串）。
    """
    now_dt = now or datetime.now(UTC)
    observed_at = now_dt.isoformat(timespec="seconds")
    stats = UnderstatSyncStats(observed_at=observed_at)
    resolved_seasons = seasons or (season_start_year(now_dt.date()),)
    warm_session(client, settings)
    for season in resolved_seasons:
        for league in leagues:
            entry: dict[str, Any] = {
                "league": league,
                "season": season,
                "matches": 0,
                "results": 0,
                "status": "ok",
            }
            try:
                doc = fetch_league(client, settings, league, season)
            except (httpx.HTTPError, ValueError) as exc:
                # HTTPError=网络/非 2xx；ValueError=200 垃圾体（json 解码）
                logger.warning(
                    "understat fetch failed: {}/{} ({})", league, season, exc
                )
                doc = None
                entry["status"] = "failed"
            time.sleep(0.5)  # 对源站礼貌限速（robots Disallow，低频使用）
            if doc is None:
                if entry["status"] == "ok":
                    entry["status"] = "not_covered"
                stats.leagues.append(entry)
                upsert_source_coverage(
                    conn,
                    source=SOURCE,
                    coverage_date=season,
                    match_count=0,
                    coverage_status=(
                        "fetch_failed" if entry["status"] == "failed" else "not_covered"
                    ),
                    observed_at=observed_at,
                    league_key=league,
                )
                continue
            try:
                matches = parse_league(doc, league=league, season=season)
                attach_prior(matches)
                joined_ids = upsert_matches(
                    conn, matches, alias=alias, observed_at=observed_at
                )
            except (ValueError, sqlite3.Error) as exc:
                logger.warning(
                    "understat parse failed: {}/{} ({})", league, season, exc
                )
                entry["status"] = "failed"
                stats.leagues.append(entry)
                upsert_source_coverage(
                    conn,
                    source=SOURCE,
                    coverage_date=season,
                    match_count=0,
                    coverage_status="fetch_failed",
                    observed_at=observed_at,
                    league_key=league,
                )
                continue
            written = len(matches)
            joined = sum(1 for row_id in joined_ids if row_id is not None)
            results = sum(1 for m in matches if m.is_result)
            missing_npxg = sum(
                1 for m in matches if m.is_result and m.npxg_home is None
            )
            entry.update(
                matches=written,
                results=results,
                joined=joined,
                results_missing_npxg=missing_npxg,  # 金丝雀：>0 = history 对齐失效
            )
            stats.leagues.append(entry)
            stats.matches += written
            stats.results += results
            stats.joined += joined
            stats.unmatched += written - joined
            if missing_npxg:
                logger.warning(
                    "understat {}/{} 有 {} 场完场缺 npxG（history 对齐失效）",
                    league,
                    season,
                    missing_npxg,
                )
            upsert_source_coverage(
                conn,
                source=SOURCE,
                coverage_date=season,
                match_count=written,
                coverage_status="covered",
                observed_at=observed_at,
                league_key=league,
            )
    record_sync_run(conn, stats)
    return stats


def record_sync_run(conn: sqlite3.Connection, stats: UnderstatSyncStats) -> int:
    """写一次同步的元信息行（append-only，最新行即状态）。"""
    cur = conn.execute(
        """
        INSERT INTO understat_sync_runs
            (source, observed_at, seasons, matches, results, joined, unmatched,
             failed, parse_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SOURCE,
            stats.observed_at or utc_now_iso(),
            json.dumps(stats.leagues, ensure_ascii=False),
            stats.matches,
            stats.results,
            stats.joined,
            stats.unmatched,
            sum(1 for e in stats.leagues if e["status"] == "failed"),
            PARSE_VERSION,
            utc_now_iso(),
        ),
    )
    conn.commit()
    if not cur.lastrowid:
        raise RuntimeError("understat_sync_runs INSERT 未产生 rowid")
    return int(cur.lastrowid)


def latest_sync_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次同步（无历史返回 None）。"""
    return conn.execute(
        "SELECT * FROM understat_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def compare_rows(
    conn: sqlite3.Connection, league: str, seasons: tuple[str, ...]
) -> list[sqlite3.Row]:
    """某联赛多季场次行，按开球时刻排序（对比语料读取；ADR-0008 SQL 归本包）。"""
    wanted = set(seasons)
    return [
        row
        for row in conn.execute(
            """
            SELECT match_id, season, datetime_utc, home_team_id, away_team_id,
                   goals_home, goals_away, npxg_home, npxg_away,
                   forecast_w, forecast_d, forecast_l
            FROM understat_matches
            WHERE league = ?
            ORDER BY datetime_utc
            """,
            (league,),
        ).fetchall()
        if str(row["season"]) in wanted
    ]


def stats_dict(stats: UnderstatSyncStats) -> dict[str, Any]:
    """统计 → 可 JSON 化 dict（flow 返回值/日志用）。"""
    return {
        "observed_at": stats.observed_at,
        "leagues": stats.leagues,
        "matches": stats.matches,
        "results": stats.results,
        "joined": stats.joined,
        "unmatched": stats.unmatched,
        "failed": sum(1 for e in stats.leagues if e["status"] == "failed"),
    }
