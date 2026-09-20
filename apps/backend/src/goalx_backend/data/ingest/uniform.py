"""
源A uniform 族官方赛果采集与并行对账（票 44）。

端点实证（2026-09-20 票 44 第一步深探；research/17 §五）：
- ``GET {sporttery_uniform_url}?matchPage=1&matchBeginDate=&matchEndDate=
  &pageSize=30&pageNo=&isFix=0&pcOrWap=1``（浏览器 UA + 源A Referer 即直通；
  jc 族 getMatchResultV1 的 403 是端点级，与本端点无关）；分页 value.pages。
- matchResultStatus 枚举（2026-08-06..09-20 七周 725 场全分布，票 44 实测）：
  ``'2'``=完场（比分 ``H:A``；poolStatus ``Payout``=已派彩、空=未派彩且可
  持久存在、``Refund``=无效场次退款）；``'0'``=取消（sectionsNo999="取消"）；
  ``'1'``=未完场（poolStatus=Close，无比分）。未见其他值；未知值 fail-closed。
- join 走定则 1 确定性键：match_codes.source_match_id = str(matchId)
  （源A calculator 写入的同一官方 ID，一跳映射；无业务日推导）。

口径守则（票 44，与源D 同构 fail-closed）：
- 并行对账阶段不改结算事实源（ADR 0001 澄清条款）——本模块只落观测
  （append-only，UNIQUE(match_id, observed_at) 同跑幂等）与对账清单，不 import；
- 源无逐场官方发布时点，published_at 不伪造；观测时点即 observed_at
  （本批采集起始时刻，与源D 票 42 同口径；多跑一次就多一行——poolStatus
  迁移留痕）；
- winFlag 与比分不自洽、状态值未知、终态但比分非数字 → 待人工，不比对；
- 对账读库内最新观测（跨 run 留痕的终态，不依赖单次抓取的完整性）。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.ingest.caiguo import candidate_business_dates
from goalx_backend.data.reconcile import (
    ReconcileStats,
    ReferenceResult,
    reconcile_draw_results,
    record_reconciliation_run,
    upsert_source_coverage,
)
from goalx_backend.db import utc_now_iso

SOURCE = "sporttery.cn"
PARSE_VERSION = "uniform_v1"
# 分页护栏：候选窗口至多 8 天、每日 ~40 场、pageSize 30 → 正常 ≤ 12 页
_MAX_PAGES = 30
_PAGE_SIZE = 30
_HAD_LETTER = {"h": "H", "d": "D", "a": "A"}
_SCORE_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


@dataclass
class UniformObservation:
    """一条官方赛果观测（解析后的形状；raw 字段全保留证据）。"""

    match_id: int
    match_num_str: str
    match_date: str
    league_id: int | None
    league_name: str
    result_status: str
    pool_status: str
    full_raw: str
    half_raw: str
    home_goals: int | None
    away_goals: int | None
    half_home_goals: int | None
    half_away_goals: int | None
    win_flag: str
    odds_h: str
    odds_d: str
    odds_a: str
    goal_line: str
    betting_single: int | None
    fixture_id: int | None = None
    business_date: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> UniformObservation:
        """库内观测行 → 观测对象（对账复用拒因/终态判定，逻辑单源）。"""
        return cls(
            match_id=int(row["match_id"]),
            match_num_str=str(row["match_num_str"]),
            match_date=str(row["match_date"]),
            league_id=int(row["league_id"]) if row["league_id"] is not None else None,
            league_name=str(row["league_name"] or ""),
            result_status=str(row["result_status"]),
            pool_status=str(row["pool_status"] or ""),
            full_raw=str(row["full_score_raw"] or ""),
            half_raw=str(row["half_score_raw"] or ""),
            home_goals=int(row["home_goals"])
            if row["home_goals"] is not None
            else None,
            away_goals=int(row["away_goals"])
            if row["away_goals"] is not None
            else None,
            half_home_goals=(
                int(row["half_home_goals"])
                if row["half_home_goals"] is not None
                else None
            ),
            half_away_goals=(
                int(row["half_away_goals"])
                if row["half_away_goals"] is not None
                else None
            ),
            win_flag=str(row["win_flag"] or ""),
            odds_h=str(row["odds_h"] or ""),
            odds_d=str(row["odds_d"] or ""),
            odds_a=str(row["odds_a"] or ""),
            goal_line=str(row["goal_line"] or ""),
            betting_single=(
                int(row["betting_single"])
                if row["betting_single"] is not None
                else None
            ),
            fixture_id=int(row["fixture_id"])
            if row["fixture_id"] is not None
            else None,
            business_date=str(row["business_date"]) if row["business_date"] else None,
        )

    @property
    def void(self) -> bool:
        """官方取消/无效场次（投注退款口径 → DrawResult.void 同义）。"""
        return self.pool_status == "Refund" or self.result_status == "0"

    @property
    def void_reason(self) -> str | None:
        """官方 void 判定的人读理由（进对账清单 detail）。"""
        if self.pool_status == "Refund":
            return "官方无效场次(Refund)"
        if self.result_status == "0":
            return "官方取消"
        return None

    @property
    def final(self) -> bool:
        """完场且全场比分为数字（可作对账参照）。"""
        return self.result_status == "2" and self.home_goals is not None

    @property
    def reject_reason(self) -> str | None:
        """fail-closed 拒因（有拒因的行不进对账，直接待人工）。"""
        if self.result_status not in {"0", "1", "2"}:
            return f"unknown_result_status:{self.result_status}"
        if self.final and self.win_flag in _HAD_LETTER.values():
            home, away = self.home_goals or 0, self.away_goals or 0
            expected = _HAD_LETTER["h" if home > away else "a" if home < away else "d"]
            if expected != self.win_flag:
                return "win_flag_inconsistent"
        if self.result_status == "2" and not self.void and self.home_goals is None:
            return f"unparseable_score:{self.full_raw}"
        return None


@dataclass
class UniformSyncStats:
    """一次 uniform 采集的统计。"""

    source: str = SOURCE
    observed_at: str = ""
    business_dates: list[str] = field(default_factory=list)
    fetched: int = 0  # 端点返回的场次数（含非终态）
    observed_rows: int = 0  # 本次新落观测行数（同跑幂等重放为 0）
    unmatched: int = 0  # 官方有、库内无对应竞彩场次（范围外，不报警）
    rejected: int = 0  # fail-closed 拒因行数（进待人工清单）


def _headers() -> dict[str, str]:
    """同域同头直通（票 44 实测）：浏览器 UA + 源A Referer。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "Referer": "https://www.sporttery.cn/",
        "Accept": "application/json, text/plain, */*",
    }


def fetch_uniform_results(
    client: httpx.Client,
    settings: Settings,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    """拉取一个 matchDate 区间的全部赛果行（分页遍历；纯 raw dict）。"""
    rows: list[dict[str, Any]] = []
    page = 1
    while page <= _MAX_PAGES:
        response = client.get(
            settings.sporttery_uniform_url,
            params={
                "matchPage": "1",
                "matchBeginDate": date_from,
                "matchEndDate": date_to,
                "pageSize": str(_PAGE_SIZE),
                "pageNo": str(page),
                "isFix": "0",
                "pcOrWap": "1",
            },
            headers=_headers(),
            timeout=25.0,
        )
        response.raise_for_status()
        payload = cast("dict[str, Any]", response.json())
        if not payload.get("success", False):
            raise RuntimeError(f"uniform 端点返回失败: {payload.get('errorMessage')}")
        value = cast("dict[str, Any]", payload["value"])
        batch = cast("list[dict[str, Any]]", value.get("matchResult") or [])
        rows.extend(batch)
        if not batch or page >= int(value.get("pages", page)):
            break
        page += 1
    return rows


def _parse_score(raw: object) -> tuple[int | None, int | None]:
    """``'H:A'`` → (H, A)；其余（取消/无效场次/空/None）→ (None, None)。"""
    if not isinstance(raw, str):
        return None, None
    match = _SCORE_RE.match(raw)
    if match is None:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _int_or_none(value: object) -> int | None:
    if isinstance(value, (int, str)) and str(value) != "":
        return int(value)
    return None


def parse_uniform_match(raw: dict[str, Any]) -> UniformObservation:
    """一行官方载荷 → 观测（纯函数；raw 字段保留证据）。"""
    home, away = _parse_score(raw.get("sectionsNo999"))
    half_home, half_away = _parse_score(raw.get("sectionsNo1"))
    return UniformObservation(
        match_id=int(raw["matchId"]),
        match_num_str=str(raw.get("matchNumStr") or ""),
        match_date=str(raw.get("matchDate") or ""),
        league_id=_int_or_none(raw.get("leagueId")),
        league_name=str(raw.get("leagueName") or raw.get("leagueNameAbbr") or ""),
        result_status=str(raw.get("matchResultStatus") or ""),
        pool_status=str(raw.get("poolStatus") or ""),
        full_raw=str(raw.get("sectionsNo999") or ""),
        half_raw=str(raw.get("sectionsNo1") or ""),
        home_goals=home,
        away_goals=away,
        half_home_goals=half_home,
        half_away_goals=half_away,
        win_flag=str(raw.get("winFlag") or ""),
        odds_h=str(raw.get("h") or ""),
        odds_d=str(raw.get("d") or ""),
        odds_a=str(raw.get("a") or ""),
        goal_line=str(raw.get("goalLine") or ""),
        betting_single=_int_or_none(raw.get("bettingSingle")),
    )


def _fixture_id_for_match(
    conn: sqlite3.Connection, obs: UniformObservation
) -> int | None:
    """官方 matchId → fixture_id（match_codes.source_match_id 一跳确定性键）。"""
    row = conn.execute(
        """
        SELECT mc.fixture_id, mc.business_date FROM match_codes mc
        WHERE mc.kind = 'jingcai' AND mc.source_match_id = ?
        """,
        (str(obs.match_id),),
    ).fetchone()
    if row is None:
        return None
    obs.business_date = str(row["business_date"])
    return int(row["fixture_id"])


def store_observations(
    conn: sqlite3.Connection, observations: list[UniformObservation], observed_at: str
) -> int:
    """落观测行（append-only；UNIQUE(match_id, observed_at) 同跑幂等）。"""
    written = 0
    for obs in observations:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO uniform_result_observations
(match_id, match_num_str, match_date, business_date, fixture_id, league_id,
    league_name, result_status, pool_status, full_score_raw, half_score_raw,
    home_goals, away_goals, half_home_goals, half_away_goals, win_flag,
    odds_h, odds_d, odds_a, goal_line, betting_single, void_flag, void_reason,
    observed_at, parse_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs.match_id,
                obs.match_num_str,
                obs.match_date,
                obs.business_date,
                obs.fixture_id,
                obs.league_id,
                obs.league_name,
                obs.result_status,
                obs.pool_status,
                obs.full_raw,
                obs.half_raw,
                obs.home_goals,
                obs.away_goals,
                obs.half_home_goals,
                obs.half_away_goals,
                obs.win_flag,
                obs.odds_h,
                obs.odds_d,
                obs.odds_a,
                obs.goal_line,
                obs.betting_single,
                int(obs.void),
                obs.void_reason,
                observed_at,
                PARSE_VERSION,
                utc_now_iso(),
            ),
        )
        written += cur.rowcount
    conn.commit()
    return written


def latest_observations_for_dates(
    conn: sqlite3.Connection, business_dates: list[str]
) -> list[UniformObservation]:
    """窗口业务日内每 matchId 的最新观测（对账取数；id 升序归约）。"""
    if not business_dates:
        return []
    placeholders = ",".join("?" for _ in business_dates)
    rows = conn.execute(
        f"""
        SELECT * FROM uniform_result_observations
        WHERE fixture_id IS NOT NULL AND business_date IN ({placeholders})
        ORDER BY id
        """,  # noqa: S608 placeholders 数量即参数
        tuple(business_dates),
    ).fetchall()
    latest: dict[int, sqlite3.Row] = {}
    for row in rows:
        latest[int(row["match_id"])] = row  # id 升序 → 后写覆盖
    return [UniformObservation.from_row(row) for row in latest.values()]


def _to_references(
    observations: list[UniformObservation], stats: ReconcileStats
) -> list[ReferenceResult]:
    """观测 → 对账参照（拒因/非终态不进参照；拒因行进待人工清单）。"""
    refs: list[ReferenceResult] = []
    for obs in observations:
        if obs.fixture_id is None or obs.business_date is None:
            continue
        reject = obs.reject_reason
        if reject is not None:
            stats.pending_manual.append(
                {
                    "business_date": obs.business_date,
                    "code": obs.match_num_str,
                    "reason": reject,
                }
            )
            continue
        if not (obs.final or obs.void):
            continue
        refs.append(
            ReferenceResult(
                fixture_id=obs.fixture_id,
                business_date=obs.business_date,
                code=obs.match_num_str,
                home_goals=obs.home_goals,
                away_goals=obs.away_goals,
                half_home_goals=obs.half_home_goals,
                half_away_goals=obs.half_away_goals,
                void=obs.void,
                void_reason=obs.void_reason,
            )
        )
    return refs


def _record_coverage(
    conn: sqlite3.Connection,
    observations: list[UniformObservation],
    date_from: str,
    date_to: str,
    observed_at: str,
) -> None:
    """
    coverage_date=matchDate、league_key=leagueId：每源每日每联赛看到了什么。

    区间内无行的 matchDate 记 fetched_empty（空≠无：采集成功但该日无场次）。
    """
    by_day_league: dict[tuple[str, str], int] = {}
    for obs in observations:
        key = (obs.match_date, str(obs.league_id if obs.league_id is not None else ""))
        by_day_league[key] = by_day_league.get(key, 0) + 1
    for (match_date, league_key), count in sorted(by_day_league.items()):
        upsert_source_coverage(
            conn,
            source=SOURCE,
            coverage_date=match_date,
            match_count=count,
            coverage_status="covered",
            observed_at=observed_at,
            league_key=league_key,
        )
    seen_days = {match_date for match_date, _ in by_day_league}
    day = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while day <= end:
        if day.isoformat() not in seen_days:
            upsert_source_coverage(
                conn,
                source=SOURCE,
                coverage_date=day.isoformat(),
                match_count=0,
                coverage_status="fetched_empty",
                observed_at=observed_at,
            )
        day += timedelta(days=1)


def sync_uniform_results(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    business_dates: list[str] | None = None,
    now: datetime | None = None,
) -> tuple[UniformSyncStats, ReconcileStats]:
    """
    同步入口：拉取 → 解析 → join → 观测落库 → coverage 登记 → 对账。

    - business_dates 缺省取 caiguo.candidate_business_dates（与源D 同一候选
      推导，空集零请求跳过）；
    - 采集区间按 matchDate 放宽 ±1 天（晚场归属前业务日），join 由
      source_match_id 决定，放宽只多采观测不多比对；
    - 对账读库内最新观测、只报清单不落事实（人工通道裁决），run 行
      append-only（空窗口也留一行 compared=0 的运行证据）。
    """
    now_dt = now or datetime.now(UTC)
    observed_at = now_dt.isoformat(timespec="seconds")
    dates = business_dates
    if dates is None:
        dates = candidate_business_dates(conn, now=now_dt)
    stats = UniformSyncStats(observed_at=observed_at, business_dates=list(dates))
    reconcile_stats = ReconcileStats(
        source=SOURCE, observed_at=observed_at, business_dates=list(dates)
    )

    if dates:
        # matchDate 区间：业务日集合放宽 ±1（晚场 matchDate=业务日+1 归属前
        # 业务日；join 由 source_match_id 决定，放宽只多采观测不多比对）
        date_from = (date.fromisoformat(min(dates)) - timedelta(days=1)).isoformat()
        date_to = (date.fromisoformat(max(dates)) + timedelta(days=1)).isoformat()
        raw_rows = fetch_uniform_results(client, settings, date_from, date_to)
        stats.fetched = len(raw_rows)
        observations = [parse_uniform_match(raw) for raw in raw_rows]
        for obs in observations:
            obs.fixture_id = _fixture_id_for_match(conn, obs)
            if obs.fixture_id is None:
                stats.unmatched += 1
        stats.observed_rows = store_observations(conn, observations, observed_at)
        stats.rejected = sum(
            1 for o in observations if o.fixture_id is not None and o.reject_reason
        )
        _record_coverage(conn, observations, date_from, date_to, observed_at)
    reconcile_stats.unmatched = stats.unmatched

    refs = _to_references(latest_observations_for_dates(conn, dates), reconcile_stats)
    reconcile_draw_results(conn, refs, reconcile_stats)
    record_reconciliation_run(conn, reconcile_stats, parse_version=PARSE_VERSION)
    return stats, reconcile_stats


def stats_dict(stats: UniformSyncStats) -> dict[str, Any]:
    """采集统计 → 可 JSON 化的 dict（flow 返回值用）。"""
    return {
        "source": stats.source,
        "observed_at": stats.observed_at,
        "business_dates": stats.business_dates,
        "fetched": stats.fetched,
        "observed_rows": stats.observed_rows,
        "unmatched": stats.unmatched,
        "rejected": stats.rejected,
    }
