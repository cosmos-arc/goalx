"""
football-data.co.uk 历史导入（票 21）：五大 2023-26 三季回测底座。

列口径（ADR 0007）：FTR 赛果；PSC*（Pinnacle 收盘）为公允基准，
AvgC*（市场均值收盘）兜底。日期格式 dd/mm/YY 或 dd/mm/YYYY，utf-8-sig。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx

from goalx_backend.config import Settings
from goalx_backend.store import results as rs_store

# 五大联赛 fd 代码（票 12 范围）
FD_COMPETITIONS: tuple[str, ...] = ("E0", "D1", "SP1", "I1", "F1")
SEASONS: tuple[str, ...] = ("2324", "2425", "2526")


@dataclass
class HistImportStats:
    """一次历史导入的统计。"""

    rows: int = 0
    written: int = 0
    skipped: int = 0


def parse_csv(
    text: str, competition: str, season: str
) -> tuple[list[dict[str, object]], int]:
    """
    解析一份 fd CSV → 行字典列表；返回 (rows, skipped)。

    清洗规则：utf-8-sig 去头；缺比分/赛果的行跳过；PSC/AvgC 空值转 None。
    """
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, object]] = []
    skipped = 0
    for rec in reader:
        fthg = _parse_int(rec.get("FTHG"))
        ftag = _parse_int(rec.get("FTAG"))
        ftr = (rec.get("FTR") or "").strip().upper()
        date = _parse_date(rec.get("Date") or "")
        home = (rec.get("HomeTeam") or "").strip()
        away = (rec.get("AwayTeam") or "").strip()
        if fthg is None or ftag is None or ftr not in ("H", "D", "A") or not date:
            skipped += 1
            continue
        if not home or not away:
            skipped += 1
            continue
        rows.append(
            {
                "competition": competition,
                "season": season,
                "match_date": date,
                "home_team": home,
                "away_team": away,
                "fthg": fthg,
                "ftag": ftag,
                "ftr": ftr,
                "psc_home": _parse_float(rec.get("PSCH")),
                "psc_draw": _parse_float(rec.get("PSCD")),
                "psc_away": _parse_float(rec.get("PSCA")),
                "avgc_home": _parse_float(rec.get("AvgCH")),
                "avgc_draw": _parse_float(rec.get("AvgCD")),
                "avgc_away": _parse_float(rec.get("AvgCA")),
            }
        )
    return rows, skipped


def _parse_int(value: str | None) -> int | None:
    """解析整数列，空/无效返回 None。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_float(value: str | None) -> float | None:
    """解析浮点列，空/无效返回 None。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_date(value: str) -> str:
    """Fd 日期（dd/mm/YY 或 dd/mm/YYYY）→ ISO 日期。"""
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def import_history(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    competitions: tuple[str, ...] = FD_COMPETITIONS,
    seasons: tuple[str, ...] = SEASONS,
    fetch: Callable[[str], str] | None = None,
) -> HistImportStats:
    """批量拉取并 upsert 历史 CSV（幂等可重跑，票 21 验收）。"""
    stats = HistImportStats()
    for season in seasons:
        for competition in competitions:
            url = f"{settings.fd_base_url}/{season}/{competition}.csv"
            if fetch is not None:
                text = fetch(url)
            else:
                response = client.get(url, timeout=30.0, follow_redirects=True)
                response.raise_for_status()
                text = response.content.decode("utf-8-sig", errors="replace")
            rows, skipped = parse_csv(text, competition, season)
            stats.rows += len(rows)
            stats.skipped += skipped
            stats.written += rs_store.upsert_hist_matches(conn, rows)
    conn.commit()
    return stats
