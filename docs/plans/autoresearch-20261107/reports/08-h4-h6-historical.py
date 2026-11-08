#!/usr/bin/env python3
# 票 08：H4 调盘滞后率（第一段）+ H6 情报先行（第二段）——历史面回放
#（autoresearch-20261107 批次 α）
#
# 纯离线只读研究脚本（票 06/07 先例）：零写任何库。产出打印 stdout，
# 供 08-h4-h6-historical.md 引用。配对口径=票 07（_lib.pair_fixtures）。
#
# 第一段 H4（had 主面 / hhad 整数线副面）：
# - 欧盘动 = Pinnacle(srct:1x2:177) 1x2 相邻快照，任一向隐含概率变动
#   |Δ(1/o)|≥0.5pp，时刻=后快照 published_at，须落在 JC 销售窗内且距
#   JC 收笔 ≥60min（可观测尾巴，否则不计入分母并报剔除账）。
# - JC 跟随 = JC had 同向首个变价（任意 tick）；滞后=t_jc−t_euro。
# - 指标：跟随率、滞后>15min 案例率（H4 门>10% 且逐季稳定）、滞后分布、
#   反向先行率；切片=逐季/联赛/销售窗前中后段/变动幅度/玩法(hhad)。
# 第二段 H6（LLM 已抽取事件复用，零新增 LLM 成本）：
# - 样本=silver/guardian/news_event（glm-5.3-flash v3 抽取）伤停/停赛/
#   轮换类事件，subtype='return' 剔除；事件时点 ∈ JC 销售窗且距收笔≥30min。
# - 反应=事件后 6h 窗（截 JC 收笔）内 JC had 任一变价；方向一致=事件方
#   胜率隐含净变动<0（伤停方走弱）。
# - 对照=同场次销售窗内非事件 6h 窗（±6h 避让本場所有事件），K=3/事件。
# - 门（注册表 H6）：案例率>0 且方向一致>55%；诚实判读=反应率须显著
#   高于对照基率（否则“先行”只是常规调盘）。

from __future__ import annotations

import datetime as dt
import random
from collections import Counter, defaultdict

import duckdb
import numpy as np

from _lib import (
    DIR_CN,
    DUCK,
    NEWS_EVENTS,
    load_jc,
    load_jc_traj,
    load_euro,
    load_pin_1x2,
    load_pin_ah,
    pair_fixtures,
)

EURO_MOVE_MIN = 0.005  # 欧盘动门槛：隐含概率 0.5pp
JC_TICK = 0.0002  # JC 跟随门槛：约 odds 7 一档 0.01 的隐含变动
OBS_TAIL = dt.timedelta(minutes=60)  # 欧盘动须距 JC 收笔 ≥60min
OBS_HEAD = dt.timedelta(minutes=5)  # 距 JC 开售 ≥5min
LAG_GATE = dt.timedelta(minutes=15)  # H4 注册门：滞后>15min
H6_WINDOW = dt.timedelta(hours=6)  # H6 反应窗
H6_TAIL = dt.timedelta(minutes=30)  # 事件距 JC 收笔 ≥30min


def imp(o: float) -> float:
    return 1.0 / o


def seg1_had(pairs, pin_traj, jc_had):
    """第一段主面：Pinnacle 1x2 动 → JC had 同向首动滞后。"""
    cases = []  # dict: fx, season, league, third, k, size, cls, lag_min, opp_first
    excl_short = excl_tail = 0
    n_fx_used = 0
    for fx, (jc, eu) in enumerate(pairs):
        ptr = pin_traj.get(eu["sid"])
        jtr = jc_had.get(jc["sid"])
        if not ptr or not jtr or len(ptr) < 2 or len(jtr) < 2:
            continue
        t0, t1 = jtr[0][0], jtr[-1][0]
        if t1 - t0 < dt.timedelta(hours=2):
            excl_short += 1
            continue
        n_fx_used += 1
        span = (t1 - t0).total_seconds()
        for i in range(1, len(ptr)):
            t_prev, q_prev = ptr[i - 1]
            t, q = ptr[i]
            if t < t0 + OBS_HEAD or t > t1 - OBS_TAIL:
                if t0 + OBS_HEAD <= t <= t1:
                    excl_tail += 1
                continue
            for k in range(3):
                d = imp(q[k]) - imp(q_prev[k])
                if abs(d) < EURO_MOVE_MIN:
                    continue
                # JC 同向首动：扫事件后 JC 首个同向 tick
                cls, lag_min, opp_first = "no_follow", float("nan"), False
                prev_q = None
                for t_j, q_j in jtr:
                    if t_j <= t:
                        prev_q = q_j
                        continue
                    dj = imp(q_j[k]) - imp(prev_q[k])
                    if dj != 0:
                        if np.sign(dj) == np.sign(d):
                            lag = (t_j - t).total_seconds() / 60.0
                            cls = "le15" if lag <= 15 else ("15_60" if lag <= 60 else "gt60")
                            lag_min = lag
                            break
                        opp_first = True
                    prev_q = q_j
                third = "早" if (t - t0).total_seconds() / span < 1 / 3 else ("中" if (t - t0).total_seconds() / span < 2 / 3 else "晚")
                size = "0.5-1pp" if abs(d) < 0.01 else ("1-3pp" if abs(d) < 0.03 else "≥3pp")
                cases.append(
                    {
                        "fx": fx,
                        "season": eu["season"],
                        "league": eu["league"],
                        "third": third,
                        "k": k,
                        "size": size,
                        "dir": "升" if d > 0 else "降",
                        "cls": cls,
                        "lag": lag_min,
                        "opp": opp_first,
                    }
                )
    return cases, {"fixtures_used": n_fx_used, "excl_short_window": excl_short, "excl_no_tail": excl_tail}


def summarize_cases(cases, title):
    print(f"\n[{title}] 案例 n={len(cases)}")
    if not cases:
        print("  （无案例）")
        return
    ct = Counter(c["cls"] for c in cases)
    n = len(cases)
    follow = n - ct["no_follow"]
    lag15 = ct["15_60"] + ct["gt60"]
    print(f"  跟随率 {follow/n:.1%}（{follow}）；滞后>15min 案例率 {lag15/n:.1%}（{lag15}）——H4 门>10%")
    print(f"  分类：≤15min {ct['le15']} / 15-60min {ct['15_60']} / >60min {ct['gt60']} / 无跟随 {ct['no_follow']}")
    print(f"  反向先行（JC 先逆后从）{sum(1 for c in cases if c['opp'])} 例")
    lags = [c["lag"] for c in cases if c["cls"] != "no_follow"]
    if lags:
        print(f"  滞后分布（跟随者）：min {min(lags):.0f}min / 中位 {np.median(lags):.0f}min / p90 {np.percentile(lags, 90):.0f}min / max {max(lags):.0f}min")
    for key in ("season", "league", "third", "size", "dir", "k"):
        by: dict[str, list[dict]] = defaultdict(list)
        for c in cases:
            by[str(c[key])].append(c)
        rows = []
        for g, cs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            if len(cs) < 50:
                continue
            lag15g = sum(1 for c in cs if c["cls"] in ("15_60", "gt60"))
            follow_g = sum(1 for c in cs if c["cls"] != "no_follow")
            lg = [c["lag"] for c in cs if c["cls"] != "no_follow"]
            med = f"{np.median(lg):.0f}min" if lg else "-"
            rows.append(f"    {g:>8}: n={len(cs):>5} 跟随率 {follow_g/len(cs):>5.1%} 滞后>15m率 {lag15g/len(cs):>5.1%} 滞后中位 {med}")
        if rows:
            print(f"  按 {key} 切片（n≥50）：")
            print("\n".join(rows))


def seg1_hhad(pairs, pin_ah, jc_hhad):
    """第一段副面：Pinnacle ah 同线水位动 → JC hhad 同线同向首动。"""
    cases = []
    n_line_matched = 0
    for fx, (jc, eu) in enumerate(pairs):
        jtr = jc_hhad.get(jc["sid"])
        atr = pin_ah.get(eu["sid"])
        if not jtr or not atr or len(jtr) < 2:
            continue
        jc_lines = {q[0] for _, q in jtr}
        if len(jc_lines) != 1:
            continue  # JC 线变动场剔除（少数）
        line = next(iter(jc_lines))
        same = [(t, hw, aw) for t, lp, hw, aw in atr if abs(lp - line) < 1e-9]
        if len(same) < 2:
            continue
        n_line_matched += 1
        t0, t1 = jtr[0][0], jtr[-1][0]
        if t1 - t0 < dt.timedelta(hours=2):
            continue
        for i in range(1, len(same)):
            t_prev, hw_prev, _aw_prev = same[i - 1]
            t, hw, _aw = same[i]
            if t < t0 + OBS_HEAD or t > t1 - OBS_TAIL:
                continue
            d = hw - hw_prev  # home 水位变动（水位=赔率口径）
            if abs(d) < 0.02:
                continue
            cls, lag_min = "no_follow", float("nan")
            prev_q = None
            for t_j, q_j in jtr:
                if t_j <= t:
                    prev_q = q_j
                    continue
                dj = q_j[1] - prev_q[1]
                if dj != 0 and np.sign(dj) == np.sign(d):
                    lag = (t_j - t).total_seconds() / 60.0
                    cls = "le15" if lag <= 15 else ("15_60" if lag <= 60 else "gt60")
                    lag_min = lag
                    break
                prev_q = q_j
            cases.append({"fx": fx, "season": eu["season"], "league": eu["league"], "third": "-", "k": 0, "size": "-", "dir": "-", "cls": cls, "lag": lag_min, "opp": False})
    return cases, {"hhad_line_matched_fx": n_line_matched}


def seg2_h6(pairs, jc_had, rng):
    """第二段：情报先行——事件后 JC 调盘率/方向 vs 对照窗基率。"""
    sid2fx = {eu["sid"]: (i, jc, eu) for i, (jc, eu) in enumerate(pairs)}
    con = duckdb.connect()
    evs = con.sql(
        f"""
        select sid, published_at, event_type, side, subtype, novelty
        from read_parquet('{NEWS_EVENTS}')
        where event_type in ('injury', 'suspension', 'rotation_hint')
        """
    ).fetchall()
    con.close()
    acct = {"events_total": len(evs), "subtype_return_dropped": 0, "no_pair": 0, "no_jc_window": 0, "outside_window": 0}
    analyzed = []  # (fx, t_pub, side, reacted, d_imp, t_react)
    controls = []  # (fx, reacted) —— 每事件 K 窗
    for sid, t_pub_s, etype, side, subtype, novelty in evs:
        if subtype == "return":
            acct["subtype_return_dropped"] += 1
            continue
        hit = sid2fx.get(sid)
        if hit is None:
            acct["no_pair"] += 1
            continue
        fx, jc, eu = hit
        t_pub = dt.datetime.fromisoformat(t_pub_s.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
        jtr = jc_had.get(jc["sid"])
        if not jtr or len(jtr) < 2:
            acct["no_jc_window"] += 1
            continue
        t0, t1 = jtr[0][0], jtr[-1][0]
        if not (t0 + OBS_HEAD <= t_pub <= t1 - H6_TAIL):
            acct["outside_window"] += 1
            continue
        w_end = min(t_pub + H6_WINDOW, t1)
        pre = last_quote_before(jtr, t_pub)
        post = last_quote_before(jtr, w_end + dt.timedelta(seconds=1))
        reacted = any(t_pub < t_j <= w_end for t_j, _ in jtr)
        k = 0 if side == "home" else 2
        d_imp = imp(post[1][k]) - imp(pre[1][k]) if pre and post else float("nan")
        t_react = next((t_j for t_j, _ in jtr if t_pub < t_j <= w_end), None)
        analyzed.append((fx, t_pub, side, reacted, d_imp, t_react, etype, novelty))
        # 对照窗：同场销售窗内 6h 段，避让本場全部事件 ±6h
        ev_times = [
            dt.datetime.fromisoformat(e[1].replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
            for e in evs
            if e[0] == sid and e[4] != "return"
        ]
        k_ok = 0
        tries = 0
        span = (t1 - t0).total_seconds()
        while k_ok < 3 and tries < 30:
            tries += 1
            c0 = t0 + dt.timedelta(seconds=rng.uniform(0.05, 0.85) * span)
            c1 = c0 + (w_end - t_pub)
            if c1 > t1:
                continue
            if any(abs((c0 - te).total_seconds()) < H6_WINDOW.total_seconds() for te in ev_times):
                continue
            controls.append((fx, any(c0 < t_j <= c1 for t_j, _ in jtr)))
            k_ok += 1
    # 汇总
    n = len(analyzed)
    reacted = [a for a in analyzed if a[3]]
    d_nz = [a for a in reacted if np.isfinite(a[4]) and abs(a[4]) >= 0.0005]
    consist = [a for a in d_nz if a[4] < 0]
    ctl_n = len(controls)
    ctl_rate = sum(1 for _, r in controls if r) / ctl_n if ctl_n else float("nan")
    print(f"\n[H6 情报先行] 事件账：{acct}")
    print(f"  分析事件 n={n}；反应（6h 内 JC had 调盘）{len(reacted)}（{len(reacted)/n:.1%}）" if n else "  分析事件 n=0")
    if ctl_n:
        ev_rate = len(reacted) / n
        se_ev = np.sqrt(max(ev_rate * (1 - ev_rate), 1e-9) / n)
        se_ct = np.sqrt(max(ctl_rate * (1 - ctl_rate), 1e-9) / ctl_n)
        z = (ev_rate - ctl_rate) / np.hypot(se_ev, se_ct)
        # 场聚类稳健性：按场取事件反应均值后对照同法（防同场多事件虚增显著性）
        fx_ev: dict[int, list[int]] = defaultdict(list)
        for a in analyzed:
            fx_ev[a[0]].append(1 if a[3] else 0)
        fx_ctl: dict[int, list[int]] = defaultdict(list)
        ci = 0
        fx_seq = [c[0] for c in controls]
        for f, r in controls:
            fx_ctl[f].append(1 if r else 0)
        per_ev = np.array([np.mean(v) for v in fx_ev.values()])
        per_ct = np.array([np.mean(v) for v in fx_ctl.values()])
        z_fx = (per_ev.mean() - per_ct.mean()) / np.hypot(
            per_ev.std(ddof=1) / np.sqrt(len(per_ev)), per_ct.std(ddof=1) / np.sqrt(len(per_ct))
        )
        print(f"  对照窗基率 {ctl_rate:.1%}（{ctl_n} 窗）；事件窗 {ev_rate:.1%}（{n} 事件）→ 差 {ev_rate-ctl_rate:+.1%}，z(两率)={z:+.2f}，z(场聚类)={z_fx:+.2f}")
    if d_nz:
        print(f"  方向判读（伤停方隐含净变动<0=一致）：可判 n={len(d_nz)}，一致 {len(consist)}（{len(consist)/len(d_nz):.1%}）——H6 门>55%")
        lags = [(a[5] - a[1]).total_seconds() / 60.0 for a in reacted if a[5]]
        if lags:
            print(f"  事件→首调盘滞后：中位 {np.median(lags):.0f}min / p90 {np.percentile(lags, 90):.0f}min")
    by_type = Counter((a[6], a[3]) for a in analyzed)
    print(f"  分类型反应：{dict(by_type)}")
    by_nov = Counter((a[7], a[3]) for a in analyzed)
    print(f"  分新颖度：{dict(by_nov)}")
    return analyzed, controls, acct


def last_quote_before(jtr, t):
    best = None
    for t_j, q in jtr:
        if t_j <= t:
            best = (t_j, q)
        else:
            break
    return best


def main() -> None:
    rng = random.Random(20261108)  # 对照窗抽样可复算
    print("=" * 76)
    print("票 08：H4 调盘滞后率 + H6 情报先行（历史面）")
    print("=" * 76)
    jc_rows = load_jc()
    eu_rows = load_euro()
    pairs, pstats = pair_fixtures(jc_rows, eu_rows)
    print(f"配对漏斗: {pstats}")
    print(f"配对成功 {len(pairs)} / JC 侧 {pstats['jc_rows']}（{len(pairs)/max(pstats['jc_rows'],1):.0%}）")

    # 时钟合理性探针：JC 收笔 − srct 开球（负小值=口径对齐）
    jc_had = load_jc_traj([jc["sid"] for jc, _ in pairs], "had")
    cst = dt.timezone(dt.timedelta(hours=8))
    ok = [
        (jc_had[jc["sid"]][-1][0] - eu["kickoff"].replace(tzinfo=cst)).total_seconds() / 3600
        for jc, eu in pairs
        if jc["sid"] in jc_had and jc_had[jc["sid"]] and eu["kickoff"] is not None
    ]
    if ok:
        print(f"时钟探针：JC收笔−开球(时) 中位 {np.median(ok):.1f} / p10 {np.percentile(ok, 10):.1f} / p90 {np.percentile(ok, 90):.1f}（应≈0 或负小）")

    pin_traj = load_pin_1x2([eu["sid"] for _, eu in pairs])
    print(f"轨迹装载：Pinnacle 1x2 {len(pin_traj)} 场；JC had {len(jc_had)} 场")

    print("\n" + "-" * 76)
    print("第一段 H4：had 主面（Pinnacle 1x2 动 → JC had 同向首动）")
    cases, excl = seg1_had(pairs, pin_traj, jc_had)
    print(f"样本账：{excl}")
    summarize_cases(cases, "H4 had 全体")

    print("\n" + "-" * 76)
    print("第一段 H4：hhad 副面（Pinnacle ah 同线水位 → JC hhad 同线）")
    jc_hhad = load_jc_traj([jc["sid"] for jc, _ in pairs], "hhad")
    pin_ah = load_pin_ah([eu["sid"] for _, eu in pairs])
    cases_ah, acct_ah = seg1_hhad(pairs, pin_ah, jc_hhad)
    print(f"样本账：{acct_ah}（ah 主账书(cid3) 轨迹 {len(pin_ah)} 场；JC hhad {len(jc_hhad)} 场）")
    summarize_cases(cases_ah, "H4 hhad 副面")

    print("\n" + "-" * 76)
    print("第二段 H6：情报先行（伤停/停赛/轮换事件 → JC 调盘）")
    seg2_h6(pairs, jc_had, rng)


if __name__ == "__main__":
    main()
