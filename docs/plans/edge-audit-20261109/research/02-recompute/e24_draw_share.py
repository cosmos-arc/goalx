"""E-24 复算：H3 平局份额 vs 市场公允（JC SP 比例化去水锚）。

载重数字（账面）：平局投票份额 0.3052 vs 公允 0.2379，+6.7pp（≈8.1SE，
配对 p<0.0001，50/53 场为正）；实际平局率 22.2%。
口径（源报告 edge-program-20261107/analysis_03_h2h3.py 口径转录）：
public_shares 849 行（4 期 ttt14 × 14 场 × 3 向 × 4-6 快照）；终局份额=
每场 captured_at 最新快照；结果=fixtures→draw_results（id 直连
match_codes jingcai 唯一 + ≤2 天；回落=名字精确/前缀 + 时间窗，占位开球
日 ±2 天唯一命中）；公允=sporttery had 最新快照比例化去水。独立实现：
名字回落用前缀规则 + 少量缩写别名（数据口径输入，非计算逻辑）。
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import common

CODE2DIR = {"3": "H", "1": "D", "0": "A"}
# okooo 源B 缩写 → JC 库内全名（口径数据，自源报告口径转录）
ALIAS = {
    "曼城": "曼彻斯特城", "曼联": "曼彻斯特联", "皇马": "皇家马德里",
    "马竞技": "马德里竞技", "日尔曼": "巴黎圣日尔曼", "贝蒂斯": "皇家贝蒂斯",
    "社会": "皇家社会", "拉科鲁": "拉科鲁尼亚", "尤文": "尤文图斯",
    "亚特兰": "亚特兰大", "勒沃": "勒沃库森", "莱比锡": "莱比锡红牛",
    "沙尔克": "沙尔克04", "弗洛西": "弗洛西诺内", "利兹": "利兹联",
    "巴伦西": "巴伦西亚", "伯恩茅": "伯恩茅斯", "艾华斯堡": "埃尔沃斯堡",
}


def names_match(a: str, b: str) -> bool:
    a, b = a.replace(" ", ""), b.replace(" ", "")
    if a == b:
        return True
    return (len(a) >= 2 and b.startswith(a)) or (len(b) >= 2 and a.startswith(b))


def main() -> None:
    con = common.business_ro()
    share_rows = con.execute(
        """
        SELECT s.pool_period_id, s.match_seq, s.selection_code, s.share, s.captured_at,
               p.period_no
        FROM public_shares s JOIN pool_periods p ON p.id = s.pool_period_id
        ORDER BY s.pool_period_id, s.match_seq, s.captured_at
        """
    ).fetchall()
    print(f"public_shares 行: {len(share_rows)}")

    # 场次聚合 + 终局快照
    matches: dict[tuple[int, int], dict] = {}
    snaps: dict[tuple[int, int, str], dict[str, float]] = {}
    for r in share_rows:
        key = (r["pool_period_id"], r["match_seq"])
        if key not in matches:
            pm = con.execute(
                "SELECT source_match_id, kickoff_utc, home_team, away_team FROM pool_matches WHERE pool_period_id=? AND match_seq=?",
                key,
            ).fetchone()
            matches[key] = {
                "period_no": r["period_no"], "kickoff": pm["kickoff_utc"],
                "home": pm["home_team"], "away": pm["away_team"],
                "source_match_id": pm["source_match_id"],
                "final_cap": None, "shares": None, "fixture_id": None, "result": None, "jc": None,
            }
        snaps.setdefault((r["pool_period_id"], r["match_seq"], r["captured_at"]), {})[r["selection_code"]] = r["share"]
        m = matches[key]
        if m["final_cap"] is None or r["captured_at"] > m["final_cap"]:
            m["final_cap"] = r["captured_at"]
    for key, m in matches.items():
        snap = snaps[(key[0], key[1], m["final_cap"])]
        m["shares"] = {CODE2DIR[c]: v for c, v in snap.items()}

    fx_cache: dict[str, list] = {}

    def fixtures_near(day: str) -> list:
        if day not in fx_cache:
            fx_cache[day] = con.execute(
                """
                SELECT f.id, f.kickoff_utc, h.canonical_name AS home, a.canonical_name AS away
                FROM fixtures f JOIN teams h ON h.id=f.home_team_id JOIN teams a ON a.id=f.away_team_id
                WHERE date(f.kickoff_utc) BETWEEN date(?, '-2 day') AND date(?, '+2 day')
                """,
                (day, day),
            ).fetchall()
        return fx_cache[day]

    unresolved = []
    for key in sorted(matches):
        m = matches[key]
        fid = None
        if m["source_match_id"]:
            hits = con.execute(
                """
                SELECT DISTINCT f.id AS fid, f.kickoff_utc AS ko
                FROM fixtures f JOIN match_codes mc ON mc.fixture_id=f.id
                WHERE mc.kind='jingcai' AND mc.source_match_id=?
                """,
                (m["source_match_id"],),
            ).fetchall()
            if len(hits) == 1:
                d_pool = dt.datetime.fromisoformat(m["kickoff"]).date()
                d_fix = dt.datetime.fromisoformat(hits[0]["ko"]).date()
                if abs((d_fix - d_pool).days) <= 2:
                    fid = hits[0]["fid"]
        if fid is None:
            base = dt.datetime.fromisoformat(m["kickoff"])
            placeholder = base.hour == 16 and base.minute == 0
            fh = ALIAS.get(m["home"], m["home"])
            fa = ALIAS.get(m["away"], m["away"])
            cands = []
            for f in fixtures_near(base.date().isoformat()):
                fko = dt.datetime.fromisoformat(f["kickoff_utc"])
                if not placeholder and abs((fko - base).total_seconds()) > 3 * 3600:
                    continue
                if names_match(fh, f["home"]) and names_match(fa, f["away"]):
                    cands.append(f)
            fids = {c["id"] for c in cands}
            if len(fids) == 1:
                fid = next(iter(fids))
        if fid is None:
            unresolved.append(m)
            continue
        dr = con.execute(
            "SELECT home_goals, away_goals, void FROM draw_results WHERE fixture_id=?", (fid,)
        ).fetchone()
        if dr is None or dr["void"]:
            unresolved.append(m)
            continue
        m["fixture_id"] = fid
        m["result"] = "H" if dr["home_goals"] > dr["away_goals"] else ("A" if dr["home_goals"] < dr["away_goals"] else "D")
        od = con.execute(
            """
            SELECT selection_code, odds FROM odds_snapshots
            WHERE fixture_id=? AND market_code='had' AND source='sporttery'
              AND captured_at = (SELECT MAX(captured_at) FROM odds_snapshots
                                 WHERE fixture_id=? AND market_code='had' AND source='sporttery')
            """,
            (fid, fid),
        ).fetchall()
        o = {r["selection_code"]: r["odds"] for r in od}
        if len(o) == 3:
            m["jc"] = (o["h"], o["d"], o["a"])
    con.close()

    sample = [m for m in matches.values() if m["result"]]
    print(f"场次数 {len(matches)}；结果解析 {len(sample)}；未解析 {len(unresolved)}")
    for m in unresolved:
        print(f"  未解析: 期{m['period_no']} {m['home']}-{m['away']}（{m['kickoff'][:10]}）")

    # H3 主检验：平局份额 vs JC 公允（比例化去水），配对差
    share_d, fair_d, diffs = [], [], []
    for m in sample:
        if not m["jc"]:
            continue
        inv = np.array([1.0 / o for o in m["jc"]])
        fair = inv / inv.sum()
        share_d.append(m["shares"]["D"])
        fair_d.append(float(fair[1]))
        diffs.append(m["shares"]["D"] - float(fair[1]))
    diffs = np.array(diffs)
    se = diffs.std(ddof=1) / math.sqrt(len(diffs))
    tstat = diffs.mean() / se if se > 0 else float("nan")
    print(f"\n[H3 平局份额−公允（JC SP 比例化去水锚）]")
    print(f"  n={len(diffs)} 平局份额均值 {np.mean(share_d):.4f} vs 公允均值 {np.mean(fair_d):.4f}")
    print(f"  配对差均值 {diffs.mean():+.4f}（{diffs.mean() * 100:+.1f}pp）SE={se:.4f} → t≈{tstat:.2f}（{abs(diffs.mean() / se):.1f}SE）")
    print(f"  逐场为正 {(diffs > 0).sum()}/{len(diffs)}；为负 {(diffs < 0).sum()}")
    draw_rate = np.mean([1.0 if m["result"] == "D" else 0.0 for m in sample])
    print(f"  实际平局率 {draw_rate:.4%}（{sum(1 for m in sample if m['result'] == 'D')}/{len(sample)}）")
    # 三向行为层（H/A 对侧，账面 −4.9pp 热门佐证）
    for d, pos in (("H", 0), ("A", 2)):
        ds = []
        for m in sample:
            if not m["jc"]:
                continue
            inv = np.array([1.0 / o for o in m["jc"]])
            fair = inv / inv.sum()
            ds.append(m["shares"][d] - float(fair[pos]))
        ds = np.array(ds)
        print(f"  行为层 {d} 向份额−公允: {ds.mean():+.4f}（SE {ds.std(ddof=1)/math.sqrt(len(ds)):.4f}）")


if __name__ == "__main__":
    main()
