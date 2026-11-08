# 票 03：H2/H3 池份额校准检验（源B 849 行 × 开奖结果）
#
# 纯离线研究脚本：只读 /tmp/goalx-ro-snapshot.db（主库 data/goalx.db 的文件级
# 快照副本——主库为 WAL 模式，ro 打开需 shm 写权限，故先 cp 出副本再分析，
# 全程零写运行库）。产出全部打印到 stdout，供报告引用。
#
# 口径：
# - 份额 = public_shares.share（源B okooo 人气投票三元组归一，官方池码
#   3=胜/1=平/0=负；ingest 护栏三向和 ∈[0.9,1.1]）。
# - 终局份额 = 每 (pool_period_id, match_seq) 取 captured_at 最新快照
#   （快照时点通常距开球 0.7-30h，见时序表——非真实收盘，报告注明）。
# - 结果：fixtures→draw_results（id 直连 26 + 名字+时间窗回落 28），
#   P1 14 场俱乐部赛另用十年 hist_matches（E0/D1/I1/SP1/F1）交叉验证。
# - 市场公允 = 竞彩 sporttery had 最新快照比例化去水（13% 抽水下近似）；
#   P1 另有 pool_matches.euro_odds（okooo 欧赔）与 hist avgc（欧收盘均值）
#   双锚交叉。
# 统计：Wilson CI、二项 z、配对 Wilcoxon/t、bootstrap（重采样场次）校准斜率。
# 所有 n<300 的格子按票规标探索性（本数据集任何格子都 <300，全部探索性，
# 结论段据此只给方向与幅度，不宣称确认）。

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np
from scipy import stats

DB = "/tmp/goalx-ro-snapshot.db"
CODE2DIR = {"3": "H", "1": "D", "0": "A"}
DIRS = ("H", "D", "A")

# P1 源B缩写 → JC 库内全名（缩写惯例非前缀，8 个需手映射；其余前缀/精确命中）
P1_ALIAS = {
    "曼城": "曼彻斯特城",
    "曼联": "曼彻斯特联",
    "皇马": "皇家马德里",
    "马竞技": "马德里竞技",
    "日尔曼": "巴黎圣日尔曼",
    "贝蒂斯": "皇家贝蒂斯",
    "社会": "皇家社会",
    "艾华斯堡": "埃尔沃斯堡",
    "拉科鲁": "拉科鲁尼亚",
    "尤文": "尤文图斯",
    "亚特兰": "亚特兰大",
    "勒沃": "勒沃库森",
    "莱比锡": "莱比锡红牛",
    "沙尔克": "沙尔克04",
    "弗洛西": "弗洛西诺内",
    "利兹": "利兹联",
    "巴伦西": "巴伦西亚",
    "伯恩茅": "伯恩茅斯",
    "水晶宫": "水晶宫",
    "桑德兰": "桑德兰",
    "富勒姆": "富勒姆",
    "帕尔马": "帕尔马",
    "热那亚": "热那亚",
    "科莫": "科莫",
    "莱切": "莱切",
    "AC米兰": "AC米兰",
    "马赛": "马赛",
}
# P1 hist（英文）验证用：JC 全名 → hist_matches 英文名（2026-09-20 窗口）
P1_HIST_NAME = {
    "伯恩茅斯": ("Bournemouth", "Liverpool"),
    "利兹联": ("Leeds", "Crystal Palace"),
    "曼彻斯特城": ("Man City", "Sunderland"),
    "富勒姆": ("Fulham", "Man United"),
    "勒沃库森": ("Leverkusen", "RB Leipzig"),
    "沙尔克04": ("Schalke 04", "Elversberg"),
    "弗洛西诺内": ("Frosinone", "Como"),
    "帕尔马": ("Parma", "Genoa"),
    "尤文图斯": ("Juventus", "Atalanta"),
    "AC米兰": ("Milan", "Lecce"),
    "马德里竞技": ("Ath Madrid", "Real Madrid"),
    "拉科鲁尼亚": ("La Coruna", "Betis"),
    "巴伦西亚": ("Valencia", "Sociedad"),
    "马赛": ("Marseille", "Paris SG"),
}


@dataclass
class MatchRow:
    period_id: int
    period_no: str
    seq: int
    kickoff: str
    home: str
    away: str
    source_match_id: str | None
    euro: tuple[float, float, float] | None
    shares: dict[str, float] = field(default_factory=dict)  # 终局 H/D/A
    votes: dict[str, int] = field(default_factory=dict)
    final_captured: str | None = None
    fixture_id: int | None = None
    link_method: str | None = None
    result: str | None = None  # H/D/A
    score: tuple[int, int] | None = None
    jc_odds: tuple[float, float, float] | None = None  # sporttery had 最新
    hist_checked: str | None = None
    hist_result: str | None = None


def names_match(pool: str, fixture: str) -> bool:
    a, b = pool.replace(" ", ""), fixture.replace(" ", "")
    if a == b:
        return True
    return (len(a) >= 2 and b.startswith(a)) or (len(b) >= 2 and a.startswith(b))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---------- 0. 装载 849 行 ----------
    rows = con.execute(
        """
        SELECT s.*, p.period_no FROM public_shares s
        JOIN pool_periods p ON p.id = s.pool_period_id
        ORDER BY s.pool_period_id, s.match_seq, s.captured_at
        """
    ).fetchall()
    assert len(rows) == 849, len(rows)

    matches: dict[tuple[int, int], MatchRow] = {}
    snapshots: dict[tuple[int, int, str], dict[str, float]] = {}
    for r in rows:
        key = (r["pool_period_id"], r["match_seq"])
        if key not in matches:
            pm = con.execute(
                "SELECT * FROM pool_matches WHERE pool_period_id=? AND match_seq=?",
                key,
            ).fetchone()
            euro = (
                (pm["euro_odds_h"], pm["euro_odds_d"], pm["euro_odds_a"])
                if pm["euro_odds_h"] is not None
                else None
            )
            matches[key] = MatchRow(
                period_id=r["pool_period_id"],
                period_no=r["period_no"],
                seq=r["match_seq"],
                kickoff=pm["kickoff_utc"],
                home=pm["home_team"],
                away=pm["away_team"],
                source_match_id=pm["source_match_id"],
                euro=euro,
            )
        m = matches[key]
        skey = (r["pool_period_id"], r["match_seq"], r["captured_at"])
        snapshots.setdefault(skey, {})[r["selection_code"]] = r["share"]
        if m.final_captured is None or r["captured_at"] > m.final_captured:
            m.final_captured = r["captured_at"]
        meta = json.loads(r["meta"]) if r["meta"] else {}
        if r["captured_at"] == m.final_captured and meta.get("votes"):
            m.votes = {CODE2DIR[k]: v for k, v in meta["votes"].items()}

    # 终局份额（最新 captured_at 的三向）
    for m in matches.values():
        snap = snapshots[(m.period_id, m.seq, m.final_captured)]
        m.shares = {CODE2DIR[c]: v for c, v in snap.items()}

    # ---------- 1. 849 行全量对账 ----------
    print("=" * 72)
    print("一、849 行全量对账")
    print("=" * 72)
    print(f"总行数 {len(rows)}；场次数 {len(matches)}（4 期 × 14）；快照数 {len(snapshots)}")
    sums = [sum(s.values()) for s in snapshots.values()]
    print(
        f"快照三向和: min={min(sums):.4f} max={max(sums):.4f} "
        f"（护栏 [0.9,1.1] 全过: {all(0.9 <= s <= 1.1 for s in sums)}）"
    )
    for pid in sorted({m.period_id for m in matches.values()}):
        ms = [m for m in matches.values() if m.period_id == pid]
        nsnap = len([1 for k in snapshots if k[0] == pid])
        vt = [sum(m.votes.values()) for m in ms if m.votes]
        print(
            f"  期 {ms[0].period_no}（id={pid}）: 14 场 × {nsnap // 14} 快照均值 "
            f"({nsnap} 快照)，终局票数中位 {int(np.median(vt)):,}"
            if vt
            else f"  期 {ms[0].period_no}（id={pid}）: 无票数"
        )
    # 快照时点距开球
    import datetime as dt

    hrs = []
    for (pid, seq, cap) in snapshots:
        m = matches[(pid, seq)]
        d = (
            dt.datetime.fromisoformat(m.kickoff)
            - dt.datetime.fromisoformat(cap)
        ).total_seconds() / 3600
        hrs.append(d)
    print(f"快照距开球小时: min={min(hrs):.1f} max={max(hrs):.1f} 中位={np.median(hrs):.1f}")
    fin_hrs = []
    for m in matches.values():
        d = (
            dt.datetime.fromisoformat(m.kickoff)
            - dt.datetime.fromisoformat(m.final_captured or "")
        ).total_seconds() / 3600
        fin_hrs.append(d)
    print(
        f"终局快照距开球: min={min(fin_hrs):.1f}h max={max(fin_hrs):.1f}h "
        f"中位={np.median(fin_hrs):.1f}h"
    )
    for pid in sorted({m.period_id for m in matches.values()}):
        fh_p = [
            (
                dt.datetime.fromisoformat(m.kickoff)
                - dt.datetime.fromisoformat(m.final_captured or "")
            ).total_seconds()
            / 3600
            for m in matches.values()
            if m.period_id == pid
        ]
        caps_p = sorted(
            {k[2] for k in snapshots if k[0] == pid}
        )
        print(
            f"  期 {pid}: 终局快照距开球 min={min(fh_p):.1f}h 中位={np.median(fh_p):.1f}h "
            f"max={max(fh_p):.1f}h；最后采集 {caps_p[-1]}"
        )
    max_cap = max(k[2] for k in snapshots)
    print(f"全库最后采集时点: {max_cap}")

    # ---------- 2. 结果解析 ----------
    print()
    print("=" * 72)
    print("二、结果解析（id 直连 → 名字+时间窗回落）")
    print("=" * 72)
    fx_cache: dict[str, list[sqlite3.Row]] = {}

    def fixtures_on(date_str: str) -> list[sqlite3.Row]:
        if date_str not in fx_cache:
            fx_cache[date_str] = con.execute(
                """
                SELECT f.id, f.kickoff_utc, h.canonical_name AS home, a.canonical_name AS away
                FROM fixtures f
                JOIN teams h ON h.id = f.home_team_id
                JOIN teams a ON a.id = f.away_team_id
                WHERE date(f.kickoff_utc) BETWEEN date(?, '-1 day') AND date(?, '+1 day')
                """,
                (date_str, date_str),
            ).fetchall()
        return fx_cache[date_str]

    unlinked: list[MatchRow] = []
    for m in sorted(matches.values(), key=lambda x: (x.period_id, x.seq)):
        fid = None
        method = None
        # id 直连（match_codes jingcai 唯一命中 + 日期 ≤2 天）
        if m.source_match_id:
            hits = con.execute(
                """
                SELECT DISTINCT f.id AS fid, f.kickoff_utc AS kickoff
                FROM fixtures f JOIN match_codes mc ON mc.fixture_id = f.id
                WHERE mc.kind='jingcai' AND mc.source_match_id = ?
                """,
                (m.source_match_id,),
            ).fetchall()
            if len(hits) == 1:
                d0 = dt.datetime.fromisoformat(m.kickoff).date()
                d1 = dt.datetime.fromisoformat(hits[0]["kickoff"]).date()
                if abs((d1 - d0).days) <= 2:
                    fid, method = hits[0]["fid"], "id直连"
        # 名字+时间窗回落（P1 用缩写映射，其余精确/前缀）。
        # 两段：①池侧开球为真实时点（非 16:00Z=北京 00:00 占位）→ ±1 天 + ±3h；
        # ②占位日期期次（P2-P4）→ ±2 天名字唯一命中（官方池 kickoff 只有日期
        #   精度，pool.py match_fixture_id 文档同此口径）。
        if fid is None:
            base = dt.datetime.fromisoformat(m.kickoff)
            placeholder = (base.hour == 16 and base.minute == 0)
            cands: list[sqlite3.Row] = []
            for f in fixtures_on(base.date().isoformat()):
                fh = P1_ALIAS.get(m.home, m.home)
                fa = P1_ALIAS.get(m.away, m.away)
                fk = dt.datetime.fromisoformat(f["kickoff_utc"])
                if not placeholder and abs((fk - base).total_seconds()) > 3 * 3600:
                    continue
                if names_match(fh, f["home"]) and names_match(fa, f["away"]):
                    cands.append(f)
            # 占位日期：±2 天内名字唯一命中才采用
            if placeholder and len(cands) != 1:
                for f in fixtures_on((base.date() + dt.timedelta(days=1)).isoformat()):
                    fh = P1_ALIAS.get(m.home, m.home)
                    fa = P1_ALIAS.get(m.away, m.away)
                    if names_match(fh, f["home"]) and names_match(fa, f["away"]):
                        cands.append(f)
                fids = {c["id"] for c in cands}
                if len(fids) == 1:
                    cands = [c for c in cands if c["id"] == fids.pop()]
            if len(cands) == 1:
                fid, method = cands[0]["id"], "名字窗"
        m.fixture_id, m.link_method = fid, method
        if fid is None:
            unlinked.append(m)
            continue
        dr = con.execute(
            "SELECT home_goals, away_goals, void FROM draw_results WHERE fixture_id=?",
            (fid,),
        ).fetchone()
        if dr is None or dr["void"]:
            unlinked.append(m)
            m.link_method = f"{method}+无结果"
            continue
        m.score = (dr["home_goals"], dr["away_goals"])
        m.result = "H" if dr["home_goals"] > dr["away_goals"] else (
            "A" if dr["home_goals"] < dr["away_goals"] else "D"
        )
        # sporttery had 最新快照
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
            m.jc_odds = (o["h"], o["d"], o["a"])

    n_ok = sum(1 for m in matches.values() if m.result)
    print(f"解析成功 {n_ok}/56；未解析 {len(unlinked)} 场：")
    for m in unlinked:
        print(f"  期{m.period_no} 第{m.seq}场 {m.home}-{m.away}（{m.kickoff[:10]}）")
    methods: dict[str, int] = {}
    for m in matches.values():
        methods[m.link_method or "无"] = methods.get(m.link_method or "无", 0) + 1
    print(f"链接方法分布: {methods}")

    # P1 hist 交叉验证（14 场俱乐部赛 vs 十年底座）
    print()
    print("P1 hist 交叉验证（JC 结果 vs hist_matches 十年底座）:")
    n_agree = n_cmp = 0
    for m in sorted(matches.values(), key=lambda x: (x.period_id, x.seq)):
        if m.period_id != 1 or m.result is None:
            continue
        en = P1_HIST_NAME.get(P1_ALIAS.get(m.home, m.home))
        if en is None:
            print(f"  {m.home}-{m.away}: 无 hist 名映射（跳过）")
            continue
        h = con.execute(
            """
            SELECT fthg, ftag, ftr, avgc_home, avgc_draw, avgc_away
            FROM hist_matches WHERE match_date = date(?, '+0 day')
            AND home_team=? AND away_team=?
            """,
            (m.kickoff, en[0], en[1]),
        ).fetchone()
        if h is None:
            # 日期容差一天
            h = con.execute(
                """
                SELECT fthg, ftag, ftr, avgc_home, avgc_draw, avgc_away
                FROM hist_matches WHERE match_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')
                AND home_team=? AND away_team=?
                """,
                (m.kickoff, m.kickoff, en[0], en[1]),
            ).fetchone()
        if h is None:
            print(f"  {m.home}-{m.away}: hist 未找到")
            continue
        hr = {"H": "H", "D": "D", "A": "A"}[h["ftr"]]
        n_cmp += 1
        n_agree += hr == m.result
        m.hist_checked = "ok"
        m.hist_result = hr
        flag = "✓" if hr == m.result else "✗ 不一致!"
        print(
            f"  {m.home}-{m.away}: JC {m.score[0]}-{m.score[1]}({m.result}) "
            f"vs hist {h['fthg']}-{h['ftag']}({hr}) {flag}"
        )
    print(f"交叉验证一致 {n_agree}/{n_cmp}")

    # ---------- 3. 校准表（终局份额 vs 实际） ----------
    sample = [m for m in matches.values() if m.result]
    print()
    print("=" * 72)
    print(f"三、份额校准表（n={len(sample)} 场 × 3 向；全部格子 n<300 → 探索性）")
    print("=" * 72)
    edges = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.65, 1.01]
    labels = ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%", "50-65%", "65-100%"]

    def cal_table(subset: list[MatchRow], title: str) -> None:
        print(f"\n{title}")
        print(f"{'桶':>8} {'向':>3} {'n':>4} {'均值份额':>8} {'实际频率':>8} {'差(实际-份额)':>13} {'Wilson95%CI':>18} {'z':>7}")
        cells = []
        for d in DIRS:
            for li, lab in enumerate(labels):
                xs = [m.shares[d] for m in subset if edges[li] <= m.shares[d] < edges[li + 1]]
                ks = [1.0 if m.result == d else 0.0 for m in subset if edges[li] <= m.shares[d] < edges[li + 1]]
                n, k = len(xs), int(sum(ks))
                if n == 0:
                    continue
                ms, af = float(np.mean(xs)), k / n
                se = math.sqrt(af * (1 - af) / n) if n > 1 else math.nan
                z = (af - ms) / math.sqrt(ms * (1 - ms) / n) if n > 1 else math.nan
                lo, hi = wilson(k, n)
                print(
                    f"{lab:>8} {d:>3} {n:>4} {ms:>8.3f} {af:>8.3f} {af - ms:>+13.3f} "
                    f"[{lo:.3f},{hi:.3f}]{'':<4} {z:>+7.2f}"
                )
                cells.append((d, lab, n, ms, af))
        # 分向聚合（H/D/A 各自合并所有桶：每场该向一行）
        for d in DIRS:
            xs = [m.shares[d] for m in subset]
            ks = [1 if m.result == d else 0 for m in subset]
            n, k = len(xs), sum(ks)
            ms, af = float(np.mean(xs)), k / n
            z = (af - ms) / math.sqrt(ms * (1 - ms) / n)
            lo, hi = wilson(k, n)
            print(
                f"{'合计':>8} {d:>3} {n:>4} {ms:>8.3f} {af:>8.3f} {af - ms:>+13.3f} "
                f"[{lo:.3f},{hi:.3f}]{'':<4} {z:>+7.2f}"
            )

    cal_table(sample, "全样本（终局份额 × 实际命中）")
    cal_table([m for m in sample if m.period_id == 1], "仅 P1 26131（俱乐部赛，欧赔在）")
    cal_table([m for m in sample if m.period_id >= 2], "P2-P4 26132-34（国家队赛）")

    # ---------- 4. H2：热门被份额低估？ ----------
    print()
    print("=" * 72)
    print("四、H2 检验：终局份额是否系统性低估热门（O-S 预测）")
    print("=" * 72)
    fav_share, fav_hit, fav_fair = [], [], []
    for m in sample:
        d = max(m.shares, key=m.shares.get)  # 份额热门
        fav_share.append(m.shares[d])
        fav_hit.append(1.0 if m.result == d else 0.0)
    fs, fh = np.array(fav_share), np.array(fav_hit)
    n = len(fs)
    mean_s, act = float(fs.mean()), float(fh.mean())
    z_bin = (act - mean_s) / math.sqrt(mean_s * (1 - mean_s) / n)
    lo, hi = wilson(int(fh.sum()), n)
    print(f"[份额定义热门] n={n}，均值份额 {mean_s:.4f}，实际命中 {act:.4f}（{int(fh.sum())}/{n}）")
    print(f"  低估幅度（实际−份额）= {act - mean_s:+.4f}；二项 z={z_bin:+.2f}；实际频率 Wilson95% [{lo:.3f},{hi:.3f}]")

    # 欧赔定义热门（最小 euro/jc 赔率向；P1 用 euro，其余用 JC SP）
    fs2, fh2 = [], []
    n_two = 0
    for m in sample:
        if m.euro:
            oh, od, oa = m.euro
        elif m.jc_odds:
            oh, od, oa = m.jc_odds
        else:
            continue
        d = {"H": oh, "D": od, "A": oa}
        fd = min(d, key=d.get)
        fs2.append(m.shares[fd])
        fh2.append(1.0 if m.result == fd else 0.0)
    fs2, fh2 = np.array(fs2), np.array(fh2)
    n2 = len(fs2)
    ms2, ac2 = float(fs2.mean()), float(fh2.mean())
    z2 = (ac2 - ms2) / math.sqrt(ms2 * (1 - ms2) / n2)
    lo2, hi2 = wilson(int(fh2.sum()), n2)
    print(
        f"[赔率定义热门] n={n2}，该向均值份额 {ms2:.4f}，实际命中 {ac2:.4f}（{int(fh2.sum())}/{n2}）"
    )
    print(f"  低估幅度 = {ac2 - ms2:+.4f}；二项 z={z2:+.2f}；Wilson95% [{lo2:.3f},{hi2:.3f}]")

    # 冷门端镜像（份额最低向的实际命中 vs 份额）
    cs, ch = [], []
    for m in sample:
        d = min(m.shares, key=m.shares.get)
        cs.append(m.shares[d])
        ch.append(1.0 if m.result == d else 0.0)
    cs, ch = np.array(cs), np.array(ch)
    mcs, ach = float(cs.mean()), float(ch.mean())
    zc = (ach - mcs) / math.sqrt(mcs * (1 - mcs) / len(cs))
    loC, hiC = wilson(int(ch.sum()), len(cs))
    print(f"[份额定义冷门] n={len(cs)}，均值份额 {mcs:.4f}，实际命中 {ach:.4f}（{int(ch.sum())}/{len(cs)}）")
    print(f"  偏差（实际−份额）= {ach - mcs:+.4f}；z={zc:+.2f}；Wilson95% [{loC:.3f},{hiC:.3f}]")

    # 校准斜率（pool all directions，bootstrap 重采样场次）
    rng = np.random.default_rng(20261107)
    x = np.array([m.shares[d] for m in sample for d in DIRS])
    y = np.array([1.0 if m.result == d else 0.0 for m in sample for d in DIRS])
    n_m = len(sample)

    def slope(xi: np.ndarray, yi: np.ndarray) -> float:
        xm, ym = xi.mean(), yi.mean()
        sxx = ((xi - xm) ** 2).sum()
        return float(((xi - xm) * (yi - ym)).sum() / sxx) if sxx > 0 else math.nan

    b_obs = slope(x, y)
    bs = []
    for _ in range(10000):
        idx = rng.integers(0, n_m, n_m)
        xs = np.concatenate([[sample[i].shares[d] for d in DIRS] for i in idx])
        ys = np.concatenate([[1.0 if sample[i].result == d else 0.0 for d in DIRS] for i in idx])
        bs.append(slope(xs, ys))
    bs = np.array(bs)
    print(
        f"\n[校准斜率] OLS 实际~份额（{n_m} 场 × 3 向 = {len(x)} 格）：b={b_obs:.3f} "
        f"bootstrap95% [{np.percentile(bs, 2.5):.3f},{np.percentile(bs, 97.5):.3f}]"
        f"（b>1=人群对极端压缩→热门端被低估；b<1=人群夸大极端）"
    )

    # 热门份额-公允差（份额 vs JC去水公允，同向配对）
    print("\n[份额 vs 市场公允（JC SP 比例化去水）配对差，热门向]")
    ds_, df_ = [], []
    for m in sample:
        if not m.jc_odds:
            continue
        inv = np.array([1 / o for o in m.jc_odds])
        fair = inv / inv.sum()
        fd = {"H": fair[0], "D": fair[1], "A": fair[2]}
        d = max(m.shares, key=m.shares.get)
        ds_.append(m.shares[d] - fd[d])
        df_.append(fd[d])
    ds_, df_ = np.array(ds_), np.array(df_)
    if len(ds_) >= 5:
        w = stats.wilcoxon(ds_) if (ds_ != 0).any() else None
        t = stats.ttest_1samp(ds_, 0)
        print(
            f"  n={len(ds_)}，热门向公允均值 {df_.mean():.4f}，份额均值 {(ds_ + df_).mean():.4f}，"
            f"份额−公允 = {ds_.mean():+.4f}（SE={ds_.std(ddof=1) / math.sqrt(len(ds_)):.4f}，配对 t p={t.pvalue:.3f}"
            + (f"，Wilcoxon p={w.pvalue:.3f}" if w else "")
            + "）"
        )

    # ---------- 5. H3：平局低买 ----------
    print()
    print("=" * 72)
    print("五、H3 检验：平局份额 vs 市场公允（draw aversion）")
    print("=" * 72)
    sd_, fd_ = [], []
    for m in sample:
        if not m.jc_odds:
            continue
        inv = np.array([1 / o for o in m.jc_odds])
        fair = inv / inv.sum()
        sd_.append(m.shares["D"])
        fd_.append(fair[1])
    sd_, fd_ = np.array(sd_), np.array(fd_)
    diff = sd_ - fd_
    t = stats.ttest_1samp(diff, 0)
    try:
        w = stats.wilcoxon(diff)
        wp = f"{w.pvalue:.4f}"
    except ValueError:
        wp = "n/a"
    draws = np.array([1.0 if m.result == "D" else 0.0 for m in sample])
    loD, hiD = wilson(int(draws.sum()), len(draws))
    print(f"[JC 公允锚] n={len(diff)}")
    print(f"  平局份额均值 {sd_.mean():.4f} vs 公允均值 {fd_.mean():.4f}：份额−公允 = {diff.mean():+.4f}")
    print(f"  配对 t p={t.pvalue:.4f}；Wilcoxon p={wp}；逐场为负比例 {np.mean(diff < 0):.3f}（{int((diff < 0).sum())}/{len(diff)}）")
    print(
        f"  配对差 SE = {diff.std(ddof=1) / math.sqrt(len(diff)):.4f} (sd={diff.std(ddof=1):.4f})",
        f"  实际平局率 {draws.mean():.4f}（{int(draws.sum())}/{len(draws)}）Wilson95% [{loD:.3f},{hiD:.3f}]"
        f" vs 份额合计/场 {np.mean([m.shares['D'] for m in sample]):.4f}"
    )

    # P1 双锚（okooo 欧赔 + hist avgc）
    sd1, fe1, sh1, fh1 = [], [], [], []
    for m in sample:
        if m.period_id != 1 or not m.euro:
            continue
        en = P1_HIST_NAME.get(P1_ALIAS.get(m.home, m.home))
        h = None
        if en:
            h = con.execute(
                """SELECT avgc_home, avgc_draw, avgc_away FROM hist_matches
                   WHERE match_date BETWEEN date(?, '-1 day') AND date(?, '+1 day')
                   AND home_team=? AND away_team=?""",
                (m.kickoff, m.kickoff, en[0], en[1]),
            ).fetchone()
        inv_e = np.array([1 / o for o in m.euro]); fe = inv_e / inv_e.sum()
        sd1.append(m.shares["D"]); fe1.append(fe[1])
        if h and h["avgc_draw"]:
            inv_c = np.array([1 / h["avgc_home"], 1 / h["avgc_draw"], 1 / h["avgc_away"]])
            fh1.append((inv_c / inv_c.sum())[1])
            sh1.append(m.shares["D"])
    sd1, fe1 = np.array(sd1), np.array(fe1)
    d1 = sd1 - fe1
    print(f"\n[P1 okooo 欧赔锚] n={len(d1)}：平局份额 {sd1.mean():.4f} vs 公允 {fe1.mean():.4f}，差 {d1.mean():+.4f}")
    if len(fh1) >= 5:
        sh1, fh1 = np.array(sh1), np.array(fh1)
        print(
            f"[P1 hist avgc 收盘锚] n={len(fh1)}：平局份额 {sh1.mean():.4f} vs 公允 {fh1.mean():.4f}，"
            f"差 {(sh1 - fh1).mean():+.4f}"
        )

    # 三向全面配对表（份额 vs JC 公允，分 H/D/A）
    print("\n[三向配对差（份额−JC公允），分方向]")
    for dname, di in (("H", 0), ("D", 1), ("A", 2)):
        dd, sm = [], []
        for m in sample:
            if not m.jc_odds:
                continue
            inv = np.array([1 / o for o in m.jc_odds])
            fair = inv / inv.sum()
            dd.append(m.shares[dname] - fair[di])
            sm.append(m.shares[dname])
        dd, sm = np.array(dd), np.array(sm)
        tv = stats.ttest_1samp(dd, 0)
        print(
            f"  {dname}: n={len(dd)} 均差 {dd.mean():+.4f}（份额均值 {sm.mean():.4f}）"
            f" t p={tv.pvalue:.4f}"
        )

    # ---------- 5b. 市场公允自校准对照（同场对照：fair vs 实际） ----------
    print()
    print("[市场公允自校准对照（同 53 场，JC SP 去水 fair vs 实际）]")
    for dname, di in (("H", 0), ("D", 1), ("A", 2)):
        fq, hit = [], []
        for m in sample:
            if not m.jc_odds:
                continue
            inv = np.array([1 / o for o in m.jc_odds])
            fair = inv / inv.sum()
            fq.append(fair[di])
            hit.append(1.0 if m.result == dname else 0.0)
        fq, hit = np.array(fq), np.array(hit)
        z = (hit.mean() - fq.mean()) / math.sqrt(fq.mean() * (1 - fq.mean()) / len(fq))
        print(
            f"  {dname}: n={len(fq)} fair均值 {fq.mean():.4f} 实际 {hit.mean():.4f} "
            f"差 {hit.mean() - fq.mean():+.4f} z={z:+.2f}"
        )
    # 平均指派概率（对实际发生向的赋概）：份额 vs fair 谁更会分类
    sc, fc = [], []
    for m in sample:
        if not m.jc_odds:
            continue
        inv = np.array([1 / o for o in m.jc_odds])
        fair = inv / inv.sum()
        sc.append(m.shares[m.result])
        fc.append(fair["HDA".index(m.result)])
    sc, fc = np.array(sc), np.array(fc)
    from scipy.stats import wilcoxon as _wx

    print(
        f"  平均赋概（对实际发生向）: 份额 {sc.mean():.4f} vs fair {fc.mean():.4f} "
        f"（差 {sc.mean() - fc.mean():+.4f}，配对 Wilcoxon p={_wx(sc - fc).pvalue:.4f}）"
    )
    # H2 outcome-level 精确二项 p
    from scipy.stats import binomtest

    bt = binomtest(32, 54, 0.5029)
    print(f"  [H2 精确二项] 32/54 vs p0=0.5029：p={bt.pvalue:.4f}")

    # ---------- 6. 价位分层 ----------
    print()
    print("=" * 72)
    print("六、价位分层（公允概率分桶 × 方向，份额−公允 与 实际−份额）")
    print("=" * 72)
    print(f"{'公允桶':>10} {'向':>3} {'n':>4} {'公允':>7} {'份额':>7} {'实际':>7} {'份额−公允':>9} {'实际−份额':>9}")
    fedges = [0.0, 0.15, 0.30, 0.45, 1.01]
    flabels = ["<15%", "15-30%", "30-45%", "45%+"]
    for dname, di in (("H", 0), ("D", 1), ("A", 2)):
        for li, lab in enumerate(flabels):
            fq, sq, hit = [], [], []
            for m in sample:
                if not m.jc_odds:
                    continue
                inv = np.array([1 / o for o in m.jc_odds])
                fair = inv / inv.sum()
                f = fair[di]
                if fedges[li] <= f < fedges[li + 1]:
                    fq.append(f)
                    sq.append(m.shares[dname])
                    hit.append(1.0 if m.result == dname else 0.0)
            if not fq:
                continue
            fq, sq, hit = np.array(fq), np.array(sq), np.array(hit)
            print(
                f"{lab:>10} {dname:>3} {len(fq):>4} {fq.mean():>7.3f} {sq.mean():>7.3f} "
                f"{hit.mean():>7.3f} {np.mean(sq - fq):>+9.3f} {np.mean(hit - sq):>+9.3f}"
            )

    # ---------- 7. 份额时序（O-S 延迟下注：终局 vs 首快照） ----------
    print()
    print("=" * 72)
    print("七、份额时序漂移（首快照 → 终局快照，热门向）")
    print("=" * 72)
    deltas = []
    for m in sample:
        caps = sorted(k[2] for k in snapshots if k[0] == m.period_id and k[1] == m.seq)
        if len(caps) < 2:
            continue
        first = {CODE2DIR[c]: v for c, v in snapshots[(m.period_id, m.seq, caps[0])].items()}
        d0 = max(first, key=first.get)
        deltas.append(m.shares[d0] - first[d0])
    deltas = np.array(deltas)
    if len(deltas):
        print(
            f"n={len(deltas)} 场（首→终快照热门份额变化）：均值 {deltas.mean():+.4f} "
            f"中位 {np.median(deltas):+.4f}，热门份额上行场占比 {np.mean(deltas > 0):.3f}"
        )

    # ---------- 8. 明细表 ----------
    print()
    print("=" * 72)
    print("八、56 场明细（终局份额/结果/链接法）")
    print("=" * 72)
    print(f"{'期':>6} {'场':>2} {'主':　<6} {'客':　<6} {'sH':>6} {'sD':>6} {'sA':>6} {'果':>2} {'比分':>5} {'链接':>6}")
    for m in sorted(matches.values(), key=lambda x: (x.period_id, x.seq)):
        s = m.shares
        print(
            f"{m.period_no:>6} {m.seq:>2} {m.home:　<6} {m.away:　<6} "
            f"{s.get('H', float('nan')):>6.3f} {s.get('D', float('nan')):>6.3f} {s.get('A', float('nan')):>6.3f} "
            f"{m.result or '?':>2} {f'{m.score[0]}-{m.score[1]}' if m.score else '?':>5} {m.link_method or '缺':>6}"
        )

    con.close()


if __name__ == "__main__":
    main()
