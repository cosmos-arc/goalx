"""
xG 消费数学层（票 45）：xG 版 DC 拟合 + 早赛季 npxG 收缩校准（纯函数）。

- ``fit_xg_dc``：对 npxG（浮点）而非进球拟合攻防强度模型（文献口径：
  opisthokonta 等——xG 响应的似然更贴近真实攻防水平）。penaltyblog 的
  ``BaseGoalsModel`` 把 goals 强转整型，浮点响应走本模块自实现：加权双
  Poisson GLM（attack/defence/home_advantage，Σattack=Σdefence=0 参数化
  约束，scipy L-BFGS-B）。ρ 不适用：DC tau 修正定义在整型低比分格上，
  浮点 xG 无 0/1 格语义——xG 版为独立双 Poisson（rho=0，文档化差异）。
- ``shrink_dc_params``：goal-DC 参数的事后收缩校准——早赛季（n 场小样本）
  把攻防强度向本季累计 npxG/npxGA 速率收缩，权重 w = n/(n+k)；在速率空间
  混合后取对数增量、重归零均值（联赛总进球水平与主场优势不动）。

两者的融合方式（收缩 vs 概率加权）由 evaluation/xg_compare 实证对比后
定（票 45 人裁决项），本模块只提供可测的数学件。
"""

# scipy 无 py.typed 存根，以下规则的第三方 unknown 在本文件放宽（dc_model
# 对 penaltyblog 同先例）。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import date

from scipy.optimize import minimize

from goalx_backend.modelling.dc_model import DCArtifact

_PARAM_BOUND = 3.0  # |attack|/|defence| 对数界（足够宽，不裁真值）
_GAMMA_BOUNDS = (math.log(0.1), math.log(10.0))
_MIN_TEAMS = 2


@dataclass(frozen=True)
class XGRow:
    """一行 xG 训练数据（understat 命名域，team 为源球队 id）。"""

    match_date: str
    home_team: str
    away_team: str
    xg_home: float
    xg_away: float


def _decay_weights(
    dates: list[str], *, as_of: date, half_life_days: float
) -> list[float]:
    """指数时间衰减权重（与 dc_model.decay_weights 同式，独立实现避免跨型）。"""
    xi = math.log(2.0) / half_life_days
    return [math.exp(-xi * (as_of - date.fromisoformat(d)).days) for d in dates]


def _fingerprint(rows: list[XGRow]) -> str:
    """数据指纹（dc_model._rows_fingerprint 同构，浮点值原样入串）。"""
    canonical = "\n".join(
        f"{r.match_date}|{r.home_team}|{r.away_team}|{r.xg_home:.4f}:{r.xg_away:.4f}"
        for r in sorted(rows, key=lambda r: (r.match_date, r.home_team, r.away_team))
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _expand(
    teams: list[str], params: list[float]
) -> tuple[dict[str, float], dict[str, float]]:
    """自由参数（前 n-1 队）→ 全队字典（末队 = −Σ其余，约束闭合）。"""
    attack = dict(zip(teams[:-1], params[: len(teams) - 1], strict=True))
    defence = dict(
        zip(
            teams[:-1],
            params[len(teams) - 1 : 2 * (len(teams) - 1)],
            strict=True,
        )
    )
    attack[teams[-1]] = -sum(attack.values())
    defence[teams[-1]] = -sum(defence.values())
    return attack, defence


def fit_xg_dc(
    rows: list[XGRow],
    *,
    competition: str,
    half_life_days: float = 365.0,
    as_of: date | None = None,
) -> DCArtifact:
    """
    加权双 Poisson 攻防拟合（浮点 xG 响应）→ DCArtifact（rho=0）。

    拟合不收敛或样本不足（少于两队）抛 ValueError；调用方按周跳过。
    """
    teams = sorted({r.home_team for r in rows} | {r.away_team for r in rows})
    if len(rows) < _MIN_TEAMS or len(teams) < _MIN_TEAMS:
        raise ValueError(
            f"{competition} xG 训练样本不足: {len(rows)} 行 {len(teams)} 队"
        )
    resolved = as_of or date.fromisoformat(max(r.match_date for r in rows))
    weights = _decay_weights(
        [r.match_date for r in rows], as_of=resolved, half_life_days=half_life_days
    )
    n_free = len(teams) - 1
    mean_goals = sum(
        weights[i] * (rows[i].xg_home + rows[i].xg_away) for i in range(len(rows))
    ) / (2.0 * sum(weights))
    x0 = [0.0] * (2 * n_free) + [math.log(max(mean_goals, 0.1))]
    bounds = [(-_PARAM_BOUND, _PARAM_BOUND)] * (2 * n_free) + [_GAMMA_BOUNDS]

    def neg_log_likelihood(free: list[float]) -> float:
        attack, defence = _expand(teams, free)
        gamma = free[-1]
        total = 0.0
        for row, weight in zip(rows, weights, strict=True):
            lam_home = math.exp(gamma + attack[row.home_team] + defence[row.away_team])
            lam_away = math.exp(gamma + attack[row.away_team] + defence[row.home_team])
            for goals, lam in ((row.xg_home, lam_home), (row.xg_away, lam_away)):
                total -= weight * (
                    goals * math.log(lam) - lam - math.lgamma(goals + 1.0)
                )
        return total

    result = minimize(
        neg_log_likelihood,
        x0,
        method="L-BFGS-B",
        bounds=bounds,
        options={"maxiter": 200},
    )
    if not bool(result.success):
        raise ValueError(f"{competition} xG 拟合未收敛: {result.message}")
    attack, defence = _expand(teams, list(result.x))
    gamma = float(result.x[-1])
    return DCArtifact(
        competition=competition,
        train_window_start=min(r.match_date for r in rows),
        train_window_end=max(r.match_date for r in rows),
        as_of=resolved.isoformat(),
        n_matches=len(rows),
        half_life_days=half_life_days,
        teams={
            team: {"attack": attack[team], "defence": defence[team]} for team in teams
        },
        home_advantage=gamma,
        rho=0.0,
        data_fingerprint=_fingerprint(rows),
        loglikelihood=-float(result.fun),
        fitted_at="",
    )


def shrink_dc_params(
    base: DCArtifact,
    npxg: dict[str, tuple[float, float, int]],
    *,
    k: float = 6.0,
) -> DCArtifact:
    """
    早赛季收缩校准：攻防参数向本季累计 npxG/npxGA 速率收缩（纯函数）。

    ``npxg``：队 →（累计 npxG, 累计 npxGA, 已赛场数）；调用方保证只含
    决策时点前已完场的累计（红线在采集层/对比层执行）。速率空间混合
    （w = n/(n+k)）后取对数增量并整体归零均值——联赛进球水平、主场
    优势与 ρ 不动。无数据队伍仅受重归一项的反向微调（约束闭合）。
    """
    teams = base.teams
    mean_attack = sum(t["attack"] for t in teams.values()) / len(teams)
    mean_defence = sum(t["defence"] for t in teams.values()) / len(teams)
    half_adv = base.home_advantage / 2.0
    delta_attack: dict[str, float] = dict.fromkeys(teams, 0.0)
    delta_defence: dict[str, float] = dict.fromkeys(teams, 0.0)
    for team, (npxg_sum, npxga_sum, played) in npxg.items():
        if team not in teams or played <= 0:
            continue
        hist_for = math.exp(teams[team]["attack"] + mean_defence + half_adv)
        hist_against = math.exp(mean_attack + teams[team]["defence"] + half_adv)
        weight = played / (played + k)
        adjusted_for = (1.0 - weight) * hist_for + weight * (npxg_sum / played)
        adjusted_against = (1.0 - weight) * hist_against + weight * (npxga_sum / played)
        delta_attack[team] = math.log(adjusted_for / hist_for)
        delta_defence[team] = math.log(adjusted_against / hist_against)
    n_adjusted = sum(1 for v in delta_attack.values() if v != 0.0)
    if n_adjusted:
        center_a = sum(delta_attack.values()) / len(teams)
        center_d = sum(delta_defence.values()) / len(teams)
        delta_attack = {t: v - center_a for t, v in delta_attack.items()}
        delta_defence = {t: v - center_d for t, v in delta_defence.items()}
    return replace(
        base,
        teams={
            team: {
                "attack": params["attack"] + delta_attack[team],
                "defence": params["defence"] + delta_defence[team],
            }
            for team, params in teams.items()
        },
    )
