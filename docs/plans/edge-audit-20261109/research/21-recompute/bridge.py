"""票 21（srct×fdhist 桥接验证）：2019-2025 重叠窗 srct cid177 收盘 vs fdhist PSC。

口径（票面 + 先例转录）：
- 配对键 = match_features.sid == odds_change_event.sid（gold build 已经
  corpus_gate.match_fixtures v3 名字身份轮把 fdhist 料次链接到语料 sid，
  本脚本零自研匹配——identity 机制既有 join 先例）。
- srct 侧 = odds_change_event bookmaker 'srct:1x2:177' market '1x2'，
  资格线 published_at <= kickoff（生产 srct_closing_prob 同语义），
  取 (published_at, source_order) 最晚一笔（corpus_gate 门② last() 同序）。
- fdhist 侧 = match_features psc_*（PSC=收盘；PSH=开赛档，仅用于同向率基线）。
- 概率域 = 三向 Shin 去水（实现转录自票 02 独立重写版 common.shin_probs，
  二分法解 z，病态盘退化比例化——两侧同一实现，桥接比较公平性不受影响）。
- 窗 = season 2019-20..2025-26（kickoff 2019-08..2026-07，票面 2019-2025）。
- 排除 admin_excluded=TRUE（8 行，A4 纪律）；psc_mature 全窗满足（today 距窗尾
  >4 个月）。
- 断点分段 = kickoff < 2025-07-23（干净）vs >=（fdhist PSC 受断点影响段；
  与 season 标签的差异仅 8 行比甲——按 kickoff 日精确切）。

产出：stdout 摘要 + outputs/bridge_stats.json（全部分层数值）。
冻结真树只读：corpus.duckdb read_only，无写库。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

import duckdb

CORPUS = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
PIN_1X2 = "srct:1x2:177"  # Pinnacle 1x2（源T 实证命名，票 02/门②同款）
BREAKPOINT = "2025-07-23"  # FD Pinnacle 断点（research/15 §一官方警示）
SEASONS = tuple(f"{y}-{(y + 1) % 100:02d}" for y in range(2019, 2026))
OUT_DIR = Path(__file__).resolve().parent / "outputs"
DIRS = ("h", "d", "a")
# 赔率带（按 fdhist PSC 十进制赔率，市场惯例切分）
BANDS = (
    (1.0, 1.5, "<1.5"),
    (1.5, 2.0, "1.5-2"),
    (2.0, 3.0, "2-3"),
    (3.0, 5.0, "3-5"),
    (5.0, 10.0, "5-10"),
    (10.0, 1e9, ">=10"),
)
MOVE_EPS_PP = 0.25  # 同向率「实质变动」线：开→收概率位移 >=0.25pp 才算动


def shin_probs(odds: tuple[float, float, float]) -> tuple[float, float, float]:
    """Shin 去水（转录票 02 独立实现）：p_i=(sqrt(z²+4(1−z)π_i²/B)−z)/(2(1−z))。"""
    if any(o <= 1.0 for o in odds):
        raise ValueError("odds <= 1")
    raw = [1.0 / o for o in odds]
    total = sum(raw)
    if total <= 1.0 + 1e-12:
        s = sum(raw)
        return tuple(r / s for r in raw)  # type: ignore[return-value]

    def probs_at(z: float) -> list[float]:
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * r * r / total) - z)
            / (2.0 * (1.0 - z))
            for r in raw
        ]

    lo, hi = 0.0, 0.9999
    if sum(probs_at(hi)) > 1.0:
        z = hi
    else:
        for _ in range(200):
            mid = (lo + hi) / 2
            if sum(probs_at(mid)) > 1.0:
                lo = mid
            else:
                hi = mid
        z = (lo + hi) / 2
    probs = probs_at(z)
    s = sum(probs)
    return tuple(p / s for p in probs)  # type: ignore[return-value]


def pct(vals: list[float], q: float) -> float:
    """经验分位（最近邻下取整，同门② _dev_stats 口径）。"""
    ordered = sorted(vals)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else float("nan")


def summarize(diffs_pp: list[float]) -> dict[str, float | int]:
    """差分布摘要：signed 均值/中位 + |diff| p50/p90/p95/p99/max（pp）。"""
    if not diffs_pp:
        return {"n": 0}
    absd = [abs(d) for d in diffs_pp]
    return {
        "n": len(diffs_pp),
        "mean_pp": sum(diffs_pp) / len(diffs_pp),
        "median_pp": statistics.median(diffs_pp),
        "abs_p50_pp": pct(absd, 0.50),
        "abs_p90_pp": pct(absd, 0.90),
        "abs_p95_pp": pct(absd, 0.95),
        "abs_p99_pp": pct(absd, 0.99),
        "abs_max_pp": max(absd),
    }


def iso_week(ko_str: str) -> str:
    """kickoff VARCHAR（北京墙钟）→ 'YYYY-Www'（ISO 周标签）。"""
    d = dt.date.fromisoformat(ko_str[:10])
    ic = d.isocalendar()
    return f"{ic[0]}-W{ic[1]:02d}"


def main() -> None:
    con = duckdb.connect(CORPUS, read_only=True)
    con.execute("SET TimeZone='Asia/Shanghai'")

    # fdhist 侧（gold 已链接 sid；窗口季 + PSC 三向齐全 + 行政排除）
    gold = con.execute(
        f"""
        SELECT sid, season, league, stage, kickoff::VARCHAR AS ko,
               psc_home, psc_draw, psc_away,
               psh_home, psh_draw, psh_away
        FROM match_features
        WHERE season IN ({','.join(f"'{s}'" for s in SEASONS)})
          AND admin_excluded = FALSE
        ORDER BY sid
        """
    ).fetchall()
    n_window = len(gold)
    psc_rows = {
        r[0]: r
        for r in gold
        if r[5] is not None and r[6] is not None and r[7] is not None
        and min(r[5], r[6], r[7]) > 1.0
    }

    # srct 侧：资格线内最晚一笔（同场内 published_at+source_order 双键取末）
    con.execute("CREATE TEMP TABLE sids(sid VARCHAR)")
    con.executemany("INSERT INTO sids VALUES (?)", [(s,) for s in psc_rows])
    srct = {
        r[0]: (r[1], r[2], r[3])
        for r in con.execute(
            f"""
            SELECT sid,
                   last(odds_home ORDER BY published_at, source_order),
                   last(odds_draw ORDER BY published_at, source_order),
                   last(odds_away ORDER BY published_at, source_order)
            FROM odds_change_event o
            WHERE bookmaker_id = '{PIN_1X2}' AND market = '1x2'
              AND published_at <= kickoff
              AND sid IN (SELECT sid FROM sids)
            GROUP BY sid
            """
        ).fetchall()
        if r[1] is not None and r[2] is not None and r[3] is not None
        and min(r[1], r[2], r[3]) > 1.0
    }

    # 哨兵分母自反性检查：paired/PSC-present 无法发现 PSC 整场塌缩（2526 缺
    # 53% 是整场缺，分母同步缩）。补一张 srct 侧做分母的覆盖率（窗内 11 FD
    # 联赛场上 cid177 资格收盘在场数 vs 其中有 PSC 的——league 清单转录
    # data/leagues.py LEAGUE_TO_FD）。
    FD_LEAGUES = (
        "英超", "西甲", "德甲", "意甲", "法甲", "英冠",
        "荷甲", "葡超", "土超", "比甲", "苏超",
    )
    con.execute("CREATE TEMP TABLE win(sid VARCHAR, season VARCHAR)")
    con.executemany(
        "INSERT INTO win VALUES (?, ?)",
        [(r[0], r[1]) for r in gold if r[2] in FD_LEAGUES],
    )
    srct_cov = con.execute(
        f"""
        SELECT w.season,
               count(DISTINCT o.sid) AS srct_eligible,
               count(DISTINCT o.sid) FILTER (
                   WHERE o.sid IN (SELECT sid FROM sids)) AS also_psc
        FROM odds_change_event o JOIN win w USING (sid)
        WHERE o.bookmaker_id = '{PIN_1X2}' AND o.market = '1x2'
          AND o.published_at <= o.kickoff
        GROUP BY w.season ORDER BY w.season
        """
    ).fetchall()
    con.close()

    # ---- 配对 + 逐 outcome 行 -------------------------------------------------
    # row = (sid, season, league, stage, seg, week, outcome, band,
    #        p_srct, p_psc, p_psh, odds_psc, odds_srct)
    rows: list[tuple] = []
    n_srct_missing = 0
    psh_missing = 0
    for sid, goldrow in psc_rows.items():
        quote = srct.get(sid)
        if quote is None:
            n_srct_missing += 1
            continue
        season, league, stage, ko = goldrow[1], goldrow[2], goldrow[3], goldrow[4]
        seg = "bp_pre" if ko < BREAKPOINT else "bp_post"
        week = iso_week(ko)
        p_psc = shin_probs((goldrow[5], goldrow[6], goldrow[7]))
        p_srct = shin_probs(quote)
        has_psh = (
            goldrow[8] is not None and goldrow[9] is not None and goldrow[10] is not None
            and min(goldrow[8], goldrow[9], goldrow[10]) > 1.0
        )
        p_psh = shin_probs((goldrow[8], goldrow[9], goldrow[10])) if has_psh else None
        if not has_psh:
            psh_missing += 1
        for i, oc in enumerate(DIRS):
            odds_psc = goldrow[5 + i]
            band = next(lbl for lo, hi, lbl in BANDS if lo <= odds_psc < hi)
            rows.append(
                (sid, season, league, stage, seg, week, oc, band,
                 p_srct[i], p_psc[i], p_psh[i] if p_psh else None,
                 odds_psc, quote[i])
            )

    diffs_all = [(r[8] - r[9]) * 100.0 for r in rows]  # signed pp
    absrel_odds = [
        abs(r[12] - r[11]) / r[11] * 100.0 for r in rows
    ]  # 赔率域相对偏差（门②同式，%）
    # 结构注记：同场三向 Shin 归一 ⇒ 三向 signed diff 逐场和恒为 0，
    # 任何跨完整场的 signed 均值都结构性 ≈0（信息量为零）——等价性判据
    # 必须落在 |diff| 分布/逐 outcome 偏移/同向率上，signed 均值只报不改判。
    same_odds = [r[11] == r[12] for r in rows]
    triplet_identical = defaultdict(list)
    for r in rows:
        triplet_identical[r[0]].append(r[11] == r[12])

    # ---- 分层 -----------------------------------------------------------------
    def by(keyfn) -> dict[str, dict]:
        buckets: dict[str, list[float]] = defaultdict(list)
        for r, d in zip(rows, diffs_all, strict=True):
            buckets[keyfn(r)].append(d)
        return {k: summarize(v) for k, v in sorted(buckets.items())}

    stats = {
        "window": {"seasons": list(SEASONS), "matches_in_window": n_window},
        "coverage": {
            "gold_rows_window_admin_excluded_out": n_window,
            "psc_present": len(psc_rows),
            "srct_closing_eligible_or_missing": {
                "paired": len(psc_rows) - n_srct_missing,
                "srct_closing_missing": n_srct_missing,
                "coverage_vs_psc": (len(psc_rows) - n_srct_missing) / len(psc_rows),
            },
            "coverage_vs_srct_fd_leagues": {
                season: {
                    "srct_eligible": n_elig,
                    "also_psc": n_psc,
                    "rate": n_psc / n_elig,
                }
                for season, n_elig, n_psc in srct_cov
            },
            "psh_present_on_paired": len(psc_rows) - n_srct_missing - psh_missing,
            "outcome_comparisons": len(rows),
        },
        "overall": summarize(diffs_all),
        "odds_rel_dev_pct": summarize(absrel_odds),
        "by_season": by(lambda r: r[1]),
        "by_band": by(lambda r: r[7]),
        "by_segment": by(lambda r: r[4]),
        "by_season_segment": by(lambda r: f"{r[1]}|{r[4]}"),
        "by_league": by(lambda r: r[2]),
        "by_stage": by(lambda r: r[3]),
        "by_outcome": by(lambda r: r[6]),
    }

    # ---- 一致性：相关 + 开→收同向率（PSH 共基线） ------------------------------
    p_srct_all = [r[8] for r in rows]
    p_psc_all = [r[9] for r in rows]
    stats["consistency"] = {
        "pearson_prob_all": pearson(p_srct_all, p_psc_all),
        "pearson_prob_bp_pre": pearson(
            [r[8] for r in rows if r[4] == "bp_pre"],
            [r[9] for r in rows if r[4] == "bp_pre"],
        ),
        "same_sign_move_all": None,
        "same_sign_move_material": None,
    }
    n_mv = same_mv = n_mat = same_mat = 0
    for r in rows:
        if r[10] is None:
            continue
        mv_psc = (r[9] - r[10]) * 100.0  # PSH→PSC 开到收位移（fdhist 族内）
        mv_srct = (r[8] - r[10]) * 100.0  # PSH→srct 收盘位移（跨源）
        n_mv += 1
        agree = (mv_psc > 0) == (mv_srct > 0)
        same_mv += agree
        if abs(mv_psc) >= MOVE_EPS_PP and abs(mv_srct) >= MOVE_EPS_PP:
            n_mat += 1
            same_mat += agree
    stats["consistency"]["same_sign_move_all"] = same_mv / n_mv if n_mv else None
    stats["consistency"]["same_sign_move_material"] = same_mat / n_mat if n_mat else None
    stats["consistency"]["move_n_all"] = n_mv
    stats["consistency"]["move_n_material"] = n_mat
    stats["consistency"]["move_eps_pp"] = MOVE_EPS_PP

    # ---- 周级分布（哨兵标定基线；干净段 + n>=30 outcome 门槛） -----------------
    weekly: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"d": [], "rel": [], "n_psc": 0, "n_pair": 0}
    )
    for sid in psc_rows:
        r = psc_rows[sid]
        week = iso_week(r[4])
        weekly[week]["n_psc"] += 1
        if sid in srct:
            weekly[week]["n_pair"] += 1
    for r, d, rel in zip(rows, diffs_all, absrel_odds, strict=True):
        weekly[r[5]]["d"].append(d)
        weekly[r[5]]["rel"].append(rel)
    wk_stats = []
    for week, w in sorted(weekly.items()):
        if w["d"]:
            wk_stats.append(
                {
                    "week": week,
                    "n_match_psc": w["n_psc"],
                    "n_match_pair": w["n_pair"],
                    "coverage": w["n_pair"] / w["n_psc"],
                    "mean_diff_pp": sum(w["d"]) / len(w["d"]),
                    "mean_absdiff_pp": sum(abs(x) for x in w["d"]) / len(w["d"]),
                    "median_rel_odds_pct": statistics.median(w["rel"]),
                    "n_outcomes": len(w["d"]),
                }
            )
    # 干净段周 = 周标签 < 断点周（2025-07-23 落 ISO W30；断点在休赛期，
    # 无跨断点周）；哨兵基线仅用干净段（post 段 PSC 已受断点污染）
    wk_clean = [w for w in wk_stats if w["n_outcomes"] >= 30 and w["week"] < "2025-W30"]
    wk_post = [w for w in wk_stats if w["n_outcomes"] >= 30 and w["week"] >= "2025-W30"]
    abs_means = [abs(w["mean_diff_pp"]) for w in wk_clean]
    mean_absd = [w["mean_absdiff_pp"] for w in wk_clean]
    med_rel = [w["median_rel_odds_pct"] for w in wk_clean]
    covs = [w["coverage"] for w in wk_clean]
    stats["weekly"] = {
        "n_weeks_clean_floor30": len(wk_clean),
        "n_weeks_post_floor30": len(wk_post),
        "clean_abs_mean_diff_pp": {
            "p50": pct(abs_means, 0.50),
            "p90": pct(abs_means, 0.90),
            "p95": pct(abs_means, 0.95),
            "max": max(abs_means),
        },
        "clean_mean_absdiff_pp": {
            "p50": pct(mean_absd, 0.50),
            "p90": pct(mean_absd, 0.90),
            "p95": pct(mean_absd, 0.95),
            "max": max(mean_absd),
        },
        "clean_median_rel_odds_pct": {
            "p50": pct(med_rel, 0.50),
            "p90": pct(med_rel, 0.90),
            "p95": pct(med_rel, 0.95),
            "max": max(med_rel),
        },
        "clean_coverage": {"min": min(covs), "p05": pct(covs, 0.05), "p50": pct(covs, 0.50)},
        "low_coverage_weeks": [
            {k: w[k] for k in ("week", "n_match_psc", "n_match_pair", "coverage")}
            for w in wk_clean
            if w["coverage"] < 0.98
        ],
        "post_weeks": [
            {
                k: w[k]
                for k in (
                    "week",
                    "n_outcomes",
                    "mean_diff_pp",
                    "mean_absdiff_pp",
                    "median_rel_odds_pct",
                    "coverage",
                )
            }
            for w in wk_post
        ],
    }
    stats["identical_quotes"] = {
        "outcome_odds_identical_frac": sum(same_odds) / len(same_odds),
        "match_triplet_fully_identical_frac": (
            sum(1 for v in triplet_identical.values() if all(v))
            / len(triplet_identical)
        ),
        "match_any_outcome_differs_frac": (
            sum(1 for v in triplet_identical.values() if not all(v))
            / len(triplet_identical)
        ),
    }

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "bridge_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=1, default=str)
    )

    # ---- stdout 摘要 ----------------------------------------------------------
    cov = stats["coverage"]
    print(f"window matches (admin_excluded out): {n_window}")
    print(f"PSC present: {cov['psc_present']}; paired: "
          f"{cov['srct_closing_eligible_or_missing']['paired']}"
          f" (coverage {cov['srct_closing_eligible_or_missing']['coverage_vs_psc']:.4%});"
          f" outcome rows: {len(rows)}")
    o = stats["overall"]
    print(f"overall diff (srct-PSC) pp: mean {o['mean_pp']:+.4f} median {o['median_pp']:+.4f}"
          f" |d| p50 {o['abs_p50_pp']:.3f} p90 {o['abs_p90_pp']:.3f}"
          f" p95 {o['abs_p95_pp']:.3f} p99 {o['abs_p99_pp']:.3f}")
    print("coverage vs srct (FD 11 leagues, PSC 整场在场率):")
    for season, v in stats["coverage"]["coverage_vs_srct_fd_leagues"].items():
        print(f"  {season}: {v['also_psc']}/{v['srct_eligible']} = {v['rate']:.4f}")
    print(f"odds rel dev %: median {stats['odds_rel_dev_pct']['median_pp']:.4f}"
          f" mean {stats['odds_rel_dev_pct']['mean_pp']:.4f}"
          f" p95 {stats['odds_rel_dev_pct']['abs_p95_pp']:.3f}")
    print("by season:")
    for s, v in stats["by_season"].items():
        print(f"  {s}: n_out={v['n']} mean {v['mean_pp']:+.4f}pp"
              f" |d|p95 {v['abs_p95_pp']:.3f}")
    print("by segment:")
    for s, v in stats["by_segment"].items():
        print(f"  {s}: n_out={v['n']} mean {v['mean_pp']:+.4f}pp"
              f" |d|p50 {v['abs_p50_pp']:.3f} p95 {v['abs_p95_pp']:.3f}")
    print("by band:")
    for s, v in stats["by_band"].items():
        print(f"  {s}: n_out={v['n']} mean {v['mean_pp']:+.4f}pp"
              f" |d|p50 {v['abs_p50_pp']:.3f} p95 {v['abs_p95_pp']:.3f}")
    print("by outcome (signed, 结构零之外的逐向偏移):")
    for s, v in stats["by_outcome"].items():
        print(f"  {s}: n_out={v['n']} mean {v['mean_pp']:+.5f}pp"
              f" median {v['median_pp']:+.5f}pp |d|p50 {v['abs_p50_pp']:.3f}")
    idq = stats["identical_quotes"]
    print(f"identical quotes: outcome {idq['outcome_odds_identical_frac']:.4%},"
          f" full triplet {idq['match_triplet_fully_identical_frac']:.4%},"
          f" any differs {idq['match_any_outcome_differs_frac']:.4%}")
    c = stats["consistency"]
    print(f"pearson(all)={c['pearson_prob_all']:.6f} pearson(bp_pre)={c['pearson_prob_bp_pre']:.6f}")
    print(f"same-sign open→close: all {c['same_sign_move_all']:.4f} (n={c['move_n_all']})"
          f" material {c['same_sign_move_material']:.4f} (n={c['move_n_material']})")
    w = stats["weekly"]
    print(f"weekly clean (n>=30 outcomes): {w['n_weeks_clean_floor30']} weeks")
    print(f"  weekly mean|diff| pp: p50 {w['clean_mean_absdiff_pp']['p50']:.4f}"
          f" p90 {w['clean_mean_absdiff_pp']['p90']:.4f}"
          f" p95 {w['clean_mean_absdiff_pp']['p95']:.4f}"
          f" max {w['clean_mean_absdiff_pp']['max']:.4f}")
    print(f"  weekly median rel-odds %: p50 {w['clean_median_rel_odds_pct']['p50']:.4f}"
          f" p90 {w['clean_median_rel_odds_pct']['p90']:.4f}"
          f" p95 {w['clean_median_rel_odds_pct']['p95']:.4f}"
          f" max {w['clean_median_rel_odds_pct']['max']:.4f}")
    print(f"  weekly coverage: min {w['clean_coverage']['min']:.4f}"
          f" p05 {w['clean_coverage']['p05']:.4f}")
    for lw in w["low_coverage_weeks"]:
        print(f"  low-cov week {lw['week']}: {lw['n_match_pair']}/{lw['n_match_psc']}"
              f" = {lw['coverage']:.4f}")
    print(f"post-breakpoint weeks (n>=30): {w['n_weeks_post_floor30']}")
    for pw in w["post_weeks"][:8]:
        print(f"  {pw['week']}: mean|d| {pw['mean_absdiff_pp']:.3f}pp"
              f" medRel {pw['median_rel_odds_pct']:.3f}% cov {pw['coverage']:.3f}")


if __name__ == "__main__":
    main()
