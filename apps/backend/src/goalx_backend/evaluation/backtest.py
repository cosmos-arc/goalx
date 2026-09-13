"""
回测引擎核心（票 28，ADR 0007）：五大三季 walk-forward + 模拟竞彩价。

- 按比赛周（ISO 年-周）重估：每联赛每周拟合一次时间衰减 DC，训练截止
  严格早于该周最早比赛日（``assert_no_lookahead`` 常开断言）；
- 公允基准 fair = Pinnacle 收盘 Shin（PSC），缺失行用 AvgC 兜底；
- 模拟竞彩价 = fair 赔率 × (1 − haircut)（默认 −10%，票 30 可替换校准值）；
  hhad/ttg 的市场侧分布由 fair 1X2 经 penaltyblog goal_expectancy 反推
  (λ_home, λ_away) 构造市场隐含矩阵后推导；
- 模拟玩法 v1 收窄为 had-only：用户裁决原为 had+hhad+ttg，但实测发现
  1X2→比分矩阵反推（goal_expectancy）在总进球维度系统性欠分散——6,174 场
  全量对照中 ttg 桶 4/5/6 隐含概率低于实际 15-40%（桶5 7.1% vs 9.1%），
  hhad 实际命中率 33% 也低于隐含 40%——由该反推派生的 hhad/ttg 模拟价
  会制造假 edge（回测假阳性），故 v1 不从反推价下注这两个玩法；等真实
  totals/handicap 市场报价接入（The Odds API totals）后再启用；
- EV 阈值 τ=1.5% + 1/4 Kelly（单注 1% 参考资金上限）；单关 + 每周每联赛
  至多一笔 2串1（取 EV 最高且不同场次的两注，按一笔联合 EV 判定）；
- 结算复用 M1 Settlement 引擎（口径统一，票 12 第 4 条）；收盘价只作
  定价/结算基准，不进模型特征；
- run 维度隔离：每次运行独立 run_id（幂等可重跑）。
"""

# penaltyblog 无 py.typed 存根，以下规则的第三方 unknown 在本文件放宽。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from penaltyblog.models import goal_expectancy

from goalx_backend import odds_math as om
from goalx_backend.data import results as rs_store
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS
from goalx_backend.modelling.dc_model import (
    TIER1_COMPETITIONS,
    DCArtifact,
    TrainingRow,
    fit_dc_model,
    implementation_versions,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix
from goalx_backend.settlement import LegSpec, ResultFacts, settle_fixed_bet

MARKET_MAX_GOAL_ERROR = 0.02  # 市场隐含 λ 反推的 had 拟合误差上限
_MIN_PARLAY_SINGLES = 2  # 串关需要的合格单关数
# 模拟竞彩价天花板：真实竞彩不报价超过该量级的选项（ttg 极端档最高几十），
# haircut 外推在市场隐含概率极低的尾部无效——两头（反推 λ 与 DC 模型）都
# 无法分辨该量级的概率，产生的"EV"是模型-市场尾部分歧的噪声。
DEFAULT_MAX_SIM_ODDS = 50.0
# 市场隐含概率下限：1X2 → 比分分布的反推在极低概率尾部不可辨识（不同 λ 组合
# 拟合出同一 1X2，尾部概率却相差数倍），低于该下限的选项不定价不下注。
MIN_MARKET_PROB = 0.02
HHAD_LINE_RANGE = range(-3, 4)  # 竞彩整数让球线搜索范围


@dataclass(frozen=True)
class BacktestParams:
    """一次回测的全部可调参数（落盘到 backtest_runs.params）。"""

    competitions: tuple[str, ...] = TIER1_COMPETITIONS
    seasons: tuple[str, ...] = ("2324", "2425", "2526")
    haircut: float = 0.10
    ev_threshold: float = 0.015
    kelly_fraction: float = 0.25
    ref_bankroll: float = 5000.0
    stake_cap: float = 50.0
    half_life_days: float = 365.0
    min_train_matches: int = 100
    parlay2: bool = True
    max_sim_odds: float = DEFAULT_MAX_SIM_ODDS
    # 公允基准来源：auto=PSC 优先、缺失用 AvgC 兜底；psc/avgc = 只用该源
    # （票 34 分期/来源对照 run 用，缺失行跳过并计入 no_fair_baseline）
    fair_source: str = "auto"


@dataclass
class BacktestResult:
    """一次回测的汇总统计。"""

    run_id: int
    predictions: int = 0
    bets: int = 0
    staked: float = 0.0
    profit: float = 0.0
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def roi(self) -> float:
        """Flat 口径投资回报率。"""
        return self.profit / self.staked if self.staked else 0.0


def match_week_key(match_date: str) -> str:
    """比赛周键（ISO 年-周）。"""
    iso = date.fromisoformat(match_date).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def group_by_week(
    rows: list[sqlite3.Row],
) -> list[tuple[str, list[sqlite3.Row]]]:
    """按 ISO 比赛周分组（周内按日期排序）。"""
    weeks: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        weeks.setdefault(match_week_key(str(row["match_date"])), []).append(row)
    return [
        (key, sorted(week, key=lambda r: str(r["match_date"])))
        for key, week in sorted(weeks.items())
    ]


def assert_no_lookahead(train_rows: list[TrainingRow], week_dates: list[str]) -> None:
    """防前视断言（常开）：训练窗上界必须严格早于比赛周最早日期。"""
    if not train_rows or not week_dates:
        return
    latest_train = max(row.match_date for row in train_rows)
    earliest_match = min(week_dates)
    if latest_train >= earliest_match:
        raise AssertionError(
            f"look-ahead detected: train {latest_train} >= week {earliest_match}"
        )


def fair_probs_from_close(
    row: sqlite3.Row, *, fair_source: str = "auto"
) -> tuple[dict[str, float], str] | None:
    """收盘公允概率：按 fair_source 选择 PSC/AvgC（ADR 0007；票 34 对照）。"""
    psc = (row["psc_home"], row["psc_draw"], row["psc_away"])
    avgc = (row["avgc_home"], row["avgc_draw"], row["avgc_away"])
    if fair_source == "psc":
        candidates: tuple[tuple[tuple[float | int | None, ...], str], ...] = (
            (psc, "psc"),
        )
    elif fair_source == "avgc":
        candidates = ((avgc, "avgc"),)
    else:
        candidates = ((psc, "psc"), (avgc, "avgc"))
    for odds, source in candidates:
        if all(isinstance(o, (int, float)) and o > 1.0 for o in odds):
            probs = om.shin_implied(tuple(float(o) for o in odds))
            return dict(zip(SELECTIONS, probs, strict=True)), source
    return None


def simulated_jc_odds(fair_probs: dict[str, float], haircut: float) -> dict[str, float]:
    """模拟竞彩价：fair 赔率 × (1 − haircut)。"""
    return {sel: (1.0 / p) * (1.0 - haircut) for sel, p in fair_probs.items()}


def market_implied_matrix(
    fair_probs: dict[str, float],
) -> ScoreMatrix | None:
    """
    从市场 fair 1X2 反推 (λ_home, λ_away) 构造市场隐含比分矩阵。

    仅作诊断/预留工具：实测其总进球维度系统性欠分散（见模块 docstring），
    不用于定价；待真实 totals/handicap 报价接入后由报价直接构造市场侧。
    """
    raw = goal_expectancy(
        fair_probs["h"], fair_probs["d"], fair_probs["a"], max_goals=10
    )
    result: dict[str, Any] = dict(raw)
    if not result["success"] or result["error"] > MARKET_MAX_GOAL_ERROR:
        return None
    return ScoreMatrix.from_lambdas(
        float(result["home_exp"]), float(result["away_exp"])
    )


def pick_hhad_line(matrix: ScoreMatrix) -> int:
    """让球线近似：两侧概率最均衡的整数线（官方调线行为近似；hhad 预留）。"""
    best_line, best_gap = 0, float("inf")
    for line in HHAD_LINE_RANGE:
        probs = matrix.hhad(float(line))
        gap = abs(probs["h"] - probs["a"])
        if gap < best_gap:
            best_line, best_gap = line, gap
    return best_line


def kelly_stake(
    prob: float, odds: float, params: BacktestParams
) -> tuple[float, float]:
    """1/Kelly 注额：返回 (stake, 全额 Kelly 比例)；EV≤0 返回 (0, 0)。"""
    ev = prob * odds - 1.0
    if ev <= 0.0 or odds <= 1.0:
        return 0.0, 0.0
    full = ev / (odds - 1.0)
    stake = min(params.kelly_fraction * full * params.ref_bankroll, params.stake_cap)
    return round(stake, 2), full


@dataclass
class _Candidate:
    """一笔候选单关（结算前）。"""

    hist_match_id: int
    market_code: str
    selection_code: str
    odds: float
    model_prob: float
    goal_line: float | None = None

    @property
    def ev(self) -> float:
        return self.model_prob * self.odds - 1.0


def _backtest_rows(
    conn: sqlite3.Connection, params: BacktestParams
) -> dict[str, list[sqlite3.Row]]:
    """取回测范围内的 hist 行（按联赛分组、按日期排序）。"""
    rows = rs_store.hist_rows_in_seasons(conn, params.competitions, params.seasons)
    by_comp: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_comp.setdefault(str(row["competition"]), []).append(row)
    return by_comp


def _candidates_for_match(
    matrix: ScoreMatrix,
    jc_had: dict[str, float],
    params: BacktestParams,
    hist_match_id: int,
) -> list[_Candidate]:
    """一场比赛的全部候选单关（had + hhad + ttg，用户裁决）。"""
    # v1 had-only（见模块 docstring：hhad/ttg 反推价不可信，待真实市场价接入）
    candidates: list[_Candidate] = []
    had = matrix.had()
    for sel in SELECTIONS:
        market_prob = 1.0 / jc_had[sel]
        if market_prob >= MIN_MARKET_PROB and jc_had[sel] <= params.max_sim_odds:
            candidates.append(
                _Candidate(hist_match_id, "had", sel, jc_had[sel], had[sel])
            )
    return candidates


@dataclass(frozen=True)
class _WeekContext:
    """一周下注上下文（结算与落库共享）。"""

    run_id: int
    competition: str
    placed_week: str
    results: dict[int, ResultFacts]


def _settle_and_store(
    conn: sqlite3.Connection,
    ctx: _WeekContext,
    *,
    kind: str,
    stake: float,
    legs: list[_Candidate],
    kelly: float,
) -> float:
    """用 M1 Settlement 引擎结算一注并落库；返回 profit。"""
    specs = [
        LegSpec(
            fixture_id=leg.hist_match_id,
            market_code=leg.market_code,
            selection_code=leg.selection_code,
            locked_odds=leg.odds,
            goal_line=leg.goal_line,
        )
        for leg in legs
    ]
    outcome = settle_fixed_bet(stake, specs, ctx.results)
    if not outcome.settled:
        raise RuntimeError("回测注单出现未结算状态(历史赛果应完整)")
    profit = round(outcome.profit, 2)
    conn.execute(
        """
        INSERT INTO backtest_bets
        (run_id, kind, competition, placed_week, stake, legs, ev, kelly,
         status, payout, profit, detail)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.run_id,
            kind,
            ctx.competition,
            ctx.placed_week,
            stake,
            json.dumps([asdict(leg) for leg in legs], ensure_ascii=False),
            round((legs[0].ev if kind == "single" else _parlay_ev(legs)), 6),
            round(kelly, 6),
            outcome.status,
            round(outcome.payout, 2),
            profit,
            json.dumps(
                {
                    "notes": outcome.notes,
                    "legs": [
                        {"hit": leg.hit, "void": leg.void} for leg in outcome.legs
                    ],
                },
                ensure_ascii=False,
            ),
        ),
    )
    return profit


def _parlay_ev(legs: list[_Candidate]) -> float:
    """串关联合 EV（一笔）：∏p × ∏o − 1。"""
    prob = 1.0
    odds = 1.0
    for leg in legs:
        prob *= leg.model_prob
        odds *= leg.odds
    return prob * odds - 1.0


def _week_training_rows(
    conn: sqlite3.Connection, competition: str, week_dates: list[str]
) -> list[TrainingRow]:
    """该比赛周的训练集：match_date 严格早于周内最早比赛日（防前视）。"""
    cutoff = (date.fromisoformat(min(week_dates)) - timedelta(days=1)).isoformat()
    hist_rows = rs_store.hist_rows_through(conn, competition, cutoff)
    return [
        TrainingRow(
            match_date=str(r["match_date"]),
            home_team=str(r["home_team"]),
            away_team=str(r["away_team"]),
            fthg=int(r["fthg"]),
            ftag=int(r["ftag"]),
        )
        for r in hist_rows
    ]


def _record_week_predictions(
    conn: sqlite3.Connection,
    run_id: int,
    week_rows: list[sqlite3.Row],
    artifact: DCArtifact,
    params: BacktestParams,
    result: BacktestResult,
) -> dict[int, list[_Candidate]]:
    """逐场落预测行并构造候选单关；返回 hist_match_id → candidates。"""

    def skip(key: str) -> None:
        result.skipped[key] = result.skipped.get(key, 0) + 1

    candidates_by_match: dict[int, list[_Candidate]] = {}
    for row in week_rows:
        hist_id = int(row["id"])
        home, away = str(row["home_team"]), str(row["away_team"])
        if home not in artifact.teams or away not in artifact.teams:
            skip("untrained_teams")
            continue
        fair = fair_probs_from_close(row, fair_source=params.fair_source)
        if fair is None:
            skip("no_fair_baseline")
            continue
        fair_probs, fair_source = fair
        try:
            matrix = artifact.predict(home, away)
        except ValueError:
            # 小训练池 ρ 数值越界 → 该场不产生预测/候选
            skip("predict_failed")
            continue
        had = matrix.had()
        conn.execute(
            """
            INSERT OR IGNORE INTO backtest_predictions
            (run_id, hist_match_id, competition, season, match_date,
             home_team, away_team, had_probs, fair_probs, fair_source,
             model_fingerprint, train_window_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                hist_id,
                str(row["competition"]),
                str(row["season"]),
                str(row["match_date"]),
                home,
                away,
                json.dumps(had),
                json.dumps(fair_probs),
                fair_source,
                artifact.data_fingerprint,
                artifact.train_window_end,
            ),
        )
        result.predictions += 1
        jc_had = simulated_jc_odds(fair_probs, params.haircut)
        candidates_by_match[hist_id] = _candidates_for_match(
            matrix, jc_had, params, hist_id
        )
    return candidates_by_match


def _place_week_bets(
    conn: sqlite3.Connection,
    ctx: _WeekContext,
    candidates_by_match: dict[int, list[_Candidate]],
    params: BacktestParams,
    result: BacktestResult,
) -> None:
    """单关下注（EV ≥ τ + 1/4 Kelly 封顶）+ 每周每联赛至多一笔 2串1。"""
    qualified: list[_Candidate] = []
    for match_candidates in candidates_by_match.values():
        for cand in match_candidates:
            stake, kelly = kelly_stake(cand.model_prob, cand.odds, params)
            if cand.ev < params.ev_threshold or stake <= 0:
                continue
            qualified.append(cand)
            profit = _settle_and_store(
                conn, ctx, kind="single", stake=stake, legs=[cand], kelly=kelly
            )
            result.bets += 1
            result.staked += stake
            result.profit += profit
    if not params.parlay2 or len(qualified) < _MIN_PARLAY_SINGLES:
        return
    ordered = sorted(qualified, key=lambda c: c.ev, reverse=True)
    first = ordered[0]
    second = next(
        (c for c in ordered[1:] if c.hist_match_id != first.hist_match_id), None
    )
    if second is None:
        return
    legs = [first, second]
    joint_prob = first.model_prob * second.model_prob
    joint_odds = first.odds * second.odds
    stake, kelly = kelly_stake(joint_prob, joint_odds, params)
    if _parlay_ev(legs) < params.ev_threshold or stake <= 0:
        return
    profit = _settle_and_store(
        conn, ctx, kind="parlay2", stake=stake, legs=legs, kelly=kelly
    )
    result.bets += 1
    result.staked += stake
    result.profit += profit


def run_backtest(
    conn: sqlite3.Connection, params: BacktestParams, *, label: str
) -> BacktestResult:
    """
    执行一次完整回测（run 维度隔离，同参数重跑产生新 run）。

    结算实时完成（历史赛果已知）；逐注记录 EV/Kelly/盈亏，走与纸面/实盘
    相同的 ``settle_fixed_bet`` 代码路径。
    """
    run_params = asdict(params) | {
        # 票 34：合成价实验明确标注，不得称真实陈盘回放；实现/依赖版本入档
        "price_model": "simulated_jc",
        "versions": implementation_versions(),
    }
    cur = conn.execute(
        """
        INSERT INTO backtest_runs (label, params, status, created_at)
        VALUES (?, ?, 'running', ?)
        """,
        (label, json.dumps(run_params, ensure_ascii=False), utc_now_iso()),
    )
    run_id = int(cur.lastrowid or 0)
    result = BacktestResult(run_id=run_id)
    try:
        for competition, comp_rows in _backtest_rows(conn, params).items():
            for week_key, week_rows in group_by_week(comp_rows):
                week_dates = [str(r["match_date"]) for r in week_rows]
                train_rows = _week_training_rows(conn, competition, week_dates)
                if len(train_rows) < params.min_train_matches:
                    result.skipped["thin_train_weeks"] = (
                        result.skipped.get("thin_train_weeks", 0) + 1
                    )
                    continue
                assert_no_lookahead(train_rows, week_dates)
                try:
                    artifact = fit_dc_model(
                        train_rows,
                        competition=competition,
                        half_life_days=params.half_life_days,
                    )
                except ValueError:
                    # 极小训练池可能数值不可辨识(ρ 越界等) → 跳过该周
                    result.skipped["fit_failed_weeks"] = (
                        result.skipped.get("fit_failed_weeks", 0) + 1
                    )
                    continue
                results = {
                    int(r["id"]): ResultFacts(
                        home_goals=int(r["fthg"]), away_goals=int(r["ftag"])
                    )
                    for r in week_rows
                }
                candidates_by_match = _record_week_predictions(
                    conn, run_id, week_rows, artifact, params, result
                )
                _place_week_bets(
                    conn,
                    _WeekContext(run_id, competition, week_key, results),
                    candidates_by_match,
                    params,
                    result,
                )
        conn.execute(
            """
            UPDATE backtest_runs
            SET status = 'done', finished_at = ?, summary = ? WHERE id = ?
            """,
            (
                utc_now_iso(),
                json.dumps(
                    {
                        "predictions": result.predictions,
                        "bets": result.bets,
                        "staked": round(result.staked, 2),
                        "profit": round(result.profit, 2),
                        "roi": round(result.roi, 6),
                        "skipped": result.skipped,
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
    except Exception:
        conn.execute(
            "UPDATE backtest_runs SET status = 'failed', finished_at = ? WHERE id = ?",
            (utc_now_iso(), run_id),
        )
        conn.commit()
        raise
    return result


def run_summary(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """读取一次 run 的 summary JSON。"""
    row = conn.execute(
        "SELECT summary FROM backtest_runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None or row["summary"] is None:
        return None
    summary: object = json.loads(row["summary"])
    return dict(summary)  # type: ignore[arg-type]
