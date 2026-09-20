"""
xG 融合实证对比 runner（票 45）。

对比变体：goal-DC 基准 vs xG 版 DC vs npxG 收缩校准 vs understat 自带
forecast（对标基准族，票面范围 3）。walk-forward 按比赛周重估（M2 用户
裁决口径同源）：每周 cutoff = 该周最早开球时刻，训练集 = 前置赛季全量 +
本季 cutoff 前已完场（严格 <，防前视红线由数据集构造执行）。指标 RPS 主
+ log-loss 辅（票 29 口径），按周桶（1-4/5-8/9+）分列——早季节值假设的
增益应集中在 wk1_4。

语料自足：训练与评分都在 understat 命名域（比分与 npxG 同源），不依赖
fdhist join。融合终选（收缩 vs 概率加权 vs 不用）待人裁决——本 runner
只出对比数据，不改变线上 Forecast 生成（票 34 冻结边界不动）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from loguru import logger

from goalx_backend.data.ingest import understat
from goalx_backend.evaluation import metrics as ev
from goalx_backend.modelling.dc_model import DCArtifact, TrainingRow, fit_dc_model
from goalx_backend.modelling.xg_dc import XGRow, fit_xg_dc, shrink_dc_params

GOAL_DC = "goal_dc"
XG_DC = "xg_dc"
BLEND = "blend_goal_xg"  # 概率加权臂：goal×xG had 概率线性池 50/50
FORECAST = "understat_forecast"
# 早赛季假设的周桶边界（第 1..4 周 / 5..8 / 9+）
BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("wk1_4", 1, 4),
    ("wk5_8", 5, 8),
    ("wk9p", 9, 10**6),
)


@dataclass(frozen=True)
class CompareMatch:
    """一行对比语料（understat 命名域；home/away 为源球队 id）。"""

    match_id: str
    season: str
    datetime_utc: str
    home: str
    away: str
    goals_home: int | None
    goals_away: int | None
    npxg_home: float | None
    npxg_away: float | None
    forecast: tuple[float, float, float] | None


@dataclass(frozen=True)
class Record:
    """一场 × 一变体的评分记录（match_id 供与基准配对）。"""

    match_id: str
    league: str
    bucket: str
    variant: str
    rps: float
    log_loss: float


def load_compare_matches(
    conn: sqlite3.Connection, league: str, seasons: tuple[str, ...]
) -> list[CompareMatch]:
    """understat_matches → 对比语料（SQL 归 data/ingest/understat.compare_rows）。"""
    matches: list[CompareMatch] = []
    for row in understat.compare_rows(conn, league, seasons):
        raw = (row["forecast_w"], row["forecast_d"], row["forecast_l"])
        forecast = raw if all(v is not None for v in raw) else None
        matches.append(
            CompareMatch(
                match_id=str(row["match_id"]),
                season=str(row["season"]),
                datetime_utc=str(row["datetime_utc"]),
                home=str(row["home_team_id"]),
                away=str(row["away_team_id"]),
                goals_home=row["goals_home"],
                goals_away=row["goals_away"],
                npxg_home=row["npxg_home"],
                npxg_away=row["npxg_away"],
                forecast=forecast,
            )
        )
    return matches


def _week_key(match: CompareMatch) -> tuple[int, int]:
    return date.fromisoformat(match.datetime_utc[:10]).isocalendar()[:2]


def _bucket(week_ordinal: int) -> str:
    for name, low, high in BUCKETS:
        if low <= week_ordinal <= high:
            return name
    return BUCKETS[-1][0]


def _outcome_index(goals_home: int, goals_away: int) -> int:
    if goals_home > goals_away:
        return 0
    if goals_home < goals_away:
        return 2
    return 1


@dataclass(frozen=True)
class _HadOnly:
    """Blend 的预测载体：只有 had 概率（矩阵不合成——对比只评三向）。"""

    probs: dict[str, float]

    def had(self) -> dict[str, float]:
        return self.probs


@dataclass(frozen=True)
class BlendModel:
    """概率加权（票面第三臂）：两工件 had 概率的线性池（各 50%）。"""

    goal: DCArtifact
    xg: DCArtifact

    def predict(self, home: str, away: str) -> _HadOnly:
        """两侧 had 概率线性平均（和为 1，无需再归一）。"""
        goal = self.goal.predict(home, away).had()
        xg = self.xg.predict(home, away).had()
        return _HadOnly({k: (goal[k] + xg[k]) / 2.0 for k in ("h", "d", "a")})


def _had_probs(
    artifact: DCArtifact | BlendModel | None, home: str, away: str
) -> dict[str, float] | None:
    if artifact is None:
        return None
    try:
        return artifact.predict(home, away).had()
    except (KeyError, ValueError):
        return None


def _score(probs: dict[str, float] | None, outcome: int) -> tuple[float, float] | None:
    if probs is None:
        return None
    ordered = [probs["h"], probs["d"], probs["a"]]
    return ev.rps(ordered, outcome), ev.log_loss(ordered, outcome)


def _group_weeks(target_matches: list[CompareMatch]) -> list[list[CompareMatch]]:
    """按 ISO 周分组（语料已按开球时刻排序，相邻同周归一组）。"""
    weeks: list[list[CompareMatch]] = []
    for match in target_matches:
        if weeks and _week_key(weeks[-1][0]) == _week_key(match):
            weeks[-1].append(match)
        else:
            weeks.append([match])
    return weeks


def _fit_variants(
    league: str,
    season: str,
    prior: list[CompareMatch],
    cutoff_day: date,
    *,
    half_life_days: float,
    shrink_k: tuple[float, ...],
    skipped: dict[str, int],
) -> dict[str, DCArtifact | BlendModel | None]:
    """周 cutoff 处的训练与变体构造（goal 基准 + xG 版 + 概率加权 + 收缩族）。"""
    goal_rows = [
        TrainingRow(
            match_date=m.datetime_utc[:10],
            home_team=m.home,
            away_team=m.away,
            fthg=int(m.goals_home),
            ftag=int(m.goals_away),
        )
        for m in prior
        if m.goals_home is not None and m.goals_away is not None
    ]
    xg_rows = [
        XGRow(
            match_date=m.datetime_utc[:10],
            home_team=m.home,
            away_team=m.away,
            xg_home=float(m.npxg_home),
            xg_away=float(m.npxg_away),
        )
        for m in prior
        if m.npxg_home is not None and m.npxg_away is not None
    ]
    artifacts: dict[str, DCArtifact | BlendModel | None] = {}
    for name, fit in (
        (
            GOAL_DC,
            lambda: fit_dc_model(
                goal_rows,
                competition=f"{league}-{season}",
                half_life_days=half_life_days,
                as_of=cutoff_day,
            ),
        ),
        (
            XG_DC,
            lambda: fit_xg_dc(
                xg_rows,
                competition=f"{league}-{season}",
                half_life_days=half_life_days,
                as_of=cutoff_day,
            ),
        ),
    ):
        try:
            artifacts[name] = fit()
        except ValueError:
            skipped[f"fit_{name}"] = skipped.get(f"fit_{name}", 0) + 1
            artifacts[name] = None
    goal_artifact = artifacts[GOAL_DC]
    xg_artifact = artifacts[XG_DC]
    if isinstance(goal_artifact, DCArtifact) and isinstance(xg_artifact, DCArtifact):
        artifacts[BLEND] = BlendModel(goal=goal_artifact, xg=xg_artifact)
        cumulative = _season_npxg_cumulative(prior, season)
        for k in shrink_k:
            artifacts[f"shrink_k{k:g}"] = shrink_dc_params(
                goal_artifact, cumulative, k=k
            )
    return artifacts


def _season_npxg_cumulative(
    prior: list[CompareMatch], season: str
) -> dict[str, tuple[float, float, int]]:
    """本季 prior 内各队累计 (npxG, npxGA, 场数)——收缩观测，同源防前视。"""
    cumulative: dict[str, tuple[float, float, int]] = {}
    for m in prior:
        if m.season != season or m.npxg_home is None or m.npxg_away is None:
            continue
        for team, attack, concede in (
            (m.home, float(m.npxg_home), float(m.npxg_away)),
            (m.away, float(m.npxg_away), float(m.npxg_home)),
        ):
            npxg, npxga, played = cumulative.get(team, (0.0, 0.0, 0))
            cumulative[team] = (npxg + attack, npxga + concede, played + 1)
    return cumulative


def _score_week(
    league: str,
    week_matches: list[CompareMatch],
    week_ordinal: int,
    artifacts: dict[str, DCArtifact | BlendModel | None],
    skipped: dict[str, int],
) -> list[Record]:
    """评一周：逐场逐变体（+understat forecast）；未完场不评分。"""
    records: list[Record] = []
    bucket = _bucket(week_ordinal)
    for match in week_matches:
        if match.goals_home is None or match.goals_away is None:
            continue
        outcome = _outcome_index(match.goals_home, match.goals_away)
        for variant, artifact in artifacts.items():
            rps_ll = _score(_had_probs(artifact, match.home, match.away), outcome)
            if rps_ll is None:
                skipped[f"no_team_{variant}"] = skipped.get(f"no_team_{variant}", 0) + 1
                continue
            records.append(
                Record(
                    match_id=match.match_id,
                    league=league,
                    bucket=bucket,
                    variant=variant,
                    rps=rps_ll[0],
                    log_loss=rps_ll[1],
                )
            )
        if match.forecast is not None:
            rps_ll = _score(
                dict(zip(("h", "d", "a"), match.forecast, strict=True)), outcome
            )
            if rps_ll is not None:
                records.append(
                    Record(
                        match_id=match.match_id,
                        league=league,
                        bucket=bucket,
                        variant=FORECAST,
                        rps=rps_ll[0],
                        log_loss=rps_ll[1],
                    )
                )
    return records


def _weekly_walk_forward(
    league: str,
    target_matches: list[CompareMatch],
    corpus: list[CompareMatch],
    *,
    half_life_days: float,
    shrink_k: tuple[float, ...],
    skipped: dict[str, int],
) -> list[Record]:
    """
    一个联赛一个目标赛季的 walk-forward：按 ISO 周分组重估。

    corpus = 该联赛全部已载入场次（跨季）；每周训练集 = corpus 中
    datetime 严格早于该周最早开球者——本季已完场增量自然包含（升班马
    首战后才可训练），防前视由 cutoff 过滤唯一执行。
    """
    records: list[Record] = []
    season = target_matches[0].season
    for week_ordinal, week_matches in enumerate(_group_weeks(target_matches), start=1):
        cutoff = min(m.datetime_utc for m in week_matches)
        prior = [m for m in corpus if m.datetime_utc < cutoff]
        artifacts = _fit_variants(
            league,
            season,
            prior,
            date.fromisoformat(cutoff[:10]),
            half_life_days=half_life_days,
            shrink_k=shrink_k,
            skipped=skipped,
        )
        records.extend(
            _score_week(league, week_matches, week_ordinal, artifacts, skipped)
        )
    return records


def _summarize(
    records: list[Record], *, baseline: str = GOAL_DC
) -> dict[str, dict[str, Any]]:
    """逐变体：均值指标 + 对基准的配对差（负 = 优于基准）。"""
    variants = sorted({r.variant for r in records})
    summary: dict[str, dict[str, Any]] = {}
    base_by_match = {r.match_id: r.rps for r in records if r.variant == baseline}
    for variant in variants:
        rows = [r for r in records if r.variant == variant]
        entry: dict[str, Any] = {
            "n": len(rows),
            "rps": sum(r.rps for r in rows) / len(rows),
            "log_loss": sum(r.log_loss for r in rows) / len(rows),
        }
        if variant != baseline:
            paired = [
                r.rps - base_by_match[r.match_id]
                for r in rows
                if r.match_id in base_by_match
            ]
            if paired:
                entry["rps_delta_vs_goal_dc"] = sum(paired) / len(paired)
                entry["paired_n"] = len(paired)
        summary[variant] = entry
    return summary


def run_xg_comparison(
    conn: sqlite3.Connection,
    *,
    leagues: tuple[str, ...] = understat.DEFAULT_LEAGUES,
    seasons: tuple[str, ...],
    train_seasons: int = 3,
    half_life_days: float = 365.0,
    shrink_k: tuple[float, ...] = (6.0,),
) -> dict[str, Any]:
    """
    实证对比入口：语料在 understat_matches（先 understat-sync/backfill）。

    seasons 为目标赛季（评分对象）；训练窗 = 各目标赛季前 train_seasons 季
    + 本季 walk-forward 增量。返回 overall + 周桶双层摘要与跳过计数。
    """
    first_target = min(int(s) for s in seasons)
    # 训练窗 = 最早目标季前 train_seasons 季 → 最晚目标季，整段载入
    load_seasons = tuple(
        str(y)
        for y in range(first_target - train_seasons, max(int(s) for s in seasons) + 1)
    )
    records: list[Record] = []
    skipped: dict[str, int] = {}
    for league in leagues:
        matches = load_compare_matches(conn, league, load_seasons)
        if not matches:
            skipped[f"no_data_{league}"] = 1
            continue
        for season in seasons:
            target = [m for m in matches if m.season == season]
            if not target:
                skipped[f"no_data_{league}_{season}"] = 1
                continue
            # 训练池=全部场次：本季 walk-forward 增量由 prior 的
            # datetime < cutoff 过滤保证（含收缩观测的本季累计，同源防前视）
            season_records = _weekly_walk_forward(
                league,
                target,
                matches,
                half_life_days=half_life_days,
                shrink_k=shrink_k,
                skipped=skipped,
            )
            logger.info(
                "xg-compare {} {}: {} 条评分记录",
                league,
                season,
                len(season_records),
            )
            records.extend(season_records)
    by_bucket: dict[str, dict[str, dict[str, Any]]] = {}
    for name, _, _ in BUCKETS:
        bucket_records = [r for r in records if r.bucket == name]
        if bucket_records:
            by_bucket[name] = _summarize(bucket_records)
    return {
        "params": {
            "leagues": list(leagues),
            "seasons": list(seasons),
            "train_seasons": train_seasons,
            "half_life_days": half_life_days,
            "shrink_k": list(shrink_k),
            "bucket_edges": [name for name, _, _ in BUCKETS],
        },
        "overall": _summarize(records),
        "by_bucket": by_bucket,
        "skipped": skipped,
    }
