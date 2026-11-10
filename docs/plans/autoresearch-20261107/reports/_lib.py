#!/usr/bin/env python3
"""autoresearch-20261107 批次 α 共享库（票 08/09/10/11 复用）。

来源=票 07 脚本的配对/统计段原样抽出（league+名字+比分自举，85% 配对率
已验证）；新增轨迹装载（Pinnacle 1x2 / JC had/hhad 逐笔变价）。全部只读。

用法（同目录脚本）：
    from _lib import pair_fixtures, load_pin_1x2, load_jc_traj, devig, ...
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Sequence

import duckdb
import numpy as np

BRONZE = "/Users/chevy/goalx-data/bronze/jc/sp_history.ndjson.gz"
DUCK = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
NEWS_EVENTS = "/Users/chevy/goalx-data/silver/guardian/news_event/all/data.parquet"
PIN_1X2 = "srct:1x2:177"  # Pinnacle（bookmaker.name_en 实证，44K 场覆盖）
PIN_AH = "srct:ah:3"  # ah 面主账书（Pinnacle 无 ah 命名空间覆盖；cid3=最大 ah 书，12.2M 事件）

# JC 联盟全名 → srct 联赛名（match_features 实测值，票 07 同款）
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
# EM-1 预注册粗桶（票 09 起，防切片爆炸；细桶七档留 07 口径不复用）
COARSE_EDGES = (0.0, 0.20, 0.40, 0.60, 1.01)
COARSE_LABELS = ("<20%", "20-40%", "40-60%", "60%+")


def norm(s: str) -> str:
    return s.replace(" ", "")


def bucket_idx(p: float) -> int:
    for i in range(len(BUCKET_EDGES) - 1):
        if BUCKET_EDGES[i] <= p < BUCKET_EDGES[i + 1]:
            return i
    return -1


def coarse_idx(p: float) -> int:
    for i in range(len(COARSE_EDGES) - 1):
        if COARSE_EDGES[i] <= p < COARSE_EDGES[i + 1]:
            return i
    return -1


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
    """按场聚类的均值与 SE：rows=(fixture_key, value)。同场多向取场内均值。"""
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


def clustered_prop(rows: list[tuple[float, float]]) -> tuple[float, float, int]:
    """按场聚类的比例与 SE：rows=(fixture_key, 0|1)。"""
    return clustered_mean([(k, v) for k, v in rows])


# ---------------------------------------------------------------- JC 侧装载


def parse_jc_score(payload: dict) -> tuple[int, int] | None:
    raw = payload.get("sectionsNo999")
    if not isinstance(raw, str) or ":" not in raw:
        return None
    try:
        return int(raw.split(":")[0]), int(raw.split(":")[1])
    except ValueError:
        return None


def load_jc(*, min_year: str = "2016") -> list[dict]:
    """JC sp_history → 场次行（had/hhad/ttg 收盘+名字+比分验证位）。"""
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
            if not date or date < min_year:
                continue
            try:
                jc = (float(close["h"]), float(close["d"]), float(close["a"]))
            except (KeyError, ValueError, TypeError):
                continue
            if min(jc) <= 1.0:
                continue
            hhad = None
            hh = (oh.get("hhadList") or [None])[-1]
            if hh is not None:
                try:
                    line_f = float(hh["goalLine"])
                    trip = (float(hh["h"]), float(hh["d"]), float(hh["a"]))
                    if min(trip) > 1.0:
                        hhad = (line_f, *trip)
                except (KeyError, ValueError, TypeError):
                    hhad = None
            ttg = None
            tt = (oh.get("ttgList") or [None])[-1]
            if tt is not None:
                try:
                    oct_ = tuple(float(tt[f"s{i}"]) for i in range(8))
                    if min(oct_) > 1.0:
                        ttg = oct_
                except (KeyError, ValueError, TypeError):
                    ttg = None
            rows.append(
                {
                    "sid": r["sid"],
                    "league": lg,
                    "home": norm(oh.get("homeTeamAllName") or ""),
                    "away": norm(oh.get("awayTeamAllName") or ""),
                    "date": dt.date.fromisoformat(date),
                    "jc": jc,
                    "hhad": hhad,
                    "ttg": ttg,
                    "score": parse_jc_score(p),
                }
            )
    return rows


def load_euro(*, min_year: str = "2016-01-01") -> list[dict]:
    """match_features 金表 → 场次行（三锚+OU/AH+xG/Elo 全列）。"""
    con = duckdb.connect(DUCK, read_only=True)
    rows = con.sql(
        """
        select sid, league, kickoff, home, away, home_goals, away_goals, season,
               avgc_home, avgc_draw, avgc_away,
               psc_home, psc_draw, psc_away,
               close1x2_cons_h, close1x2_cons_d, close1x2_cons_a,
               fd_avgc_ou_over, fd_avgc_ou_under, fd_avgc_ah_home, fd_avgc_ah_away,
               fd_ahc_line, q1h1x2_h, q1h1x2_d, q1h1x2_a,
               q24h1x2_h, q24h1x2_d, q24h1x2_a,
               elo_home_pre, elo_away_pre, xg_home, xg_away
        from match_features
        where home_goals is not null and admin_excluded = 0
          and kickoff >= ?
        """,
        params=[min_year],
    ).fetchall()
    con.close()
    keys = (
        "sid league kickoff home away hg ag season "
        "avgc_h avgc_d avgc_a psc_h psc_d psc_a srct_h srct_d srct_a "
        "ouo ouu ahh aha ah_line q1h_h q1h_d q1h_a q24h_h q24h_d q24h_a "
        "elo_h elo_a xg_h xg_a"
    ).split()
    out = []
    for r in rows:
        d = dict(zip(keys, r, strict=True))
        d["date"] = d["kickoff"].date()
        d["home"] = norm(d["home"])
        d["away"] = norm(d["away"])
        for g in ("avgc", "psc", "srct", "q1h", "q24h"):
            d[g] = (d[f"{g}_h"], d[f"{g}_d"], d[f"{g}_a"])
        out.append(d)
    return out


def names_agree(a: str, b: str) -> bool:
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a))


def score_agree(jc: dict, eu: dict) -> bool | None:
    if jc["score"] is None:
        return None
    return jc["score"] == (eu["hg"], eu["ag"])


def pair_fixtures(jc_rows: list[dict], eu_rows: list[dict]) -> tuple[list[tuple[dict, dict]], dict]:
    """票 07 原样：三轮配对（名字→比分自举名票迭代→残差），(jc, eu) 对。"""
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

    def try_name(jc: dict, h2: str | None = None, a2: str | None = None) -> dict | None:
        hh, aa = h2 or jc["home"], a2 or jc["away"]
        found = None
        for cell in cells(jc):
            for eu in eu_by_cell.get(cell, []):
                if eu["sid"] in used_eu:
                    continue
                if names_agree(hh, eu["home"]) and names_agree(aa, eu["away"]):
                    if score_agree(jc, eu) is False:
                        continue
                    found = eu
                    break
            if found:
                break
        return found

    for jc in jc_rows:
        eu = try_name(jc)
        if eu is not None:
            pairs.append((jc, eu))
            used_eu.add(eu["sid"])
            matched_jc.add(jc["sid"])
    n_name = len(pairs)

    votes: dict[tuple[str, str], int] = defaultdict(int)
    alias: dict[str, str] = {}
    n_boot = 0
    for _ in range(6):
        for (jname, ename), v in list(votes.items()):
            if v >= 2 and jname not in alias:
                alias[jname] = ename
        for jc in jc_rows:
            if jc["sid"] in matched_jc:
                continue
            found = try_name(jc, alias.get(jc["home"], jc["home"]), alias.get(jc["away"], jc["away"]))
            if found is not None:
                pairs.append((jc, found))
                used_eu.add(found["sid"])
                matched_jc.add(jc["sid"])
                n_boot += 1
                votes[(jc["home"], found["home"])] += 1
                votes[(jc["away"], found["away"])] += 1
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


# ---------------------------------------------------------------- 轨迹装载


def load_pin_1x2(sids: Iterable[str]) -> dict[str, list[tuple[dt.datetime, tuple[float, float, float]]]]:
    """Pinnacle 1x2 逐笔轨迹：sid → [(t, (h,d,a))] 按时排序，去重连续同价。"""
    sid_list = list(sids)
    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in sid_list])
    rows = con.sql(
        f"""
        select o.sid, o.published_at, o.odds_home, o.odds_draw, o.odds_away
        from odds_change_event o join sids s using (sid)
        where o.bookmaker_id = '{PIN_1X2}' and o.market = '1x2'
          and o.published_at is not null
        order by o.sid, o.published_at, o.source_order
        """
    ).fetchall()
    con.close()
    traj: dict[str, list[tuple[dt.datetime, tuple[float, float, float]]]] = defaultdict(list)
    for sid, t, h, d, a in rows:
        if h is None or d is None or a is None or min(h, d, a) <= 1.0:
            continue
        lst = traj[sid]
        q = (float(h), float(d), float(a))
        if lst and lst[-1][1] == q:
            continue
        lst.append((t, q))
    return dict(traj)


def load_jc_traj(match_ids: Iterable[str], playtype: str) -> dict[str, list[tuple[dt.datetime, tuple[float, ...]]]]:
    """JC 逐笔轨迹（silver jc_sp_change_event）：match_id → [(t, odds...)]。

    had → (h,d,a)；hhad → (line, h水, a水)；ttg → (sp,)（outcome 单值）。
    """
    ids = list(match_ids)
    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table mids(mid varchar)")
    con.executemany("insert into mids values (?)", [(m,) for m in ids])
    rows = con.sql(
        f"""
        select j.match_id, j.published_at, j.odds_h, j.odds_d, j.odds_a, j.sp,
               j.goal_line
        from jc_sp_change_event j join mids m on j.match_id = m.mid
        where j.playtype = '{playtype}' and j.published_at is not null
        order by j.match_id, j.published_at, j.source_order
        """
    ).fetchall()
    con.close()
    traj: dict[str, list[tuple[dt.datetime, tuple[float, ...]]]] = defaultdict(list)
    for mid, t, h, d, a, sp, line in rows:
        if playtype == "had":
            if h is None or d is None or a is None:
                continue
            q: tuple[float, ...] = (float(h), float(d), float(a))
        elif playtype == "hhad":
            if h is None or a is None or line is None:
                continue
            q = (float(line), float(h), float(a))
        else:
            if sp is None:
                continue
            q = (float(sp),)
        lst = traj[mid]
        if lst and lst[-1][1] == q:
            continue
        lst.append((t, q))
    return dict(traj)


OU_BOOKS = ("srct:ou:14", "srct:ou:42", "srct:ou:8", "srct:ou:22", "srct:ou:24", "srct:ou:17", "srct:ou:12")  # pre 板覆盖 top 书（ou:3 滚后为主不入梯）


@dataclass(frozen=True)
class LadderRung:
    """OU 梯单线档：跨书均值锚 + 覆盖/新鲜度账（edge-audit-20261109 票 12 修正）。"""

    p_over: float  # 跨书均值 P(over|line)（每书末价两侧去水后均值）
    n_books: int  # 该线有合格报价的书数（书覆盖）
    age_h: float  # 距开球最陈旧书的报价年龄（小时；kickoff 缺失=nan）


def aggregate_ou_ladder(
    rows: Iterable[tuple[str, str, float, dt.datetime, dt.datetime | None, float, float]],
) -> dict[str, dict[float, LadderRung]]:
    """合格 OU 报价行 → 每书最晚报价 → 跨书均值梯（纯函数）。

    rows=(sid, book, line, published_at, kickoff, home_water, away_water)，
    须已按 (sid, line, book, published_at, source_order) 升序（load_ou_ladder
    SQL 保证）且已过 pre/early+sane 过滤。每 (sid,line,book) 取最晚一条
    （同刻以 source_order 后者为准），两侧去水得该书 P_over，再跨书均值；
    age_h=kickoff−各书末价时刻的最大值（锚的新鲜度受最陈旧成分约束）。
    """
    last: dict[tuple[str, float, str], tuple[dt.datetime, dt.datetime | None, float, float]] = {}
    for sid, book, line, t, ko, hw, aw in rows:
        last[(sid, float(line), book)] = (t, ko, hw, aw)  # 升序输入 → 后写覆盖=该书最晚
    per: dict[tuple[str, float], list[tuple[float, dt.datetime, dt.datetime | None]]] = defaultdict(list)
    for (sid, line, _book), (t, ko, hw, aw) in last.items():
        d_o, d_u = 1.0 + hw, 1.0 + aw
        per[(sid, line)].append(((1.0 / d_o) / (1.0 / d_o + 1.0 / d_u), t, ko))
    ladder: dict[str, dict[float, LadderRung]] = defaultdict(dict)
    for (sid, line), books in per.items():
        kos = {ko for _p, _t, ko in books}
        if None in kos or len(kos) > 1:
            age = float("nan")  # kickoff 缺失/异常不报告年龄
        else:
            ko = next(iter(kos))
            age = max((ko - t).total_seconds() / 3600.0 for _p, t, _ko in books)
        pos = [p for p, _t, _ko in books]
        ladder[sid][line] = LadderRung(p_over=sum(pos) / len(pos), n_books=len(pos), age_h=age)
    return dict(ladder)


def load_ou_ladder(sids: Iterable[str]) -> dict[str, dict[float, LadderRung]]:
    """OU 赛前梯（部分梯）：sid → {line: LadderRung}。

    过滤（既有口径保留）：status in ('pre','early') 且双侧水位 sane 带
    （0.25..1.75，剔除滚后/盘后垃圾板与深尾水位）的纯半线（0.5..6.5），
    书集=OU_BOOKS。聚合语义（edge-audit-20261109 票 12 修正，名实对齐）：
    **每书取其最晚合格赛前报价，两侧去水得该书 P_over，再跨书均值**——
    修正前 SQL 按 (sid,line) arg_max(published_at) 直接取水位，拿到的是
    最后更新一家的报价而非跨书共识。rung 附书覆盖数 n_books 与报价年龄
    age_h（距开球最陈旧书的报价小时数，供噪声锚诊断）。
    """
    sid_list = list(sids)
    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in sid_list])
    books_sql = ",".join(f"'{b}'" for b in OU_BOOKS)
    rows = con.sql(
        f"""
        select o.sid, o.bookmaker_id, o.line, o.published_at, o.kickoff,
               o.home_water, o.away_water
        from odds_change_event o join sids s using (sid)
        where o.market = 'ou' and o.bookmaker_id in ({books_sql})
          and o.status in ('pre', 'early') and o.published_at is not null
          and o.line in (0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 6.5)
          and o.home_water between 0.25 and 1.75
          and o.away_water between 0.25 and 1.75
        order by o.sid, o.line, o.bookmaker_id, o.published_at, o.source_order
        """
    ).fetchall()
    con.close()
    return aggregate_ou_ladder(rows)


def ladder_coherent(ladder: dict[float, LadderRung]) -> bool:
    """梯一致性门：主线 2.5 概率 sane + 全梯非增（容差 0.02 噪声）。"""
    if 2.5 not in ladder or not 0.25 <= ladder[2.5].p_over <= 0.8:
        return False
    ls = sorted(ladder)
    for a, b in zip(ls, ls[1:]):
        if ladder[b].p_over > ladder[a].p_over + 0.02:
            return False
    return True


def ladder_to_totals(ladder: dict[float, LadderRung]) -> dict[int, float]:
    """部分梯 → {k: P(T=k)}（k=0..7，7=7+）；只含两条所需半线齐备的 k。

    fair=梯差原始值（协议修订 A：不做跨线归一）。空梯→空 dict。
    """
    out: dict[int, float] = {}
    cum = {0: 1.0}
    for k in range(1, 8):
        if (k - 0.5) in ladder:
            cum[k] = ladder[k - 0.5].p_over
    if 1 in cum and 0 in cum:
        p = cum[0] - cum[1]
        if p > 0:
            out[0] = p
    for k in range(1, 7):
        if k in cum and (k + 1) in cum:
            p = cum[k] - cum[k + 1]
            if p > 0:
                out[k] = p
    if 7 in cum:
        out[7] = cum[7]
    return out


# ------------------------------------------------- ttg 结算（edge-audit-20261109 票 12 修正）


def ttg_hit(k: int, total: int) -> bool:
    """ttg 命中判定：k=7 为「7+」档按 total≥7 计；k=0..6 维持恰好命中。

    修正前 t==k 使总进球 8/9 漏掉 7+ 档回款（外部审计反例：{3..7+} 等注
    五腿、7+ 赔 30、总进球 8 → 正确 +500%，原实现 −100%）。
    """
    return total >= 7 if k == 7 else total == k


def ttg_basket_ev(odds: tuple[float, ...], total: int, ks: Sequence[int]) -> float:
    """等注篮实现 EV：篮=ks 档等注 1，回款=命中档赔率（7+ 档按 ≥7）。

    与 09 脚本 F8 口径一致：EV=mean(命中腿赔率)−1；odds=JC ttg s0..s7。
    """
    payoff = sum(odds[k] for k in ks if ttg_hit(k, total))
    return payoff / len(ks) - 1.0


# ------------------------------------------------- 多重校正（edge-audit-20261109 票 12 修正）


def one_sided_p(z: float) -> float:
    """单侧 p 值（正 z=正收益方向）。"""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def bh(pairs_pv: Sequence[tuple[str, float]], alpha: float = 0.05) -> set[str]:
    """Benjamini–Hochberg：对传入 p 值族做校正，返回通过者 entry_id 集。"""
    if not pairs_pv:
        return set()
    m = len(pairs_pv)
    ranked = sorted(pairs_pv, key=lambda kv: kv[1])
    k_max = 0
    for i, (_eid, p) in enumerate(ranked, 1):
        if p <= alpha * i / m:
            k_max = i
    return {eid for eid, _p in ranked[:k_max]}


def bh_family(zs: Sequence[tuple[str, float]], alpha: float = 0.05) -> set[str]:
    """BH 校正（家族=传入的全部候选，含全阴与不可计算者）。

    票 12 修正：先筛后校废除——校正家族必须是该轮进入验证的全部候选，
    只把过门者送进 bh() 会放大假阳性（审计反例：20 候选 1 个 p=0.02 +
    19 个 p=0.5，全家族 BH 无一通过，先筛后校则 0.02 通过）。z 非有限
    （样本不足等无法检定）→ p=1.0 入族，保守扩大分母。
    """
    pv = [(eid, one_sided_p(z) if math.isfinite(z) else 1.0) for eid, z in zs]
    return bh(pv, alpha)


# --------------------------------- 轮级单家族装载（edge-audit-20261109 票 20，E-32 修订 C）

EM1_ROUND_BH_GROUPS: tuple[tuple[str, str], ...] = (("09", "A"), ("10", "B"), ("10", "C"))
"""一轮 EM 的 BH 贡献组（协议预注册轮定义，09-em1-protocol.md 修订 C）：
拥有搜索→候选→晋升门路径的全部 (ticket, facet) 组。facet D（票 11）按前置
refuted 关闭、无候选路径，不入组；未来 facet 开候选路径时在此扩组（粒度
跑数前预注册，跑数后不得更换）。"""

EM1_ROUND = 2
"""当前轮号（correctness 五修 F1+F2：轮=台账文件边界，一轮一文件）。
轮 1=09-em1-trial-ledger.csv（196 行冻结历史档，字节不变）；开新轮 EM
跑数时只改本常量——新轮落新文件，缺组 fail-closed 对空/不完整的新轮
文件自然成立，entry_id 幂等只作用于同轮重跑。"""

EM1_LEDGER_COLS = [
    "entry_id", "ts", "ticket", "facet", "family", "slice", "window",
    "n_fx", "n_sel", "metric", "value", "se", "z", "p", "stage", "note",
]


def em1_round_ledger_path(round_no: int = EM1_ROUND) -> str:
    """当前轮台账文件路径单源（09/10 脚本与装载/裁定接缝共用）。

    轮 1=09-em1-trial-ledger.csv（无后缀，冻结历史档）；轮 N≥2=
    09-em1-trial-ledger-rN.csv。read_trial_ledger / append_trial_ledger /
    em1_round_adjudicate 默认全走本函数返回的当前轮文件——轮=文件边界，
    历史轮的 search 行无法解除新轮缺组防护，跨轮同名 entry_id 落在各自
    轮文件里互不吞。
    """
    d = os.path.dirname(os.path.abspath(__file__))
    suffix = "" if round_no <= 1 else f"-r{round_no}"
    return os.path.join(d, f"09-em1-trial-ledger{suffix}.csv")


def read_trial_ledger(path: str) -> list[dict]:
    """读 EM-1 试验账全部行（文件不存在→空表，视作该账尚未落任何条目）。"""
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def append_trial_ledger(rows: Sequence[dict], path: str | None = None) -> None:
    """当前轮台账追加（entry_id 幂等=同轮重跑不重复落账；文件按需建头）。

    path 缺省=em1_round_ledger_path()（当前轮文件）。幂等只按本文件内已见
    entry_id 判定——轮=台账文件边界后，新轮同名切片行落在新轮文件里，
    不再被旧轮同名行静默吞掉（correctness 五修 F2）。
    """
    ledger = em1_round_ledger_path() if path is None else path
    seen = {r["entry_id"] for r in read_trial_ledger(ledger)}
    new = [r for r in rows if r["entry_id"] not in seen]
    if not new:
        return
    need_header = not os.path.exists(ledger) or os.path.getsize(ledger) == 0
    with open(ledger, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EM1_LEDGER_COLS)
        if need_header:
            w.writeheader()
        for r in new:
            w.writerow({k: r.get(k, "") for k in EM1_LEDGER_COLS})


def _parse_z(s: str) -> float:
    """试验账 z 列 → float；空串/不可解析 → nan（bh_family 按 p=1.0 入族）。"""
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def em1_format_z(z: float) -> str:
    """落账 z 列格式（correctness 五修 F4）：6dp。

    裁定取台账 z 装载值，3dp 截断在 BH 刃缘可反向翻转裁定（真 z=1.64451
    →p=0.050035 应拒；3dp 落账读回 1.645→p=0.049985 通过），且与台账
    p 列（全精度算出）自相矛盾。新落账行一律 6dp；轮 1 既有 3dp 行=历史
    冻结不动，装载侧（_parse_z）解析不变。
    """
    return f"{z:.6f}"


def em1_pre_adjudication_rows(rows: Sequence[dict]) -> list[dict]:
    """裁定前首批落账行（correctness 五修 F3）。

    候选追踪族（F1-F6/F8、B/C）的确认窗行不得在裁定前以占位 stage 落账：
    裁定后第二批按最终 stage（survivor/dead）写入时同 entry_id 会被
    append 幂等吞掉，幸存者确认行无声丢失（票 20 diff 曾把旧「同批双写
    重复」换成「两批丢失」）。首批经本函数过滤：确认窗行只保留 F7 类
    structure_only 结构行（无晋升路径、无二批重写，三窗覆盖账照常首批落）。
    """
    return [
        r
        for r in rows
        if r.get("window") != "confirmation" or r.get("stage") == "structure_only"
    ]


def em1_round_family(rows: Sequence[dict]) -> list[tuple[str, float]]:
    """轮级单家族成员装载（E-32 修订 C，票 20）。

    家族=该轮进入验证窗的全部候选：试验账 window=search 且 stage=candidate
    的**全部条目跨 facet 并集**（不分脚本/组，任何按组先筛减族=违约）；全阴、
    样本不足、验证窗无行者均在族内，验证条目缺失或 z 不可计算 → nan
    （bh_family 按 p=1.0 入族，只放大分母不放松分位阈值）。返回
    (validation_entry_id, z)，与各脚本晋升成员身份口径一致。
    """
    val_z: dict[str, float] = {}
    for r in rows:
        if r.get("window") != "validation":
            continue
        base, _sep, _w = r["entry_id"].rpartition("-")
        val_z[base] = _parse_z(r.get("z", ""))
    family: list[tuple[str, float]] = []
    for r in rows:
        if r.get("window") == "search" and r.get("stage") == "candidate":
            base, _sep, _w = r["entry_id"].rpartition("-")
            family.append((f"{base}-validation", val_z.get(base, float("nan"))))
    return family


def em1_round_missing_groups(rows: Sequence[dict]) -> list[tuple[str, str]]:
    """轮完整性 fail-closed 判据：预注册 BH 贡献组中尚无任何 search 落账的组。

    修订 C 下家族=轮内全部候选——兄弟组未跑搜索就把族缩成「本脚本候选」
    与先筛后校同罪；返回非空时调用方不得做轮级 BH 裁定（候选留 bh_pending，
    待轮内其余脚本落账后重跑收口；脚本幂等，无「先跑谁」硬序）。
    """
    seen = {(r.get("ticket"), r.get("facet")) for r in rows if r.get("window") == "search"}
    return [g for g in EM1_ROUND_BH_GROUPS if g not in seen]


def em1_round_adjudicate(
    ledger_path: str | None = None,
) -> tuple[set[str], list[tuple[str, float]], list[tuple[str, str]]]:
    """轮级单家族 BH 裁定（09/10 共用接缝；须在调用方落完搜索+验证账后调）。

    装载全轮族→完整性 fail-closed→一次 BH：missing_groups 非空时返回
    bh_pass=空集（不裁，候选留 bh_pending，调用方打印收口指引），否则
    返回 bh_family(round_family)。ledger_path 缺省=当前轮台账文件
    （em1_round_ledger_path，轮=台账文件边界）。返回
    (bh_pass, round_family, missing_groups)。
    """
    path = em1_round_ledger_path() if ledger_path is None else ledger_path
    ledger_all = read_trial_ledger(path)
    round_family = em1_round_family(ledger_all)
    missing = em1_round_missing_groups(ledger_all)
    if missing:
        return set(), round_family, missing
    return bh_family(round_family), round_family, missing


def em1_round_rejected_rows(rows: Sequence[dict], bh_pass: set[str]) -> list[dict]:
    """完整轮裁定后被拒候选的终态行（correctness 五修 F5）。

    验证窗 stage=bh_pending（过晋升门待裁）而 BH 未通过的候选 → 追加
    stage=bh_rejected 行：entry_id=验证条目的窗段换为 bh（新条目不违
    append-only，幂等重跑只落一次），行内容复制自该验证行（z/p 等检定量
    随之可复原）。门死者（dead）与通过者不生成——被拒候选的台账终态不再
    永留 bh_pending、裁定结果不再只存在于 stdout。
    """
    out: list[dict] = []
    for r in rows:
        if r.get("window") != "validation" or r.get("stage") != "bh_pending":
            continue
        if r["entry_id"] in bh_pass:
            continue
        row = dict(r)
        base, _sep, _w = r["entry_id"].rpartition("-")
        note = r.get("note", "")
        row["entry_id"] = f"{base}-bh"
        row["stage"] = "bh_rejected"
        row["note"] = f"轮级BH拒绝(α=0.05);{note}" if note else "轮级BH拒绝(α=0.05)"
        out.append(row)
    return out


def em1_round_finalize(
    ledger_path: str | None = None,
) -> tuple[set[str], list[tuple[str, float]], list[tuple[str, str]]]:
    """轮级裁定+被拒终态落账（09/10 共用接缝，correctness 五修 F5）。

    em1_round_adjudicate 之后再进一步：轮完整裁定完成时，对被拒候选追加
    bh_rejected 终态行（台账终态可区分「轮未齐 bh_pending」与「裁毕被拒
    bh_rejected」）；轮不完整时不动台账（fail-closed，候选留 bh_pending）。
    返回值与 em1_round_adjudicate 一致：(bh_pass, round_family, missing)。
    """
    path = em1_round_ledger_path() if ledger_path is None else ledger_path
    bh_pass, round_family, missing = em1_round_adjudicate(path)
    if not missing:
        append_trial_ledger(em1_round_rejected_rows(read_trial_ledger(path), bh_pass), path)
    return bh_pass, round_family, missing


def load_pin_ah(sids: Iterable[str]) -> dict[str, list[tuple[dt.datetime, float, float, float]]]:
    """ah 主账书轨迹：sid → [(t, line, home水, away水)]。书=PIN_AH。"""
    sid_list = list(sids)
    con = duckdb.connect(DUCK, read_only=True)
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in sid_list])
    rows = con.sql(
        f"""
        select o.sid, o.published_at, o.line, o.home_water, o.away_water
        from odds_change_event o join sids s using (sid)
        where o.bookmaker_id = '{PIN_AH}' and o.market = 'ah'
          and o.published_at is not null
          and o.line is not null and o.home_water is not null
        order by o.sid, o.published_at, o.source_order
        """
    ).fetchall()
    con.close()
    traj: dict[str, list[tuple[dt.datetime, float, float, float]]] = defaultdict(list)
    for sid, t, line, hw, aw in rows:
        if line is None or hw is None or aw is None:
            continue
        lst = traj[sid]
        q = (float(line), float(hw), float(aw))
        if lst and lst[-1][1:] == q[1:]:
            continue
        lst.append((t, *q))
    return dict(traj)
