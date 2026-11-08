#!/usr/bin/env python3
# 票 07：H1 FLB 侵蚀差——JC 收盘 vs 欧收盘十年双源对比（autoresearch-20261107 批次 α）
#
# 纯离线只读研究脚本（analysis_03 先例）：零写任何库。产出打印 stdout，
# 供 07-h1-flb-erosion-gap.md 引用。
#
# 数据与配对：
# - JC 侧 = bronze/jc/sp_history.ndjson.gz（官方独供 SP 史，hadList 逐条变价，
#   末条=收盘；sectionsNo999=终场比分，配对验证用）。
# - 欧侧 = corpus duckdb gold match_features（srct 命名），三锚：
#   avgc（FD 欧收盘共识，主锚）/ psc（FD Pinnacle 收盘）/ close1x2_cons（srct 收盘）。
# - 配对 = 联赛映射 + 名字精确/前缀 + 比分一致性验证；残余用
#   「(联赛,日期±1)格内唯一未配 + 比分相等」自举名票，迭代至不动点；
#   未配残差如实报数（诚实处理识别不确定，不硬配）。
# 口径：
# - 桶界沿用十年扫描（analysis_03 同族）：公允（欧收盘比例化去水）分桶
#   [0,.10,.20,.30,.40,.50,.65,1]；长赔带 = p<0.30。
# - 每桶×每向：n、两源实现 ROI（同场同向配对差 = mean((odds_jc−odds_euro)×hit)）、
#   SE（桶内向 = 每场至多 1 行 → 场独立；跨向合并行按场聚类）。
# - 侵蚀差两读数：原始差（含 JC 均一抽水的底差）+ 差中差（桶差 − 全局差，
#   机制预测 FLB 保留使长赔桶差中差>0）。
# 门（注册表 H1）：配对 n≥500/向，差>2SE。
# 判读边界：本票判校准缺口存在性，不判正 EV（JC 抽水摆着）。

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

# JC 联盟全名 → srct 联赛名（match_features 实测值）
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
            "srct": (r[13], r[14], r[15]),
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
    """三轮：精确/前缀名配（比分验证）→ 唯一格+比分自举名票迭代 → 残差。"""
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


def clustered_mean(rows: list[tuple[float, int]]) -> tuple[float, float, int]:
    """按场聚类的均值与 SE：rows=(fixture_key, diff)。同场多向取场内均值。"""
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


def main() -> None:
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)

    print("=" * 76)
    print("H1 FLB 侵蚀差：JC 收盘 vs 欧收盘（同场同向配对）")
    print("=" * 76)
    print(f"配对漏斗: {pstats}")
    print(f"配对成功 {len(pairs)} / JC 侧 {pstats['jc_rows']}（{len(pairs)/pstats['jc_rows']:.0%}）")

    # 分析样本：三个欧锚各自的可用子集
    for anchor_name, key in (("avgc（FD 欧收盘共识·主锚）", "avgc"), ("psc（FD Pinnacle 收盘）", "psc"), ("srct（源T 收盘共识）", "srct")):
        sample = []
        for i, (jc, eu) in enumerate(pairs):
            fair = devig(eu[key])
            euro = eu[key]
            if fair is None or euro is None or min(euro) <= 1.0:
                continue
            hg, ag = eu["hg"], eu["ag"]
            hit = (1.0 if hg > ag else 0.0, 1.0 if hg == ag else 0.0, 1.0 if hg < ag else 0.0)
            sample.append((i, jc, eu, euro, fair, hit))
        if not sample:
            continue
        print()
        print("-" * 76)
        seasons = sorted({eu["season"] for _, _, eu, _, _, _ in sample})
        print(f"锚：{anchor_name}  n={len(sample)} 场  季节 {seasons[0]}…{seasons[-1]}")

        # 全局（平水基线）
        diffs_all = []
        for i, jc, eu, euro, fair, hit in sample:
            for d in range(3):
                diffs_all.append((i, (jc["jc"][d] - euro[d]) * hit[d]))
        g_all, se_all, n_all = clustered_mean(diffs_all)
        print(f"全局侵蚀差（JC−欧，配对 ROI 差）: {g_all:+.4f} ± {se_all:.4f} (SE, 场聚类 n={n_all})")

        # 分桶×向表
        cells: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)
        roi_jc: dict[tuple[int, int], list[float]] = defaultdict(list)
        roi_eu: dict[tuple[int, int], list[float]] = defaultdict(list)
        for i, jc, eu, euro, fair, hit in sample:
            for d in range(3):
                bi = bucket_idx(fair[d])
                if bi < 0:
                    continue
                cells[(bi, d)].append((i, (jc["jc"][d] - euro[d]) * hit[d]))
                roi_jc[(bi, d)].append(jc["jc"][d] * hit[d] - 1.0)
                roi_eu[(bi, d)].append(euro[d] * hit[d] - 1.0)
        print()
        print(f"{'桶':>7} {'向':>4} {'n':>6} {'ROI_JC':>9} {'ROI_欧':>9} {'侵蚀差':>9} {'SE':>7} {'差中差':>9} {'z':>6}")
        for d in range(3):
            for bi in range(len(BUCKET_LABELS)):
                rows = cells.get((bi, d), [])
                if len(rows) < 30:
                    continue
                m, se, n = clustered_mean(rows)
                rj = float(np.mean(roi_jc[(bi, d)]))
                re_ = float(np.mean(roi_eu[(bi, d)]))
                z = (m - g_all) / se if se > 0 else float("nan")
                print(
                    f"{BUCKET_LABELS[bi]:>7} {DIR_CN[DIRS[d]]:>4} {n:>6} {rj:>+9.3%} {re_:>+9.3%} "
                    f"{m:>+9.3%} {se:>7.3%} {m - g_all:>+9.3%} {z:>+6.2f}"
                )
        # 长赔带合并（p<0.30，跨向，场聚类）
        long_rows = [
            (i, (jc["jc"][d] - euro[d]) * hit[d])
            for i, jc, eu, euro, fair, hit in sample
            for d in range(3)
            if fair[d] < 0.30
        ]
        m, se, n = clustered_mean(long_rows)
        z = (m - g_all) / se if se > 0 else float("nan")
        print()
        print(f"[长赔带 p<0.30 合并] n={n} 场：侵蚀差 {m:+.3%} ± {se:.3%}，差中差 {m - g_all:+.3%}，z(对全局)={z:+.2f}")
        # 逐向长赔（门判读用）
        for d in range(3):
            rows = [
                (i, (jc["jc"][d] - euro[d]) * hit[d])
                for i, jc, eu, euro, fair, hit in sample
                if fair[d] < 0.30
            ]
            m, se, n = clustered_mean(rows)
            flag = "n<500" if n < 500 else ""
            print(f"  {DIR_CN[DIRS[d]]}: n={n} 差 {m:+.3%} ± {se:.3%} 差中差 {m - g_all:+.3%} {flag}")

        # 假想 EV：按欧收盘公允找 JC 价值单（jc_odds×fair>1）在 JC 价实现的 ROI
        ev_rows, ev_hits = [], []
        for i, jc, eu, euro, fair, hit in sample:
            for d in range(3):
                if jc["jc"][d] * fair[d] > 1.0:
                    ev_rows.append((i, jc["jc"][d] * hit[d] - 1.0))
        m, se, n = clustered_mean(ev_rows)
        print()
        print(f"[假想 EV 口径] 欧公允×JC价>1 的选择：n={n} 场，实现 ROI {m:+.3%} ± {se:.3%}（JC 抽水摆着，负=无边际）")

        # 逐季同质性
        print()
        print("[逐季侵蚀差（长赔带 p<0.30，场聚类）]")
        by_season: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for i, jc, eu, euro, fair, hit in sample:
            for d in range(3):
                if fair[d] < 0.30:
                    by_season[eu["season"]].append((i, (jc["jc"][d] - euro[d]) * hit[d]))
        for s in sorted(by_season):
            m, se, n = clustered_mean(by_season[s])
            print(f"  {s}: n={n} 差 {m:+.3%} ± {se:.3%}")

    # 未配残差样本（诚实报数）
    print()
    print("[未配 JC 残差样本（前 12，配对口径审计用）]")
    paired_sids = {jc["sid"] for jc, _ in pairs}
    shown = 0
    for jc in jc_rows:
        if jc["sid"] not in paired_sids and shown < 12:
            print(f"  {jc['league']} {jc['home']} vs {jc['away']} {jc['date']}")
            shown += 1


if __name__ == "__main__":
    main()
