"""
auto-v2 探针阵列引擎 + 每注 CLV 回填（edge-program-20261107 票 01）。

纸面账户从「EV 集中注」重构为「带来源标签的分布式探针阵列」（prereg
修订⑤）：每注带 edge_source 标签，分来源结算 P&L 与 CLV——哪个方向真
有边际一目了然。auto-v2 接管锚拍落注槽位（auto-v1 冻结版代码与其历史
账面保留不改写，报告分列）；台账 auto_paper_runs 沿用（换栈日=分界，
v1/v2 不共享同日同锚行）。

- 校准探针（calibration_probe，常亮）：claimed-EV 注——每场最优 EV
  （修订①去相关）top-3、全四玩法（had 面使每注 CLV 有覆盖）、平注
  ¥2/注=¥6/晚。平注非 quarter-Kelly：探针要等权测量 EV 实现差，注码
  分层会污染 realized ROI vs 决策 EV 的配对。
- 陈盘/滚存/价格带探针（stale_line/rollover/price_band，各 ¥2）：
  随扫描器发现晋升（edge_findings status=promoted，人裁）逐个点亮；
  熄灯期 stats 记 `probe_<name>=0` 零注留痕，点亮=改 params 升版本。
- 池面：既有三玩法期次一票随本引擎落（source=auto-v2 分账），受
  ¥20/晚测量帽约束（2026-11-08 人裁 A 案，纯测量定位）。
- CLV 回填（结算后，daily-wrap 挂缝）：全腿 had 的已结算 auto 纸面注
  ——源T Pinnacle 收盘三向（as-of 开球，published_at 资格线）Shin 去水
  得每腿收盘概率，票级 CLV=Π收盘概率 − Π入场隐含（与 evaluation/clv.py
  概率域同法；srct 锚=1x2，非 had 腿/无锚诚实 NULL 不倒填）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb

from goalx_backend import odds_math as om
from goalx_backend.betting import auto_strategy as au
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.data.quote_evidence import QuoteReads
from goalx_backend.db import atomic
from goalx_backend.markets import SELECTIONS
from goalx_backend.models import (
    EDGE_SOURCE_CALIBRATION_PROBE,
    EDGE_SOURCE_PRICE_BAND,
    EDGE_SOURCE_ROLLOVER,
    EDGE_SOURCE_STALE_LINE,
)

type duckdb_con_t = duckdb.DuckDBPyConnection | None

AUTO_V2 = "auto-v2"
CLV_SCAN_LIMIT = 500  # 每夜回填扫描注数上限（新结算优先；shortcut 注记见 backfill）


@dataclass(frozen=True)
class ProbeSpec:
    """一个探针的部署参数：来源标签 + 单注金额 + 是否点亮。"""

    name: str
    stake: float
    enabled: bool = False


@dataclass(frozen=True)
class AutoV2Params:
    """
    auto-v2 探针口径（prereg 修订⑤冻结）。

    校准 ¥6/晚=平注 ¥2×top-3；三条件探针各 ¥2 熄灯待扫描器晋升；
    池面 ¥20/晚测量帽。可购判定与 auto-v1 同口径（sale_buffer+
    kickoff_guard 近似，语义见 AutoV1Params docstring）。改参数=升版本
    （prereg C 节纪律）。
    """

    single_markets: tuple[str, ...] = ("had", "hhad", "crs", "ttg")
    parlay_markets: tuple[str, ...] = ()
    top_singles: int = 3
    # had 保底槽：claimed-EV 纯 EV 排名几乎恒被 crs/ttg 长票占据（十年
    # 逆向选择实证），而源T 收盘锚只覆盖 had——无 had 单关则每注 CLV
    # 仪器断流（票 01 问题陈述：无每注 CLV 则验证轨缺最高样本效率的
    # 仪表）。开启=末槽让位给未入选项中最优正 EV had 单关。
    had_clv_slot: bool = True
    sale_buffer_minutes: int = 30
    kickoff_guard_minutes: int = 10
    stake_per_combination: float = 2.0
    pool_daily_cap: float = 20.0
    probes: tuple[ProbeSpec, ...] = (
        ProbeSpec(EDGE_SOURCE_CALIBRATION_PROBE, stake=2.0, enabled=True),
        ProbeSpec(EDGE_SOURCE_STALE_LINE, stake=2.0),
        ProbeSpec(EDGE_SOURCE_ROLLOVER, stake=2.0),
        ProbeSpec(EDGE_SOURCE_PRICE_BAND, stake=2.0),
    )


AUTO_V2_PARAMS = AutoV2Params()


def _with_had_slot(
    chosen: list[au.Pick], singles: list[au.Pick], params: AutoV2Params
) -> list[au.Pick]:
    """
    Had 保底槽（修订单见 AutoV2Params.had_clv_slot）。

    不满员→从未入选项的场补一注最优正 EV had（稀疏夜不降级既有 EV 信号，
    无可补则诚实零 had）；满员→末槽让位：被让位场解禁后取保留集外最优
    had，修订①每场至多一注不变。
    """
    if not params.had_clv_slot or any(p.market_code == "had" for p in chosen):
        return chosen
    had_picks = [pick for pick in singles if pick.market_code == "had"]
    taken = {pick.fixture_id for pick in chosen}
    if len(chosen) < params.top_singles:
        appended = au.select_singles(
            [pick for pick in had_picks if pick.fixture_id not in taken], params
        )
        return [*chosen, appended[0]] if appended else chosen
    retained = chosen[:-1]
    freed = {pick.fixture_id for pick in retained}
    had_best = au.select_singles(
        [pick for pick in had_picks if pick.fixture_id not in freed], params
    )
    return [*retained, had_best[0]] if had_best else chosen


def _run_calibration_probe(
    conn: sqlite3.Connection,
    *,
    anchor_label: str,
    business_date: str,
    run_at: datetime,
    spec: ProbeSpec,
    params: AutoV2Params,
    reads: QuoteReads | None,
) -> dict[str, int]:
    """校准探针一锚：claimed-EV top-3 平注落账（官方性同比对沿用 v1 通道）。"""
    anchor_at = au.anchor_datetime(anchor_label, run_at)
    singles, _parlay_pool, excluded = au.anchor_candidates(
        conn, anchor_at=anchor_at, params=params
    )
    run_iso = run_at.astimezone(UTC).isoformat()
    created = drift = rejected = replayed = 0
    for pick in _with_had_slot(au.select_singles(singles, params), singles, params):
        request_id = (
            f"{AUTO_V2}:{business_date}:{anchor_label}:"
            f"cal:{pick.fixture_id}:{pick.market_code}"
        )
        outcome = au.place_single(
            conn,
            pick,
            stake=spec.stake,
            request_id=request_id,
            run_iso=run_iso,
            reads=reads,
            strategy_version=AUTO_V2,
            edge_source=spec.name,
        )
        if outcome == "placed":
            created += 1
        elif outcome == "replayed":
            replayed += 1
        elif outcome == "drift":
            drift += 1
        else:
            rejected += 1
    stats = {
        "candidates": len(singles),
        "bets": created,
        "price_drift": drift,
        "gate_rejected": rejected,
        "replayed": replayed,
    }
    stats.update({f"excluded_{key}": value for key, value in excluded.items()})
    return stats


def _pool_stake_today(
    conn: sqlite3.Connection, source: str, business_date: str
) -> float:
    """某 source 当业务日已落池票组合金额（帽余量计算；日期下推 SQL）。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(c.stake), 0) AS stake
        FROM bet_slips s JOIN combinations c ON c.slip_id = s.id
        WHERE s.source = ?
          AND substr(datetime(s.placed_at, '+8 hours'), 1, 10) = ?
        """,
        (source, business_date),
    ).fetchone()
    return float(row["stake"]) if row is not None else 0.0


def run_auto_v2(
    conn: sqlite3.Connection,
    *,
    run_at: datetime,
    anchor_label: str,
    params: AutoV2Params = AUTO_V2_PARAMS,
    reads: QuoteReads | None = None,
) -> dict[str, int]:
    """
    一锚完整执行（task 入口）：逐探针落注/零注留痕 + 池票（帽内）+ 台账。

    熄灯探针在 stats 记 ``probe_<name>=0``（无信号日零注留痕）；点亮后
    该探针换自己的选票 runner 并记更细统计（升级=升版本）。
    """
    business_date = fx_store.beijing_business_date(run_at)
    stats: dict[str, int] = {}
    for spec in params.probes:
        if not spec.enabled:
            stats[f"probe_{spec.name}"] = 0
            continue
        if spec.name == EDGE_SOURCE_CALIBRATION_PROBE:
            stats.update(
                _run_calibration_probe(
                    conn,
                    anchor_label=anchor_label,
                    business_date=business_date,
                    run_at=run_at,
                    spec=spec,
                    params=params,
                    reads=reads,
                )
            )
        else:  # pragma: no cover - 点亮条件探针的 runner 属晋升票
            stats[f"probe_{spec.name}"] = 0
    budget = params.pool_daily_cap - _pool_stake_today(conn, AUTO_V2, business_date)
    stats.update(
        au.place_pool_slips(
            conn,
            run_at=run_at,
            params=params,
            source=AUTO_V2,
            stake_budget=budget,
        )
    )
    au.record_anchor_run(
        conn, business_date, anchor_label, stats=stats, strategy=AUTO_V2
    )
    return stats


# ---- 每注 CLV 回填（结算后只读派生；SQL 归 betting，ADR-0008） ----


def bet_clv(close_probs: list[float], locked_odds: list[float]) -> float:
    """票级 CLV（概率域）：Π收盘概率 − Π入场隐含（单腿即差值，逐腿对应）。"""
    joint_close = 1.0
    joint_implied = 1.0
    for prob in close_probs:
        joint_close *= prob
    for odds in locked_odds:
        joint_implied *= 1.0 / odds
    return joint_close - joint_implied


def _closing_triplet_by_fixture(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    fixture_id: int,
) -> tuple[float, float, float] | None:
    """一场的源T Pinnacle 收盘三向（物化链 sid 优先，两步兜底在桥内）。"""
    fixture = fx_store.get_fixture(conn, fixture_id)
    info = fx_store.fixture_team_info(conn, fixture_id)
    if fixture is None or info is None:
        return None
    return quote_evidence.srct_pinnacle_closing_triplet(
        duck_con,
        str(info["home_name"]),
        str(info["away_name"]),
        str(fixture["kickoff_utc"]),
        sid=mapping.srct_sid_for_fixture(conn, fixture_id),
    )


def backfill_bet_clv(
    conn: sqlite3.Connection, duck_con: duckdb_con_t
) -> dict[str, int]:
    """
    已结算 auto 纸面注的 CLV 回填（幂等：只补 clv IS NULL 行）。

    范围=mode=paper 且 strategy_version 非空（auto-v1/auto-v2 系统决策；
    人工纸面/回录注不属探针仪器面）。全腿 had 且选择码在三向闭集内才可
    算——源T 收盘锚是 1x2，非 had 腿/闭集外选择码记 skipped_non_had，
    无锚（语料缺席/CorpusScope 外/匹配不中）记 skipped_no_anchor，列保持
    NULL 待下次 wrap 重试（correctness F1：闭集外选择码按无锚降级不炸
    wrap——历史脏行不制造永久夜炸循环）。as-of 由 data 桥保证
    （published_at ≤ 开球）。

    shortcut: 每夜只扫最近 CLV_SCAN_LIMIT 注（新结算优先），无锚旧行
    不再重试；夜注量升破限或锚回补需求出现时，升级=无锚墓碑列。
    """
    stats = {"filled": 0, "skipped_non_had": 0, "skipped_no_anchor": 0}
    rows = conn.execute(
        """
        SELECT b.id AS bet_id, l.fixture_id, l.market_code, l.selection_code,
               l.locked_odds
        FROM bets b JOIN bet_legs l ON l.bet_id = b.id
        WHERE b.mode = 'paper' AND b.status IN ('won', 'lost', 'void')
          AND b.clv IS NULL AND b.strategy_version IS NOT NULL
        ORDER BY b.settled_at DESC, b.id DESC, l.id
        LIMIT ?
        """,
        (CLV_SCAN_LIMIT * 3,),
    ).fetchall()
    by_bet: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_bet.setdefault(int(row["bet_id"]), []).append(row)
    triplets: dict[int, tuple[float, float, float] | None] = {}
    with atomic(conn):
        for bet_id, legs in by_bet.items():
            if any(
                str(leg["market_code"]) != "had"
                or str(leg["selection_code"]) not in SELECTIONS
                for leg in legs
            ):
                stats["skipped_non_had"] += 1
                continue
            close_probs: list[float] = []
            for leg in legs:
                fixture_id = int(leg["fixture_id"])
                if fixture_id not in triplets:
                    triplets[fixture_id] = (
                        _closing_triplet_by_fixture(conn, duck_con, fixture_id)
                        if duck_con is not None
                        else None
                    )
                triplet = triplets[fixture_id]
                if triplet is None:
                    break
                probs = om.shin_implied(triplet)
                close_probs.append(probs[SELECTIONS.index(str(leg["selection_code"]))])
            if len(close_probs) != len(legs):
                stats["skipped_no_anchor"] += 1
                continue
            conn.execute(
                "UPDATE bets SET clv = ? WHERE id = ?",
                (
                    bet_clv(close_probs, [float(leg["locked_odds"]) for leg in legs]),
                    bet_id,
                ),
            )
            stats["filled"] += 1
    return stats


__all__ = [
    "AUTO_V2",
    "AUTO_V2_PARAMS",
    "AutoV2Params",
    "ProbeSpec",
    "backfill_bet_clv",
    "bet_clv",
    "run_auto_v2",
]
