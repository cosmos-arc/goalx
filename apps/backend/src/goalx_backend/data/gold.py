"""
Gold 特征面构建器（backtest-decade B 相，spec User Story 6-12）。

语料树 ``gold/goalx/match_features``：fixture 粒度一行一场、特征列平铺
（_meta 版本戳 + 输入 silver digest 钉死；确定性排序 → 字节级幂等重建，
silver 同款舞步）。输入全经 duckdb 只读桥（silver 视图 + goalx.* ATTACH
只读）；gold 写入只落语料树 parquet，运行面永不写（ADR-0011）。

**时代分层（era 常量唯一落点，下游只读 era 字段）**：断点 = 1x2 轨迹
起点 2023/24（实测 2023-24 起步 115 场/2024-25 全季 4,491 场）。
- ``psc_proxy``（2023-24 前）：欧赔收盘正典 = fdhist PSC（收盘代理）/
  PSH（开盘）/AvgC，经 corpus_gate.match_fixtures v3 名字身份配对。
- ``trajectory``（2023-24 起）：正典 = 语料轨迹派生收盘（cid177 锚
  赛前末价 + 多书中位数共识归一）。两族配对可得时并存，era 字段声明
  哪族是正典（era 对照量化留给 D 相，PSC≠轨迹末价的已知局限不在 gold
  逐场调和）。

**PIT 纪律（无前视断言，违规即构建失败）**：逐行 ``pit_max_ms`` = 所用
PIT 类输入（轨迹收盘与 1h/24h 切片的 published_at）最大值，断言严格
早于开球。非 PIT 类输入的防前视面：market_quote 初/终盘组=页面定义的
盘前价（latest 组不进特征）；fdhist PSC 族=定义在收盘时点；xG/比分/
半场技统=赛后观测（标签侧，列名自明，决策面禁用）。

**卫生接线（A4/A5 单一取数口，不散写第二份语义）**：行政判赛命中 →
``admin_excluded=true`` 且不消费该 hist 行任何特征；fdhist 全族 odds 列
经 ``psc_mature`` 成熟度门（同一 CSV 回填滞后，未成熟=暂定置空；today
入 _meta 审计）。引擎训练集过滤 = 票 14（C 相），gold 只标不改。

**xG 源纪律**：Understat（2014+，xg 总值）主源、源T stats
（xg_observation，2024/25+）近代源；单场严禁混源——一行一源
（``xg_source`` 标记，主源优先），配对缺席诚实留空不补（2023/24
非五大缺口即留空）。understat 配对 = 联赛+日期±1+比分（名字空侧），
歧义不硬配（match_fixtures 语义）。

pyarrow 无官方 stub（同 silver 先例）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb
import pyarrow as pa

from goalx_backend.data.corpus_gate import (
    FD_TO_LEAGUE,
    LEAGUE_TO_FD,
    FixtureRow,
    HistRow,
    match_fixtures,
)
from goalx_backend.data.corpus_store import (
    GOLD_DATASET,
    GOLD_PROVIDER,
    CorpusStore,
)
from goalx_backend.data.hygiene import admin_exclusions, is_admin_excluded, psc_mature
from goalx_backend.data.ingest.srct_market import MARKET_AH
from goalx_backend.data.ingest.srct_odds import AH_ANCHOR_CID, beijing_ms
from goalx_backend.data.results import UNDERSTAT_LEAGUES
from goalx_backend.data.silver import write_dataset_meta, write_partition
from goalx_backend.db import utc_now_iso

# 数据集身份 GOLD_PROVIDER/GOLD_DATASET 唯一落点=corpus_store（防环，见其注释）
GOLD_VERSION = "gold_features_v1"

# ---- 时代分层常量（spec 实现决策：集中一处，下游只读 era 字段）----
ERA_TRAJECTORY_SEASON = "2023-24"  # 1x2 轨迹起点季（含）起 = trajectory 时代
ERA_PSC_PROXY = "psc_proxy"
ERA_TRAJECTORY = "trajectory"

# 轨迹切片窗（开球前）：1h / 24h
SLICE_1H_MS = 3_600_000
SLICE_24H_MS = 86_400_000
# 欧赔 1x2 轨迹锚（票 75 收盘接管同锚）与亚盘锚（design-15 双空间）
E1X2_ANCHOR = "srct:1x2:177"
AH_ANCHOR = f"srct:ah:{AH_ANCHOR_CID}"
# understat 主源赛季下界（2014+ 语料窗内全量取）
UNDERSTAT_SEASON_MIN = 2014

DuckCon = duckdb.DuckDBPyConnection


def _f64(name: str) -> pa.Field:
    return pa.field(name, pa.float64())


def _i32(name: str) -> pa.Field:
    return pa.field(name, pa.int32())


_SCHEMA = pa.schema(
    [
        # 身份 + 标签（home/away_goals 与半场/技统 = 赛后观测，标签侧）
        pa.field("sid", pa.string()),
        pa.field("league", pa.string()),
        pa.field("kickoff", pa.timestamp("ms")),  # 北京墙钟 naive（同 universe）
        pa.field("season", pa.string()),
        pa.field("home", pa.string()),
        pa.field("away", pa.string()),
        pa.field("home_goals", pa.int16()),
        pa.field("away_goals", pa.int16()),
        pa.field("stage", pa.string()),
        pa.field("era", pa.string()),  # psc_proxy / trajectory（正典声明）
        pa.field("admin_excluded", pa.bool_()),
        pa.field("pit_max_ms", pa.int64()),  # PIT 类输入最大时间戳（无则 None）
        # Elo（票 78 elo_self 赛前值，sid 直连）
        _f64("elo_home_pre"),
        _f64("elo_away_pre"),
        # xG（标签侧；一行一源，严禁混源）
        _f64("xg_home"),
        _f64("xg_away"),
        pa.field("xg_source", pa.string()),  # understat / srct / None
        # fdhist 欧赔（era1 正典；配对行；成熟度门内整族置空）
        _f64("psc_home"),
        _f64("psc_draw"),
        _f64("psc_away"),
        _f64("psh_home"),
        _f64("psh_draw"),
        _f64("psh_away"),
        _f64("avgc_home"),
        _f64("avgc_draw"),
        _f64("avgc_away"),
        # fdhist 26 扩展列（S12：O/U 均值开收 + 亚盘均值两端点 + 标签侧技统）
        _f64("fd_avg_ou_over"),
        _f64("fd_avg_ou_under"),
        _f64("fd_avgc_ou_over"),
        _f64("fd_avgc_ou_under"),
        _f64("fd_ah_line"),
        _f64("fd_avg_ah_home"),
        _f64("fd_avg_ah_away"),
        _f64("fd_ahc_line"),
        _f64("fd_avgc_ah_home"),
        _f64("fd_avgc_ah_away"),
        pa.field("fd_hthg", pa.int16()),
        pa.field("fd_htag", pa.int16()),
        pa.field("fd_shots_home", pa.int16()),
        pa.field("fd_shots_away", pa.int16()),
        pa.field("fd_sot_home", pa.int16()),
        pa.field("fd_sot_away", pa.int16()),
        pa.field("fd_corners_home", pa.int16()),
        pa.field("fd_corners_away", pa.int16()),
        pa.field("fd_fouls_home", pa.int16()),
        pa.field("fd_fouls_away", pa.int16()),
        pa.field("fd_yellow_home", pa.int16()),
        pa.field("fd_yellow_away", pa.int16()),
        pa.field("fd_red_home", pa.int16()),
        pa.field("fd_red_away", pa.int16()),
        # 轨迹派生欧赔收盘（era2 正典；cid177 锚 + 多书共识）
        _i32("close1x2_books"),
        _f64("close1x2_h"),
        _f64("close1x2_d"),
        _f64("close1x2_a"),
        _f64("close1x2_cons_h"),
        _f64("close1x2_cons_d"),
        _f64("close1x2_cons_a"),
        # 临场切片（1x2 锚 + AH 锚轨迹，开球前 1h/24h 时点报价）
        _f64("q1h1x2_h"),
        _f64("q1h1x2_d"),
        _f64("q1h1x2_a"),
        _f64("q24h1x2_h"),
        _f64("q24h1x2_d"),
        _f64("q24h1x2_a"),
        _f64("q1hah_line"),
        _f64("q1hah_home_water"),
        _f64("q1hah_away_water"),
        _f64("q24hah_line"),
        _f64("q24hah_home_water"),
        _f64("q24hah_away_water"),
        # AH/OU 收盘族（market_quote 盘1；中位数共识 + 分歧度 + cid8 亚盘锚）
        _i32("ah_n_books"),
        _f64("ah_open_line_med"),
        _f64("ah_close_line_med"),
        _f64("ah_line_drift"),
        _f64("ah_close_dispersion"),
        _f64("ah_close_home_water_med"),
        _f64("ah_close_away_water_med"),
        _f64("ah_anchor_open_line"),
        _f64("ah_anchor_close_line"),
        _f64("ah_anchor_drift"),
        _f64("ah_anchor_close_home_water"),
        _f64("ah_anchor_close_away_water"),
        _i32("ou_n_books"),
        _f64("ou_open_line_med"),
        _f64("ou_close_line_med"),
        _f64("ou_line_drift"),
        _f64("ou_close_dispersion"),
        _f64("ou_close_over_water_med"),
        _f64("ou_close_under_water_med"),
        # 相位计数（多庄页矩阵变价事件；early/pre=盘前两态，inplay=场内）
        _i32("ah_n_early"),
        _i32("ah_n_pre"),
        _i32("ah_n_inplay"),
        _i32("ou_n_early"),
        _i32("ou_n_pre"),
        _i32("ou_n_inplay"),
        pa.field("version", pa.string()),
    ]
)


@dataclass
class GoldBuildReport:
    """一次 match_features 重物化报告（CLI/审计面）。"""

    gold_version: str = GOLD_VERSION
    built_at: str = ""
    rows: int = 0
    era_psc_proxy_rows: int = 0
    era_trajectory_rows: int = 0
    fd_paired: int = 0
    fd_immature: int = 0  # 配对行被成熟度门截走（odds 族整族置空）
    admin_flagged: int = 0
    xg_understat: int = 0
    xg_srct: int = 0
    elo_rows: int = 0
    close1x2_rows: int = 0
    market_ah_rows: int = 0
    market_ou_rows: int = 0
    pit_rows: int = 0  # pit_max_ms 非空行（断言覆盖面）
    maturity_today: str = ""
    input_digests: dict[str, str] = field(default_factory=dict)
    degraded: str | None = None


def _era2_cte() -> str:
    """era2 场次 CTE（season ≥ 断点；kickoff 北京墙钟 → UTC epoch 毫秒）。"""
    return f"""
        era2 AS (
            SELECT sid, epoch_ms(kickoff AT TIME ZONE 'Asia/Shanghai') AS kickoff_ms
            FROM fixture_universe
            WHERE season >= '{ERA_TRAJECTORY_SEASON}' AND kickoff IS NOT NULL
        )
    """  # noqa: S608 era 界=模块常量


def _fetch_fixtures(con: DuckCon) -> list[dict[str, Any]]:
    """fixture_universe 全量（含 hive 分区 season；era 与身份的底座）。"""
    rows = con.execute(
        """
        SELECT sid, league, kickoff, season, home, away,
               home_goals, away_goals, stage
        FROM fixture_universe
        WHERE kickoff IS NOT NULL
        ORDER BY kickoff, sid
        """
    ).fetchall()
    return [
        {
            "sid": str(r[0]),
            "league": str(r[1]),
            "kickoff": r[2],
            "season": str(r[3]),
            "home": str(r[4]),
            "away": str(r[5]),
            "home_goals": r[6],
            "away_goals": r[7],
            "stage": r[8],
        }
        for r in rows
    ]


def _trajectory_close(con: DuckCon) -> dict[str, dict[str, Any]]:
    """
    era2 轨迹派生欧赔收盘。

    逐书赛前末价 → 锚（cid177）原值 + 多书中位数共识（1/中位数归一）+
    pit_ms。SQL 侧严格 published_at < 开球。
    """
    rows = con.execute(
        f"""
        WITH {_era2_cte()},
        per_book AS (
            SELECT e.sid, e.bookmaker_id,
                   arg_max(
                       struct_pack(
                           h := e.odds_home, d := e.odds_draw, a := e.odds_away,
                           t := epoch_ms(e.published_at)
                       ),
                       struct_pack(
                           t := epoch_ms(e.published_at), o := e.source_order
                       )
                   ) FILTER (WHERE epoch_ms(e.published_at) < era2.kickoff_ms)
                       AS close_q
            FROM odds_change_event e
            JOIN era2 ON era2.sid = e.sid
            WHERE e.market = '1x2'
            GROUP BY e.sid, e.bookmaker_id
        )
        SELECT sid,
               count(close_q) AS books,
               median(close_q.h) AS med_h,
               median(close_q.d) AS med_d,
               median(close_q.a) AS med_a,
               max(close_q.t) AS pit_ms,
               any_value(close_q)
                   FILTER (WHERE bookmaker_id = '{E1X2_ANCHOR}') AS anchor
        FROM per_book
        WHERE close_q IS NOT NULL
        GROUP BY sid
        """  # noqa: S608 锚 id/era 界=模块常量
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for sid, books, med_h, med_d, med_a, pit_ms, anchor in rows:
        entry: dict[str, Any] = {
            "close1x2_books": int(books),
            "pit_ms": int(pit_ms) if pit_ms is not None else None,
        }
        prices = (med_h, med_d, med_a)
        if all(p is not None and p > 0 for p in prices):
            total = sum(1.0 / p for p in prices if p is not None)
            entry["close1x2_cons_h"] = (1.0 / med_h) / total
            entry["close1x2_cons_d"] = (1.0 / med_d) / total
            entry["close1x2_cons_a"] = (1.0 / med_a) / total
        if anchor is not None:
            entry["close1x2_h"] = anchor["h"]
            entry["close1x2_d"] = anchor["d"]
            entry["close1x2_a"] = anchor["a"]
            if anchor["t"] is not None:
                entry["pit_ms"] = max(entry["pit_ms"] or 0, int(anchor["t"]))
        out[str(sid)] = entry
    return out


def _anchor_slices(con: DuckCon) -> dict[str, dict[str, Any]]:
    """
    锚轨迹临场切片（era2）。

    1x2 cid177 与 AH cid8 双锚，开球前 1h/24h 时点的末价（arg_max ≤ 界点
    ——时点报价语义，含等于）。条目携带 _pit（该侧所用最大 published_at），
    装配侧并入 pit_max_ms。
    """
    rows = con.execute(
        f"""
        WITH {_era2_cte()}
        SELECT e.sid, e.market,
               arg_max(struct_pack(v1 := e.odds_home, v2 := e.odds_draw,
                                   v3 := e.odds_away, t := epoch_ms(e.published_at)),
                       struct_pack(t := epoch_ms(e.published_at), o := e.source_order))
                   FILTER (WHERE epoch_ms(e.published_at)
                               <= era2.kickoff_ms - {SLICE_1H_MS}) AS q1h,
               arg_max(struct_pack(v1 := e.odds_home, v2 := e.odds_draw,
                                   v3 := e.odds_away, t := epoch_ms(e.published_at)),
                       struct_pack(t := epoch_ms(e.published_at), o := e.source_order))
                   FILTER (WHERE epoch_ms(e.published_at)
                               <= era2.kickoff_ms - {SLICE_24H_MS}) AS q24h,
               arg_max(struct_pack(v1 := e.line, v2 := e.home_water,
                                   v3 := e.away_water, t := epoch_ms(e.published_at)),
                       struct_pack(t := epoch_ms(e.published_at), o := e.source_order))
                   FILTER (WHERE epoch_ms(e.published_at)
                               <= era2.kickoff_ms - {SLICE_1H_MS}) AS q1h_ah,
               arg_max(struct_pack(v1 := e.line, v2 := e.home_water,
                                   v3 := e.away_water, t := epoch_ms(e.published_at)),
                       struct_pack(t := epoch_ms(e.published_at), o := e.source_order))
                   FILTER (WHERE epoch_ms(e.published_at)
                               <= era2.kickoff_ms - {SLICE_24H_MS}) AS q24h_ah
        FROM odds_change_event e
        JOIN era2 ON era2.sid = e.sid
        WHERE (e.market = '1x2' AND e.bookmaker_id = '{E1X2_ANCHOR}')
           OR (e.market = 'ah' AND e.bookmaker_id = '{AH_ANCHOR}')
        GROUP BY e.sid, e.market
        """  # noqa: S608 锚 id/era 界/窗常量=模块常量
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for sid, market, q1h, q24h, q1h_ah, q24h_ah in rows:
        entry = out.setdefault(str(sid), {})
        pits = [q["t"] for q in (q1h, q24h, q1h_ah, q24h_ah) if q is not None]
        if pits:
            entry["_pit"] = max(int(p) for p in pits if p is not None)
        if market == "1x2":
            pairs = (("q1h1x2", q1h), ("q24h1x2", q24h))
        else:
            pairs = (("q1hah", q1h_ah), ("q24hah", q24h_ah))
        for prefix, q in pairs:
            if q is None:
                continue
            if market == "1x2":
                entry[f"{prefix}_h"] = q["v1"]
                entry[f"{prefix}_d"] = q["v2"]
                entry[f"{prefix}_a"] = q["v3"]
            else:
                entry[f"{prefix}_line"] = q["v1"]
                entry[f"{prefix}_home_water"] = q["v2"]
                entry[f"{prefix}_away_water"] = q["v3"]
    return out


def _market_family(
    con: DuckCon,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """
    AH/OU 收盘族（market_quote 盘1 行，全时代）。

    多书中位数终盘线/水、初→终漂移、分歧度（终盘线样本标准差）、cid8
    亚盘锚行值（AH 族独有）。
    """
    rows = con.execute(
        f"""
        SELECT sid, market,
               count(DISTINCT cid) AS n_books,
               median(close_line) AS close_line_med,
               stddev_samp(close_line) AS close_dispersion,
               median(open_line) AS open_line_med,
               median(close_home_water) AS close_home_water_med,
               median(close_away_water) AS close_away_water_med,
               any_value(struct_pack(ol := open_line, cl := close_line,
                                     hw := close_home_water,
                                     aw := close_away_water))
                   FILTER (WHERE cid = '{AH_ANCHOR_CID}') AS anchor
        FROM market_quote
        WHERE multi = '盘1' AND close_line IS NOT NULL
        GROUP BY sid, market
        """  # noqa: S608 cid 锚=模块常量
    ).fetchall()
    ah: dict[str, dict[str, Any]] = {}
    ou: dict[str, dict[str, Any]] = {}
    for sid, market, n_books, close_med, disp, open_med, hw_med, aw_med, anchor in rows:
        base: dict[str, Any] = {
            "n_books": int(n_books),
            "open_line_med": open_med,
            "close_line_med": close_med,
            "line_drift": (
                close_med - open_med
                if close_med is not None and open_med is not None
                else None
            ),
            "close_dispersion": disp,
        }
        if market == MARKET_AH:
            base["close_home_water_med"] = hw_med
            base["close_away_water_med"] = aw_med
            if anchor is not None:
                base["anchor_open_line"] = anchor["ol"]
                base["anchor_close_line"] = anchor["cl"]
                base["anchor_close_home_water"] = anchor["hw"]
                base["anchor_close_away_water"] = anchor["aw"]
                if anchor["cl"] is not None and anchor["ol"] is not None:
                    base["anchor_drift"] = anchor["cl"] - anchor["ol"]
            ah[str(sid)] = base
        else:
            base["close_over_water_med"] = hw_med
            base["close_under_water_med"] = aw_med
            ou[str(sid)] = base
    return ah, ou


def _phase_counts(con: DuckCon) -> dict[str, dict[str, int]]:
    """
    多庄页矩阵变价事件的相位计数（early/pre 盘前两态、inplay 场内）。

    内层键 = 最终列名（{market}_n_{phase}），装配侧直接 merge。不经 CTE
    别名（表名静态归属审只认登记表，JOIN fixture_universe 直接内联）。
    """
    rows = con.execute(
        """
        SELECT e.sid, e.market,
               count(*) FILTER (WHERE e.status = 'early'
                                  AND epoch_ms(e.published_at)
                                      < epoch_ms(fu.kickoff
                                                 AT TIME ZONE 'Asia/Shanghai')),
               count(*) FILTER (WHERE e.status = 'pre'
                                  AND epoch_ms(e.published_at)
                                      < epoch_ms(fu.kickoff
                                                 AT TIME ZONE 'Asia/Shanghai')),
               count(*) FILTER (WHERE e.status = 'inplay')
        FROM odds_change_event e
        JOIN fixture_universe fu ON fu.sid = e.sid AND fu.kickoff IS NOT NULL
        WHERE e.market IN ('ah', 'ou') AND e.status IN ('early', 'pre', 'inplay')
        GROUP BY e.sid, e.market
        """
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for sid, market, n_early, n_pre, n_inplay in rows:
        entry = out.setdefault(str(sid), {})
        mkt = str(market)
        entry[f"{mkt}_n_early"] = int(n_early)
        entry[f"{mkt}_n_pre"] = int(n_pre)
        entry[f"{mkt}_n_inplay"] = int(n_inplay)
    return out


def _elo_rows(con: DuckCon) -> dict[str, tuple[float, float]]:
    rows = con.execute(
        "SELECT sid, elo_home_pre, elo_away_pre FROM elo_self"
    ).fetchall()
    return {str(r[0]): (float(r[1]), float(r[2])) for r in rows if r[1] is not None}


def _srct_xg_rows(con: DuckCon) -> dict[str, tuple[float, float]]:
    """源T stats xG（近代源；sid 直连零映射风险）。"""
    rows = con.execute(
        """
        SELECT sid, xg_home, xg_away FROM xg_observation
        WHERE has_xg AND xg_home IS NOT NULL AND xg_away IS NOT NULL
        """
    ).fetchall()
    return {str(r[0]): (float(r[1]), float(r[2])) for r in rows}


# fdhist 配对取回列（odds 族 19 列 + 标签侧技统；配对经 match_fixtures v3）
_HIST_COLUMNS = (
    "psc_home",
    "psc_draw",
    "psc_away",
    "psh_home",
    "psh_draw",
    "psh_away",
    "avgc_home",
    "avgc_draw",
    "avgc_away",
    "avg_ou_over",
    "avg_ou_under",
    "avgc_ou_over",
    "avgc_ou_under",
    "ah_line",
    "avg_ah_home",
    "avg_ah_away",
    "ahc_line",
    "avgc_ah_home",
    "avgc_ah_away",
    "hthg",
    "htag",
    "shots_home",
    "shots_away",
    "shots_on_target_home",
    "shots_on_target_away",
    "corners_home",
    "corners_away",
    "fouls_home",
    "fouls_away",
    "yellow_home",
    "yellow_away",
    "red_home",
    "red_away",
)
_HIST_EURO_N = 9  # psc/psh/avgc 欧赔三族（era1 正典；schema 同名）
_HIST_ODDS_N = 19  # 至 ou/ah 均值族末——成熟度门内整族


def _fetch_fdhists(con: DuckCon) -> list[tuple[HistRow, dict[str, Any]]]:
    """
    运行面 hist_matches（11 重叠联赛、语料窗起、定序）。

    返回 (HistRow, 扩展列字典)——扩展列随行解包（duckdb 行=tuple）。
    """
    codes = ",".join(f"'{code}'" for code in LEAGUE_TO_FD.values())
    columns = ", ".join(
        (
            "competition",
            "match_date",
            "home_team",
            "away_team",
            "fthg",
            "ftag",
            *_HIST_COLUMNS,
        )
    )
    rows = con.execute(
        f"""
        SELECT {columns}
        FROM goalx.hist_matches
        WHERE competition IN ({codes}) AND match_date >= '2016-01-01'
          AND fthg IS NOT NULL AND ftag IS NOT NULL
        ORDER BY match_date, competition, home_team, away_team
        """  # noqa: S608 联赛码=模块常量；窗=语料窗起点
    ).fetchall()
    out: list[tuple[HistRow, dict[str, Any]]] = []
    for r in rows:
        hist = HistRow(
            league_key=str(r[0]),
            match_date=str(r[1])[:10],
            home=str(r[2]),
            away=str(r[3]),
            goals_home=int(r[4]),
            goals_away=int(r[5]),
            psc_home=r[6],
            psc_draw=r[7],
            psc_away=r[8],
        )
        out.append((hist, {c: r[6 + i] for i, c in enumerate(_HIST_COLUMNS)}))
    return out


def _fetch_understat(con: DuckCon) -> list[tuple[HistRow, tuple[float, float]]]:
    """Understat xG 主源行 → (名字空侧 HistRow, (xg_home, xg_away))。"""
    code_to_cn = {code: FD_TO_LEAGUE[fd] for fd, code in UNDERSTAT_LEAGUES.items()}
    codes = ",".join(f"'{code}'" for code in UNDERSTAT_LEAGUES.values())
    rows = con.execute(
        f"""
        SELECT league, strftime(CAST(datetime_utc AS TIMESTAMP), '%Y-%m-%d') AS d,
               goals_home, goals_away, xg_home, xg_away
        FROM goalx.understat_matches
        WHERE league IN ({codes}) AND CAST(season AS INTEGER) >= {UNDERSTAT_SEASON_MIN}
          AND xg_home IS NOT NULL AND xg_away IS NOT NULL
          AND goals_home IS NOT NULL AND goals_away IS NOT NULL
        ORDER BY league, d, goals_home, goals_away
        """  # noqa: S608 联盟码/赛季下界=模块常量
    ).fetchall()
    out: list[tuple[HistRow, tuple[float, float]]] = []
    for league, day, gh, ga, xh, xa in rows:
        out.append(
            (
                HistRow(
                    league_key=code_to_cn.get(str(league), str(league)),
                    match_date=str(day),
                    home="",
                    away="",
                    goals_home=int(gh),
                    goals_away=int(ga),
                    psc_home=None,
                    psc_draw=None,
                    psc_away=None,
                ),
                (float(xh), float(xa)),
            )
        )
    return out


def _fd_feature_row(ext: dict[str, Any], *, mature: bool) -> dict[str, Any]:
    """
    配对 hist 扩展列 → gold 特征列。

    未成熟 = odds 族（psc/psh/avgc + ou/ah 均值族）整族置空；半场/技统=
    标签侧，不过成熟度门。列名：欧赔三族与 schema 同名，其余加 fd_ 前缀
    （与盘口市场族列名隔离）。
    """
    row: dict[str, Any] = {}
    for i, key in enumerate(_HIST_COLUMNS):
        if i < _HIST_EURO_N:  # psc/psh/avgc：era1 正典欧赔三族
            row[key] = ext[key] if mature else None
        elif i < _HIST_ODDS_N:  # ou/ah 均值族：fd_ 前缀 + 成熟度门
            row[f"fd_{key}"] = ext[key] if mature else None
        else:  # 半场/技统=标签侧，不过成熟度门
            row[f"fd_{key.replace('shots_on_target', 'sot')}"] = ext[key]
    return row


def _assert_no_lookahead(rows: list[tuple[int, dict[str, Any]]]) -> None:
    """PIT 纪律断言：pit_max_ms 非空行须严格早于开球，违规即构建失败。"""
    bad = [
        (row["sid"], row.get("pit_max_ms"), kickoff_ms)
        for kickoff_ms, row in rows
        if row.get("pit_max_ms") is not None and row["pit_max_ms"] >= kickoff_ms
    ]
    if bad:
        raise AssertionError(
            "gold PIT violation（特征输入时间戳 ≥ 开球）: "
            + "; ".join(f"sid={sid} pit={pit} kickoff={k}" for sid, pit, k in bad[:5])
        )


def _input_digest(store: CorpusStore, provider: str, dataset: str) -> str:
    """数据集 parquet 摘要（同 gate⑤ 口径；_meta 钉输入用）。"""
    sha = hashlib.sha256()
    root = store.silver_path(provider, dataset)
    for part in sorted(root.glob("**/data.parquet")) if root.exists() else []:
        sha.update(part.relative_to(root).as_posix().encode())
        sha.update(part.read_bytes())
    return sha.hexdigest()[:16]


_INPUT_DATASETS: tuple[tuple[str, str], ...] = (
    ("srct", "fixture_universe"),
    ("srct", "market_quote"),
    ("srct", "odds_change_event"),
    ("srct", "xg_observation"),
    ("elo", "elo_self"),
)


def _fd_pairing(
    fixture_rows: list[FixtureRow],
    fdhists: list[tuple[HistRow, dict[str, Any]]],
    exclusions: list[Any],
    maturity: date,
) -> tuple[dict[str, dict[str, Any]], set[str], int, int]:
    """
    Fdhist 配对（v3 名字身份轮；输入定序=SQL ORDER BY，交错语义无忧）。

    命中排除表 → 标记 fixture 且不消费该行任何特征（行政判赛：比分是
    真的、盘面从未真正交易）。返回 (特征列 by sid, admin 标记 sid 集,
    配对数, 未成熟数)。
    """
    ext_by_id = {id(h): ext for h, ext in fdhists}
    matched = match_fixtures(fixture_rows, [h for h, _ in fdhists])
    fd_by_sid: dict[str, dict[str, Any]] = {}
    admin_sids: set[str] = set()
    immature = 0
    for fixture, hist in matched.pairs:
        if is_admin_excluded(
            hist.league_key, hist.home, hist.away, hist.match_date, exclusions
        ):
            admin_sids.add(fixture.sid)
            continue
        mature = psc_mature(hist.match_date, today=maturity)
        fd_by_sid[fixture.sid] = _fd_feature_row(ext_by_id[id(hist)], mature=mature)
        immature += 0 if mature else 1
    return fd_by_sid, admin_sids, len(fd_by_sid), immature


def _understat_pairing(
    fixture_rows: list[FixtureRow],
    under_rows: list[tuple[HistRow, tuple[float, float]]],
) -> dict[str, tuple[float, float]]:
    """Understat 主源配对（名字空侧；歧义不硬配）→ sid → (xg_home, xg_away)。"""
    if not under_rows:
        return {}
    under_by_id = {id(h): xg for h, xg in under_rows}
    matched = match_fixtures(fixture_rows, [h for h, _ in under_rows])
    return {fixture.sid: under_by_id[id(hist)] for fixture, hist in matched.pairs}


@dataclass(frozen=True)
class _Sources:
    """装配所需的全部特征源（一次取数，逐场查表）。"""

    fd_by_sid: dict[str, dict[str, Any]]
    admin_sids: set[str]
    elo: dict[str, tuple[float, float]]
    understat_by_sid: dict[str, tuple[float, float]]
    srct_xg: dict[str, tuple[float, float]]
    close1x2: dict[str, dict[str, Any]]
    slices: dict[str, dict[str, Any]]
    ah_family: dict[str, dict[str, Any]]
    ou_family: dict[str, dict[str, Any]]
    phases: dict[str, dict[str, int]]


def _attach_trajectory(row: dict[str, Any], src: _Sources) -> int | None:
    """
    era2 轨迹族装载：收盘 + 1h/24h 切片。

    返回该行 PIT 类输入最大时间戳（无则 None）。slices 条目的 _pit 在此
    弹出（不落盘）。
    """
    sid = row["sid"]
    pit: int | None = None
    if sid in src.close1x2:
        entry = src.close1x2[sid]
        row.update({k: v for k, v in entry.items() if k != "pit_ms"})
        pit = entry.get("pit_ms")
    slice_entry = src.slices.get(sid)
    if slice_entry is not None:
        slice_pit = slice_entry.pop("_pit", None)
        row.update(slice_entry)
        if slice_pit is not None:
            pit = slice_pit if pit is None else max(pit, slice_pit)
    return pit


def _assemble_row(f: dict[str, Any], src: _Sources) -> dict[str, Any]:
    """一场 → gold 特征行（各族平铺合并；era 决定轨迹族是否装载）。"""
    sid = f["sid"]
    era = ERA_TRAJECTORY if f["season"] >= ERA_TRAJECTORY_SEASON else ERA_PSC_PROXY
    row: dict[str, Any] = {
        "sid": sid,
        "league": f["league"],
        "kickoff": f["kickoff"],
        "season": f["season"],
        "home": f["home"],
        "away": f["away"],
        "home_goals": f["home_goals"],
        "away_goals": f["away_goals"],
        "stage": f["stage"],
        "era": era,
        "admin_excluded": sid in src.admin_sids,
        "version": GOLD_VERSION,
    }
    if sid in src.elo:
        row["elo_home_pre"], row["elo_away_pre"] = src.elo[sid]
    if sid in src.understat_by_sid:  # 主源优先，一行一源（严禁混源）
        row["xg_home"], row["xg_away"] = src.understat_by_sid[sid]
        row["xg_source"] = "understat"
    elif sid in src.srct_xg:
        row["xg_home"], row["xg_away"] = src.srct_xg[sid]
        row["xg_source"] = "srct"
    row.update(src.fd_by_sid.get(sid, {}))
    row["pit_max_ms"] = _attach_trajectory(row, src) if era == ERA_TRAJECTORY else None
    if sid in src.ah_family:
        row.update({f"ah_{k}": v for k, v in src.ah_family[sid].items()})
    if sid in src.ou_family:
        row.update({f"ou_{k}": v for k, v in src.ou_family[sid].items()})
    row.update(src.phases.get(sid, {}))
    return row


def _tally(rows: list[dict[str, Any]], report: GoldBuildReport) -> None:
    """装配后从行集派生报告计数（era/覆盖面；不在装配循环里散加）。"""
    report.rows = len(rows)
    report.era_psc_proxy_rows = sum(1 for r in rows if r["era"] == ERA_PSC_PROXY)
    report.era_trajectory_rows = sum(1 for r in rows if r["era"] == ERA_TRAJECTORY)
    report.elo_rows = sum(1 for r in rows if r.get("elo_home_pre") is not None)
    report.xg_understat = sum(1 for r in rows if r.get("xg_source") == "understat")
    report.xg_srct = sum(1 for r in rows if r.get("xg_source") == "srct")
    report.close1x2_rows = sum(1 for r in rows if r.get("close1x2_books"))
    report.market_ah_rows = sum(1 for r in rows if r.get("ah_n_books"))
    report.market_ou_rows = sum(1 for r in rows if r.get("ou_n_books"))
    report.pit_rows = sum(1 for r in rows if r.get("pit_max_ms") is not None)


def build_match_features(
    store: CorpusStore,
    face: sqlite3.Connection,
    duck_con: DuckCon,
    *,
    today: date | None = None,
) -> GoldBuildReport:
    """
    全量重算并物化 gold match_features（幂等；确定性排序 → 字节级一致）。

    输入面：silver 视图（fixture_universe/market_quote/odds_change_event/
    xg_observation/elo_self）+ 运行面只读（hist_matches/understat_matches/
    admin_match_exclusions——缺表经 hygiene fail-closed 抛错）。
    """
    maturity = today if today is not None else date.today()
    report = GoldBuildReport(
        built_at=utc_now_iso(), maturity_today=maturity.isoformat()
    )
    exclusions = admin_exclusions(face)
    try:
        fixtures = _fetch_fixtures(duck_con)
    except duckdb.CatalogException as exc:
        # 空语料（srct-silver 零 parquet → duckdb 桥不建视图）= 合法初生态，
        # 降级报告；其余输入视图缺席不降级——fail-loud（先跑齐 silver 命令）
        report.degraded = f"fixture_universe view missing: {exc}"
        write_dataset_meta(
            store.gold_path(GOLD_PROVIDER, GOLD_DATASET),
            {"gold_version": GOLD_VERSION, "degraded": report.degraded},
        )
        return report

    fixture_rows = [
        FixtureRow(
            sid=f["sid"],
            league=f["league"],
            kickoff_day=str(f["kickoff"])[:10],
            goals_home=int(f["home_goals"]),
            goals_away=int(f["away_goals"]),
            home=f["home"],
            away=f["away"],
            season=f["season"],
        )
        for f in fixtures
    ]
    fd_by_sid, admin_sids, paired, immature = _fd_pairing(
        fixture_rows,
        _fetch_fdhists(duck_con),
        exclusions,
        maturity,
    )
    ah_family, ou_family = _market_family(duck_con)
    sources = _Sources(
        fd_by_sid=fd_by_sid,
        admin_sids=admin_sids,
        elo=_elo_rows(duck_con),
        understat_by_sid=_understat_pairing(fixture_rows, _fetch_understat(duck_con)),
        srct_xg=_srct_xg_rows(duck_con),
        close1x2=_trajectory_close(duck_con),
        slices=_anchor_slices(duck_con),
        ah_family=ah_family,
        ou_family=ou_family,
        phases=_phase_counts(duck_con),
    )
    rows = [_assemble_row(f, sources) for f in fixtures]
    report.fd_paired = paired
    report.fd_immature = immature
    report.admin_flagged = len(admin_sids)
    _tally(rows, report)
    _assert_no_lookahead([(beijing_ms(r["kickoff"]), r) for r in rows])

    rows.sort(key=lambda r: (r["kickoff"], r["sid"]))
    root = store.gold_path(GOLD_PROVIDER, GOLD_DATASET)
    write_partition(root / "all", rows, _SCHEMA)
    report.input_digests = {
        f"{p}/{d}": _input_digest(store, p, d) for p, d in _INPUT_DATASETS
    }
    write_dataset_meta(
        root,
        {
            "gold_version": GOLD_VERSION,
            "built_at": report.built_at,
            "rows": report.rows,
            "era": {
                ERA_PSC_PROXY: report.era_psc_proxy_rows,
                ERA_TRAJECTORY: report.era_trajectory_rows,
            },
            "maturity_today": report.maturity_today,
            "input_digests": report.input_digests,
            "partitioning": "single",
        },
    )
    return report
