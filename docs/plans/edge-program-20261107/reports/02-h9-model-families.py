#!/usr/bin/env python3
# 票 02（edge-program-20261107）：H9 模型分族离线检验——胜负族 vs 进球族。
#
# 纯离线只读研究脚本（analysis_03 先例）。产出打印 stdout，供
# 02-h9-model-families.md 引用。
#
# 问题（H9 注册表行）：单矩阵架构把 (λh,λa,ρ) 误差传染全部玩法；独立
# 进球族估计器是否优于矩阵派生 ttg 视图。
#
# 对比面（同期同分母 = 同一批场上逐一配对）：
#   A. 矩阵 ttg 视图 = 生产 canonical 市场侧联合矩阵（票 23 转正路径：
#      avgc 1X2 fair（Shin）+ FD OU2.5 收盘 → joint_lambdas → ScoreMatrix.ttg()）。
#      注：模型侧 DC 矩阵不重跑十年 walk——其 1X2 面已十年否定（RPS −0.04，
#      D 组五面一致），ttg 面是同一 λ 机器的下游；市场侧矩阵是当前生产
#      ttg 定价面，为「单矩阵」的最强代表。
#   B. 独立 totals 估计器·市场版：Poisson GLM（scipy L-BFGS-B，log 链，
#      xg_dc.py 同款自实现），特征 = OU 市场面（fd_avgc_ou 去水 p_over、
#      srct OU 收盘线），周度 walk-forward。
#   C. 独立 totals 估计器·全特征（票文主估计器）：B + understat npxG
#      双队滚动速率（严格 PIT：只取开球更早的场次，每队 ≥6 场带 xg 史）。
# 指标：ttg 八档（0..7+）多类 Brier（配对差 ± SE）+ 分档校准分箱。
# 门（H9 注册表）：C 对 A 的 Brier 差 > 2SE 才支持分族（开 ADR-0006
# 修订票），否则 refuted 留痕。

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

import duckdb
import numpy as np
from penaltyblog.models import goal_expectancy
from scipy.optimize import minimize

from goalx_backend import odds_math as om
from goalx_backend.evaluation.backtest import joint_lambdas
from goalx_backend.modelling.score_matrix import ScoreMatrix

DUCK = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
TTG_CLASSES = list(range(8))  # 0..6 精确 + 7=7+
MIN_TEAM_XG_HISTORY = 6
MIN_TRAIN_ROWS = 400
ROLLING_N = 12  # npxG 滚动窗口（场）


def load_universe() -> list[dict]:
    con = duckdb.connect(DUCK, read_only=True)
    rows = con.sql(
        """
        select sid, league, kickoff, home, away, home_goals, away_goals, season,
               avgc_home, avgc_draw, avgc_away,
               fd_avgc_ou_over, fd_avgc_ou_under,
               ou_close_line_med, ou_close_over_water_med, ou_close_under_water_med,
               xg_home, xg_away
        from match_features
        where home_goals is not null and admin_excluded = 0
          and kickoff >= '2019-08-01' and kickoff < '2026-08-01'
          and avgc_home is not null
          and fd_avgc_ou_over is not null and fd_avgc_ou_under is not null
        order by kickoff
        """
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        # fd OU = 2.5 线：over/under 收盘价去水 → p_over
        inv_o, inv_u = 1.0 / r[11], 1.0 / r[12]
        p_over_fd = inv_o / (inv_o + inv_u) if inv_o + inv_u > 0 else None
        p_over_srct = None
        if r[14] and r[15] and r[14] > 1.0 and r[15] > 1.0:
            io, iu = 1.0 / r[15], 1.0 / r[14]
            if io + iu > 0:
                p_over_srct = iu / (io + iu)
        out.append(
            {
                "sid": r[0],
                "league": r[1],
                "kickoff": r[2],
                "home": r[3],
                "away": r[4],
                "total": r[5] + r[6],
                "season": r[7],
                "avgc": (r[8], r[9], r[10]),
                "p_over_fd": p_over_fd,
                "line_srct": r[13],
                "p_over_srct": p_over_srct,
                "xg": (r[16], r[17]) if r[16] is not None and r[17] is not None else None,
            }
        )
    return out


# ---------- Poisson GLM（scipy，xg_dc.py 同款模式） ----------


def poisson_nll(beta: np.ndarray, X: np.ndarray, y: np.ndarray) -> float:
    eta = np.clip(X @ beta, -8.0, 8.0)  # 数值护栏
    lam = np.exp(eta)
    return float(np.sum(lam - y * np.log(np.maximum(lam, 1e-10))))


def fit_poisson(X: np.ndarray, y: np.ndarray) -> np.ndarray | None:
    if len(y) < MIN_TRAIN_ROWS:
        return None
    beta0 = np.zeros(X.shape[1])
    beta0[0] = math.log(max(y.mean(), 0.5))
    res = minimize(poisson_nll, beta0, args=(X, y), method="L-BFGS-B")
    return res.x if res.success else None


def poisson_ttg(lam: float) -> np.ndarray:
    logp = np.array(
        [-lam + k * math.log(max(lam, 1e-10)) - math.lgamma(k + 1) for k in TTG_CLASSES]
    )
    p = np.exp(logp)
    p[7] = 1.0 - p[:7].sum()  # 7+ 尾
    return np.clip(p, 0.0, 1.0)


def feats_market(m: dict) -> np.ndarray | None:
    if m["p_over_fd"] is None or m["line_srct"] is None:
        return None
    return np.array([1.0, m["p_over_fd"], float(m["line_srct"])])


def feats_full(m: dict, npxg_total: float | None) -> np.ndarray | None:
    base = feats_market(m)
    if base is None or npxg_total is None or npxg_total <= 0:
        return None
    return np.append(base, math.log(npxg_total))


# ---------- npxG 滚动特征（严格 PIT：只用开球更早的场次） ----------


def build_npxg_proxy(universe: list[dict]) -> dict[str, float | None]:
    """每场 npxG 总量代理 = 主/客两队各自「过去带 xg 场次的场均 npxG 总量」的均值。"""
    hist: dict[str, list[float]] = defaultdict(list)
    proxy: dict[str, float | None] = {}
    for m in universe:
        vals = []
        for team in (m["home"], m["away"]):
            h = hist[team]
            if len(h) >= MIN_TEAM_XG_HISTORY:
                vals.append(float(np.mean(h[-ROLLING_N:])))
        proxy[m["sid"]] = float(np.mean(vals)) if len(vals) == 2 else None
        if m["xg"] is not None:
            total_xg = m["xg"][0] + m["xg"][1]
            hist[m["home"]].append(total_xg)
            hist[m["away"]].append(total_xg)
    return proxy


# ---------- 基线 A：生产 market-λ 联合矩阵 ----------


def matrix_ttg(m: dict) -> np.ndarray | None:
    fair3 = om.shin_implied(m["avgc"])
    fair = {"h": fair3[0], "d": fair3[1], "a": fair3[2]}
    raw = goal_expectancy(fair["h"], fair["d"], fair["a"], max_goals=10)
    if not raw["success"]:
        return None
    x0 = (float(raw["home_exp"]), float(raw["away_exp"]))
    joint = joint_lambdas(fair, 2.5, m["p_over_fd"], x0=x0)
    if joint is None:
        return None
    probs = ScoreMatrix.from_lambdas(joint[0], joint[1]).ttg()
    vec = np.array([probs[str(k)] for k in TTG_CLASSES])
    if vec.sum() <= 0:
        return None
    return vec / vec.sum()


def brier(p: np.ndarray, total: int) -> float:
    y = np.zeros(8)
    y[min(total, 7)] = 1.0
    return float(((p - y) ** 2).sum())


def main() -> None:
    universe = load_universe()
    proxy = build_npxg_proxy(universe)
    print("=" * 76)
    print("H9 分族检验：矩阵 ttg 视图（市场侧联合矩阵） vs 独立 totals GLM")
    print("=" * 76)
    n_proxy = sum(1 for m in universe if proxy.get(m["sid"]) is not None)
    print(
        f"宇宙（avgc+FD OU2.5 收盘在场，2019-08…2026-07）: {len(universe)} 场；"
        f"npxG 双队史可用 {n_proxy}"
    )

    # 周度 walk-forward（ISO 周一界）：周内预测 → 周后入训（冻结纪律）
    def week_key(m: dict) -> str:
        d = m["kickoff"].date() if isinstance(m["kickoff"], dt.datetime) else m["kickoff"]
        return (d - dt.timedelta(days=d.weekday())).isoformat()

    by_week: dict[str, list[dict]] = defaultdict(list)
    for m in universe:
        by_week[week_key(m)].append(m)

    Xb_train: list[np.ndarray] = []
    Xc_train: list[np.ndarray] = []
    yb_train: list[float] = []
    yc_train: list[float] = []
    beta_b = beta_c = None
    records: list[dict] = []
    for w in sorted(by_week):
        week_rows = by_week[w]
        preds = []
        for m in week_rows:
            pb, pc = feats_market(m), feats_full(m, proxy.get(m["sid"]))
            pa = matrix_ttg(m)
            if pb is None or pc is None or pa is None:
                continue
            pred_b = poisson_ttg(float(np.exp(pb @ beta_b))) if beta_b is not None else None
            pred_c = poisson_ttg(float(np.exp(pc @ beta_c))) if beta_c is not None else None
            preds.append((m, pa, pred_b, pred_c))
        for m in week_rows:  # 本周冻结后入训（B/C 各自的特征面）
            pb, pc = feats_market(m), feats_full(m, proxy.get(m["sid"]))
            if pb is not None:
                Xb_train.append(pb)
                yb_train.append(float(m["total"]))
            if pc is not None:
                Xc_train.append(pc)
                yc_train.append(float(m["total"]))
        if len(yb_train) >= MIN_TRAIN_ROWS:
            new_b = fit_poisson(np.array(Xb_train), np.array(yb_train))
            if new_b is not None:
                beta_b = new_b
        if len(yc_train) >= MIN_TRAIN_ROWS:
            new_c = fit_poisson(np.array(Xc_train), np.array(yc_train))
            if new_c is not None:
                beta_c = new_c
        for m, pa, pred_b, pred_c in preds:
            if pred_c is None:
                continue  # 分母=C 可预测场（配对同分母）
            records.append(
                {
                    "m": m,
                    "bA": brier(pa, m["total"]),
                    "bB": brier(pred_b, m["total"]) if pred_b is not None else None,
                    "bC": brier(pred_c, m["total"]),
                    "pA": pa,
                    "pC": pred_c,
                }
            )

    print(f"配对同分母样本: {len(records)} 场（C 面可预测 = 双队 npxG 史 + 训练预热 ≥{MIN_TRAIN_ROWS} 行）")
    if len(records) < 500:
        print("样本不足 500——探索性判读！")

    def report(name: str, key: str) -> None:
        sub = [r for r in records if r[key] is not None]
        if not sub:
            return
        vals = np.array([r[key] for r in sub])
        bA = np.array([r["bA"] for r in sub])
        d = bA - vals
        se = d.std(ddof=1) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
        z = d.mean() / se if se > 0 else float("nan")
        print(
            f"[{name}] Brier {vals.mean():.5f} vs 矩阵 {bA.mean():.5f}："
            f"配对差(矩阵−{name}) {d.mean():+.5f} ± {se:.5f}（z={z:+.2f}，n={len(d)}）"
        )

    print()
    report("B 市场版 GLM", "bB")
    report("C 全特征 GLM", "bC")

    print()
    print("[ttg 分档校准：均值概率 vs 实际频率]")
    print(f"{'档':>4} {'n':>6} {'P_矩阵':>7} {'P_C':>7} {'实际':>7} {'矩阵偏差':>8} {'C偏差':>8}")
    for k in TTG_CLASSES:
        pm = np.array([r["pA"][k] for r in records])
        pc = np.array([r["pC"][k] for r in records])
        y = np.array(
            [
                1.0 if (r["m"]["total"] == k if k < 7 else r["m"]["total"] >= 7) else 0.0
                for r in records
            ]
        )
        print(
            f"{k:>4} {len(y):>6} {pm.mean():>7.3f} {pc.mean():>7.3f} {y.mean():>7.3f} "
            f"{y.mean() - pm.mean():>+8.3f} {y.mean() - pc.mean():>+8.3f}"
        )

    print()
    print("[按季配对差（矩阵−C）]")
    by_season: dict[str, list[float]] = defaultdict(list)
    for r in records:
        by_season[r["m"]["season"]].append(r["bA"] - r["bC"])
    for s in sorted(by_season):
        v = np.array(by_season[s])
        se = v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 1 else float("nan")
        print(f"  {s}: n={len(v)} 差 {v.mean():+.5f} ± {se:.5f}")


if __name__ == "__main__":
    main()
