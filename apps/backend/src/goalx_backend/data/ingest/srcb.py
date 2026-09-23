"""
源B（澳客移动端）欧指变化时序采集（票 49 采集先行，2026-09-21）。

定位（research/18 §五更新 + 用户裁决）：**补充语料源**——给时序研究/回测
补"开盘→临场完整变化路径"（fdhist 只有开/收两端点），并作主锚时间线的
独立第二源。不作主锚替代：pid 身份掩码（/tmp/srcb_books.json 本地映射，
身份绑定须数值交叉验证钉死）、ToS 不明、会话配方脆。

口径守则（沿 zucai.py 模式）：网络层薄（移动 UA + 三步预热 Referer 链）；
解析纯函数；append-only 入库（重复拉取幂等吸收）。采低频回溯式——每场
每日 1-2 拉取，连续拉取的并集自然积累完整路径（单页 30 行、翻页参数未
定，不依赖翻页）。robots/ToS 注记：个人研究低频使用。

行不带 fixture_id：mid↔fixture 身份绑定属消费侧工作（数值交叉验证后
才落键），本模块只攒语料。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from limits import RateLimitItemPerMinute
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.db import utc_now_iso
from goalx_backend.rate_limit import default_limiter, throttle

PARSE_VERSION = "srcb_change_v1"
# 用户圈定核心书小集（2026-09-20，17 pid；TARGET=竞彩官方在售时才有行）
CORE_PIDS: tuple[str, ...] = (
    "2",  # TARGET 竞彩官方
    "50",
    "879",
    "220",
    "206",  # SHARP
    "27",
    "14",
    "180",
    "49",
    "94",
    "245",  # GLOBAL RETAIL
    "406",
    "578",
    "250",
    "131",
    "84",  # ASIAN
    "24",  # AGGREGATE（全场共识对照，不进传导链）
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    + "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
# 滑动窗口加顶（票 57）：0.5s 间距≈120/min 持续，200/min 纯加顶零行为变化
_RATE_CEILING = RateLimitItemPerMinute(200)

_ROW_PATTERNS = (
    r"<tr>\s*<td>\s*<span[^>]*>([\d.]+)</span>\s*"
    r"<span[^>]*>([\d.]+)</span>\s*<span[^>]*>([\d.]+)</span>\s*</td>\s*"
    r'<td class="timetd[^"]*" time="([^"]+)">([^<]+)</td>'
)
_ROW_RE = re.compile("".join(_ROW_PATTERNS), re.S)
_MINUTES_TOKENS = re.compile(r"(\d+)天|(\d+)小时|(\d+)分钟?")


@dataclass(frozen=True)
class ChangeRow:
    """一条变化行（三向十进制 + 距开赛分钟 + 源页 time 标签）。"""

    odds_h: float
    odds_d: float
    odds_a: float
    minutes_before: int
    time_label: str


@dataclass
class SrcbCollectStats:
    """一次采集的统计。"""

    mids: list[str] = field(default_factory=list)
    pids: list[str] = field(default_factory=list)
    requests: int = 0
    rows_added: int = 0
    rows_absorbed: int = 0
    failed: dict[str, str] = field(default_factory=dict)


def minutes_before_from_label(label: str) -> int | None:
    """“赛前4小时51分” → 291（天/小时/分钟混合；无分钟语义返回 None）。"""
    if "赛前" not in label:
        return None
    total = 0
    for day, hour, minute in _MINUTES_TOKENS.findall(label.split("赛前", 1)[1]):
        if day:
            total += int(day) * 24 * 60
        if hour:
            total += int(hour) * 60
        if minute:
            total += int(minute)
    return total if total > 0 else None


def parse_change_rows(html: str) -> list[ChangeRow]:
    """变化页 HTML → 行列表（纯函数；非数据行/无分钟语义行跳过）。"""
    rows: list[ChangeRow] = []
    for h, d, a, time_label, minutes_label in _ROW_RE.findall(html):
        minutes = minutes_before_from_label(minutes_label)
        if minutes is None:
            continue
        rows.append(
            ChangeRow(
                odds_h=float(h),
                odds_d=float(d),
                odds_a=float(a),
                minutes_before=minutes,
                time_label=time_label,
            )
        )
    return rows


def warm_session(client: httpx.Client, settings: Settings) -> None:
    """三步预热第一步：移动首页取源站 cookie（后续页同 client 复用）。"""
    response = client.get(
        f"{settings.srcb_mobile_base}/", headers={"User-Agent": MOBILE_UA}, timeout=20.0
    )
    response.raise_for_status()


def fetch_change_page(
    client: httpx.Client, settings: Settings, mid: str, pid: str
) -> str:
    """一变化页（Referer=该场 odds 页；Type=odds 小写实测坑）。"""
    response = client.get(
        f"{settings.srcb_mobile_base}/match/change.php",
        params={"mid": mid, "pid": pid, "Type": "odds"},
        headers={
            "User-Agent": MOBILE_UA,
            "Referer": f"{settings.srcb_mobile_base}/match/odds.php?MatchID={mid}",
        },
        timeout=20.0,
    )
    response.raise_for_status()
    return response.content.decode("gb18030", errors="replace")


def insert_change_rows(
    conn: sqlite3.Connection,
    rows: list[ChangeRow],
    *,
    mid: str,
    pid: str,
    observed_at: str,
) -> tuple[int, int]:
    """
    变化行 append（UNIQUE 幂等）；返回 (added, absorbed)。

    行不可变：重复观测被吸收时 observed_at/first_seen_at 保持首见值
    （行身份即内容，无"最近看到"语义——回放确定性优先）。
    """
    added = absorbed = 0
    for row in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO srcb_change_rows
                (mid, pid, odds_h, odds_d, odds_a, minutes_before, time_label,
                 observed_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mid,
                pid,
                row.odds_h,
                row.odds_d,
                row.odds_a,
                row.minutes_before,
                row.time_label,
                observed_at,
                observed_at,
            ),
        )
        if cur.rowcount > 0:
            added += 1
        else:
            absorbed += 1
    return added, absorbed


def collect_srcb_changes(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    mids: list[str],
    pids: tuple[str, ...] = CORE_PIDS,
    now: datetime | None = None,
    sleep_seconds: float = 0.5,
) -> SrcbCollectStats:
    """
    回溯式采集：warm 一次 → 逐 (mid, pid) 拉变化页入库。

    单页失败计入 failed 不炸整跑；礼貌限速默认 0.5s/请求。run 行 append-only。
    """
    observed_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    stats = SrcbCollectStats(mids=list(mids), pids=list(pids))
    if not mids:
        _record_run(conn, stats, observed_at)
        return stats
    limiter = default_limiter()
    throttle(limiter, _RATE_CEILING)  # 预热首页也是线上请求，先过闸
    warm_session(client, settings)
    for mid in mids:
        for pid in pids:
            try:
                throttle(limiter, _RATE_CEILING)
                html = fetch_change_page(client, settings, mid, pid)
                rows = parse_change_rows(html)
            except httpx.HTTPError as exc:
                stats.failed[f"{mid}:{pid}"] = str(exc)[:120]
                logger.warning("srcb fetch failed {}:{} ({})", mid, pid, exc)
                continue
            stats.requests += 1
            added, absorbed = insert_change_rows(
                conn, rows, mid=mid, pid=pid, observed_at=observed_at
            )
            stats.rows_added += added
            stats.rows_absorbed += absorbed
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    conn.commit()
    _record_run(conn, stats, observed_at)
    return stats


def _record_run(
    conn: sqlite3.Connection, stats: SrcbCollectStats, observed_at: str
) -> None:
    cur = conn.execute(
        """
        INSERT INTO srcb_change_runs
            (observed_at, mids, pids, requests, rows_added, rows_absorbed,
             failed, parse_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_at,
            json.dumps(stats.mids),
            json.dumps(stats.pids),
            stats.requests,
            stats.rows_added,
            stats.rows_absorbed,
            json.dumps(stats.failed, ensure_ascii=False),
            PARSE_VERSION,
            utc_now_iso(),
        ),
    )
    conn.commit()
    if not cur.lastrowid:
        raise RuntimeError("srcb_change_runs INSERT 未产生 rowid")


def upcoming_pool_mids(
    conn: sqlite3.Connection, *, now_utc: str, ahead_hours: int = 36
) -> list[dict[str, Any]]:
    """未开赛彩池场次的源B mid（采集对象 v1；jczq 列表发现属后续）。"""
    # 上界在 Python 侧算（ISO 带时区格式与 kickoff_utc 串可比；sqlite
    # datetime() 输出空格分隔格式，串比较会错位）
    horizon = (
        datetime.fromisoformat(now_utc) + timedelta(hours=ahead_hours)
    ).isoformat(timespec="seconds")
    return [
        {
            "mid": str(row["source_match_id"]),
            "kickoff_utc": str(row["kickoff_utc"]),
            "home": str(row["home_team"]),
            "away": str(row["away_team"]),
        }
        for row in conn.execute(
            """
            SELECT pm.source_match_id, pm.kickoff_utc, pm.home_team, pm.away_team
            FROM pool_matches pm
            WHERE pm.source_match_id IS NOT NULL
              AND pm.kickoff_utc > ? AND pm.kickoff_utc <= ?
            ORDER BY pm.kickoff_utc
            """,
            (now_utc, horizon),
        ).fetchall()
    ]


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次采集（无历史返回 None）。"""
    return conn.execute(
        "SELECT * FROM srcb_change_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
