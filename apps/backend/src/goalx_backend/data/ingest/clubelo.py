"""
clubelo Elo 评级层采集（票 74；research/27 缺口矩阵 C 区 P0）。

免费无钥 CSV API（http://api.clubelo.com）：
- ``GET /{YYYY-MM-DD}`` = 该日全部俱乐部评级快照（单请求，日拍）；
- ``GET /{ClubName}`` = 该队全历史区间行（回填，当日快照取清单后逐队）。

行粒度 = clubelo 原生区间：一行 = 一队一段连续同 Elo 的
[valid_from, valid_to]；日拍行 From=区间起点，重见同 (club, valid_from)
即 upsert 延长 valid_to——点时查询与逐日快照等价且行数压缩。

范围：不过滤国家/联赛（全量约 600 队一次拿全，CorpusScope 15 联赛自然
覆盖）；club 为源侧英文名，竞彩中文队名映射走消费端 team_aliases
（本票只落原始评级面）。

探针注记（2026-09-26）：api.clubelo.com 本机代理 502 + 旁路超时，疑站点
自身故障——回填实跑待站点恢复（幂等可断点重跑，不阻塞合码）。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date

import httpx
from loguru import logger

BASE_URL = "http://api.clubelo.com"
REQUEST_GAP_SECONDS = 0.3  # 回填逐队礼貌限速（~600 请求 ≈ 3 分钟）
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    )
}


@dataclass
class EloSyncStats:
    """一次同步的统计（日拍/回填共用）。"""

    snapshot_rows: int = 0
    written: int = 0
    clubs: list[str] = field(default_factory=list)  # 回填清单（快照侧为空）
    failed_clubs: list[str] = field(default_factory=list)
    history_rows: int = 0  # 回填侧区间行累计


def parse_ratings(text: str) -> list[dict[str, object]]:
    """
    Clubelo CSV → 行字典（Club,Country,Level,Elo,From,To）。

    空行/缺 Club/缺 Elo/缺 From 跳过；To 空容忍为 None。
    """
    rows: list[dict[str, object]] = []
    for rec in csv.DictReader(io.StringIO(text)):
        club = (rec.get("Club") or "").strip()
        from_date = (rec.get("From") or "").strip()
        try:
            elo = float(str(rec.get("Elo")).strip())
        except (TypeError, ValueError):
            continue
        if not club or not from_date:
            continue
        level_raw = (rec.get("Level") or "").strip()
        try:
            level = int(level_raw) if level_raw else None
        except ValueError:
            level = None
        to_date = (rec.get("To") or "").strip()
        rows.append(
            {
                "club": club,
                "country": (rec.get("Country") or "").strip(),
                "level": level,
                "elo": elo,
                "valid_from": from_date,
                "valid_to": to_date or None,
            }
        )
    return rows


def upsert_ratings(conn: sqlite3.Connection, rows: list[dict[str, object]]) -> int:
    """幂等 upsert：同 (club, valid_from) 冲突即更新评级与区间右端。"""
    count = 0
    for row in rows:
        cur = conn.execute(
            """
            INSERT INTO elo_ratings (club, country, level, elo, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (club, valid_from) DO UPDATE SET
                country=excluded.country, level=excluded.level,
                elo=excluded.elo, valid_to=excluded.valid_to
            """,
            (
                row["club"],
                row["country"],
                row["level"],
                row["elo"],
                row["valid_from"],
                row["valid_to"],
            ),
        )
        if cur.rowcount > 0:
            count += 1
    return count


def _fetch_text(client: httpx.Client, url: str) -> str:
    response = client.get(url, timeout=30.0, headers=_HEADERS)
    response.raise_for_status()
    return response.text


def sync_snapshot(
    conn: sqlite3.Connection, client: httpx.Client, day: date
) -> EloSyncStats:
    """日拍：单请求取当日全量快照并 upsert。"""
    stats = EloSyncStats()
    rows = parse_ratings(_fetch_text(client, f"{BASE_URL}/{day.isoformat()}"))
    stats.snapshot_rows = len(rows)
    stats.written = upsert_ratings(conn, rows)
    conn.commit()
    return stats


def backfill_history(
    conn: sqlite3.Connection, client: httpx.Client, day: date
) -> EloSyncStats:
    """
    回填：当日快照取俱乐部清单 → 逐队全历史区间（幂等可断点重跑）。

    单队拉取失败计数跳过不中断（fdhist failed_files 同型）。
    """
    stats = sync_snapshot(conn, client, day)  # 顺带落当日快照
    clubs = [
        str(row["club"])
        for row in conn.execute(
            "SELECT DISTINCT club FROM elo_ratings ORDER BY club"
        ).fetchall()
    ]
    stats.clubs = clubs
    for index, club in enumerate(clubs):
        try:
            rows = parse_ratings(_fetch_text(client, f"{BASE_URL}/{club}"))
        except httpx.HTTPError as exc:
            stats.failed_clubs.append(club)
            logger.warning("clubelo backfill failed, skipped: {} ({})", club, exc)
            continue
        stats.history_rows += len(rows)
        stats.written += upsert_ratings(conn, rows)
        if index < len(clubs) - 1:
            time.sleep(REQUEST_GAP_SECONDS)
    conn.commit()
    return stats


def stats_dict(stats: EloSyncStats) -> dict[str, object]:
    """统计 → 日志/CLI 友好字典（clubs 全清单不回显，只给计数）。"""
    return {
        "snapshot_rows": stats.snapshot_rows,
        "written": stats.written,
        "clubs": len(stats.clubs),
        "failed_clubs": stats.failed_clubs,
        "history_rows": stats.history_rows,
    }
