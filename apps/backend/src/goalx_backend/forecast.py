"""
Forecast 生成与内容哈希存证（票 27，ADR 0001/0006）。

对在售竞彩场次（经票 25 对齐映射到训练域球队）用最新 DC 工件生成 10×10
矩阵 Forecast（track=ml）：

- append-only 落库 + 内容哈希存证：同 payload 同哈希，重复生成被 UNIQUE
  吸收（幂等）；
- bootstrap CI：训练期重采样工件（票 26）逐个重算 λ → 矩阵 → had 概率，
  百分位区间随 payload 落库（样本数=工件数，alpha 参数化）；
- 玩法概率推导视图即时计算（视图态不落库，ADR 0006）；
- 无映射/无模型场次进跳过清单（明确可查）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from goalx_backend.dc_model import TrainingRun, load_latest_run
from goalx_backend.score_matrix import ScoreMatrix
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import forecasts as fc_store
from goalx_backend.team_align import match_model_team

# 竞彩联赛名（competitions.name）→ fd 历史底座代码（票 26 五大主动）
FD_LEAGUE_MAP: dict[str, str] = {
    "英超": "E0",
    "西甲": "SP1",
    "意甲": "I1",
    "德甲": "D1",
    "法甲": "F1",
    # Tier2 顺手（票 26 可选项；导入 N1 历史后自动生效）
    "荷甲": "N1",
}
CI_ALPHA = 0.1  # had CI 双侧分位（0.1 → 80% 区间）
_MIN_BOOTSTRAP_SAMPLES = 2  # 少于该数不落 CI


@dataclass
class ForecastStats:
    """一次 Forecast 生成的统计与跳过清单。"""

    generated: int = 0
    duplicates: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)


def content_hash(payload: dict[str, Any]) -> str:
    """规范 JSON 序列化的 sha256（同 payload 恒同哈希，票 27 验收）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _team_aliases(conn: sqlite3.Connection, team_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT alias FROM team_aliases WHERE team_id = ? ORDER BY id",
        (team_id,),
    ).fetchall()
    return [str(row["alias"]) for row in rows]


def bootstrap_distribution(
    run: TrainingRun, home: str, away: str
) -> tuple[list[list[float]], list[dict[str, float]]]:
    """
    单次遍历 bootstrap 工件：返回 (λ 样本列表, had 概率样本列表)。

    重采样工件缺该队或 ρ 数值越界（小样本常见）的样本不计入。
    """
    bootstrap_lambda: list[list[float]] = []
    had_samples: list[dict[str, float]] = []
    for boot in run.bootstrap:
        try:
            lambdas = boot.lambdas(home, away)
            had = boot.predict(home, away).had()
        except (KeyError, ValueError):
            continue
        bootstrap_lambda.append(list(lambdas))
        had_samples.append(had)
    return bootstrap_lambda, had_samples


def had_ci_from_samples(
    samples: list[dict[str, float]], *, alpha: float = CI_ALPHA
) -> dict[str, list[float]] | None:
    """Bootstrap had 概率区间（百分位法）；样本不足返回 None。"""
    if len(samples) < _MIN_BOOTSTRAP_SAMPLES:
        return None

    def _bounds(selection: str) -> list[float]:
        values = sorted(s[selection] for s in samples)
        low = values[max(0, int(len(values) * alpha / 2) - 1)]
        high = values[min(len(values) - 1, int(len(values) * (1 - alpha / 2)))]
        return [low, high]

    return {sel: _bounds(sel) for sel in ("h", "d", "a")}


def build_forecast_payload(
    run: TrainingRun,
    *,
    fd_competition: str,
    home_model_team: str,
    away_model_team: str,
) -> dict[str, Any]:
    """从训练工件构造 Forecast payload（矩阵 + λ + bootstrap CI 材料）。"""
    lam_home, lam_away = run.base.lambdas(home_model_team, away_model_team)
    matrix = run.base.predict(home_model_team, away_model_team)
    bootstrap_lambda, had_samples = bootstrap_distribution(
        run, home_model_team, away_model_team
    )
    had_ci = had_ci_from_samples(had_samples)
    return {
        "fd_competition": fd_competition,
        "model_fingerprint": run.base.data_fingerprint,
        "artifact_id": run.artifact_id(),
        "model_as_of": run.base.as_of,
        "half_life_days": run.base.half_life_days,
        "home_team": home_model_team,
        "away_team": away_model_team,
        "lambda_home": lam_home,
        "lambda_away": lam_away,
        "rho": run.base.rho,
        "matrix": [list(row) for row in matrix.grid],
        "bootstrap_lambda": bootstrap_lambda,
        # 名义水平 + 有效样本数（票 34：覆盖未验收，不得称校准成功）
        "had_ci": had_ci,
        "ci_nominal_level": 1.0 - CI_ALPHA if had_ci is not None else None,
        "ci_effective_samples": len(had_samples),
    }


def generate_forecasts(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    models_dir: str,
) -> ForecastStats:
    """
    对一个业务日的全部竞彩场次生成 ML Forecast（幂等）。

    跳过原因：not_fd_league（无 fd 历史映射）、no_model（无训练工件）、
    no_mapping（队名对不上训练域）、no_odds（无模型可用赔率以外的输入缺失
    不在此列）。
    """
    stats = ForecastStats()
    run_cache: dict[str, TrainingRun | None] = {}

    def skip(fixture: sqlite3.Row, reason: str, detail: str | None = None) -> None:
        entry: dict[str, str] = {
            "fixture_id": str(fixture["id"]),
            "match_code": str(fixture["match_code"]),
            "fixture": f"{fixture['home_team']} vs {fixture['away_team']}",
            "reason": reason,
        }
        if detail is not None:
            entry["detail"] = detail
        stats.skipped.append(entry)

    for fixture in fx_store.fixtures_for_business_date(conn, business_date):
        fixture_id = int(fixture["id"])
        fd_competition = FD_LEAGUE_MAP.get(str(fixture["competition_name"]))
        if fd_competition is None:
            skip(fixture, "not_fd_league")
            continue
        if fd_competition not in run_cache:
            run_cache[fd_competition] = load_latest_run(models_dir, fd_competition)
        run = run_cache[fd_competition]
        if run is None:
            skip(fixture, "no_model")
            continue
        model_teams = sorted(run.base.teams)
        home_names = [
            str(fixture["home_team"]),
            *_team_aliases(conn, int(fixture["home_team_id"])),
        ]
        away_names = [
            str(fixture["away_team"]),
            *_team_aliases(conn, int(fixture["away_team_id"])),
        ]
        home_model = match_model_team(model_teams, home_names)
        away_model = match_model_team(model_teams, away_names)
        if home_model is None or away_model is None:
            skip(
                fixture,
                "no_mapping",
                detail=f"home={home_model} away={away_model}",
            )
            continue
        payload = build_forecast_payload(
            run,
            fd_competition=fd_competition,
            home_model_team=home_model,
            away_model_team=away_model,
        )
        inserted = fc_store.insert_forecast(
            conn,
            fixture_id=fixture_id,
            track="ml",
            # 工件身份（数据+配置+seed+版本）而非裸数据指纹：不同配置的
            # 预测分组可见，旧 Forecast 不被覆盖（票 34 验收 6）
            model_version=f"dc-{run.artifact_id()[:12]}",
            content_hash=content_hash(payload),
            payload=payload,
        )
        if inserted is None:
            stats.duplicates += 1
        else:
            stats.generated += 1
    conn.commit()
    return stats


def forecast_matrix_from_payload(payload: dict[str, Any]) -> ScoreMatrix:
    """把落库 payload 重建为 ScoreMatrix（视图即时计算的入口）。"""
    return ScoreMatrix(
        tuple(tuple(cell for cell in row) for row in payload["matrix"]),
        lam_home=float(payload["lambda_home"]),
        lam_away=float(payload["lambda_away"]),
    )
