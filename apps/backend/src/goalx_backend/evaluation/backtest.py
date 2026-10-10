"""
回测引擎 v2（backtest-decade 票 13，ADR-0007/0011）：十年 11 联赛 walk-forward。

数据面 = gold ``match_features``（corpus duckdb 只读视图，ADR-0011 只读桥
纪律，不经运行面倒手；v1 五大三季版吃 hist_matches 的路径已退役，票 34
baseline 对照随本引擎走 gold 配对行）：

- 窗口：kickoff 北京墙钟日 ∈ [start, end]（缺省 2016-08-01 → 数据尾）；
  训练池吃 cutoff 前全部 gold 历史（窗口前场次自然充当首季热身池）；
- 按比赛周（ISO 年-周）重估：每联赛每周拟合一次时间衰减 DC，训练截止
  严格早于该周最早比赛日（``assert_no_lookahead`` 常开断言）；特征时间
  戳的 PIT 纪律在 gold 构建面把关（票 06 逐行断言）；
- 公允基准时代分层（era 字段唯一真相源，断点常量定义落 data/leagues.py
  叶子、gold 再导出）：era1（psc_proxy）= PSC→AvgC（Shin）；era2
  （trajectory）= cid177 锚收盘价（Shin，与 PSC 同为尖货收盘，方法学
  跨时代连续）→ 多书中位数共识归一（traj_cons）兜底；
  ``fair_source='psc'/'avgc'`` 语义保留为源选择器（票 34 对照 run），
  ``auto`` = 时代感知正典链；
- 卫生在引擎入口统一生效（单一取数口，不散落 if）：gold 行
  ``admin_excluded=false`` 过滤同时罩训练面与预测面（S4"训练集与对账
  集都剔除"由构造满足）；PSC 成熟度由 gold 快照承载（未成熟行 odds
  列整族置空=无基准，门=gold 构建一处）——运行时再判只可能是空集上
  的 no-op，不二次判防两套口径；
- 模拟竞彩价 = fair 赔率 × (1 − haircut)（默认 −10%，票 30 校准/票 17
  十年重估可替换）；收盘价只作定价/结算基准，不进模型特征；
- 模拟玩法 had-only 维持（hhad/ttg 解锁 = 票 15 的门 = 报告+用户点头，
  不因 AH/OU 数据可用自动放宽）：1X2→比分矩阵反推（goal_expectancy）
  在总进球维度系统性欠分散——6,174 场全量对照 ttg 桶 4/5/6 隐含概率
  低于实际 15-40%（桶5 7.1% vs 9.1%），hhad 实际命中率 33% 低于隐含
  40%——反推价制造假 edge；
- EV 阈值 τ + 1/4 Kelly（单注上限封顶）；单关 + 每周每联赛至多一笔
  2串1（取 EV 最高且不同场次的两注，按一笔联合 EV 判定）；
- 结算复用 M1 Settlement 引擎（口径统一，票 12 第 4 条）：结算内核以
  run 内整数 ref 寻址，审计面（predictions/legs JSON）键 = gold sid；
- run 维度隔离：每次运行独立 run_id（run 内确定可重放）；run params
  入档 gold 版本/构建戳/输入 digest——重放可解释。
"""

# penaltyblog 无 py.typed 存根，以下规则的第三方 unknown 在本文件放宽。
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import duckdb
from penaltyblog.models import goal_expectancy

from goalx_backend import odds_math as om
from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore

# 联赛映射/era 常量经零依赖叶子引（corpus_gate/gold 传递依赖 data.ingest，
# 分层执法 evaluation 不可达——见 data/leagues.py 模块注释）
from goalx_backend.data.leagues import ERA_TRAJECTORY, LEAGUE_TO_FD
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS
from goalx_backend.modelling.dc_model import (
    DCArtifact,
    TrainingRow,
    fit_dc_model,
    implementation_versions,
)
from goalx_backend.modelling.score_matrix import ScoreMatrix
from goalx_backend.settlement import LegSpec, ResultFacts, settle_fixed_bet

logger = logging.getLogger(__name__)

# 十年窗（spec S13：2016-08→2026-10）与 11 重叠联赛（data/leagues 单一
# 真相源；2016-01~08 的 gold 历史行作首季训练热身池，不产生下注面）
DECADE_COMPETITIONS = tuple(sorted(LEAGUE_TO_FD.values()))
DECADE_START = "2016-08-01"

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

    competitions: tuple[str, ...] = DECADE_COMPETITIONS
    # kickoff 北京墙钟日窗口（含端点）；start 前的 gold 历史行只进训练池
    start: str = DECADE_START
    end: str | None = None
    haircut: float = 0.10
    ev_threshold: float = 0.015
    kelly_fraction: float = 0.25
    ref_bankroll: float = 5000.0
    stake_cap: float = 50.0
    half_life_days: float = 365.0
    min_train_matches: int = 100
    parlay2: bool = True
    max_sim_odds: float = DEFAULT_MAX_SIM_ODDS
    # 公允基准来源：auto=时代感知正典链（era1 PSC→AvgC / era2 锚→共识）；
    # psc/avgc = 只用该源（票 34 分期/来源对照 run 用，跨 era 作用于配对行）
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


@dataclass(frozen=True)
class GoldMatch:
    """gold match_features 一行的引擎投影（收盘族按候选链原样携带）。"""

    sid: str
    competition: str  # fd 联赛码
    match_date: str  # kickoff 北京墙钟日
    season: str
    home: str
    away: str
    fthg: int
    ftag: int
    era: str
    ref: int  # run 内整数寻址（结算内核）；审计面用 sid
    psc_odds: tuple[float | None, float | None, float | None]
    avgc_odds: tuple[float | None, float | None, float | None]
    anchor_odds: tuple[float | None, float | None, float | None]  # cid177 锚收盘
    cons_probs: tuple[float | None, float | None, float | None]  # 多书中位数归一


def match_week_key(match_date: str) -> str:
    """比赛周键（ISO 年-周）。"""
    iso = date.fromisoformat(match_date).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def group_by_week(rows: Sequence[GoldMatch]) -> list[tuple[str, list[GoldMatch]]]:
    """按 ISO 比赛周分组（周内按日期排序）。"""
    weeks: dict[str, list[GoldMatch]] = {}
    for row in rows:
        weeks.setdefault(match_week_key(row.match_date), []).append(row)
    return [
        (key, sorted(week, key=lambda r: r.match_date))
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


def _ftr(fthg: int, ftag: int) -> str:
    """全场胜负（H/D/A）——随预测行落库，指标计算不回查运行面。"""
    return "H" if fthg > ftag else ("A" if fthg < ftag else "D")


def _valid_odds(
    odds: tuple[float | None, float | None, float | None],
) -> tuple[float, float, float] | None:
    """
    收盘三元组校验并收窄：全部有限且 >1.0 才可用，否则 None。

    有限性必须显式判：NaN 满足 ``nan <= 1.0 == False``（否定式放行），
    而 parquet float64 可承载 NaN（旧 sqlite REAL 面不可）——漏进 Shin
    去水会让 scipy 抛 ValueError 炸整 run（correctness F1）。
    """
    vals: list[float] = []
    for o in odds:
        if not isinstance(o, (int, float)):
            return None
        value = float(o)
        if not math.isfinite(value) or value <= 1.0:
            return None
        vals.append(value)
    return vals[0], vals[1], vals[2]


def _valid_probs(
    probs: tuple[float | None, float | None, float | None],
) -> tuple[float, float, float] | None:
    """
    已归一概率三元组校验并收窄：全部有限且 ∈(0,1) 才可用，否则 None。

    链式 ``0.0 < p < 1.0`` 对 NaN 恒 False（比较链任一 False 即 False），
    天然拒绝非有限值。
    """
    vals: list[float] = []
    for p in probs:
        if not isinstance(p, (int, float)):
            return None
        value = float(p)
        if not math.isfinite(value) or not 0.0 < value < 1.0:
            return None
        vals.append(value)
    return vals[0], vals[1], vals[2]


Odds3 = tuple[float | None, float | None, float | None]


def era_fair_probs(
    era: str,
    psc_odds: Odds3,
    avgc_odds: Odds3,
    anchor_odds: Odds3,
    cons_probs: Odds3,
    *,
    fair_source: str = "auto",
) -> tuple[dict[str, float], str] | None:
    """
    收盘公允概率（时代分层正典链 / 源选择器；ADR-0007 修订）。

    纯数据入参版，票 15 解锁报告等消费面复用（语义单一落点，不散写
    第二份链）。

    - auto：era1 = PSC→AvgC（Shin 去水）；era2 = cid177 锚收盘（Shin）→
      多书中位数共识归一（traj_cons，构建侧已归一的概率直用）；
    - psc/avgc：只用该源（票 34 对照 run；跨 era 作用于配对行）。

    缺列/无效价 = 无基准返回 None。PSC 成熟度由 gold 快照承载：未成熟行
    odds 列整族置空（门=gold 构建一处，票 06），消费侧不二次判。
    """
    if fair_source not in ("auto", "psc", "avgc"):
        raise ValueError(f"未知 fair_source {fair_source!r}（合法: auto/psc/avgc）")
    candidates: tuple[tuple[Odds3, str], ...]
    if fair_source == "psc":
        candidates = ((psc_odds, "psc"),)
    elif fair_source == "avgc":
        candidates = ((avgc_odds, "avgc"),)
    elif era == ERA_TRAJECTORY:
        candidates = ((anchor_odds, "traj_anchor"), (cons_probs, "traj_cons"))
    else:
        candidates = ((psc_odds, "psc"), (avgc_odds, "avgc"))
    for quote, source in candidates:
        if source == "traj_cons":
            probs3 = _valid_probs(quote)
            if probs3 is not None:
                return dict(zip(SELECTIONS, probs3, strict=True)), source
        else:
            odds3 = _valid_odds(quote)
            if odds3 is not None:
                probs = om.shin_implied(odds3)
                return dict(zip(SELECTIONS, probs, strict=True)), source
    return None


def fair_probs_from_gold(
    match: GoldMatch, *, fair_source: str = "auto"
) -> tuple[dict[str, float], str] | None:
    """单场 gold 行的收盘公允概率（era 正典链委托 ``era_fair_probs``）。"""
    return era_fair_probs(
        match.era,
        match.psc_odds,
        match.avgc_odds,
        match.anchor_odds,
        match.cons_probs,
        fair_source=fair_source,
    )


def simulated_jc_odds(fair_probs: dict[str, float], haircut: float) -> dict[str, float]:
    """模拟竞彩价：fair 赔率 × (1 − haircut)。"""
    return {sel: (1.0 / p) * (1.0 - haircut) for sel, p in fair_probs.items()}


def market_implied_matrix(
    fair_probs: dict[str, float],
) -> ScoreMatrix | None:
    """
    从市场 fair 1X2 反推 (λ_home, λ_away) 构造市场隐含比分矩阵。

    仅作诊断/预留工具：实测其总进球维度系统性欠分散（见模块 docstring），
    不用于定价；真实 AH/OU 收盘线解锁判定 = 票 15（报告+用户点头）。
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

    match_ref: int  # 结算内核寻址（run 内唯一）
    match_key: str  # gold sid（审计面）
    market_code: str
    selection_code: str
    odds: float
    model_prob: float
    goal_line: float | None = None

    @property
    def ev(self) -> float:
        return self.model_prob * self.odds - 1.0


def fetch_decade_matches(
    duck_con: duckdb.DuckDBPyConnection, params: BacktestParams
) -> tuple[list[GoldMatch], str | None, str, int]:
    """
    Gold 引擎行集 → GoldMatch 投影（取数/卫生单一入口=``data.gold_reader``）。

    视图缺席（未建桥/空语料）= 合法初生态，返回 (空集, None, "missing", 0)
    由 run params 如实标注，不冒充零过滤数据；重复 sid 已在取数侧去重
    （计数返回入档，保持预测/下注/审计三面身份一致）。
    """
    rows, version, view_status, duplicates = gold_reader.fetch_decade_rows(
        duck_con, params.competitions, end=params.end
    )
    matches: list[GoldMatch] = []
    for ref, row in enumerate(rows):
        (
            sid,
            _league,
            day,
            season,
            home,
            away,
            fthg,
            ftag,
            era,
            *odds_cols,
            _row_version,
        ) = row
        matches.append(
            GoldMatch(
                sid=str(sid),
                competition=LEAGUE_TO_FD[str(_league)],
                match_date=str(day),
                season=str(season),
                home=str(home),
                away=str(away),
                fthg=int(fthg),
                ftag=int(ftag),
                era=str(era),
                ref=ref,
                psc_odds=(odds_cols[0], odds_cols[1], odds_cols[2]),
                avgc_odds=(odds_cols[3], odds_cols[4], odds_cols[5]),
                anchor_odds=(odds_cols[6], odds_cols[7], odds_cols[8]),
                cons_probs=(odds_cols[9], odds_cols[10], odds_cols[11]),
            )
        )
    return matches, version, view_status, duplicates


def _candidates_for_match(
    matrix: ScoreMatrix,
    jc_had: dict[str, float],
    params: BacktestParams,
    match: GoldMatch,
) -> list[_Candidate]:
    """一场比赛的全部候选单关（had-only，用户裁决维持到票 15 门）。"""
    candidates: list[_Candidate] = []
    had = matrix.had()
    for sel in SELECTIONS:
        market_prob = 1.0 / jc_had[sel]
        if market_prob >= MIN_MARKET_PROB and jc_had[sel] <= params.max_sim_odds:
            candidates.append(
                _Candidate(match.ref, match.sid, "had", sel, jc_had[sel], had[sel])
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
            fixture_id=leg.match_ref,
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
            json.dumps(
                [
                    {
                        "match_key": leg.match_key,
                        "market_code": leg.market_code,
                        "selection_code": leg.selection_code,
                        "odds": leg.odds,
                        "model_prob": leg.model_prob,
                        "goal_line": leg.goal_line,
                    }
                    for leg in legs
                ],
                ensure_ascii=False,
            ),
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


def _record_week_predictions(
    conn: sqlite3.Connection,
    run_id: int,
    week_rows: list[GoldMatch],
    artifact: DCArtifact,
    params: BacktestParams,
    result: BacktestResult,
) -> dict[int, list[_Candidate]]:
    """逐场落预测行并构造候选单关；返回 ref → candidates。"""

    def skip(key: str) -> None:
        result.skipped[key] = result.skipped.get(key, 0) + 1

    candidates_by_match: dict[int, list[_Candidate]] = {}
    for match in week_rows:
        if match.home not in artifact.teams or match.away not in artifact.teams:
            skip("untrained_teams")
            continue
        fair = fair_probs_from_gold(match, fair_source=params.fair_source)
        if fair is None:
            skip("no_fair_baseline")
            continue
        fair_probs, fair_source = fair
        try:
            matrix = artifact.predict(match.home, match.away)
        except ValueError:
            # 小训练池 ρ 数值越界 → 该场不产生预测/候选
            skip("predict_failed")
            continue
        had = matrix.had()
        conn.execute(
            """
            INSERT OR IGNORE INTO backtest_predictions
            (run_id, match_key, competition, season, match_date,
             home_team, away_team, era, ftr, had_probs, fair_probs,
             fair_source, model_fingerprint, train_window_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                match.sid,
                match.competition,
                match.season,
                match.match_date,
                match.home,
                match.away,
                match.era,
                _ftr(match.fthg, match.ftag),
                json.dumps(had),
                json.dumps(fair_probs),
                fair_source,
                artifact.data_fingerprint,
                artifact.train_window_end,
            ),
        )
        result.predictions += 1
        jc_had = simulated_jc_odds(fair_probs, params.haircut)
        candidates_by_match[match.ref] = _candidates_for_match(
            matrix, jc_had, params, match
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
    second = next((c for c in ordered[1:] if c.match_ref != first.match_ref), None)
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


def _validate_window(params: BacktestParams) -> None:
    """窗口日期 fail-loud（correctness F2：坏值不得冒充视图缺失/静默前视）。"""
    for name, value in (("start", params.start), ("end", params.end)):
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name}={value!r} 非法：须零填充 YYYY-MM-DD") from exc


def _walk_competition(
    conn: sqlite3.Connection,
    run_id: int,
    competition: str,
    comp_rows: list[GoldMatch],
    params: BacktestParams,
    result: BacktestResult,
) -> None:
    """单联赛 walk-forward：训练池推进 + 周重估 + 预测/下注。"""
    pool: list[TrainingRow] = []
    ptr = 0
    # ponytail: 每周全池重拟合（~11 联赛×~540 周，小时级 CLI 跑）；
    # 周间增量拟合是缓存投机，量级不适配再议
    for week_key, week_rows in group_by_week(comp_rows):
        week_dates = [m.match_date for m in week_rows]
        first_day = min(week_dates)
        cutoff = (date.fromisoformat(first_day) - timedelta(days=1)).isoformat()
        while ptr < len(comp_rows) and comp_rows[ptr].match_date <= cutoff:
            row = comp_rows[ptr]
            pool.append(
                TrainingRow(
                    match_date=row.match_date,
                    home_team=row.home,
                    away_team=row.away,
                    fthg=row.fthg,
                    ftag=row.ftag,
                )
            )
            ptr += 1
        # 窗口门逐场施加（correctness F4）：整周跳过会丢跨 start 周内的
        # 窗口内场次；cutoff 仍按整周最早日算（窗口前行也不在训练池——
        # 同周比赛一律晚于 cutoff）
        in_window = [m for m in week_rows if m.match_date >= params.start]
        pre_start = len(week_rows) - len(in_window)
        if pre_start:
            result.skipped["pre_start_matches"] = (
                result.skipped.get("pre_start_matches", 0) + pre_start
            )
        if not in_window:
            continue
        if len(pool) < params.min_train_matches:
            result.skipped["thin_train_weeks"] = (
                result.skipped.get("thin_train_weeks", 0) + 1
            )
            continue
        assert_no_lookahead(pool, week_dates)
        try:
            artifact = fit_dc_model(
                pool,
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
            m.ref: ResultFacts(home_goals=m.fthg, away_goals=m.ftag) for m in in_window
        }
        candidates_by_match = _record_week_predictions(
            conn, run_id, in_window, artifact, params, result
        )
        _place_week_bets(
            conn,
            _WeekContext(run_id, competition, week_key, results),
            candidates_by_match,
            params,
            result,
        )


def run_backtest(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    params: BacktestParams,
    *,
    label: str,
) -> BacktestResult:
    """
    执行一次完整回测（run 维度隔离，同参数重跑产生新 run）。

    结算实时完成（历史赛果已知）；逐注记录 EV/Kelly/盈亏，走与纸面/实盘
    相同的 ``settle_fixed_bet`` 代码路径。数据面 = gold 快照：run params
    入档 gold 版本/构建戳/输入 digest 与成熟度基准日（gold 构建侧钉的
    口径，非 run 日）——重放差异由该组字段解释。
    """
    _validate_window(params)
    matches, gold_version, view_status, duplicates = fetch_decade_matches(
        duck_con, params
    )
    # meta 后读（correctness F6）：行集与 _meta 两次独立读之间发生 gold
    # 重建时，版本不一致=溯源已漂移，警告暴露（独占窗口纪律下不应发生）
    meta = gold_reader.read_gold_meta(store)
    if (
        gold_version is not None
        and meta.get("gold_version") is not None
        and gold_version != meta.get("gold_version")
    ):
        logger.warning(
            "gold 版本漂移（行集 %s ≠ _meta %s）：重建与读取交错",
            gold_version,
            meta.get("gold_version"),
        )
    run_params = asdict(params) | {
        # 票 34：合成价实验明确标注，不得称真实陈盘回放；实现/依赖版本入档
        "price_model": "simulated_jc",
        "versions": implementation_versions(),
        "data_source": "gold/match_features",
        "gold_view": view_status,
        "gold_rows": len(matches),
        "gold_duplicate_sids": duplicates,
        "gold_version": gold_version or meta.get("gold_version"),
        "gold_built_at": meta.get("built_at"),
        "gold_maturity_today": meta.get("maturity_today"),
        "gold_input_digests": meta.get("input_digests"),
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
        by_comp: dict[str, list[GoldMatch]] = {}
        for match in matches:
            by_comp.setdefault(match.competition, []).append(match)
        for competition, comp_rows in by_comp.items():
            _walk_competition(conn, run_id, competition, comp_rows, params, result)
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
