#!/usr/bin/env python3
# 票 16：E-02 FD 断点隔离重算——2425 截止口径（edge-audit-20261109）
#
# 源脚本：.scratch/autoresearch-20261107/reports/07-h1-flb-erosion-gap.py（复用改窗，
# 票面明示不要求独立重写）。修改点（其余逐字保留）：
#   1. 配对阶段不动（全窗配对，漏斗与原跑可比）；
#   2. 分析阶段按 season <= '2024-25' 截断（断点=2025-07-23，十一联赛
#      2024-25 末场 2025-06-02 / 2025-26 首场 2025-08-02，断点恰落休赛期，
#      2526 全季=断点季——整季剔除无季内劈裂）；
#   3. 逐季序列加逆方差加权线性趋势检验（判「收窄仍在 vs 消失」）+
#      2526 对断点前加权均值的离群 z；
#   4. srct 锚交叉验证（票面可选）：close1x2_cons_* 现为概率列（≤1，
#      票 01/02 已核），按概率归一后 fair=cons/Σcons、euro_odds=1/fair
#      （无水公允锚——与 avgc/psc 含水锚不同基，只看逐季形状不可比水位）。
#      srct 管道独立于 FD，断点不应显形——若 2526 同步收窄则指向 JC 侧。
# 冻结真树只读。产出打印 stdout 供 research/16-e02-fd-isolation.md 引用。

from __future__ import annotations

import datetime as dt
import gzip
import json
import math
from collections import defaultdict

import duckdb
import numpy as np

BRONZE = "/Users/chevy/goalx-data/bronze/jc/sp_history.ndjson.gz"
DUCK = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
CUTOFF_SEASON = "2024-25"  # FD 断点（2025-07-23）前最后完整季

# JC 联盟全名 → srct 联赛名（match_features 实测值）——与源脚本逐字同
LEAGUE_MAP = {
    "英格兰超级联赛": "英超",
    "英格兰冠军联赛": "英冠",
    "意大利甲级联赛": "意甲",
    "西班牙甲级联赛": "西甲",
    "法国甲级联赛": "法甲",
    "德国甲级联赛": "德甲",
    "荷兰甲级联赛": "荷甲",
    "葡萄牙超级联赛": "葡超",
    "土耳其超级联赛": "土超",
    "苏格兰超级联赛": "苏超",
}
DIRS = ("h", "d", "a")
DIR_CN = {"h": "主胜", "d": "平局", "a": "客胜"}
BUCKET_EDGES = (0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.65, 1.01)
BUCKET_LABELS = ("<10%", "10-20%", "20-30%", "30-40%", "40-50%", "50-65%", "65%+")


def norm(s: str) -> str:
    return s.replace(" ", "")


def bucket_idx(p: float) -> int:
    for i in range(len(BUCKET_EDGES) - 1):
        if BUCKET_EDGES[i] <= p < BUCKET_EDGES[i + 1]:
            return i
    return -1


def parse_jc_score(payload: dict) -> tuple[int, int] | None:
    """sectionsNo999 '3:0' → (3,0)；缺失/取消 None。"""
    raw = payload.get("sectionsNo999")
    if not isinstance(raw, str) or ":" not in raw:
        return None
    try:
        return int(raw.split(":")[0]), int(raw.split(":")[1])
    except ValueError:
        return None


def load_jc() -> list[dict]:
    rows: list[dict] = []
    with gzip.open(BRONZE, "rt") as f:
        for line in f:
            r = json.loads(line)
            p = r["payload"]
            if p.get("isCancel"):
                continue
            oh = p.get("oddsHistory") or {}
            hl = oh.get("hadList") or []
            if not hl:
                continue
            league_jc = oh.get("leagueAllName") or ""
            lg = LEAGUE_MAP.get(league_jc)
            if lg is None:
                continue
            close = hl[-1]
            date = close.get("updateDate") or ""
            if not date or date < "2019":  # avgc 锚 2019-20 起才有
                continue
            try:
                jc = (float(close["h"]), float(close["d"]), float(close["a"]))
            except (KeyError, ValueError, TypeError):
                continue
            if min(jc) <= 1.0:
                continue
            rows.append(
                {
                    "sid": r["sid"],
                    "league": lg,
                    "home": norm(oh.get("homeTeamAllName") or ""),
                    "away": norm(oh.get("awayTeamAllName") or ""),
                    "date": dt.date.fromisoformat(date),
                    "jc": jc,
                    "score": parse_jc_score(p),
                }
            )
    return rows


def load_euro() -> list[dict]:
    con = duckdb.connect(DUCK, read_only=True)
    rows = con.sql(
        """
        select sid, league, kickoff::date, home, away, home_goals, away_goals,
               avgc_home, avgc_draw, avgc_away,
               psc_home, psc_draw, psc_away,
               close1x2_cons_h, close1x2_cons_d, close1x2_cons_a,
               season
        from match_features
        where home_goals is not null and admin_excluded = 0
          and kickoff >= '2019-01-01'
        """
    ).fetchall()
    con.close()
    return [
        {
            "sid": r[0],
            "league": r[1],
            "date": r[2],
            "home": norm(r[3]),
            "away": norm(r[4]),
            "hg": r[5],
            "ag": r[6],
            "avgc": (r[7], r[8], r[9]),
            "psc": (r[10], r[11], r[12]),
            "srct_p": (r[13], r[14], r[15]),
            "season": r[16],
        }
        for r in rows
    ]


def names_agree(a: str, b: str) -> bool:
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a))


def score_agree(jc: dict, eu: dict) -> bool | None:
    """None=无 JC 比分无法验证；True/False=验证结果。"""
    if jc["score"] is None:
        return None
    return jc["score"] == (eu["hg"], eu["ag"])


def pair_fixtures(jc_rows: list[dict], eu_rows: list[dict]) -> tuple[list[tuple[dict, dict]], dict]:
    """三轮：精确/前缀名配（比分验证）→ 唯格+比分自举名票迭代 → 残差。（与源脚本逐字同）"""
    eu_by_cell: dict[tuple[str, dt.date], list[dict]] = defaultdict(list)
    for eu in eu_rows:
        eu_by_cell[(eu["league"], eu["date"])].append(eu)
    jc_by_cell: dict[tuple[str, dt.date], list[dict]] = defaultdict(list)
    for jc in jc_rows:
        jc_by_cell[(jc["league"], jc["date"])].append(jc)

    def cells(jc: dict):
        for off in (-1, 0, 1):
            yield (jc["league"], jc["date"] + dt.timedelta(days=off))

    pairs: list[tuple[dict, dict]] = []
    used_eu: set[str] = set()
    matched_jc: set[str] = set()

    def try_name(jc: dict) -> dict | None:
        found = None
        for cell in cells(jc):
            for eu in eu_by_cell.get(cell, []):
                if eu["sid"] in used_eu:
                    continue
                if names_agree(jc["home"], eu["home"]) and names_agree(jc["away"], eu["away"]):
                    if score_agree(jc, eu) is False:
                        continue
                    found = eu
                    break
            if found:
                break
        return found

    # 轮 1：名字
    for jc in jc_rows:
        eu = try_name(jc)
        if eu is not None:
            pairs.append((jc, eu))
            used_eu.add(eu["sid"])
            matched_jc.add(jc["sid"])
    n_name = len(pairs)

    # 轮 2：唯格 + 比分相等自举（迭代至不动点，≤6 轮）
    votes: dict[tuple[str, str], int] = defaultdict(int)  # (jc 名, srct 名) → 票
    alias: dict[str, str] = {}
    n_boot = 0
    for _ in range(6):
        # 名票 → 别名
        for (jname, ename), v in list(votes.items()):
            if v >= 2 and jname not in alias:
                alias[jname] = ename
        # 应用别名重试名字匹配
        for jc in jc_rows:
            if jc["sid"] in matched_jc:
                continue
            h2, a2 = alias.get(jc["home"], jc["home"]), alias.get(jc["away"], jc["away"])
            found = None
            for cell in cells(jc):
                for eu in eu_by_cell.get(cell, []):
                    if eu["sid"] in used_eu:
                        continue
                    if names_agree(h2, eu["home"]) and names_agree(a2, eu["away"]):
                        if score_agree(jc, eu) is False:
                            continue
                        found = eu
                        break
                if found:
                    break
            if found is not None:
                pairs.append((jc, found))
                used_eu.add(found["sid"])
                matched_jc.add(jc["sid"])
                n_boot += 1
                votes[(jc["home"], found["home"])] += 1
                votes[(jc["away"], found["away"])] += 1
        # 唯格：该 (联赛,日期) 格内 JC 未配唯一 且 比分相等的 srct 未用唯一
        progressed = False
        for jc in jc_rows:
            if jc["sid"] in matched_jc or jc["score"] is None:
                continue
            for cell in cells(jc):
                jc_open = [j for j in jc_by_cell.get(cell, []) if j["sid"] not in matched_jc]
                eu_open = [
                    e
                    for e in eu_by_cell.get(cell, [])
                    if e["sid"] not in used_eu and (e["hg"], e["ag"]) == jc["score"]
                ]
                if len(jc_open) == 1 and len(eu_open) == 1 and jc_open[0]["sid"] == jc["sid"]:
                    pairs.append((jc, eu_open[0]))
                    used_eu.add(eu_open[0]["sid"])
                    matched_jc.add(jc["sid"])
                    n_boot += 1
                    votes[(jc["home"], eu_open[0]["home"])] += 1
                    votes[(jc["away"], eu_open[0]["away"])] += 1
                    progressed = True
        if not progressed:
            break

    stats = {
        "jc_rows": len(jc_rows),
        "eu_rows": len(eu_rows),
        "paired_name": n_name,
        "paired_bootstrap": n_boot,
        "unpaired_jc": len(jc_rows) - len(pairs),
        "alias_learned": len(alias),
    }
    return pairs, stats


def devig(odds: tuple[float | None, float | None, float | None] | None) -> tuple[float, float, float] | None:
    if odds is None or any(o is None or o <= 1.0 for o in odds):
        return None
    try:
        inv = np.array([1.0 / o for o in odds])
    except (TypeError, ZeroDivisionError):
        return None
    if not np.isfinite(inv).all() or inv.sum() <= 0:
        return None
    fair = inv / inv.sum()
    return tuple(float(x) for x in fair)  # type: ignore[return-value]


def probs_to_fair(
    probs: tuple[float | None, float | None, float | None] | None,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """srct 概率列（≤1）→ (归一 fair, fair-odds=1/fair)。任一缺失/非正/和≠合理值拒。"""
    if probs is None or any(p is None or p <= 0.0 for p in probs):
        return None
    arr = np.array(probs, dtype=float)
    s = arr.sum()
    if not np.isfinite(arr).all() or s <= 0 or abs(s - 1.0) > 0.05:
        return None
    fair = arr / s
    odds = tuple(1.0 / x for x in fair)
    return tuple(float(x) for x in fair), odds  # type: ignore[return-value]


def clustered_mean(rows: list[tuple[float, int]]) -> tuple[float, float, int]:
    """按场聚类的均值与 SE：rows=(fixture_key, diff)。同场多向取场内均值。（与源脚本逐字同）"""
    by_fx: dict[int, list[float]] = defaultdict(list)
    for fx, v in rows:
        by_fx[fx].append(v)
    per_fx = np.array([np.mean(v) for v in by_fx.values()])
    n = len(per_fx)
    if n < 2:
        return float("nan"), float("nan"), n
    m = float(per_fx.mean())
    se = float(per_fx.std(ddof=1) / math.sqrt(n))
    return m, se, n


def analyze(sample: list, label: str) -> dict[str, tuple[float, float, int]]:
    """三读数：全局侵蚀差 / 长赔带合并+差中差 / 假想 EV + 逐季序列（返回逐季序列）。"""
    if not sample:
        print(f"  [{label}] 样本空")
        return {}
    seasons = sorted({eu["season"] for _, _, eu, _, _, _ in sample})
    print(f"  [{label}] n={len(sample)} 场  季节 {seasons[0]}…{seasons[-1]}")

    out: dict[str, tuple[float, float, int]] = {}

    # 全局（平水基线）
    diffs_all = []
    for i, jc, eu, euro, fair, hit in sample:
        for d in range(3):
            diffs_all.append((i, (jc["jc"][d] - euro[d]) * hit[d]))
    g_all, se_all, n_all = clustered_mean(diffs_all)
    out["global"] = (g_all, se_all, n_all)
    print(f"  全局侵蚀差（JC−欧，配对 ROI 差）: {g_all:+.4f} ± {se_all:.4f} (SE, 场聚类 n={n_all})")

    # 长赔带合并（p<0.30，跨向，场聚类）+ 差中差
    long_rows = [
        (i, (jc["jc"][d] - euro[d]) * hit[d])
        for i, jc, eu, euro, fair, hit in sample
        for d in range(3)
        if fair[d] < 0.30
    ]
    m, se, n = clustered_mean(long_rows)
    z = (m - g_all) / se if se > 0 else float("nan")
    out["long"] = (m, se, n)
    print(f"  [长赔带 p<0.30 合并] n={n} 场：侵蚀差 {m:+.3%} ± {se:.3%}，差中差 {m - g_all:+.3%}，z(对全局)={z:+.2f}")
    for d in range(3):
        rows = [
            (i, (jc["jc"][d] - euro[d]) * hit[d])
            for i, jc, eu, euro, fair, hit in sample
            if fair[d] < 0.30
        ]
        m_d, se_d, n_d = clustered_mean(rows)
        flag = "n<500" if n_d < 500 else ""
        print(f"    {DIR_CN[DIRS[d]]}: n={n_d} 差 {m_d:+.3%} ± {se_d:.3%} 差中差 {m_d - g_all:+.3%} {flag}")

    # 假想 EV
    ev_rows = []
    for i, jc, eu, euro, fair, hit in sample:
        for d in range(3):
            if jc["jc"][d] * fair[d] > 1.0:
                ev_rows.append((i, jc["jc"][d] * hit[d] - 1.0))
    m_ev, se_ev, n_ev = clustered_mean(ev_rows)
    out["ev"] = (m_ev, se_ev, n_ev)
    print(f"  [假想 EV 口径] 欧公允×JC价>1 的选择：n={n_ev} 场，实现 ROI {m_ev:+.3%} ± {se_ev:.3%}")

    # 逐季同质性（长赔带）
    print("  [逐季侵蚀差（长赔带 p<0.30，场聚类）]")
    by_season: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for i, jc, eu, euro, fair, hit in sample:
        for d in range(3):
            if fair[d] < 0.30:
                by_season[eu["season"]].append((i, (jc["jc"][d] - euro[d]) * hit[d]))
    series: dict[str, tuple[float, float, int]] = {}
    for s in sorted(by_season):
        ms, ses, ns = clustered_mean(by_season[s])
        series[s] = (ms, ses, ns)
        print(f"    {s}: n={ns} 差 {ms:+.3%} ± {ses:.3%}")
    return series  # type: ignore[return-value]


def trend_test(
    series: dict[str, tuple[float, float, int]],
) -> tuple[float, float, float, float, float]:
    """逆方差加权线性趋势（x=季序 0..k-1）：返回 (slope/季, SE, z, Q, z_过散校正)。
    slope>0=收窄；Q=残差加权平方和（df=k−2），Q>>df 时按 sqrt(Q/df) 膨胀 SE 做稳健 z。"""
    ks = sorted(series)
    xs = np.arange(len(ks), dtype=float)
    ys = np.array([series[k][0] for k in ks])
    ws = np.array([1.0 / series[k][1] ** 2 for k in ks])
    xbar = float(np.average(xs, weights=ws))
    ybar = float(np.average(ys, weights=ws))
    sxx = float(np.sum(ws * (xs - xbar) ** 2))
    sxy = float(np.sum(ws * (xs - xbar) * (ys - ybar)))
    b = sxy / sxx
    a = ybar - b * xbar
    se_b = math.sqrt(1.0 / sxx)
    resid = ys - (a + b * xs)
    q = float(np.sum(ws * resid**2))
    df = len(ks) - 2
    infl = math.sqrt(max(1.0, q / df)) if df > 0 else 1.0
    return b, se_b, b / se_b, q, b / (se_b * infl)


def step_test(
    series: dict[str, tuple[float, float, int]], s_pre: str, s_post: str
) -> tuple[float, float]:
    """断点两侧相邻季一步差（y_post − y_pre）与 z。"""
    m1, se1, _ = series[s_pre]
    m2, se2, _ = series[s_post]
    diff = m2 - m1
    se_d = math.sqrt(se1**2 + se2**2)
    return diff, diff / se_d


def outlier_test(
    series_pre: dict[str, tuple[float, float, int]], y_tail: tuple[float, float, int]
) -> float:
    """断点季值 vs 断点前逆方差加权均值的 z。"""
    ws = np.array([1.0 / v[1] ** 2 for v in series_pre.values()])
    ys = np.array([v[0] for v in series_pre.values()])
    ybar = float(np.average(ys, weights=ws))
    se_bar = math.sqrt(1.0 / float(ws.sum()))
    return (y_tail[0] - ybar) / math.sqrt(y_tail[1] ** 2 + se_bar**2)


def main() -> None:
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)

    print("=" * 76)
    print("票 16 · E-02 FD 断点隔离重算——2425 截止口径（avgc 主锚 + psc + srct 交叉）")
    print(f"断点=2025-07-23（Pinnacle 剔出 avgc）；CUTOFF season≤{CUTOFF_SEASON}")
    print("=" * 76)
    print(f"配对漏斗（全窗，与原跑同口径）: {pstats}")
    print(f"配对成功 {len(pairs)} / JC 侧 {pstats['jc_rows']}（{len(pairs)/pstats['jc_rows']:.0%}）")

    # ---------- avgc / psc：odds 锚 ----------
    for anchor_name, key in (
        ("avgc（FD 欧收盘共识·主锚）", "avgc"),
        ("psc（FD Pinnacle 收盘）", "psc"),
    ):
        sample_all: list = []
        for i, (jc, eu) in enumerate(pairs):
            fair = devig(eu[key])
            euro = eu[key]
            if fair is None or euro is None or min(euro) <= 1.0:
                continue
            hg, ag = eu["hg"], eu["ag"]
            hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
            sample_all.append((i, jc, eu, euro, fair, hit))
        print()
        print("-" * 76)
        print(f"锚：{anchor_name}")
        series_full = analyze(sample_all, "全窗复现（对照原跑）")
        sample_cut = [t for t in sample_all if t[2]["season"] <= CUTOFF_SEASON]
        print()
        series_pre = analyze(sample_cut, f"2425 截止（season≤{CUTOFF_SEASON}）")
        if len(series_pre) >= 3:
            b, se_b, z_b, q, z_b_rob = trend_test(series_pre)
            print()
            print(
                f"  [断点前逐季趋势] slope={b:+.4%}/季 ± {se_b:.4%}，z={z_b:+.2f}，"
                f"Q={q:.1f}(df={len(series_pre)-2})，过散校正 z={z_b_rob:+.2f}（>0=收窄；|z|<2=趋势不立）"
            )
            if series_full:
                tail = series_full.get("2025-26")
                if tail is not None:
                    z_out = outlier_test(series_pre, tail)
                    print(
                        f"  [断点季离群] 2025-26 {tail[0]:+.3%}±{tail[1]:.3%} vs 断点前加权均值 "
                        f"z={z_out:+.2f}（同号收窄且 |z| 大=断点季显著偏出断点前水平）"
                    )
                if "2024-25" in series_full and "2025-26" in series_full:
                    d_step, z_step = step_test(series_full, "2024-25", "2025-26")
                    print(f"  [断点一步跳变] 2526−2425 = {d_step:+.3%}，z={z_step:+.2f}")

    # ---------- srct：概率锚（交叉验证，票面可选） ----------
    print()
    print("-" * 76)
    print("锚：srct（源T 收盘共识·概率列 fair-odds 化，FD 独立管道——只看逐季形状）")
    sample_srct: list = []
    srct_cov = defaultdict(lambda: [0, 0])
    for i, (jc, eu) in enumerate(pairs):
        srct_cov[eu["season"]][0] += 1
        got = probs_to_fair(eu["srct_p"])
        if got is None:
            continue
        fair, euro = got
        srct_cov[eu["season"]][1] += 1
        hg, ag = eu["hg"], eu["ag"]
        hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
        sample_srct.append((i, jc, eu, euro, fair, hit))
    print("  [srct 概率列逐季覆盖（配对行中非缺失数）]")
    for s in sorted(srct_cov):
        tot, ok = srct_cov[s]
        print(f"    {s}: {ok}/{tot}（{ok/tot:.0%}）")
    series_srct_full = analyze(sample_srct, "srct 全窗")
    sample_srct_cut = [t for t in sample_srct if t[2]["season"] <= CUTOFF_SEASON]
    print()
    series_srct_pre = analyze(sample_srct_cut, f"srct 2425 截止（season≤{CUTOFF_SEASON}）")
    if len(series_srct_pre) >= 2 and "2025-26" in series_srct_full:
        d_step, z_step = step_test(series_srct_full, "2024-25", "2025-26")
        print()
        print(
            f"  [srct 断点一步跳变] 2526−2425 = {d_step:+.3%}，z={z_step:+.2f}"
            "（srct 不受 FD 断点影响——跳变≈0=2526 收窄在锚侧=伪迹；同幅跳变=JC 侧）"
        )
    if len(series_srct_pre) >= 3:
        b, se_b, z_b, q, z_b_rob = trend_test(series_srct_pre)
        print(f"  [srct 断点前逐季趋势] slope={b:+.4%}/季 ± {se_b:.4%}，z={z_b:+.2f}，过散校正 z={z_b_rob:+.2f}")
        tail = series_srct_full.get("2025-26") if series_srct_full else None
        if tail is not None:
            z_out = outlier_test(series_srct_pre, tail)
            print(
                f"  [srct 断点季离群] 2025-26 {tail[0]:+.3%}±{tail[1]:.3%} vs 断点前加权均值 z={z_out:+.2f}"
            )

    # 断点窗核验（休赛期零场）
    print()
    print("[断点窗核验] 十一联赛 2025-06-03..2025-08-01 场次数（应为 0=断点恰落休赛期）")
    con = duckdb.connect(DUCK, read_only=True)
    leagues = tuple(LEAGUE_MAP.values())
    n_gap = con.sql(
        f"""
        select count(*) from match_features
        where kickoff >= '2025-06-03' and kickoff < '2025-08-01'
          and home_goals is not null and admin_excluded = 0
          and league in {leagues}
        """
    ).fetchone()[0]
    con.close()
    print(f"  gap matches = {n_gap}（2425 末场 2025-06-02 / 2526 首场 2025-08-02）")


if __name__ == "__main__":
    main()
