"""
football-data.co.uk 历史导入（票 21 起底座；票 46 扩联赛 + 十年窗）。

列口径（ADR 0007）：FTR 赛果；PSC*（Pinnacle 收盘）为公允基准，
AvgC*（市场均值收盘）兜底；PSH*（Pinnacle 早期）开→收
漂移镜（票 46 增补裁决，v19 落列；实测 1617 起文件即含该组列）。
AvgC 自 1920 季起才有列——1617-1819 三季仅 PSC/PSH（票 51 报告按此
标注覆盖）。日期格式 dd/mm/YY 或 dd/mm/YYYY，utf-8-sig。

覆盖实测（2026-09-20，票 46）：五大+N1 全季可得；扩联赛 E1 英冠/P1 葡超/
T1 土超/B1 比甲/SC0 苏超均含 PSC 收盘列（2526 季 200 实测）。美职/巴甲/
墨超/日职不在 fd.co.uk 标准季目录（404）——竞彩混编的美洲/日职场次收盘
基准仍缺口（结构限制，语料用途记 docs/票 46 Answer）。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import httpx
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store

# 五大 + N1 荷甲（票 26 模型线）+ 票 46 扩联赛（竞彩混编频率排序：
# 英冠/葡超/土超/比甲/苏超——清单待人追认，增删只动本元组）
FD_COMPETITIONS: tuple[str, ...] = (
    "E0",
    "D1",
    "SP1",
    "I1",
    "F1",
    "N1",
    "E1",
    "P1",
    "T1",
    "B1",
    "SC0",
)
# 十年窗（票 46）：2016/17 起 + 当季；早期季新联赛可能缺文件（404 跳过
# 计数，failed_files 可查）。每季初维护惯例不变：新赛季加一年
SEASONS: tuple[str, ...] = (
    "1617",
    "1718",
    "1819",
    "1920",
    "2021",
    "2122",
    "2223",
    "2324",
    "2425",
    "2526",
    "2627",
)


@dataclass
class HistImportStats:
    """一次历史导入的统计。"""

    rows: int = 0
    written: int = 0
    skipped: int = 0
    failed_files: int = 0


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
                "psh_home": _parse_float(rec.get("PSH")),
                "psh_draw": _parse_float(rec.get("PSD")),
                "psh_away": _parse_float(rec.get("PSA")),
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
    """
    批量拉取并 upsert 历史 CSV（幂等可重跑，票 21 验收）。

    单文件拉取失败（如新赛季文件未发布 404）跳过并计数，不中断其余文件；
    失败文件下次重跑幂等补上。
    """
    stats = HistImportStats()
    for season in seasons:
        for competition in competitions:
            url = f"{settings.fd_base_url}/{season}/{competition}.csv"
            try:
                if fetch is not None:
                    text = fetch(url)
                else:
                    response = client.get(url, timeout=30.0, follow_redirects=True)
                    response.raise_for_status()
                    text = response.content.decode("utf-8-sig", errors="replace")
            except httpx.HTTPError as exc:
                stats.failed_files += 1
                logger.warning("fdhist fetch failed, skipped: {} ({})", url, exc)
                continue
            rows, skipped = parse_csv(text, competition, season)
            stats.rows += len(rows)
            stats.skipped += skipped
            stats.written += rs_store.upsert_hist_matches(conn, rows)
            time.sleep(0.5)  # 对源站礼貌限速
    conn.commit()
    return stats


def hist_team_names(conn: sqlite3.Connection, competition: str) -> list[str]:
    """某 fd 联赛在库的全部队名（别名匹配候选集）。"""
    return [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT home_team FROM hist_matches WHERE competition = ?
            UNION
            SELECT DISTINCT away_team FROM hist_matches WHERE competition = ?
            """,
            (competition, competition),
        )
    ]


def recent_team_matches(
    conn: sqlite3.Connection, competition: str, team: str, *, limit: int = 6
) -> list[sqlite3.Row]:
    """某队最近 N 场已赛（当前赛季优先，跨季自然衔接）。"""
    return conn.execute(
        """
        SELECT season, match_date, home_team, away_team, fthg, ftag, ftr
        FROM hist_matches
        WHERE competition = ? AND (home_team = ? OR away_team = ?)
        ORDER BY match_date DESC LIMIT ?
        """,
        (competition, team, team, limit),
    ).fetchall()


def h2h_matches(
    conn: sqlite3.Connection, competition: str, home: str, away: str, *, limit: int = 6
) -> list[sqlite3.Row]:
    """两队最近 N 次同联赛交锋。"""
    return conn.execute(
        """
        SELECT season, match_date, home_team, away_team, fthg, ftag, ftr
        FROM hist_matches
        WHERE competition = ?
          AND ((home_team = ? AND away_team = ?)
               OR (home_team = ? AND away_team = ?))
        ORDER BY match_date DESC LIMIT ?
        """,
        (competition, home, away, away, home, limit),
    ).fetchall()
