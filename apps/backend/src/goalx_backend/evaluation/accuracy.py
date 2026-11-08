"""
前瞻准确率评分与聚合报告（auto-paper-20261106 票 02，prereg A 节 acc-v1）。

评分集合 = 每业务日全部竞彩在售场次中 ML 轨覆盖者（含未下注场次，评分
集合与投注集合分离）；Forecast 取法 = 赛前最新一条（latest_forecast_asof，
前瞻冻结同口径）。逐场逐玩法落评分行：

- had/hhad：RPS（hhad 仅整数线；非整数线/无线进 skip_reason 行=覆盖账
  可见，不删行）；
- ttg(8桶)/crs(31格)/hafu(9)：多类 Brier + log-loss（hafu 需半场比分，
  缺半场 → skip_reason 行）；
- 双基准 = 票 01 冻结快照（jc_official 归一化 + eu_consensus），赛前
  最近锚同窗；基准缺失 = 该场该玩法 unknown 差值（NULL），不删行；
- 派生概率自冻结矩阵 payload 即时计算（ADR-0006），不落派生态——评分
  行只落指标值与 pred_prob_outcome（模型赋给实现结果的概率，校准与
  诊断输入）；报告侧校准分箱同样自 payload 即时派生。

幂等：UNIQUE(fixture_id, market, score_version) 吸收同结果重跑；结果更正
通道重评 = result_fingerprint 变化后覆盖（append-only 语义的更正例外，
prereg 冻结）。判读规则文字随报告展示（n≥1800 且 |差值|>2SE 才可定论），
禁止读数后改口径。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from statistics import fmean, pstdev
from typing import Any

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.db import utc_now_iso
from goalx_backend.evaluation import metrics
from goalx_backend.evaluation.baseline_freeze import baseline_probs_asof
from goalx_backend.markets import (
    CRS_EXACT_SCORES,
    CRS_SELECTIONS,
    HAFU_SELECTIONS,
    SELECTIONS,
    TTG_SELECTIONS,
)
from goalx_backend.modelling.forecast import (
    coverage_census_by_month,
    coverage_months,
    forecast_matrix_from_payload,
    forecast_payloads_by_ids,
    latest_forecast_asof,
)

_CRS_EXACT_SET = frozenset(CRS_EXACT_SCORES)

# prereg C：评分版本；口径改动=acc-v2 另起，历史行永不改写
SCORE_VERSION = "acc-v1"
# 判读规则冻结（prereg A）：定论读数门槛
VERDICT_MIN_N = 1800
_CALIB_BINS = 10
_MARKETS: tuple[str, ...] = ("had", "hhad", "ttg", "crs", "hafu")
_MARKET_ORDER: dict[str, tuple[str, ...]] = {
    "had": SELECTIONS,
    "hhad": SELECTIONS,
    "ttg": TTG_SELECTIONS,
    "crs": CRS_SELECTIONS,
    "hafu": HAFU_SELECTIONS,
}

READING_RULE = (
    "月读只描述趋势不改口径；三结局 RPS 单指标 SE≈0.084/√n，差值门槛用"
    "逐场配对差值的样本 SE（std/√n）；定论读数=单玩法累计 n≥1800（约一"
    "季度）且 |差值|>2SE（任一方向均可定论：负=模型胜市场，正=模型输市"
    "场）——未达标只报不判，禁止读数后改口径。差值为 模型−基准，负值="
    "模型损失更低（更好）。"
)


def _kickoff_as_of(row: sqlite3.Row) -> str | None:
    """
    Kickoff 规范化为 aware UTC ISO（kickoff_to_utc 单源）。

    legacy naive=北京墙钟；不可解析 None——PIT 窗不可证即不评分。
    """
    kickoff_dt = fx_store.kickoff_to_utc(str(row["kickoff_utc"]))
    return kickoff_dt.isoformat() if kickoff_dt is not None else None


def _sign_code(home: int, away: int) -> str:
    if home > away:
        return "h"
    if home < away:
        return "a"
    return "d"


def _crs_outcome(home: int, away: int) -> str:
    if (home, away) in _CRS_EXACT_SET:
        return f"{home}:{away}"
    return f"{_sign_code(home, away)}_other"


def _result_fingerprint(row: sqlite3.Row) -> str:
    half = ""
    if row["half_home_goals"] is not None and row["half_away_goals"] is not None:
        half = f"|h{row['half_home_goals']}:{row['half_away_goals']}"
    return f"{row['home_goals']}:{row['away_goals']}{half}"


@dataclass
class ScoreStats:
    """一次评分运行的计数面（晨检/日志用）。"""

    scored: int = 0  # 新落/覆盖的指标行数（含 skip_reason 行）
    absorbed: int = 0  # 同结果重跑整场吸收（五玩法齐且指纹一致）
    rescored: int = 0  # 结果更正后的覆盖重评场数
    skipped_no_forecast: int = (
        0  # 赛前无 ML Forecast（覆盖账由 forecast_coverage 承担）
    )
    skipped_hhad_line: int = 0  # hhad 非整数线/无线（skip_reason 行）
    skipped_hafu_half: int = 0  # hafu 缺半场比分（skip_reason 行）
    skipped_invalid_kickoff: int = 0  # kickoff 不可解析（PIT 窗不可证，不评分）
    skipped_bad_payload: int = 0  # Forecast payload 畸形（跳过该场不炸整链）


def _hhad_goal_line(
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> float | None:
    """赛前最新 hhad 让球线（odds meta 存证，PIT=as_of；线重发时后到者胜）。"""
    evidence = fx_store.latest_odds_evidence_asof(
        conn, fixture_id, "hhad", as_of, source="sporttery"
    )
    best: tuple[str, float] | None = None
    for row in evidence.values():
        if row["meta"] is None:
            continue
        try:
            raw = json.loads(str(row["meta"])).get("goal_line")
            line = float(str(raw))
        except (TypeError, ValueError):
            continue
        seen = str(row["seen_at"])
        if best is None or seen > best[0]:
            best = (seen, line)
    return best[1] if best is not None else None


def _upsert_score(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    columns = ",".join(values)
    placeholders = ",".join("?" * len(values))
    conn.execute(
        f"""
        INSERT INTO forecast_scores ({columns})
        VALUES ({placeholders})
        ON CONFLICT (fixture_id, market, score_version) DO UPDATE SET
            month_key=excluded.month_key, kickoff_utc=excluded.kickoff_utc,
            competition=excluded.competition, forecast_id=excluded.forecast_id,
            model_metric=excluded.model_metric,
            model_logloss=excluded.model_logloss,
            jc_baseline_metric=excluded.jc_baseline_metric,
            eu_baseline_metric=excluded.eu_baseline_metric,
            diff_jc=excluded.diff_jc, diff_eu=excluded.diff_eu,
            pred_prob_outcome=excluded.pred_prob_outcome,
            outcome=excluded.outcome,
            hhad_goal_line=excluded.hhad_goal_line,
            skip_reason=excluded.skip_reason,
            result_fingerprint=excluded.result_fingerprint,
            scored_at=excluded.scored_at
        """,  # noqa: S608 — 列名模块内常量，值全参数化
        tuple(values.values()),
    )


def _base_values(row: sqlite3.Row, forecast_id: int, now: str) -> dict[str, Any]:
    return {
        "fixture_id": int(row["fixture_id"]),
        "market": "had",  # 各市场函数覆盖
        "month_key": str(row["business_date"])[:7],  # 业务日月=覆盖账同钟面
        "kickoff_utc": str(row["kickoff_utc"]),
        "competition": str(row["competition"]),
        "score_version": SCORE_VERSION,
        "forecast_id": forecast_id,
        "model_metric": None,
        "model_logloss": None,
        "jc_baseline_metric": None,
        "eu_baseline_metric": None,
        "diff_jc": None,
        "diff_eu": None,
        "pred_prob_outcome": None,
        "outcome": "",
        "hhad_goal_line": None,
        "skip_reason": None,
        "result_fingerprint": _result_fingerprint(row),
        "scored_at": now,
    }


def _attach_baselines(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    fixture_id: int,
    as_of: str,
    outcome: str,
) -> None:
    """Had 双锚同窗差值（票 01 快照；缺基准=unknown→NULL 不删行）。"""
    outcome_idx = SELECTIONS.index(outcome)
    for source, metric_col, diff_col in (
        ("jc_official", "jc_baseline_metric", "diff_jc"),
        ("eu_consensus", "eu_baseline_metric", "diff_eu"),
    ):
        baseline = baseline_probs_asof(conn, fixture_id, source, as_of)
        if baseline is None:
            continue
        baseline_metric = metrics.rps(baseline[1], outcome_idx)
        values[metric_col] = baseline_metric
        values[diff_col] = values["model_metric"] - baseline_metric


def _score_wdl(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    forecast_id: int,
    probs: dict[str, float],
    outcome: str,
    now: str,
    hhad_goal_line: float | None = None,
) -> None:
    """had/hhad 共用三向 RPS 落行（hhad 基准缺=票 01 只冻 had，unknown 差值）。"""
    values = _base_values(row, forecast_id, now)
    vector = tuple(probs[sel] for sel in SELECTIONS)
    values.update(
        market="had" if hhad_goal_line is None else "hhad",
        hhad_goal_line=hhad_goal_line,
        model_metric=metrics.rps(vector, SELECTIONS.index(outcome)),
        pred_prob_outcome=probs[outcome],
        outcome=outcome,
    )
    if hhad_goal_line is None:
        as_of = _kickoff_as_of(row)
        if as_of is not None:
            _attach_baselines(conn, values, int(row["fixture_id"]), as_of, outcome)
    _upsert_score(conn, values)


def _score_multiclass(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    forecast_id: int,
    probs: dict[str, float],
    outcome: str,
    market: str,
    now: str,
) -> None:
    """多类玩法（ttg/crs/hafu）：Brier + log-loss（基准只冻 had → NULL 差值）。"""
    order = _MARKET_ORDER[market]
    values = _base_values(row, forecast_id, now)
    vector = tuple(probs[code] for code in order)
    idx = order.index(outcome)
    values.update(
        market=market,
        model_metric=metrics.multiclass_brier(vector, idx),
        model_logloss=metrics.log_loss(vector, idx),
        pred_prob_outcome=probs[outcome],
        outcome=outcome,
    )
    _upsert_score(conn, values)


@dataclass
class _ScoreState:
    """一场的已落评分态（吸收门槛三要素）。"""

    fingerprint: str
    markets: set[str]
    hhad_skip: bool


def _score_fixture(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    forecast: sqlite3.Row,
    as_of: str,
    now: str,
    stats: ScoreStats,
) -> None:
    """
    一场五玩法落行。

    correctness 修：让球线取 seen_at 最新（线重发后到者胜）；畸形 payload
    只跳该场不炸结果链。
    """
    try:
        matrix = forecast_matrix_from_payload(json.loads(str(forecast["payload"])))
    except (ValueError, KeyError, TypeError):
        stats.skipped_bad_payload += 1
        return
    forecast_id = int(forecast["id"])
    home, away = int(row["home_goals"]), int(row["away_goals"])
    # had：双基准差值（票 01 快照同窗）
    _score_wdl(conn, row, forecast_id, matrix.had(), _sign_code(home, away), now)
    # hhad：仅整数线（prereg A）；非整数/无线=skip_reason 行
    goal_line = _hhad_goal_line(conn, int(row["fixture_id"]), as_of)
    if goal_line is None or goal_line != int(goal_line):
        values = _base_values(row, forecast_id, now)
        values.update(
            market="hhad",
            hhad_goal_line=goal_line,
            skip_reason="non_integer_line" if goal_line is not None else "no_line",
        )
        _upsert_score(conn, values)
        stats.skipped_hhad_line += 1
    else:
        _score_wdl(
            conn,
            row,
            forecast_id,
            matrix.hhad(goal_line),
            _sign_code(int(home + goal_line), away),
            now,
            hhad_goal_line=goal_line,
        )
    _score_multiclass(
        conn, row, forecast_id, matrix.ttg(), str(min(home + away, 7)), "ttg", now
    )
    _score_multiclass(
        conn, row, forecast_id, matrix.crs(), _crs_outcome(home, away), "crs", now
    )
    # hafu：需半场比分（源T 物化无半场 → skip_reason 行）
    if row["half_home_goals"] is None or row["half_away_goals"] is None:
        values = _base_values(row, forecast_id, now)
        values.update(market="hafu", skip_reason="no_half_score")
        _upsert_score(conn, values)
        stats.skipped_hafu_half += 1
    else:
        half_code = _sign_code(int(row["half_home_goals"]), int(row["half_away_goals"]))
        outcome = f"{half_code}{_sign_code(home, away)}"
        _score_multiclass(conn, row, forecast_id, matrix.hafu(), outcome, "hafu", now)
    stats.scored += len(_MARKETS)


def score_settled_fixtures(
    conn: sqlite3.Connection, *, now_iso: str | None = None
) -> ScoreStats:
    """
    对已结算竞彩且 ML 覆盖的场次逐玩法评分（幂等，acc-v1）。

    挂夜班结果链后（tasks.draw_results_sync）；重跑同结果被吸收，结果
    更正后 fingerprint 变化触发覆盖重评。吸收门槛=指纹一致且五玩法齐且
    hhad 无 skip 行——让球线回填不改指纹，欠线行须留重评口；hafu 缺半场
    不拦（半场 None→有值改指纹自然触发重评）。
    """
    stats = ScoreStats()
    now = now_iso or utc_now_iso()
    existing: dict[int, _ScoreState] = {}
    for score in conn.execute(
        """
        SELECT fixture_id, result_fingerprint, market, skip_reason
        FROM forecast_scores WHERE score_version = ?
        """,
        (SCORE_VERSION,),
    ).fetchall():
        fid = int(score["fixture_id"])
        state = existing.setdefault(
            fid, _ScoreState(str(score["result_fingerprint"]), set(), False)
        )
        state.markets.add(str(score["market"]))
        state.hhad_skip = state.hhad_skip or (
            score["market"] == "hhad" and score["skip_reason"] is not None
        )

    # 一场多卡（改期重出卡）：取业务日最大的一行——月归属确定（correctness F1）
    by_fixture: dict[int, sqlite3.Row] = {}
    for row in rs_store.settled_jingcai_fixtures(conn):
        fid = int(row["fixture_id"])
        held = by_fixture.get(fid)
        if held is None or str(row["business_date"]) > str(held["business_date"]):
            by_fixture[fid] = row

    for row in by_fixture.values():
        fixture_id = int(row["fixture_id"])
        as_of = _kickoff_as_of(row)
        if as_of is None:
            stats.skipped_invalid_kickoff += 1
            continue
        fingerprint = _result_fingerprint(row)
        done = existing.get(fixture_id)
        if (
            done is not None
            and done.fingerprint == fingerprint
            and done.markets == set(_MARKETS)
            and not done.hhad_skip
        ):
            stats.absorbed += 1
            continue
        forecast = latest_forecast_asof(conn, fixture_id, as_of, track="ml")
        if forecast is None:
            stats.skipped_no_forecast += 1
            continue
        if done is not None and done.fingerprint != fingerprint:
            stats.rescored += 1
        _score_fixture(conn, row, forecast, as_of, now, stats)
    conn.commit()
    return stats


# ---- 聚合报告（只读派生面：报告与 API/页面/晨检对账同源） ----


@dataclass(frozen=True)
class CoverageRow:
    """覆盖率首行（与 forecast_coverage 同源对账）。"""

    month: str
    total: int  # 在售（业务日竞彩场）数
    covered: int
    not_fd_league: int
    no_model: int
    no_mapping: int
    unknown: int


@dataclass(frozen=True)
class CalibBin:
    """一个校准箱：模型最热向的置信度 vs 实际命中率（报告时自 payload 派生）。"""

    bin_index: int
    n: int
    mean_conf: float
    hit_rate: float


@dataclass(frozen=True)
class MarketRow:
    """期次(月)×玩法×联赛 一行（competition=None 为该月该玩法总体行）。"""

    month: str
    market: str
    competition: str | None
    metric_kind: str  # rps | brier_logloss
    n: int
    skipped: int  # skip_reason 行数（hhad 非整数线/无半场）
    model_metric: float | None  # 全 skip 组=None（诚实，不伪造 0）
    model_logloss: float | None
    jc_baseline: float | None
    eu_baseline: float | None
    diff_jc: float | None
    diff_jc_se: float | None
    diff_eu: float | None
    diff_eu_se: float | None
    n_baseline: int
    cum_n_baseline: int  # 单玩法累计对照分母（prereg 定论门槛用；联赛行=自身 n）
    verdict_ready: bool  # 累计门槛：n≥1800 且 |diff|>2SE（仅总体行可判）
    calibration: tuple[CalibBin, ...]


@dataclass(frozen=True)
class AccuracyReport:
    """准确率报告（月块新在前；判读规则文字冻结随报告展示）。"""

    reading_rule: str
    months: tuple[tuple[CoverageRow, tuple[MarketRow, ...]], ...]


def _mean_or_none(values: list[float]) -> float | None:
    return round(fmean(values), 6) if values else None


_MIN_SE_SAMPLES = 2


def _se(values: list[float]) -> float | None:
    """逐行样本标准误（std/√n）；样本不足或零方差 None（不伪造分辨力）。"""
    if len(values) < _MIN_SE_SAMPLES:
        return None
    sd = pstdev(values)
    return round(sd / math.sqrt(len(values)), 6) if sd > 0 else None


def _calibration(
    rows: list[sqlite3.Row], payloads: dict[int, str]
) -> tuple[CalibBin, ...]:
    """
    校准分箱：模型最热向置信度 vs 实际命中频率（10 箱）。

    每评分行自 payload 即时派生概率向量（ADR-0006，不入库），取最热向
    置信度分箱，命中=最热向==实现向。payload 缺席（forecast 被物理清理
    等）→ 该行不进校准（指标行照报）。
    """
    bins: dict[int, tuple[list[float], list[float]]] = {}
    for row in rows:
        payload = payloads.get(int(row["forecast_id"]))
        if payload is None or row["outcome"] == "":
            continue
        market = str(row["market"])
        matrix = forecast_matrix_from_payload(json.loads(payload))
        if market == "had":
            probs: dict[str, float] = matrix.had()
        elif market == "hhad" and row["hhad_goal_line"] is not None:
            probs = matrix.hhad(float(row["hhad_goal_line"]))
        elif market == "ttg":
            probs = matrix.ttg()
        elif market == "crs":
            probs = matrix.crs()
        else:
            probs = matrix.hafu()
        pick = max(probs, key=lambda code: probs[code])
        conf = probs[pick]
        bin_index = min(int(conf * _CALIB_BINS), _CALIB_BINS - 1)
        confs, hits = bins.setdefault(bin_index, ([], []))
        confs.append(conf)
        hits.append(1.0 if pick == str(row["outcome"]) else 0.0)
    return tuple(
        CalibBin(
            bin_index=bin_index,
            n=len(confs),
            mean_conf=round(fmean(confs), 6),
            hit_rate=round(fmean(hits), 6),
        )
        for bin_index, (confs, hits) in sorted(bins.items())
    )


def _market_row(
    month: str,
    market: str,
    competition: str | None,
    rows: list[sqlite3.Row],
    payloads: dict[int, str],
    cumulative: tuple[int, bool] = (0, False),
) -> MarketRow:
    """
    一行聚合；累计门槛输入只喂总体行（prereg 单玩法累计口径）。

    cumulative=(累计对照分母, 累计门槛是否达标)——定论门槛按 prereg 是
    单玩法**累计** n≥1800（按月看永远不可达），联赛行不单独判。
    """
    scored = [r for r in rows if r["skip_reason"] is None]
    diffs_jc = [float(r["diff_jc"]) for r in scored if r["diff_jc"] is not None]
    diffs_eu = [float(r["diff_eu"]) for r in scored if r["diff_eu"] is not None]
    diff_jc, diff_jc_se = _mean_or_none(diffs_jc), _se(diffs_jc)
    diff_eu, diff_eu_se = _mean_or_none(diffs_eu), _se(diffs_eu)
    n_baseline = max(len(diffs_jc), len(diffs_eu))
    return MarketRow(
        month=month,
        market=market,
        competition=competition,
        metric_kind="rps" if market in ("had", "hhad") else "brier_logloss",
        n=len(scored),
        skipped=len(rows) - len(scored),
        model_metric=_mean_or_none([float(r["model_metric"]) for r in scored]),
        model_logloss=_mean_or_none(
            [
                float(r["model_logloss"])
                for r in scored
                if r["model_logloss"] is not None
            ]
        ),
        jc_baseline=_mean_or_none(
            [
                float(r["jc_baseline_metric"])
                for r in scored
                if r["jc_baseline_metric"] is not None
            ]
        ),
        eu_baseline=_mean_or_none(
            [
                float(r["eu_baseline_metric"])
                for r in scored
                if r["eu_baseline_metric"] is not None
            ]
        ),
        diff_jc=diff_jc,
        diff_jc_se=diff_jc_se,
        diff_eu=diff_eu,
        diff_eu_se=diff_eu_se,
        n_baseline=n_baseline,
        cum_n_baseline=cumulative[0] if competition is None else n_baseline,
        verdict_ready=cumulative[1] if competition is None else False,
        calibration=_calibration(scored, payloads),
    )


def _coverage_row(conn: sqlite3.Connection, month: str) -> CoverageRow:
    """月覆盖首行：forecast_coverage 逐业务日汇总（modelling 所属读，同源对账）。"""
    return CoverageRow(month=month, **coverage_census_by_month(conn, month))


def build_accuracy_report(conn: sqlite3.Connection) -> AccuracyReport:
    """月块聚合：覆盖率首行 + 月×玩法（总体行在前）×联赛拆解。"""
    score_rows = [
        row
        for row in conn.execute(
            """
            SELECT month_key, market, competition, fixture_id, forecast_id,
                   model_metric, model_logloss, jc_baseline_metric,
                   eu_baseline_metric, diff_jc, diff_eu, pred_prob_outcome,
                   outcome, hhad_goal_line, skip_reason
            FROM forecast_scores WHERE score_version = ?
            """,
            (SCORE_VERSION,),
        ).fetchall()
        # void 场次剔除（ADR-0001 真值=draw_results；评分行 append-only
        # 不删，报告与累计门槛只数未作废场）
        if int(row["fixture_id"]) not in rs_store.voided_fixture_ids(conn)
    ]
    payloads = forecast_payloads_by_ids(
        conn, sorted({int(r["forecast_id"]) for r in score_rows})
    )
    by_month: dict[str, dict[tuple[str, str | None], list[sqlite3.Row]]] = {}
    for row in score_rows:
        month = str(row["month_key"])
        groups = by_month.setdefault(month, {})
        groups.setdefault((str(row["market"]), None), []).append(row)
        groups.setdefault((str(row["market"]), str(row["competition"])), []).append(row)
    # 累计定论门槛（prereg：单玩法累计 n≥1800 且 |差|>2SE）：跨月池化逐行
    # 差值算总体行的门槛输入——按月看永远不可达，季度才可判
    cumulative: dict[str, tuple[int, bool]] = {}
    for market in _MARKETS:
        scored = [
            r
            for r in score_rows
            if str(r["market"]) == market and r["skip_reason"] is None
        ]
        diffs = [
            [float(r[col]) for r in scored if r[col] is not None]
            for col in ("diff_jc", "diff_eu")
        ]
        n_cum = max(len(d) for d in diffs)
        ready = any(
            n_cum >= VERDICT_MIN_N
            and len(d) >= VERDICT_MIN_N
            and (mean := _mean_or_none(d)) is not None
            and (se := _se(d)) is not None
            and abs(mean) > 2 * se
            for d in diffs
        )
        cumulative[market] = (n_cum, ready)
    # 月块=评分月∪覆盖月（覆盖先行、评分未到的月也出块——首行对账不断档）
    months: list[tuple[CoverageRow, tuple[MarketRow, ...]]] = []
    for month in sorted({*by_month, *coverage_months(conn)}, reverse=True):
        rows_out = tuple(
            _market_row(
                month,
                market,
                competition,
                group,
                payloads,
                cumulative=cumulative.get(market, (0, False)),
            )
            for (market, competition), group in sorted(
                by_month.get(month, {}).items(),
                key=lambda kv: (kv[0][0], kv[0][1] or ""),
            )
        )
        months.append((_coverage_row(conn, month), rows_out))
    return AccuracyReport(reading_rule=READING_RULE, months=tuple(months))
