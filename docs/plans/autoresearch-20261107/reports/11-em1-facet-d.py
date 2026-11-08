#!/usr/bin/env python3
# 票 11：EM-1 facet D LLM 软信息面——小样本探查后关闭（H6 前提 refuted）
#
# 票文门控条款：票 08 第二段 H6 先行存在性 refuted（事件后 JC 调盘率≈基率
# 且方向不一致）→ 本票降级为小样本探查后关闭（refuted-by-prerequisite）。
# 探查=诚实描述性：伤停/停赛事件的「JC 未调价而欧锚已动」子集内，信号
# 方向侧的 gap 与实现 EV——不进三窗挖掘、不开确认窗（样本账先钉）。
# 样本账：复用 silver news_event 1,011 条既有 LLM 抽取（glm-5.3-flash v3），
# 本票零新增 LLM 调用/零成本。

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np

from _lib import NEWS_EVENTS, devig, load_jc, load_jc_traj, load_euro, load_pin_1x2, pair_fixtures

H6_WINDOW = dt.timedelta(hours=6)
EURO_MOVE = 0.01  # 欧锚已动门槛：隐含 1pp


def quote_at(traj, t):
    lo, hi = 0, len(traj) - 1
    res = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if traj[mid][0] <= t:
            res = traj[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return res


def main() -> None:
    print("=" * 76)
    print("票 11：EM-1 facet D（LLM 软信息）小样本探查——refuted-by-prerequisite 关闭")
    print("=" * 76)
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)
    sid2fx = {eu["sid"]: (i, jc, eu) for i, (jc, eu) in enumerate(pairs)}
    jc_had = load_jc_traj([jc["sid"] for jc, _ in pairs], "had")
    pin = load_pin_1x2([eu["sid"] for _, eu in pairs])
    con = duckdb.connect()
    evs = con.sql(
        f"""
        select sid, published_at, event_type, side, subtype
        from read_parquet('{NEWS_EVENTS}')
        where event_type in ('injury', 'suspension') and subtype != 'return'
        """
    ).fetchall()
    con.close()
    cst = dt.timezone(dt.timedelta(hours=8))
    acct = {"events": len(evs), "no_pair": 0, "no_traj": 0, "outside_jc_window": 0, "analyzed": 0,
            "jc_moved_subset": 0, "probe_subset_euro_moved_jc_not": 0}
    probe_rows = []  # (fx, gap_at_end, ev_real_signal_side)
    for sid, t_pub_s, _etype, side, _sub in evs:
        hit = sid2fx.get(sid)
        if hit is None:
            acct["no_pair"] += 1
            continue
        fx, jc, eu = hit
        t_pub = dt.datetime.fromisoformat(t_pub_s.replace("Z", "+00:00")).astimezone(cst)
        jtr, ptr = jc_had.get(jc["sid"]), pin.get(eu["sid"])
        if not jtr or not ptr:
            acct["no_traj"] += 1
            continue
        t0, t1 = jtr[0][0], jtr[-1][0]
        if not (t0 + dt.timedelta(minutes=5) <= t_pub <= t1 - dt.timedelta(minutes=30)):
            acct["outside_jc_window"] += 1
            continue
        acct["analyzed"] += 1
        w_end = min(t_pub + H6_WINDOW, t1)
        jc_moved = any(t_pub < t_j <= w_end for t_j, _ in jtr)
        if jc_moved:
            acct["jc_moved_subset"] += 1
            continue
        # 探查子集：JC 未调价；欧锚（Pinnacle）在窗内是否已动（信号方向=伤停方走弱）
        k = 0 if side == "home" else 2
        q_pre_pin = quote_at(ptr, t_pub)
        q_end_pin = quote_at(ptr, w_end)
        if q_pre_pin is None or q_end_pin is None:
            continue
        f_pre, f_end = devig(q_pre_pin[1]), devig(q_end_pin[1])
        q_jc = quote_at(jtr, w_end)
        if f_pre is None or f_end is None or q_jc is None:
            continue
        euro_d = f_end[k] - f_pre[k]  # <0 = 欧锚同意伤停方走弱
        if euro_d > -EURO_MOVE:
            continue  # 欧锚未按信号方向动——不在「JC 未反应而锚已动」子集
        acct["probe_subset_euro_moved_jc_not"] += 1
        gap = 1.0 / q_jc[1][k] - f_end[k]  # JC 隐含 − 锚公允（伤停方）
        # 实现值：探查只报伤停方下注 EV（描述性）
        hg, ag = eu["hg"], eu["ag"]
        hit_k = (1.0 if hg > ag else 0.0) if k == 0 else (1.0 if hg == ag else 0.0) if k == 1 else (1.0 if hg < ag else 0.0)
        probe_rows.append((fx, gap, q_jc[1][k] * hit_k - 1.0))
    print(f"事件账：{acct}")
    if probe_rows:
        gaps = np.array([g for _, g, _ in probe_rows])
        evs_r = np.array([e for _, _, e in probe_rows])
        print(f"探查子集 n={len(probe_rows)}：伤停方 gap(JC−锚@窗末) 均值 {gaps.mean():+.3%} ± {gaps.std(ddof=1)/np.sqrt(len(gaps)):.3%}")
        print(f"  伤停方下注实现 EV {evs_r.mean():+.3%}（描述性，无门判读——票已按前提关闭）")
    else:
        print("探查子集 n=0（JC 未调价∧欧锚按信号方向动 的交集为空）")


if __name__ == "__main__":
    main()
