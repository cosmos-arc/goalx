"""
前瞻评分集合（票 34）：冻结的赛前 Forecast + 同期市场基准的 skill。

与回测严格分离（handoff：正式前瞻 skill 不读取「最新历史回测 run」）：

- 集合规则（固定，随本模块版本冻结）：对每场已结算（有 DrawResult）的
  竞彩场次，取 ``issued_at`` 严格早于 kickoff 的最新一条 Forecast
  （重复生成同内容已被内容哈希吸收；不同内容按时间冻结最后一条赛前版）；
- 市场基准：同一比较时点（= 该 Forecast 的 issued_at）之前最新观测的
  欧赔完整三向共识（Shin）。基准是评分对照，不是成交候选，不施加
  新鲜度窗；观测语义沿用 quote_evidence 的按源解释；
- 覆盖：包含没有下注的比赛；按 model_version（工件身份）分组，不混
  策略/模型版本；不按事后胜负或是否投注挑样本；
- 排除与分母全部计数：赛后才生成的 Forecast 只能算历史 replay、无基准
  的场次不进样本——都不静默丢弃。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from goalx_backend import odds_math as om
from goalx_backend.evaluation import evaluate_predictions
from goalx_backend.forecast import forecast_matrix_from_payload
from goalx_backend.markets import SELECTIONS
from goalx_backend.quote_evidence import effective_observed_at

FROZEN_RULE = "latest_forecast_before_kickoff_v1"
MIN_GROUP_SAMPLES = 30  # 小于该数标「样本不足」，不作通过依据


@dataclass(frozen=True)
class ForwardSample:
    """一个前瞻评分样本：一场比赛的冻结 Forecast + 同期市场基准 + 结果。"""

    fixture_id: int
    kickoff_utc: str
    model_version: str
    issued_at: str
    had_probs: dict[str, float]
    fair_probs: dict[str, float]
    ftr: str
    competition: str
    season: str


def market_baseline_asof(
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> dict[str, float] | None:
    """as_of 前最新欧赔完整三向共识的 Shin 概率（评分基准，非成交口径）。"""
    rows = conn.execute(
        """
        SELECT selection_code, source, odds, captured_at, observed_at
        FROM odds_snapshots
        WHERE fixture_id = ? AND market_code = 'had' AND source LIKE 'odds_api:%'
        ORDER BY captured_at, id
        """,
        (fixture_id,),
    ).fetchall()
    by_book: dict[str, dict[str, tuple[str, float]]] = {}
    for row in rows:
        observed = effective_observed_at(row)
        if observed is None or observed > as_of:
            continue
        book = str(row["source"])
        by_book.setdefault(book, {})[str(row["selection_code"])] = (
            str(row["captured_at"]),
            float(row["odds"]),
        )
    complete: dict[str, dict[str, float]] = {}
    for book, prices in by_book.items():
        if set(prices) >= set(SELECTIONS):
            complete[book] = {sel: prices[sel][1] for sel in SELECTIONS}
    if not complete:
        return None
    consensus = om.consensus_odds(
        [{b: complete[b][s] for b in complete} for s in SELECTIONS]
    )
    if consensus is None:
        return None
    probs = om.shin_implied(consensus)
    return dict(zip(SELECTIONS, probs, strict=True))


def _ftr(home_goals: int, away_goals: int) -> str:
    """比分 → H/D/A。"""
    if home_goals > away_goals:
        return "H"
    return "D" if home_goals == away_goals else "A"


def build_forward_samples(
    conn: sqlite3.Connection, *, track: str = "ml"
) -> tuple[list[ForwardSample], dict[str, int]]:
    """构建前瞻评分集合；返回 (样本, 排除/覆盖计数)。"""
    rows = conn.execute(
        """
        SELECT f.id AS fixture_id, f.kickoff_utc, c.name AS competition,
               d.home_goals, d.away_goals,
               fc.id AS forecast_id, fc.model_version, fc.issued_at, fc.payload
        FROM fixtures f
        JOIN match_codes mc ON mc.fixture_id = f.id AND mc.kind = 'jingcai'
        JOIN competitions c ON c.id = f.competition_id
        JOIN draw_results d ON d.fixture_id = f.id AND d.void = 0
        LEFT JOIN forecasts fc ON fc.fixture_id = f.id AND fc.track = ?
        ORDER BY f.kickoff_utc, fc.issued_at, fc.id
        """,
        (track,),
    ).fetchall()
    by_fixture: dict[int, dict[str, Any]] = {}
    counts = {
        "settled_fixtures": 0,
        "no_forecast": 0,
        "post_kickoff_only": 0,
        "no_market_baseline": 0,
        "scored": 0,
    }
    for row in rows:
        fixture_id = int(row["fixture_id"])
        entry = by_fixture.setdefault(
            fixture_id,
            {"row": row, "pre": [], "post": 0},
        )
        if row["forecast_id"] is None:
            continue
        if str(row["issued_at"]) < str(row["kickoff_utc"]):
            entry["pre"].append(row)
        else:
            entry["post"] += 1
    samples: list[ForwardSample] = []
    for fixture_id, entry in sorted(by_fixture.items()):
        row = entry["row"]
        counts["settled_fixtures"] += 1
        if entry["pre"]:
            frozen = entry["pre"][-1]  # 赛前最新一条（冻结规则）
        elif entry["post"]:
            counts["post_kickoff_only"] += 1  # 只有赛后预测 → 历史 replay
            continue
        else:
            counts["no_forecast"] += 1
            continue
        payload = json.loads(str(frozen["payload"]))
        had = payload.get("had_probs") or _had_from_matrix_payload(payload)
        baseline = market_baseline_asof(conn, fixture_id, str(frozen["issued_at"]))
        if had is None or baseline is None:
            counts["no_market_baseline"] += 1
            continue
        kickoff = str(row["kickoff_utc"])
        samples.append(
            ForwardSample(
                fixture_id=fixture_id,
                kickoff_utc=kickoff,
                model_version=str(frozen["model_version"]),
                issued_at=str(frozen["issued_at"]),
                had_probs={s: float(had[s]) for s in SELECTIONS},
                fair_probs=baseline,
                ftr=_ftr(int(row["home_goals"]), int(row["away_goals"])),
                competition=str(row["competition"]),
                season=kickoff[:4],
            )
        )
        counts["scored"] += 1
    return samples, counts


def _had_from_matrix_payload(payload: dict[str, Any]) -> dict[str, float] | None:
    """老 payload 无 had_probs 键时从矩阵重推（视图口径，不落库）。"""
    if "matrix" not in payload:
        return None
    return forecast_matrix_from_payload(payload).had()


def forward_skill_report(
    conn: sqlite3.Connection, *, track: str = "ml"
) -> dict[str, Any]:
    """
    前瞻 skill 分组报告：每个 model_version 一组，含排除/覆盖分母。

    组内指标沿用 evaluate_predictions（RPS/Brier/log loss 双侧 + skill +
    DM 探索性标注）；组样本 < MIN_GROUP_SAMPLES 标「样本不足」。
    """
    samples, counts = build_forward_samples(conn, track=track)
    groups: dict[str, list[ForwardSample]] = {}
    for sample in samples:
        groups.setdefault(sample.model_version, []).append(sample)
    report: dict[str, Any] = {
        "rule": FROZEN_RULE,
        "track": track,
        "coverage": counts,
        "groups": {},
    }
    for version, group in sorted(groups.items()):
        metrics = evaluate_predictions(
            [
                {
                    "had_probs": s.had_probs,
                    "fair_probs": s.fair_probs,
                    "ftr": s.ftr,
                    "competition": s.competition,
                    "season": s.season,
                }
                for s in group
            ]
        )
        metrics["insufficient_samples"] = len(group) < MIN_GROUP_SAMPLES
        metrics["n_fixtures"] = len({s.fixture_id for s in group})
        report["groups"][version] = metrics
    return report
