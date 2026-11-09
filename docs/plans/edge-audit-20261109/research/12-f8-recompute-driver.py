#!/usr/bin/env python3
"""票 12 F8 定点重算驱动（edge-audit-20261109）。

四臂归因：旧/新 OU 锚 × 旧/新 7+ 结算，对冻结真树只读复算 F8 全窗指标。
- 臂 OO=旧锚+旧结算：必须逐格复现冻结台账（09-em1-trial-ledger.csv 的
  09-A-F8-* 行）→ 证明树漂移不触 F8 行集、旧算法复现无误；
- 臂 NN=新锚+新结算：搜索窗数字必须与修后 09 脚本 stdout 一致；
- 臂 ON / NO：分离两修各自贡献；交互=NN−ON−NO+OO。

用法：uv run --no-sync python 12-f8-recompute-driver.py（工作树 reports 目录在 sys.path）
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict

import numpy as np

REPORTS = "/Users/chevy/Desktop/code/goalx-ea12/docs/plans/autoresearch-20261107/reports"
sys.path.insert(0, REPORTS)

from _lib import (  # noqa: E402
    OU_BOOKS,
    aggregate_ou_ladder,
    clustered_mean,
    devig,
    load_jc,
    load_euro,
    pair_fixtures,
    ttg_basket_ev,
)

DUCK = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
LEDGER = f"{REPORTS}/09-em1-trial-ledger.csv"
N_FX_FLOOR = 200


def window_of(season: str | None) -> str | None:
    if not season or len(season) < 4:
        return None
    y0 = int(season[:4])
    if y0 <= 2021:
        return "search"
    if y0 in (2022, 2023):
        return "validation"
    if y0 in (2024, 2025):
        return "confirmation"
    return None


def load_ou25_raw(sids: list[str]):
    """2.5 线全部合格报价行（与新旧装载同过滤），含 source_order 供平序裁决。"""
    import duckdb

    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in sids])
    books_sql = ",".join(f"'{b}'" for b in OU_BOOKS)
    rows = con.sql(
        f"""
        select o.sid, o.bookmaker_id, o.published_at, o.source_order,
               o.home_water, o.away_water
        from odds_change_event o join sids s using (sid)
        where o.market = 'ou' and o.bookmaker_id in ({books_sql})
          and o.status in ('pre', 'early') and o.published_at is not null
          and o.line = 2.5
          and o.home_water between 0.25 and 1.75
          and o.away_water between 0.25 and 1.75
        order by o.sid, o.published_at, o.source_order
        """
    ).fetchall()
    con.close()
    return rows


def old_anchor(sids: list[str], raw_rows):
    """旧口径精确复现：原 SQL 的两个独立 arg_max(列, published_at)（无书维）。

    平序时 hw/aw 可能取自不同书的同刻行（混合水位嵌合价）——这是旧锚的
    真实行为，必须按原样复现才能对上冻结台账。
    """
    import duckdb

    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in sids])
    books_sql = ",".join(f"'{b}'" for b in OU_BOOKS)
    rows = con.sql(
        f"""
        select sid, arg_max(home_water, published_at) as hw,
               arg_max(away_water, published_at) as aw
        from odds_change_event o join sids s using (sid)
        where o.market = 'ou' and o.bookmaker_id in ({books_sql})
          and o.status in ('pre', 'early') and o.published_at is not null
          and o.line = 2.5
          and o.home_water between 0.25 and 1.75
          and o.away_water between 0.25 and 1.75
        group by sid
        """
    ).fetchall()
    con.close()
    out = {}
    for sid, hw, aw in rows:
        d_o, d_u = 1.0 + hw, 1.0 + aw
        out[sid] = (1.0 / d_o) / (1.0 / d_o + 1.0 / d_u)
    # 诊断：末刻平序（≥2 行异价）暴露率 + hw/aw 嵌合（无任何单行同时=arg_max 对）率
    by_sid: dict[str, list[tuple]] = defaultdict(list)
    for sid, _bk, t, _so, hw2, aw2 in raw_rows:
        by_sid[sid].append((t, hw2, aw2))
    n_tie = n_chim = 0
    for sid, hw, aw in rows:
        rows_s = by_sid.get(sid, [])
        if not rows_s:
            continue
        tmax = max(r[0] for r in rows_s)
        at_max = [r for r in rows_s if r[0] == tmax]
        if len({(r[1], r[2]) for r in at_max}) > 1:
            n_tie += 1
        if not any(r[1] == hw and r[2] == aw for r in at_max):
            n_chim += 1
    print(f"  旧锚诊断：末刻多行异价（平序暴露）{n_tie}/{len(rows)} 场；hw/aw 嵌合（合成非真实报价对）{n_chim}/{len(rows)} 场")
    return out


def new_anchor(rows):
    """新口径：每书末价→跨书均值（直接走 _lib.aggregate_ou_ladder）。"""
    agg = aggregate_ou_ladder(
        (sid, bk, 2.5, t, None, hw, aw) for sid, bk, t, _so, hw, aw in rows
    )
    return {sid: lad[2.5].p_over for sid, lad in agg.items()}, agg


def old_basket_ev(odds, t, ks):
    return sum(odds[k] * (1.0 if t == k else 0.0) for k in ks) / len(ks) - 1.0


def f8_metrics_of(sub):
    n_fx = len({r["fx"] for r in sub})
    sel = [r for r in sub if r["ev_anchor"] > 0]
    em, es, _ = clustered_mean([(r["fx"], r["ev_real"]) for r in sel])
    gm, gs, _ = clustered_mean([(r["fx"], r["gap"]) for r in sub])
    return {
        "n_fx": n_fx, "n_sel": len(sel), "gap": gm, "gap_se": gs, "ev": em, "ev_se": es,
        "ev_z": (em / es if es and es > 0 and len(sel) >= 2 else float("nan")),
    }


def f8_slices(sample):
    out = [("over", [r for r in sample if r["basket"] == "over"]), ("under", [r for r in sample if r["basket"] == "under"])]
    for lg in sorted({r["league"] for r in sample}):
        for b in ("over", "under"):
            out.append((f"{lg}×{b}", [r for r in sample if r["league"] == lg and r["basket"] == b]))
    return out


def main() -> None:
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, _ = pair_fixtures(jc_rows, eu_rows)
    in_win = [(fx, jc, eu) for fx, (jc, eu) in enumerate(pairs) if window_of(eu["season"])]
    raw = load_ou25_raw([eu["sid"] for _fx, _jc, eu in in_win])
    p_old = old_anchor([eu["sid"] for _fx, _jc, eu in in_win], raw)
    p_new, rungs = new_anchor(raw)
    assert set(p_old) == set(p_new), "新旧锚覆盖的 sid 集应一致（同一过滤行集）"

    arms = ("OO", "ON", "NO", "NN")
    tt_rows: dict[str, list[dict]] = {a: [] for a in arms}
    meta = []  # 每场一行（锚无关事实：t、odds、f8、league、window）
    n_no_ladder = 0
    for fx, jc, eu in in_win:
        w = window_of(eu["season"])
        if jc["ttg"] is None:
            continue
        sid = eu["sid"]
        if sid not in p_old:
            n_no_ladder += 1
            continue
        t = eu["hg"] + eu["ag"]
        f8 = devig(jc["ttg"])
        if f8 is None:
            continue
        meta.append({"fx": fx, "sid": sid, "window": w, "league": eu["league"], "t": t, "ttg": jc["ttg"], "f8": f8})
    print(f"配对入窗 {len(in_win)}；无 2.5 板 {n_no_ladder}；F8 行（场×篮）= {len(meta)*2}")

    for m in meta:
        for basket, ks in (("over", list(range(3, 8))), ("under", list(range(0, 3)))):
            jc_basket = sum(m["f8"][k] for k in ks)
            for a in arms:
                p = p_old[m["sid"]] if a[0] == "O" else p_new[m["sid"]]
                fair_b = p if basket == "over" else 1.0 - p
                ev_real = (
                    old_basket_ev(m["ttg"], m["t"], ks)
                    if a[1] == "O"
                    else ttg_basket_ev(m["ttg"], m["t"], ks)
                )
                ev_anchor = fair_b * sum(m["f8"][k] * m["ttg"][k] for k in ks) / (jc_basket * len(ks)) - 1.0
                tt_rows[a].append({
                    "fx": m["fx"], "window": m["window"], "league": m["league"], "basket": basket,
                    "gap": jc_basket - fair_b, "ev_real": ev_real, "ev_anchor": ev_anchor,
                })

    # ---- 指标四臂 × 全窗全切片
    metrics: dict[tuple[str, str, str], dict] = {}
    for a in arms:
        for w in ("search", "validation", "confirmation"):
            wsample = [r for r in tt_rows[a] if r["window"] == w]
            for sname, sub in f8_slices(wsample):
                if not sub:
                    continue
                metrics[(a, sname, w)] = f8_metrics_of(sub)

    # ---- Phase A：臂 OO vs 冻结台账（逐格对）
    frozen = {}
    with open(LEDGER) as f:
        for row in csv.DictReader(f):
            if row["family"] == "F8":
                ev, gap = row["value"].split("/")
                frozen[(row["slice"], row["window"])] = {
                    "n_fx": int(row["n_fx"]), "n_sel": int(row["n_sel"]),
                    "ev": float(ev), "gap": float(gap), "se": float(row["se"]) if row["se"] else float("nan"),
                    "z": float(row["z"]) if row["z"] else float("nan"),
                }
    mism = 0
    print("\n== Phase A：臂 OO（旧锚+旧结算）vs 冻结台账 ==")
    for (sname, w), fr in sorted(frozen.items()):
        m = metrics.get(("OO", sname, w))
        if m is None:
            print(f"  MISSING OO {sname} {w}")
            mism += 1
            continue
        ok = (
            m["n_fx"] == fr["n_fx"] and m["n_sel"] == fr["n_sel"]
            and abs(m["ev"] - fr["ev"]) < 5e-5 and abs(m["gap"] - fr["gap"]) < 5e-5
            and (np.isfinite(fr["z"]) and abs(m["ev_z"] - fr["z"]) < 5e-4 or not np.isfinite(fr["z"]) and not np.isfinite(m["ev_z"]))
        )
        if not ok:
            mism += 1
            print(f"  MISMATCH {sname} {w}: 冻结(n_fx={fr['n_fx']},n_sel={fr['n_sel']},ev={fr['ev']:.4f},gap={fr['gap']:.4f},z={fr['z']}) vs OO({m['n_fx']},{m['n_sel']},{m['ev']:.4f},{m['gap']:.4f},{m['ev_z']:.3f})")
    print(f"  Phase A mismatch={mism}/{len(frozen)}")

    # ---- 指标对照表（全窗主行 + 联赛行），四臂
    print("\n== 四臂对照（ev/gap/z/n_sel；OO=冻结复现 ON=+7%修 NO=+锚修 NN=双修=重算） ==")
    for w in ("search", "validation", "confirmation"):
        print(f"-- window={w}")
        for sname in ["over", "under"] + [f"{lg}×{b}" for lg in ("德甲", "意甲", "法甲", "苏超", "英冠", "英超", "荷甲", "葡超", "西甲") for b in ("over", "under")]:
            row = [sname]
            for a in arms:
                m = metrics.get((a, sname, w))
                if m is None:
                    row.append("--")
                else:
                    ev = f"{m['ev']:+.4f}" if np.isfinite(m["ev"]) else "nan"
                    z = f"{m['ev_z']:+.3f}" if np.isfinite(m["ev_z"]) else "nan"
                    row.append(f"{ev}/{m['gap']:+.4f}/{z}/n{m['n_sel']}")
            print("  " + f"{row[0]:>8}: " + " | ".join(row[1:]))

    # ---- 归因：结算效应（ON−OO）与锚效应（NO−OO）的主行级分解
    print("\n== 主行归因（Δev = 臂−OO） ==")
    for w in ("search", "validation", "confirmation"):
        for sname in ("over", "under"):
            mo, mon, mn, mnn = (metrics.get((a, sname, w)) for a in arms)
            if mo is None:
                continue
            def d(m_x, key):
                return (m_x[key] - mo[key]) if m_x else float("nan")
            print(f"  {w:>13} {sname:>5}: Δev(7+修)={d(mon,'ev'):+.4f} Δev(锚修)={d(mn,'ev'):+.4f} Δev(合计/NN)={d(mnn,'ev'):+.4f} | Δgap(锚修)={d(mn,'gap'):+.4f} | n_sel {mo['n_sel']}→{mnn['n_sel'] if mnn else '--'}")

    # ---- 记录级：7+ 修（t>=8 的 over 行）
    print("\n== 7+ 修记录级：t>=8 的场（over 篮 7+ 腿回款；under 篮两径皆 0 无差） ==")
    n8 = defaultdict(int)
    pay8 = defaultdict(float)
    examples = []
    for m in meta:
        if m["t"] >= 8:
            key = (m["window"], "over")
            n8[key] += 1
            pay8[key] += m["ttg"][7] / 5 - 0.0
            if len(examples) < 12:
                examples.append(m)
    for k in sorted(n8):
        print(f"  {k[0]:>13} {k[1]:>5}: t>=8 场数={n8[k]}；每场 ev_real 增量=odds[7]/5（合计 {pay8[k]:+.2f} 单位注）")
    for m in examples:
        print(f"    例: fx={m['fx']} sid={m['sid']} {m['league']} {m['window']} t={m['t']} s7={m['ttg'][7]:.1f} Δev_real={m['ttg'][7]/5:+.4f}")

    # t==7 的场不受影响（旧新同 hit）；t in 3..6 同。核对：仅 t>=8 有差
    for m in meta:
        for ks, basket in ((list(range(3, 8)), "over"), (list(range(0, 3)), "under")):
            o, n = old_basket_ev(m["ttg"], m["t"], ks), ttg_basket_ev(m["ttg"], m["t"], ks)
            if abs(o - n) > 1e-12 and not (basket == "over" and m["t"] >= 8):
                raise AssertionError(f"结算差出现在非 t>=8 行: {m['sid']} t={m['t']} basket={basket}")
    print("  校验：结算差只出现在 over×t>=8 行 ✔")

    # ---- 记录级：锚修（每场 Δp_over25）
    print("\n== 锚修记录级：Δp_over25（new−old）分布 ==")
    deltas = []
    for m in meta:
        deltas.append((p_new[m["sid"]] - p_old[m["sid"]], m["sid"], m["league"], m["window"]))
    arr = np.array([d for d, *_ in deltas])
    print(f"  n={len(arr)} mean={arr.mean():+.5f} p50={np.percentile(arr,50):+.5f} p05={np.percentile(arr,5):+.5f} p95={np.percentile(arr,95):+.5f} max|Δ|={np.abs(arr).max():.5f}")
    by_w = defaultdict(list)
    for d, _s, _l, w in deltas:
        by_w[w].append(d)
    for w in ("search", "validation", "confirmation"):
        a2 = np.array(by_w[w])
        print(f"  {w:>13}: mean={a2.mean():+.5f} |Δ|>1pp 率={np.mean(np.abs(a2)>0.01):.3%} |Δ|>5pp 率={np.mean(np.abs(a2)>0.05):.3%}")
    print("  Δp 最强 10 场（含每书末价明细见下）:")
    top = sorted(deltas, key=lambda x: -abs(x[0]))[:10]
    for d, sid, lg, w in top:
        print(f"    sid={sid} {lg} {w}: Δ={d:+.5f} old={p_old[sid]:.5f} new={p_new[sid]:.5f} books={rungs[sid][2.5].n_books} age_h={rungs[sid][2.5].age_h:.1f}")

    # ---- 记录级：选择集变化（ev_anchor>0 成员差）
    print("\n== 锚修记录级：价值侧选择集变化（ev_anchor>0） ==")
    sel_old = {(r["fx"], r["window"], r["league"], r["basket"]) for r in tt_rows["OO"] if r["ev_anchor"] > 0}
    sel_new = {(r["fx"], r["window"], r["league"], r["basket"]) for r in tt_rows["NN"] if r["ev_anchor"] > 0}
    added, dropped = sel_new - sel_old, sel_old - sel_new
    by_key = defaultdict(lambda: {"add": 0, "drop": 0})
    for fx, w, lg, b in added:
        by_key[(w, b)]["add"] += 1
    for fx, w, lg, b in dropped:
        by_key[(w, b)]["drop"] += 1
    for k in sorted(by_key):
        print(f"  {k[0]:>13} {k[1]:>5}: +{by_key[k]['add']} / −{by_key[k]['drop']}")
    # 变动行的 ev_real（新结算）分布，看它们把均值拉向哪边
    rows_by_key = {(r["fx"], r["window"], r["league"], r["basket"]): r for r in tt_rows["NN"]}
    print("  新增行（新结算 ev_real）：")
    for fx, w, lg, b in sorted(added)[:20]:
        r = rows_by_key[(fx, w, lg, b)]
        print(f"    fx={fx} {lg} {w} {b}: ev_real={r['ev_real']:+.4f} gap={r['gap']:+.5f}")
    print("  剔除行（新结算 ev_real，供参照）：")
    for fx, w, lg, b in sorted(dropped)[:20]:
        r = rows_by_key[(fx, w, lg, b)]
        print(f"    fx={fx} {lg} {w} {b}: ev_real={r['ev_real']:+.4f} gap={r['gap']:+.5f}")

    # ---- 每书末价明细（Δp 最大 3 场 + 随机 2 场）
    print("\n== 每书末价明细样例 ==")
    import duckdb

    con = duckdb.connect(DUCK, read_only=True)
    for d, sid, lg, w in sorted(deltas, key=lambda x: -abs(x[0]))[:3]:
        books_sql = ",".join(f"'{b}'" for b in OU_BOOKS)
        rows = con.sql(
            f"""
            select bookmaker_id, published_at, home_water, away_water, source_order
            from odds_change_event
            where sid = '{sid}' and market = 'ou' and bookmaker_id in ({books_sql})
              and status in ('pre','early') and published_at is not null and line = 2.5
              and home_water between 0.25 and 1.75 and away_water between 0.25 and 1.75
            order by published_at, source_order
            """
        ).fetchall()
        per_book: dict[str, tuple] = {}
        for bk, t, hw, aw, so in rows:
            per_book[bk] = (t, hw, aw)
        print(f"  sid={sid} {lg} {w} Δ={d:+.5f} old={p_old[sid]:.5f} new={p_new[sid]:.5f}")
        for bk, (t, hw, aw) in sorted(per_book.items()):
            d_o, d_u = 1.0 + hw, 1.0 + aw
            print(f"    {bk}: last@{t} hw={hw:.2f} aw={aw:.2f} p_over={(1/d_o)/(1/d_o+1/d_u):.5f}")
    con.close()

    # ---- 锚修对新旧行集的 gap 影响核对（主行）
    print("\n== gap 主行核对：Δgap(锚修) vs mean(∓Δp) ==")
    for w in ("search", "validation", "confirmation"):
        dp = float(np.mean([p_new[m2["sid"]] - p_old[m2["sid"]] for m2 in meta if m2["window"] == w]))
        for b in ("over", "under"):
            m_o = metrics.get(("OO", b, w))
            m_n = metrics.get(("NO", b, w))
            if not m_o or not m_n:
                continue
            # over: Δgap=−Δp；under: Δgap=+Δp
            pred = -dp if b == "over" else dp
            print(f"  {w:>13} {b:>5}: Δgap={m_n['gap']-m_o['gap']:+.5f} | 场均Δp={dp:+.5f} 预测={pred:+.5f}")


if __name__ == "__main__":
    main()
