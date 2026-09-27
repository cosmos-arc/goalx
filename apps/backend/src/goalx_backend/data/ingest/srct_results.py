"""
源T 日页赛果物化（票 76）：赛果落事实主源切源T（官方 uniform 降审计+差集兜底）。

数据面零新请求：day_page raw-first 全联赛留档（票 59 起），silver
fixture_universe 的 home_goals/away_goals 即完场比分（一行=一日
CorpusScope 完场清单）。物化 = 待出场次 × 映射链（票 77
source_match_links）→ import_draw_results 落事实——与 uniform 同一
原子语义（更正校验+重算冲正，ADR-0001），库内已有结果不冲正。

与 uniform 的分工（同任务链序保证）：源T 先物化已覆盖场 → uniform
同步的 no-冲正规则天然把它限制在差集（CorpusScope 外联赛/映射未达场）
与官方 void 观测——审计源顺手救兜底，不需要 uniform 侧任何改动。

口径注记：
- 全场比分 only——日页无半场，half 置空不冲正（reconcile 半场比较仅在
  两侧都有值时生效的既有约定）；uniform 观测里的半场值只用于对账；
- published_at 不伪造：silver 行不带源发布时刻，bronze fetched_at 是
  本机抓取时间（ADR-0010 三时间，语义不合，不冒充 published）；
- void：日页取消/腰斩场不出现或无比分 → 不导入；官方 void 观测由
  uniform 兜底通道落（票 44 语义原样）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import duckdb

from goalx_backend.data import mapping
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.models import DrawResultInput

SOURCE = "srct"
_SID_CHUNK = 500  # IN 子句分块（待出面个位~百位级，防御性上限）

DuckCon = duckdb.DuckDBPyConnection


@dataclass
class SrctResultsStats:
    """一次物化的统计（degraded 非空 = 语料桥缺席，本次只让 uniform 独走）。"""

    pending: int = 0
    unmapped: int = 0
    imported: int = 0
    awaiting_universe: int = 0
    degraded: str | None = None

    @property
    def linked(self) -> int:
        """已链待出场数（pending − 差集）。"""
        return self.pending - self.unmapped

    def as_dict(self) -> dict[str, Any]:
        """统计 → 日志/CLI 字典。"""
        return {
            "pending": self.pending,
            "linked": self.linked,
            "unmapped": self.unmapped,
            "imported": self.imported,
            "awaiting_universe": self.awaiting_universe,
            "degraded": self.degraded,
        }


def _universe_goals_by_sid(
    duck_con: DuckCon, sids: list[str]
) -> dict[str, tuple[int, int] | None]:
    """Sid → 完场比分（无行或比分缺 → None）；duckdb.Error 上抛由调用方降级。"""
    out: dict[str, tuple[int, int] | None] = {}
    for start in range(0, len(sids), _SID_CHUNK):
        chunk = sids[start : start + _SID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        # placeholders 仅由 chunk 长度生成，无用户输入拼接
        sql = (
            "SELECT sid, home_goals, away_goals FROM fixture_universe"  # noqa: S608
            f" WHERE sid IN ({placeholders})"
        )
        rows = duck_con.execute(sql, chunk).fetchall()
        for sid, home_goals, away_goals in rows:
            if home_goals is None or away_goals is None:
                out[str(sid)] = None
            else:
                out[str(sid)] = (int(home_goals), int(away_goals))
    return out


def materialize_results(
    conn: sqlite3.Connection,
    duck_con: DuckCon | None,
    *,
    now: datetime | None = None,
) -> SrctResultsStats:
    """
    待出赛果 → 源T 日页比分落事实（幂等；库内已有结果不冲正）。

    待出窗口与 uniform.candidate_business_dates 同源（近 7 天已开赛无开奖）。
    语料桥缺席（silver 未建/库缺）降级零动作——调用链 uniform 独走，行为
    与票 44 完全一致。
    """
    stats = SrctResultsStats()
    now_iso = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    rows = mapping.pending_srct_results(conn, now_iso)
    stats.pending = len(rows)
    if duck_con is None:
        stats.degraded = "duck_con unavailable"
        return stats
    linked = [row for row in rows if row["sid"] is not None]
    stats.unmapped = stats.pending - len(linked)
    if not linked:
        return stats
    try:
        goals = _universe_goals_by_sid(duck_con, [str(row["sid"]) for row in linked])
    except duckdb.Error as exc:
        stats.degraded = str(exc)
        return stats
    inputs: list[DrawResultInput] = []
    for row in linked:
        score = goals.get(str(row["sid"]))
        if score is None:
            stats.awaiting_universe += 1
            continue
        inputs.append(
            DrawResultInput(
                fixture_id=int(row["fixture_id"]),
                home_goals=score[0],
                away_goals=score[1],
                source=SOURCE,
            )
        )
    if inputs:
        stats.imported = import_draw_results(conn, inputs)
    return stats


def coverage_diff_report(
    conn: sqlite3.Connection,
    duck_con: DuckCon | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    覆盖差集报告（票 76 第一验收项）：竞彩待出赛果 × 源T 日页覆盖，按联赛分桶。

    差集 = 无链场（CorpusScope 外联赛为主，uniform 兜底通道的口径）；
    awaiting = 已链但日页比分未到（silver 滞后/未赛完）。语料桥缺席时
    桶内 linked 维度诚实降为 unknown（不伪装已覆盖）。
    """
    now_iso = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    rows = mapping.pending_srct_results(conn, now_iso)
    goals: dict[str, tuple[int, int] | None] = {}
    degraded = duck_con is None
    if duck_con is not None:
        sids = [str(row["sid"]) for row in rows if row["sid"] is not None]
        try:
            goals = _universe_goals_by_sid(duck_con, sids)
        except duckdb.Error:
            degraded = True
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["competition_name"])
        bucket = buckets.setdefault(
            name,
            {
                "tier": str(row["competition_tier"]),
                "pending": 0,
                "covered": 0,
                "awaiting": 0,
                "diffset": 0,
            },
        )
        bucket["pending"] += 1
        sid = row["sid"]
        if sid is None:
            bucket["diffset"] += 1
        elif degraded or goals.get(str(sid)) is None:
            bucket["awaiting"] += 1
        else:
            bucket["covered"] += 1
    total = len(rows)
    return {
        "pending": total,
        "universe_degraded": degraded,
        "srct_covered": sum(b["covered"] for b in buckets.values()),
        "diffset_unmapped": sum(b["diffset"] for b in buckets.values()),
        "per_competition": dict(sorted(buckets.items())),
    }
