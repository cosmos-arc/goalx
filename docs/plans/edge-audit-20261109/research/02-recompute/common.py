"""票 02（载重判决独立复算）共享工具——独立代码路径。

纪律：不复用/不 import 原报告脚本（autoresearch-20261107/reports/_lib.py 等
只读参考其口径定义）；本文件全部计算逻辑为本次审计独立重写。

数据面（全部只读）：
- corpus: /Users/chevy/goalx-data/duckdb/corpus.duckdb（duckdb read_only）
- 业务库: repo data/goalx.db（sqlite mode=ro URI）
- bronze JC SP 史: /Users/chevy/goalx-data/bronze/jc/sp_history.ndjson.gz

口径常量（league 映射、Pinnacle bookmaker id 等）为源报告口径的输入参数，
非计算逻辑，按原口径转录。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Sequence

import duckdb
import numpy as np

CORPUS = "/Users/chevy/goalx-data/duckdb/corpus.duckdb"
BUSINESS_DB = "/Users/chevy/Desktop/code/goalx/data/goalx.db"
BRONZE_JC = "/Users/chevy/goalx-data/bronze/jc/sp_history.ndjson.gz"
CACHE_DIR = Path(__file__).resolve().parent / "_cache"

# JC 联盟全名 → srct 联赛名（源报告票 07 口径转录）
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
PIN_1X2 = "srct:1x2:177"  # Pinnacle 1x2（源报告实证命名）
DIRS = ("h", "d", "a")


def corpus_ro() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(CORPUS, read_only=True)


def business_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{BUSINESS_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------- 去水


def devig_ratio(odds: Sequence[float | None]) -> tuple[float, float, float] | None:
    """比例化去水（1/o 归一）。任一价缺失或 ≤1 → None。"""
    if odds is None or any(o is None or o <= 1.0 for o in odds):
        return None
    inv = np.array([1.0 / float(o) for o in odds])
    if not np.isfinite(inv).all() or inv.sum() <= 0:
        return None
    fair = inv / inv.sum()
    return float(fair[0]), float(fair[1]), float(fair[2])


def shin_probs(odds: Sequence[float | None]) -> tuple[float, ...] | None:
    """Shin 去水（独立实现）：p_i=(sqrt(z²+4(1−z)π_i²/B)−z)/(2(1−z))，
    z 由 Σp=1 解出（二分法自实现，[0,0.9999]，无根取上界后归一）。
    病态盘（Σ1/o≤1）退化为比例化。"""
    if odds is None or any(o is None or float(o) <= 1.0 for o in odds):
        return None
    raw = [1.0 / float(o) for o in odds]
    total = sum(raw)
    if total <= 1.0 + 1e-12:
        return tuple(r / total for r in raw)

    def probs_at(z: float) -> list[float]:
        return [
            (math.sqrt(z * z + 4.0 * (1.0 - z) * r * r / total) - z) / (2.0 * (1.0 - z))
            for r in raw
        ]

    # Σp 随 z 单调下降（z=0 时 Σp=sqrt(B)>1）；解 Σp=1。
    lo, hi = 0.0, 0.9999
    if sum(probs_at(hi)) > 1.0:
        z = hi  # 病态盘口：区间内无根，取上界（同源报告口径）
    else:
        for _ in range(200):  # 二分至 1e-15 量级
            mid = (lo + hi) / 2
            if sum(probs_at(mid)) > 1.0:
                lo = mid  # Σp>1 → 根在更大 z 一侧
            else:
                hi = mid
        z = (lo + hi) / 2
    probs = probs_at(z)
    s = sum(probs)
    return tuple(p / s for p in probs)


# --------------------------------------------------------------- 统计


def clustered_mean_se(rows: list[tuple[int, float]]) -> tuple[float, float, int]:
    """按场聚类均值与 SE：同场多行先取场内均值，再跨场求均值/SE（ddof=1）。"""
    by_fx: dict[int, list[float]] = defaultdict(list)
    for fx, v in rows:
        by_fx[fx].append(v)
    per_fx = np.array([float(np.mean(v)) for v in by_fx.values()])
    n = len(per_fx)
    if n < 2:
        return float("nan"), float("nan"), n
    return float(per_fx.mean()), float(per_fx.std(ddof=1) / math.sqrt(n)), n


def welch_diff(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    ma, mb = float(a.mean()), float(b.mean())
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return ma - mb, se


# --------------------------------------------------------------- JC bronze 装载


def load_jc_bronze(min_year: str = "2016") -> list[dict]:
    """bronze sp_history → JC had 收盘行（末条 hadList=收盘，官方独供 SP 史）。

    口径（源报告票 07）：跳过 isCancel；leagueAllName 须在 11 联赛映射内；
    close=hadList 末条，updateDate ≥ min_year；三向价齐且 >1；比分取
    sectionsNo999（配对验证位）。首次解析后缓存 parquet。
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"jc_bronze_{min_year}.parquet"
    con = duckdb.connect()
    if cache.exists():
        rows = con.sql(
            f"select sid, league, home, away, date, odds_h, odds_d, odds_a, score_h, score_a from read_parquet('{cache}')"
        ).fetchall()
        con.close()
        return [
            {
                "sid": sid, "league": lg, "home": h, "away": a, "date": d,
                "odds": (oh, od, oa),
                "score": (sh, sa) if sh is not None and sa is not None else None,
            }
            for sid, lg, h, a, d, oh, od, oa, sh, sa in rows
        ]

    out: list[dict] = []
    with gzip.open(BRONZE_JC, "rt") as f:
        for line in f:
            rec = json.loads(line)
            payload = rec.get("payload") or {}
            if payload.get("isCancel"):
                continue
            oh = payload.get("oddsHistory") or {}
            had = oh.get("hadList") or []
            if not had:
                continue
            lg = LEAGUE_MAP.get(oh.get("leagueAllName") or "")
            if lg is None:
                continue
            close = had[-1]
            date_str = close.get("updateDate") or ""
            if not date_str or date_str < min_year:
                continue
            try:
                jc = (float(close["h"]), float(close["d"]), float(close["a"]))
            except (KeyError, ValueError, TypeError):
                continue
            if min(jc) <= 1.0:
                continue
            score = None
            raw_score = payload.get("sectionsNo999")
            if isinstance(raw_score, str) and ":" in raw_score:
                try:
                    score = (int(raw_score.split(":")[0]), int(raw_score.split(":")[1]))
                except ValueError:
                    score = None
            out.append(
                {
                    "sid": rec["sid"],
                    "league": lg,
                    "home": (oh.get("homeTeamAllName") or "").replace(" ", ""),
                    "away": (oh.get("awayTeamAllName") or "").replace(" ", ""),
                    "date": dt.date.fromisoformat(date_str),
                    "odds": jc,
                    "score": score,
                }
            )
    df_cols = "sid varchar, league varchar, home varchar, away varchar, date date, odds_h double, odds_d double, odds_a double, score_h int, score_a int"
    con.execute(f"create table t({df_cols})")
    con.executemany(
        "insert into t values (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["sid"], r["league"], r["home"], r["away"], r["date"],
                r["odds"][0], r["odds"][1], r["odds"][2],
                r["score"][0] if r["score"] else None, r["score"][1] if r["score"] else None,
            )
            for r in out
        ],
    )
    con.execute(f"copy t to '{cache}' (format parquet)")
    con.close()
    return out


# --------------------------------------------------------------- 欧侧金表装载


def load_euro(min_date: str = "2016-01-01") -> list[dict]:
    """corpus match_features → 场次行（三锚+开球+比分+赛季）。只读。"""
    con = corpus_ro()
    rows = con.sql(
        """
        select sid, league, kickoff, home, away, home_goals, away_goals, season,
               avgc_home, avgc_draw, avgc_away,
               psc_home, psc_draw, psc_away,
               close1x2_cons_h, close1x2_cons_d, close1x2_cons_a
        from match_features
        where home_goals is not null and admin_excluded = 0
          and kickoff >= ?
        """,
        params=[min_date],
    ).fetchall()
    con.close()
    out = []
    for sid, lg, ko, h, a, hg, ag, season, *anchors in rows:
        out.append(
            {
                "sid": sid,
                "league": lg,
                "kickoff": ko,
                "date": ko.date(),
                "home": (h or "").replace(" ", ""),
                "away": (a or "").replace(" ", ""),
                "hg": hg,
                "ag": ag,
                "season": season,
                "avgc": tuple(anchors[0:3]),
                "psc": tuple(anchors[3:6]),
                "srct": tuple(anchors[6:9]),
            }
        )
    return out


# --------------------------------------------------------------- 配对（独立实现）


def _names_agree(a: str, b: str) -> bool:
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a))


def pair_jc_euro(jc_rows: list[dict], eu_rows: list[dict]) -> tuple[list[tuple[dict, dict]], dict]:
    """JC(bronze had 收盘) ↔ 金表 三轮配对（口径=源报告票 07，逻辑独立重写）：
    轮1 名字精确/前缀（(联赛,日期±1) 格内、比分一致验证、EU 唯一占用）；
    轮2 迭代 ≤6 次：名票≥2 学别名重试 + 「格内唯一未配 JC ∧ 唯一比分相等
    未用 EU」自举；无进展即停。返回 (jc, eu) 对 + 漏斗统计。"""
    eu_cell: dict[tuple[str, dt.date], list[dict]] = defaultdict(list)
    jc_cell: dict[tuple[str, dt.date], list[dict]] = defaultdict(list)
    for e in eu_rows:
        eu_cell[(e["league"], e["date"])].append(e)
    for j in jc_rows:
        jc_cell[(j["league"], j["date"])].append(j)

    def cells(row: dict):
        for off in (-1, 0, 1):
            yield (row["league"], row["date"] + dt.timedelta(days=off))

    def score_ok(jc: dict, eu: dict) -> bool:
        """None=无 JC 比分（放行）；True/False=比对结果（False 拒配）。"""
        if jc["score"] is None:
            return True
        return jc["score"] == (eu["hg"], eu["ag"])

    def name_match(jc: dict, alias: dict[str, str]) -> dict | None:
        h = alias.get(jc["home"], jc["home"])
        a = alias.get(jc["away"], jc["away"])
        for cell in cells(jc):
            for eu in eu_cell.get(cell, ()):  # 格内序=装载序（确定性）
                if eu["sid"] in used_eu:
                    continue
                if _names_agree(h, eu["home"]) and _names_agree(a, eu["away"]) and score_ok(jc, eu):
                    return eu
        return None

    pairs: list[tuple[dict, dict]] = []
    used_eu: set[str] = set()
    matched: set[str] = set()

    for jc in jc_rows:  # 轮 1：名字
        eu = name_match(jc, {})
        if eu is not None:
            pairs.append((jc, eu))
            used_eu.add(eu["sid"])
            matched.add(jc["sid"])
    n_name = len(pairs)

    votes: dict[tuple[str, str], int] = defaultdict(int)
    alias: dict[str, str] = {}
    n_boot = 0
    for _ in range(6):  # 轮 2：别名 + 唯格自举，迭代至不动点
        for (jname, ename), v in list(votes.items()):
            if v >= 2 and jname not in alias:
                alias[jname] = ename
        for jc in jc_rows:
            if jc["sid"] in matched:
                continue
            eu = name_match(jc, alias)
            if eu is not None:
                pairs.append((jc, eu))
                used_eu.add(eu["sid"])
                matched.add(jc["sid"])
                n_boot += 1
                votes[(jc["home"], eu["home"])] += 1
                votes[(jc["away"], eu["away"])] += 1
        progressed = False
        for jc in jc_rows:
            if jc["sid"] in matched or jc["score"] is None:
                continue
            for cell in cells(jc):
                open_jc = [j for j in jc_cell.get(cell, ()) if j["sid"] not in matched]
                open_eu = [
                    e
                    for e in eu_cell.get(cell, ())
                    if e["sid"] not in used_eu and (e["hg"], e["ag"]) == jc["score"]
                ]
                if len(open_jc) == 1 and len(open_eu) == 1 and open_jc[0]["sid"] == jc["sid"]:
                    pairs.append((jc, open_eu[0]))
                    used_eu.add(open_eu[0]["sid"])
                    matched.add(jc["sid"])
                    n_boot += 1
                    votes[(jc["home"], open_eu[0]["home"])] += 1
                    votes[(jc["away"], open_eu[0]["away"])] += 1
                    progressed = True
                    break
        if not progressed:
            break

    stats = {
        "jc_rows": len(jc_rows),
        "eu_rows": len(eu_rows),
        "paired_name": n_name,
        "paired_bootstrap": n_boot,
        "paired_total": len(pairs),
        "unpaired_jc": len(jc_rows) - len(pairs),
        "alias_learned": len(alias),
    }
    return pairs, stats


# --------------------------------------------------------------- 轨迹装载


def load_jc_had_traj(match_ids: Iterable[str]) -> dict[str, list[tuple[dt.datetime, tuple[float, float, float]]]]:
    """silver jc_sp_change_event had → mid → [(t,(h,d,a))]（去连续同价）。"""
    ids = list(match_ids)
    con = corpus_ro()
    con.execute("create temp table mids(mid varchar)")
    con.executemany("insert into mids values (?)", [(m,) for m in ids])
    rows = con.sql(
        """
        select j.match_id, j.published_at, j.odds_h, j.odds_d, j.odds_a
        from jc_sp_change_event j join mids m on j.match_id = m.mid
        where j.playtype = 'had' and j.published_at is not null
        order by j.match_id, j.published_at, j.source_order
        """
    ).fetchall()
    con.close()
    traj: dict[str, list[tuple[dt.datetime, tuple[float, float, float]]]] = defaultdict(list)
    for mid, t, h, d, a in rows:
        if h is None or d is None or a is None or min(h, d, a) <= 1.0:
            continue
        q = (float(h), float(d), float(a))
        lst = traj[mid]
        if lst and lst[-1][1] == q:
            continue
        lst.append((t, q))
    return dict(traj)


def load_pin1x2_traj(sids: Iterable[str]) -> dict[str, list[tuple[dt.datetime, tuple[float, float, float]]]]:
    """odds_change_event Pinnacle 1x2 → sid → [(t,(h,d,a))]（去连续同价）。"""
    ids = list(sids)
    con = corpus_ro()
    con.execute("create temp table sids(sid varchar)")
    con.executemany("insert into sids values (?)", [(s,) for s in ids])
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
        q = (float(h), float(d), float(a))
        lst = traj[sid]
        if lst and lst[-1][1] == q:
            continue
        lst.append((t, q))
    return dict(traj)


def quote_at(traj: list[tuple[dt.datetime, tuple]], t: dt.datetime):
    """升序轨迹中 ≤t 的最后报价（自实现二分）。"""
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
