"""
Transfermarkt 教练任职层采集（autoresearch 票 18；账本 §4.1#12 换帅轴前置）。

免费页面直爬（本仓 Python 栈，worldfootballR 为 R 包不适用；httpx+tenacity
+limits 三件套走 rate_limit.py 共享闸门）。两页型：

- 联赛赛季页 ``/{slug}/startseite/wettbewerb/{code}/plus/?saison_id=YYYY``
  → 该季俱乐部清单（枚举面，联赛×赛季覆盖报告的分母）；
- 俱乐部 staff-history 页 ``/{slug}/mitarbeiterhistorie/verein/{id}?personalie_id=N``
  （旧名 Trainerhistorie 已改名）→ 一页全历史任职行；N=1 Manager、
  N=10 Caretaker Manager 两角色分别拉（页混排全部职员，必须显式过滤）。

行粒度 = TM 原生区间：一行 = 一教练一段 [date_from, date_to]（开区间=现任）。
落库 upsert 现态（同键冲突更新右端/games，clubelo 先例）；覆盖登记走
source_coverage（source='transfermarkt'，league_key=TM 码、coverage_date=
赛季起始年）；run 台账 append-only。identity：TM 侧英文名，不在此模块做
中文映射（JC 中文面归消费端别名桥，票面红线）。

采集范围：CorpusScope fdhist 重叠 11 联赛（TM_LEAGUES 映射），2016 起始
季至今（backfill_seasons）。整跑一次回填 ≈ 11×N 联赛页 + 逐俱乐部 2 页，
~2s 礼貌间距约 15-25 分钟——回填实跑是合并后运维步（本票交付代码+测试）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
from limits import RateLimitItemPerMinute
from limits.strategies import MovingWindowRateLimiter
from loguru import logger
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from goalx_backend.config import Settings
from goalx_backend.data.ingest.shell import CHROME_129_UA
from goalx_backend.data.reconcile import upsert_source_coverage
from goalx_backend.db import utc_now_iso
from goalx_backend.rate_limit import default_limiter, throttle

SOURCE = "transfermarkt"
PARSE_VERSION = "tm_coach_v1"

#: CorpusScope fdhist 重叠 11 联赛 ↔ TM competition 码（2026-11-10 实测全 200）
TM_LEAGUES: dict[str, str] = {
    "E0": "GB1",
    "E1": "GB2",
    "SP1": "ES1",
    "D1": "L1",
    "I1": "IT1",
    "F1": "FR1",
    "N1": "NL1",
    "P1": "PO1",
    "T1": "TR1",
    "B1": "BE1",
    "SC0": "SC1",
}

MANAGER_ROLE = "manager"
CARETAKER_ROLE = "caretaker"
#: TM staff-history 角色过滤参数（页默认混排全部职员，必须显式选角色）
ROLE_PARAMS: dict[str, str] = {MANAGER_ROLE: "1", CARETAKER_ROLE: "10"}
ROLES: tuple[str, ...] = (MANAGER_ROLE, CARETAKER_ROLE)

BACKFILL_FIRST_SEASON = 2016  # 票面：2016-2026 十年窗
# 礼貌限速：固定间距主导 + 滑窗只加顶（rate_limit.py 语义约定，票 57 不变量）
REQUEST_GAP_SECONDS = 2.0
REQUEST_WINDOW = RateLimitItemPerMinute(30)
_NOT_COVERED = 404  # 页不存在（联赛未覆盖/俱乐部缺页）——非错误
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0

_HEADERS = {
    "User-Agent": CHROME_129_UA,
    "Accept-Language": "en-US,en;q=0.9",
}

_SEASON_START_MONTH = 7  # 跨年赛季分界（7 月起属新季，understat 同口径）

_CLUB_ANCHOR_RE = re.compile(
    r'<a title="([^"]+)" href="/([a-z0-9-]+)/startseite/verein/(\d+)[^"]*">'
)
# 顶层行 = 带 odd/even class 的 tr；行内嵌 inline-table 自带无 class 的
# 子 tr，故以「下一个顶层行起点」为界切行（</tr> 非贪婪会被子行截断）
_ROW_TR = r'<tr class="(?:odd|even)(?: [^"]*)?">'
_ROW_RE = re.compile(
    _ROW_TR + r"(.*?)(?=" + _ROW_TR + r"|\Z)",
    re.S,
)
_COACH_LINK_RE = re.compile(r'href="/[a-z0-9-]+/profil/trainer/(\d+)"')
_COACH_NAME_RE = re.compile(r'<td class=hauptlink><a[^>]*title="([^"]+)"')
_NAME_CELL_RE = re.compile(r"<td><table class=inline-table>.*?</table></td>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TM_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


@dataclass(frozen=True)
class TmClub:
    """联赛页枚举出的一家俱乐部（slug 供 staff 页拼 URL）。"""

    club_tm_id: int
    club_name: str
    slug: str


@dataclass(frozen=True)
class TmTenure:
    """一行任职区间（教练-球队-起止；date_to 空=现任）。"""

    club_tm_id: int
    club_name: str
    role: str
    coach_tm_id: int | None
    coach_name: str
    date_from: str
    date_to: str | None
    games: int | None


@dataclass
class TmCoachSyncStats:
    """一次同步的统计（leagues 逐联赛×赛季，最新 run 行即状态）。"""

    observed_at: str = ""
    leagues: list[dict[str, Any]] = field(default_factory=list)
    clubs: int = 0
    tenure_rows: int = 0
    written: int = 0
    failed_clubs: list[str] = field(default_factory=list)

    def as_leagues_json(self) -> str:
        """逐联赛明细 → run 行 JSON 列。"""
        return json.dumps(self.leagues, ensure_ascii=False)


def _tm_date_to_iso(raw: str) -> str | None:
    """TM dd/mm/yyyy → ISO；空/异形/假日期（99/99/2020）返回 None（诚实留空）。"""
    cleaned = raw.strip()
    if not _TM_DATE_RE.match(cleaned):
        return None
    try:
        parsed = datetime.strptime(cleaned, "%d/%m/%Y")
    except ValueError:
        return None
    return parsed.date().isoformat()


def _plain(cell: str) -> str:
    """格内文 → 去标签纯文本（&nbsp; 收敛为空格）。"""
    text = re.sub(r"<[^>]+>", " ", cell).replace("&nbsp;", " ")
    return " ".join(text.split())


def parse_clubs(html: str) -> list[TmClub]:
    """
    联赛赛季页 → 俱乐部清单（文档序去重，首见为准）。

    页无俱乐部锚（维护/改版）返回空表——由调用方按 not_covered 登记。
    """
    clubs: dict[int, TmClub] = {}
    for name, slug, club_id in _CLUB_ANCHOR_RE.findall(html):
        cid = int(club_id)
        clubs.setdefault(cid, TmClub(club_tm_id=cid, club_name=name, slug=slug))
    return list(clubs.values())


def parse_staff_history(
    html: str, *, role: str, club_tm_id: int, club_name: str
) -> list[TmTenure]:
    """
    staff-history 页（角色已由请求参数过滤）→ 任职行列表（纯函数）。

    只认含 ``/profil/trainer/`` 的行（职员工种行跳过）；appointed=行内
    首个日期格、end=其后一格（空=现任）；games=行内纯数字格（链接计数）。
    多国籍/多徽标行不位移（按格型而非格位取值）。
    """
    tenures: list[TmTenure] = []
    for row in _ROW_RE.findall(html):
        coach_link = _COACH_LINK_RE.search(row)
        name_match = _COACH_NAME_RE.search(row)
        if coach_link is None or name_match is None:
            continue
        # 名字格（内嵌 inline-table）整块摘除后再取平铺格，防嵌套 td 错位
        flat = _NAME_CELL_RE.sub("", row, count=1)
        cells = [_plain(cell) for cell in _TD_RE.findall(flat)]
        date_cells = [c for c in cells if _TM_DATE_RE.match(c)]
        appointed = _tm_date_to_iso(date_cells[0]) if date_cells else None
        if appointed is None:
            continue  # 无上任日期的行不构成任职区间
        # end = appointed 在平铺格序列中的下一格（空串=现任）
        try:
            end_raw = cells[cells.index(date_cells[0]) + 1]
        except (IndexError, ValueError):
            end_raw = ""
        games = next((int(c) for c in cells if c.isdigit()), None)
        tenures.append(
            TmTenure(
                club_tm_id=club_tm_id,
                club_name=club_name,
                role=role,
                coach_tm_id=int(coach_link.group(1)),
                coach_name=name_match.group(1),
                date_from=appointed,
                date_to=_tm_date_to_iso(end_raw),
                games=games,
            )
        )
    return tenures


def season_start_year(day: date) -> str:
    """日期 → TM 赛季起始年（"2026" = 2026/27；7 月起属新季）。"""
    year = day.year if day.month >= _SEASON_START_MONTH else day.year - 1
    return str(year)


def backfill_seasons(*, today: date) -> tuple[str, ...]:
    """回填季集：2016 起始季至今（票面十年窗）。"""
    current = int(season_start_year(today))
    return tuple(str(y) for y in range(BACKFILL_FIRST_SEASON, current + 1))


def upsert_tenures(
    conn: sqlite3.Connection, tenures: list[TmTenure], *, observed_at: str
) -> int:
    """任职行 upsert 现态：同键冲突更新右端/games/观测时刻（clubelo 同型）。"""
    count = 0
    for t in tenures:
        cur = conn.execute(
            """
            INSERT INTO tm_coach_tenures
                (club_tm_id, club_name, role, coach_tm_id, coach_name,
                 date_from, date_to, games, first_seen_at, observed_at,
                 parse_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (club_tm_id, role, coach_name, date_from) DO UPDATE SET
                club_name = excluded.club_name,
                coach_tm_id = excluded.coach_tm_id,
                date_to = excluded.date_to,
                games = excluded.games,
                observed_at = excluded.observed_at,
                parse_version = excluded.parse_version
            """,
            (
                t.club_tm_id,
                t.club_name,
                t.role,
                t.coach_tm_id,
                t.coach_name,
                t.date_from,
                t.date_to,
                t.games,
                observed_at,
                observed_at,
                PARSE_VERSION,
            ),
        )
        if cur.rowcount > 0:
            count += 1
    return count


def upsert_club_seasons(
    conn: sqlite3.Connection,
    *,
    league_tm_code: str,
    season: str,
    clubs: list[TmClub],
    observed_at: str,
) -> int:
    """联赛×赛季成员面 upsert（首见名冻结，观测时刻刷新）。"""
    count = 0
    for club in clubs:
        cur = conn.execute(
            """
            INSERT INTO tm_club_seasons
                (league_tm_code, season, club_tm_id, club_name,
                 first_seen_at, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (league_tm_code, season, club_tm_id) DO UPDATE SET
                club_name = excluded.club_name,
                observed_at = excluded.observed_at
            """,
            (
                league_tm_code,
                season,
                club.club_tm_id,
                club.club_name,
                observed_at,
                observed_at,
            ),
        )
        if cur.rowcount > 0:
            count += 1
    return count


def _retrying(sleeper: Callable[[float], None]) -> Retrying:
    """Tenacity 重试策略（srct 同型：传输类失败指数退避，reraise）。"""
    return Retrying(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(MAX_RETRIES + 1),
        wait=wait_exponential(
            multiplier=BACKOFF_BASE_SECONDS, exp_base=2, max=BACKOFF_CAP_SECONDS
        ),
        sleep=sleeper,
        reraise=True,
    )


class _TmNotFound(Exception):
    """TM 404（联赛/俱乐部页不存在）——非错误，按无覆盖处理。"""


@dataclass
class _Fetcher:
    """采集执行器：滑窗顶帽 + tenacity 重试 + 固定礼貌间距（三件套收口）。"""

    client: httpx.Client
    limiter: MovingWindowRateLimiter
    sleeper: Callable[[float], None]

    def get(self, url: str) -> str | None:
        """闸门+重试拉一页；404 → None（无此覆盖），网络失败上抛由调用方计。"""

        def gated() -> str:
            throttle(self.limiter, REQUEST_WINDOW, self.sleeper)
            response = self.client.get(
                url, headers=_HEADERS, timeout=30.0, follow_redirects=True
            )
            if response.status_code == _NOT_COVERED:
                raise _TmNotFound(url)
            response.raise_for_status()
            return response.text

        try:
            return _retrying(self.sleeper)(gated)
        except _TmNotFound:
            return None

    def gap(self) -> None:
        """页间固定礼貌间距（主导限速；窗口只加顶——rate_limit.py 语义）。"""
        self.sleeper(REQUEST_GAP_SECONDS)


def league_url(settings: Settings, league_tm_code: str, season: str) -> str:
    """联赛赛季页 URL（slug 用占位 x，TM 服务端按 wettbewerb 码路由）。"""
    return (
        f"{settings.transfermarkt_base_url}/x/startseite/wettbewerb/"
        f"{league_tm_code}/plus/?saison_id={season}"
    )


def staff_url(settings: Settings, club: TmClub, role: str) -> str:
    """俱乐部 staff-history 页 URL（按角色过滤，slug 取联赛页锚）。"""
    return (
        f"{settings.transfermarkt_base_url}/{club.slug}/mitarbeiterhistorie/"
        f"verein/{club.club_tm_id}?personalie_id={ROLE_PARAMS[role]}"
    )


def _cover(
    conn: sqlite3.Connection,
    *,
    season: str,
    tm_code: str,
    clubs: int,
    status: str,
    observed_at: str,
) -> None:
    """一个联赛×赛季的覆盖现态登记。"""
    upsert_source_coverage(
        conn,
        source=SOURCE,
        coverage_date=season,
        match_count=clubs,
        coverage_status=status,
        observed_at=observed_at,
        league_key=tm_code,
    )


def _sync_one_league(
    conn: sqlite3.Connection,
    fetch: _Fetcher,
    settings: Settings,
    *,
    fd_code: str,
    tm_code: str,
    season: str,
    observed_at: str,
) -> tuple[dict[str, Any], list[TmClub]]:
    """一个联赛×赛季：拉页 → 覆盖登记 + 俱乐部清单（拉失败计 fetch_failed）。"""
    entry: dict[str, Any] = {
        "league": fd_code,
        "tm_code": tm_code,
        "season": season,
        "clubs": 0,
        "status": "ok",
    }
    html: str | None
    try:
        html = fetch.get(league_url(settings, tm_code, season))
    except httpx.HTTPError as exc:
        logger.warning("tm league fetch failed: {} {} ({})", tm_code, season, exc)
        html = None
        entry["status"] = "fetch_failed"
    fetch.gap()
    if html is None:
        if entry["status"] == "ok":
            entry["status"] = "not_covered"
        _cover(
            conn,
            season=season,
            tm_code=tm_code,
            clubs=0,
            status=entry["status"],
            observed_at=observed_at,
        )
        return entry, []
    clubs = parse_clubs(html)
    entry["clubs"] = len(clubs)
    if clubs:
        _cover(
            conn,
            season=season,
            tm_code=tm_code,
            clubs=len(clubs),
            status="covered",
            observed_at=observed_at,
        )
    else:
        entry["status"] = "not_covered"
        _cover(
            conn,
            season=season,
            tm_code=tm_code,
            clubs=0,
            status="not_covered",
            observed_at=observed_at,
        )
    return entry, clubs


def _collect_club_tenures(
    fetch: _Fetcher, settings: Settings, club: TmClub
) -> list[TmTenure] | None:
    """一家俱乐部两角色 staff 页 → 任职行；任一角色拉失败返回 None。"""
    tenures: list[TmTenure] = []
    for role in ROLES:
        html: str | None
        try:
            html = fetch.get(staff_url(settings, club, role))
        except httpx.HTTPError as exc:
            logger.warning("tm staff fetch failed: {} ({})", club.club_name, exc)
            html = None
        fetch.gap()
        if html is None:
            return None
        tenures.extend(
            parse_staff_history(
                html, role=role, club_tm_id=club.club_tm_id, club_name=club.club_name
            )
        )
    return tenures


def sync_tm_coach(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    leagues: tuple[str, ...] | None = None,
    seasons: tuple[str, ...] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
) -> TmCoachSyncStats:
    """
    采集入口：逐联赛×赛季枚举俱乐部 → 逐俱乐部两角色 staff 页 → upsert。

    失败语义：联赛页拉失败记 fetch_failed 继续；俱乐部 staff 页拉失败计数
    跳过（clubelo failed_clubs 同型）；逐俱乐部提交（断点重跑只补未落库）。
    seasons 缺省当前季（周拍保鲜）；回填显式传 backfill_seasons()。
    """
    now_dt = now or datetime.now(UTC)
    observed_at = now_dt.isoformat(timespec="seconds")
    stats = TmCoachSyncStats(observed_at=observed_at)
    fetch = _Fetcher(client, default_limiter(), sleeper)
    resolved_leagues = (
        tuple(TM_LEAGUES.items())
        if leagues is None
        else tuple((code, TM_LEAGUES[code]) for code in leagues)
    )
    resolved_seasons = seasons or (season_start_year(now_dt.date()),)

    clubs_seen: dict[int, TmClub] = {}
    for season in resolved_seasons:
        for fd_code, tm_code in resolved_leagues:
            entry, clubs = _sync_one_league(
                conn,
                fetch,
                settings,
                fd_code=fd_code,
                tm_code=tm_code,
                season=season,
                observed_at=observed_at,
            )
            for club in clubs:
                clubs_seen.setdefault(club.club_tm_id, club)
            if clubs:
                upsert_club_seasons(
                    conn,
                    league_tm_code=tm_code,
                    season=season,
                    clubs=clubs,
                    observed_at=observed_at,
                )
            stats.leagues.append(entry)
        conn.commit()  # 逐赛季提交：断点续跑从缺失季续起

    stats.clubs = len(clubs_seen)
    for club_tm_id in sorted(clubs_seen):
        club = clubs_seen[club_tm_id]
        tenures = _collect_club_tenures(fetch, settings, club)
        if not tenures:
            stats.failed_clubs.append(club.club_name)
            continue
        stats.tenure_rows += len(tenures)
        stats.written += upsert_tenures(conn, tenures, observed_at=observed_at)
        conn.commit()  # 逐俱乐部提交：断点重跑从缺失俱乐部续起
    record_sync_run(conn, stats)
    conn.commit()
    return stats


def record_sync_run(conn: sqlite3.Connection, stats: TmCoachSyncStats) -> int:
    """写一次同步的元信息行（append-only，最新行即状态）。"""
    cur = conn.execute(
        """
        INSERT INTO tm_coach_sync_runs
            (observed_at, leagues, clubs, tenure_rows, written, failed,
             parse_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats.observed_at or utc_now_iso(),
            stats.as_leagues_json(),
            stats.clubs,
            stats.tenure_rows,
            stats.written,
            json.dumps(stats.failed_clubs, ensure_ascii=False),
            PARSE_VERSION,
            utc_now_iso(),
        ),
    )
    if not cur.lastrowid:
        raise RuntimeError("tm_coach_sync_runs INSERT 未产生 rowid")
    return int(cur.lastrowid)


def latest_sync_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次同步（无历史返回 None）。"""
    return conn.execute(
        "SELECT * FROM tm_coach_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def distinct_clubs(conn: sqlite3.Connection) -> list[str]:
    """已落库俱乐部名清单（别名桥输入；读走属主包）。"""
    return [
        str(row["club_name"])
        for row in conn.execute(
            "SELECT DISTINCT club_name FROM tm_club_seasons ORDER BY club_name"
        )
    ]


def stats_dict(stats: TmCoachSyncStats) -> dict[str, Any]:
    """统计 → 可 JSON 化 dict（flow 返回值/日志用）。"""
    return {
        "observed_at": stats.observed_at,
        "leagues": stats.leagues,
        "clubs": stats.clubs,
        "tenure_rows": stats.tenure_rows,
        "written": stats.written,
        "failed_clubs": stats.failed_clubs,
    }
