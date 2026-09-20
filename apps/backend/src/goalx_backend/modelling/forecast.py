"""
Forecast 生成与内容哈希存证（票 27，ADR 0001/0006；票 47 xG blend 接线）。

对在售竞彩场次（经票 25 对齐映射到训练域球队）生成 10×10 矩阵 Forecast
（track=ml）：

- append-only 落库 + 内容哈希存证：同 payload 同哈希，重复生成被 UNIQUE
  吸收（幂等）；
- bootstrap CI：训练期重采样工件（票 26）逐个重算 λ → 矩阵 → had 概率，
  百分位区间随 payload 落库（样本数=工件数，alpha 参数化）；
- 玩法概率推导视图即时计算（视图态不落库，ADR 0006）；
- 无映射/无模型场次进跳过清单（明确可查）；
- **xG blend（票 45 实证裁决，票 47 接线）**：五大联赛（understat 覆盖域）
  上把 goal-DC 工件与 as-of 当日的 xG 版 DC（understat npxG 拟合）做
  had 线性池 50/50（XG_BLEND_WEIGHT）——矩阵层实现为两网格的算术混合
  （had 池精确等于实证口径；crs/ttg/hafu 由混合网格推导，保持全玩法
  一致）。xG 侧无数据/队名解析不到 → 纯 goal-DC（payload.xg_blend=None），
  model_version 前缀区分（dc- / dc-xgblend-）。CI 口径 = goal bootstrap
  样本与 xG 点估计逐样本池化（ci_method 注明——低估 xG 侧参数不确定度，
  诚实标注不冒充）。票 34 前瞻冻结口径不变：评分读赛前最新 Forecast，
  模型迭代经 model_version 可追溯。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import utc_now_iso
from goalx_backend.modelling.dc_model import (
    DCArtifact,
    TrainingRun,
    load_latest_run,
)
from goalx_backend.modelling.score_matrix import Grid, ScoreMatrix
from goalx_backend.modelling.team_align import NameIndex, match_model_team
from goalx_backend.modelling.xg_dc import XGRow, fit_xg_dc

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
# xG blend 权重（票 45 实证：had 线性池 50/50 全桶一致改善，用户裁决 2026-09-21）
XG_BLEND_WEIGHT = 0.5


@dataclass
class ForecastStats:
    """一次 Forecast 生成的统计与跳过清单。"""

    generated: int = 0
    duplicates: int = 0
    xg_blended: int = 0  # 附 xG blend 的场次数（票 47）
    xg_unresolved: int = 0  # xG 工件在但队名解析不到（纯 goal-DC 落库）
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


@dataclass(frozen=True)
class XGSide:
    """一场 blend 的 xG 侧材料（工件 + 双方 understat 队名键）。"""

    artifact: DCArtifact
    home_title: str
    away_title: str


def _blend_grid(
    goal_grid: Grid, xg_grid: Grid, weight: float = XG_BLEND_WEIGHT
) -> Grid:
    """两网格算术混合（had 池精确 = 线性池；权重为 goal 侧占比）。"""
    size = len(goal_grid)
    return tuple(
        tuple(
            weight * goal_grid[h][a] + (1.0 - weight) * xg_grid[h][a]
            for a in range(size)
        )
        for h in range(size)
    )


def _geometric_mean(*values: float) -> float:
    """几何均值（blend 的 λ 语义：Poisson 对数池保持可分解）。"""
    return math.exp(sum(math.log(v) for v in values) / len(values))


def build_forecast_payload(
    run: TrainingRun,
    *,
    fd_competition: str,
    home_model_team: str,
    away_model_team: str,
    xg_side: XGSide | None = None,
) -> dict[str, Any]:
    """
    从训练工件构造 Forecast payload（矩阵 + λ + bootstrap CI 材料）。

    xg_side 提供时（票 47 blend）：matrix = 两网格混合、λ = 几何均值
    （hafu 附属视图的半场拆分近似用）、bootstrap had 样本与 xG 点估计
    逐样本池化（ci_method 注明口径）。
    """
    lam_home, lam_away = run.base.lambdas(home_model_team, away_model_team)
    matrix = run.base.predict(home_model_team, away_model_team)
    rho = run.base.rho
    bootstrap_lambda, had_samples = bootstrap_distribution(
        run, home_model_team, away_model_team
    )
    xg_blend_meta: dict[str, Any] | None = None
    if xg_side is not None:
        xg_matrix = xg_side.artifact.predict(xg_side.home_title, xg_side.away_title)
        xg_had = xg_matrix.had()
        grid = _blend_grid(matrix.grid, xg_matrix.grid)
        lam_home = _geometric_mean(lam_home, xg_matrix.lam_home)
        lam_away = _geometric_mean(lam_away, xg_matrix.lam_away)
        matrix = ScoreMatrix(grid, lam_home=lam_home, lam_away=lam_away)
        rho = XG_BLEND_WEIGHT * rho + (1.0 - XG_BLEND_WEIGHT) * xg_side.artifact.rho
        had_samples = [
            {
                sel: XG_BLEND_WEIGHT * sample[sel]
                + (1.0 - XG_BLEND_WEIGHT) * xg_had[sel]
                for sel in ("h", "d", "a")
            }
            for sample in had_samples
        ]
        bootstrap_lambda = [
            [
                XG_BLEND_WEIGHT * lh + (1.0 - XG_BLEND_WEIGHT) * xg_matrix.lam_home,
                XG_BLEND_WEIGHT * la + (1.0 - XG_BLEND_WEIGHT) * xg_matrix.lam_away,
            ]
            for lh, la in bootstrap_lambda
        ]
        xg_blend_meta = {
            "weight_goal_side": XG_BLEND_WEIGHT,
            "xg_model_fingerprint": xg_side.artifact.data_fingerprint,
            "xg_matches": xg_side.artifact.n_matches,
            "xg_as_of": xg_side.artifact.as_of,
            "xg_home": xg_side.home_title,
            "xg_away": xg_side.away_title,
            "ci_method": "goal_bootstrap_pooled_with_xg_point",
        }
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
        "rho": rho,
        "matrix": [list(row) for row in matrix.grid],
        "bootstrap_lambda": bootstrap_lambda,
        # 名义水平 + 有效样本数（票 34：覆盖未验收，不得称校准成功）
        "had_ci": had_ci,
        "ci_nominal_level": 1.0 - CI_ALPHA if had_ci is not None else None,
        "ci_effective_samples": len(had_samples),
        "xg_blend": xg_blend_meta,
    }


def insert_forecast(
    conn: sqlite3.Connection,
    *,
    fixture_id: int,
    track: str,
    model_version: str,
    content_hash: str,
    payload: dict[str, object],
    issued_at: str | None = None,
) -> int | None:
    """
    Append 一条 Forecast（forecasts 表归本模块）。

    同 (fixture_id, content_hash) 重复插入被吸收；返回新行 id，已存在返回
    None（幂等重跑）。issued_at 缺省取当前 UTC——测试/回放可显式冻结发出
    时点（票 41 as-of 快照的用例需要）。
    """
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO forecasts
        (fixture_id, track, model_version, issued_at, content_hash, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            fixture_id,
            track,
            model_version,
            issued_at or utc_now_iso(),
            content_hash,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    if cur.rowcount > 0 and cur.lastrowid:
        return int(cur.lastrowid)
    return None


def forecasts_for_track(conn: sqlite3.Connection, track: str) -> list[sqlite3.Row]:
    """
    某轨道全部 Forecast 行，按 (fixture, issued_at, id) 排序。

    前瞻评分冻结规则要求赛前最新一条，排序保证「最后一条即最新」。
    """
    return conn.execute(
        """
        SELECT fixture_id, id, model_version, issued_at, payload
        FROM forecasts WHERE track = ?
        ORDER BY fixture_id, issued_at, id
        """,
        (track,),
    ).fetchall()


def latest_forecast(
    conn: sqlite3.Connection, fixture_id: int, track: str = "ml"
) -> sqlite3.Row | None:
    """
    某场次的最新一条 Forecast（票 wb-02 研究页模型概率）。

    与前瞻冻结规则同口径：按 (issued_at, id) 取最新——之后的覆盖之前的。
    """
    return conn.execute(
        """
        SELECT fixture_id, id, model_version, issued_at, payload
        FROM forecasts WHERE fixture_id = ? AND track = ?
        ORDER BY issued_at DESC, id DESC
        LIMIT 1
        """,
        (fixture_id, track),
    ).fetchone()


def latest_forecast_asof(
    conn: sqlite3.Connection, fixture_id: int, as_of: str, track: str = "ml"
) -> sqlite3.Row | None:
    """
    as_of 时点已发出的最新一条 Forecast（票 41 注级快照读取）。

    与 latest_forecast 同排序口径，加 issued_at <= as_of 过滤——决策时点
    之后才生成的预测不进该时点的快照（防时间泄漏，与前瞻冻结同语义）。
    """
    return conn.execute(
        """
        SELECT fixture_id, id, model_version, issued_at, payload
        FROM forecasts WHERE fixture_id = ? AND track = ? AND issued_at <= ?
        ORDER BY issued_at DESC, id DESC
        LIMIT 1
        """,
        (fixture_id, track, as_of),
    ).fetchone()


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
    不在此列）。五大联赛（understat 覆盖域）附 xG blend（票 47）；xG 侧
    不可得时纯 goal-DC 落库（xg_blend=None，model_version 无 xgblend 前缀）。
    """
    stats = ForecastStats()
    run_cache: dict[str, TrainingRun | None] = {}
    xg_cache: dict[str, tuple[DCArtifact, NameIndex, list[str]] | None] = {}
    now_iso = datetime.now(UTC).isoformat(timespec="seconds")

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
        if fd_competition not in xg_cache:
            xg_cache[fd_competition] = _fit_league_xg(conn, fd_competition, now_iso)
        home_model, away_model = _resolve_model_teams(conn, run, fixture)
        if home_model is None or away_model is None:
            skip(
                fixture,
                "no_mapping",
                detail=f"home={home_model} away={away_model}",
            )
            continue
        home_names = [
            str(fixture["home_team"]),
            *_team_aliases(conn, int(fixture["home_team_id"])),
        ]
        away_names = [
            str(fixture["away_team"]),
            *_team_aliases(conn, int(fixture["away_team_id"])),
        ]
        xg_side = _resolve_xg_side(
            xg_cache[fd_competition], home_names, away_names, stats
        )
        payload = build_forecast_payload(
            run,
            fd_competition=fd_competition,
            home_model_team=home_model,
            away_model_team=away_model,
            xg_side=xg_side,
        )
        _record_forecast(conn, fixture_id, run, xg_side, payload, stats)
    conn.commit()
    return stats


def _record_forecast(
    conn: sqlite3.Connection,
    fixture_id: int,
    run: TrainingRun,
    xg_side: XGSide | None,
    payload: dict[str, Any],
    stats: ForecastStats,
) -> None:
    """落库一条 Forecast 并计数（model_version 带 blend 溯源前缀）。"""
    inserted = insert_forecast(
        conn,
        fixture_id=fixture_id,
        track="ml",
        # 工件身份（数据+配置+seed+版本）而非裸数据指纹：不同配置的
        # 预测分组可见，旧 Forecast 不被覆盖（票 34 验收 6）
        model_version=(
            (
                "dc-xgblend-"
                f"{run.artifact_id()[:10]}+{xg_side.artifact.data_fingerprint[:8]}"
            )
            if xg_side is not None
            else f"dc-{run.artifact_id()[:12]}"
        ),
        content_hash=content_hash(payload),
        payload=payload,
    )
    if inserted is None:
        stats.duplicates += 1
    else:
        stats.generated += 1
    if xg_side is not None:
        stats.xg_blended += 1


def _resolve_model_teams(
    conn: sqlite3.Connection, run: TrainingRun, fixture: sqlite3.Row
) -> tuple[str | None, str | None]:
    """Fixture 双方 → 训练域队名（canonical + 别名三级匹配，票 25）。"""
    model_teams = sorted(run.base.teams)
    home_names = [
        str(fixture["home_team"]),
        *_team_aliases(conn, int(fixture["home_team_id"])),
    ]
    away_names = [
        str(fixture["away_team"]),
        *_team_aliases(conn, int(fixture["away_team_id"])),
    ]
    return (
        match_model_team(model_teams, home_names),
        match_model_team(model_teams, away_names),
    )


def _resolve_xg_side(
    xg_pack: tuple[DCArtifact, NameIndex, list[str]] | None,
    home_names: list[str],
    away_names: list[str],
    stats: ForecastStats,
) -> XGSide | None:
    """
    把 fixture 双方解析到 understat title（英别名优先，canonical 兜底）。

    任一侧解析不到 → None（纯 goal-DC 落库，xg_unresolved 计数）。索引值
    为 titles 列表位置（NameIndex 值类型约定 int）。
    """
    if xg_pack is None:
        return None
    artifact, index, titles = xg_pack
    home_pos = index.resolve(home_names[1]) or index.resolve(home_names[0])
    away_pos = index.resolve(away_names[1]) or index.resolve(away_names[0])
    if home_pos is None or away_pos is None:
        stats.xg_unresolved += 1
        return None
    return XGSide(
        artifact=artifact, home_title=titles[home_pos], away_title=titles[away_pos]
    )


def _fit_league_xg(
    conn: sqlite3.Connection, fd_competition: str, now_iso: str
) -> tuple[DCArtifact, NameIndex, list[str]] | None:
    """
    拟合该联赛的 xG 版 DC（as-of 当日）+ 队名解析索引（title → 位置）。

    取数走 rs_store.understat_training_rows（SQL 归 data 域）；understat
    无该联赛数据（Tier2/荷甲）或拟合不收敛 → None（纯 goal-DC 兜底）。
    """
    slug = rs_store.UNDERSTAT_LEAGUES.get(fd_competition)
    if slug is None:
        return None
    rows = rs_store.understat_training_rows(conn, slug, through_iso=now_iso)
    if not rows:
        return None
    xg_rows = [
        XGRow(
            match_date=str(row["datetime_utc"])[:10],
            home_team=str(row["home_team"]),
            away_team=str(row["away_team"]),
            xg_home=float(row["npxg_home"]),
            xg_away=float(row["npxg_away"]),
        )
        for row in rows
    ]
    try:
        artifact = fit_xg_dc(xg_rows, competition=f"xg-{fd_competition}")
    except ValueError:
        return None
    titles = sorted({r.home_team for r in xg_rows} | {r.away_team for r in xg_rows})
    index = NameIndex.build({title: pos for pos, title in enumerate(titles)})
    return artifact, index, titles


def forecast_matrix_from_payload(payload: dict[str, Any]) -> ScoreMatrix:
    """把落库 payload 重建为 ScoreMatrix（视图即时计算的入口）。"""
    return ScoreMatrix(
        tuple(tuple(cell for cell in row) for row in payload["matrix"]),
        lam_home=float(payload["lambda_home"]),
        lam_away=float(payload["lambda_away"]),
    )
