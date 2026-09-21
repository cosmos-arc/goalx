"""
奖池域读写模型（票 43）：期次/对阵/分布快照 + 彩池口径概率与 EV。

本模块拥有 pool_periods / pool_states / public_shares / pool_matches /
pool_sync_runs 全部 SQL（ADR 0008：他包不走裸 SQL）。

彩池口径（研究 03 §4 + staking-plans §2.3 三修正，票 43 只落第一修正）：
- **抽水折算**：传统足彩返奖率 65%（64% 当期 + 1% 调节基金，规则口径）。
  parimutuel 估计派彩赔率 = 返奖率 / 公众份额 share_i（share 来自源B 人气
  分布，origin=estimated 的公众分布代理，非官方池份额）；
  EV_i = p_i × (0.65 / share_i) − 1。
- **price impact**（自己的注额推动赔率）：goalx 个人资金量级下可忽略
  （Isaacs 1953 / Kelly 1956 的竞速彩池结论），接口保留"按注额重算派彩"
  的可能性但 v1 不建模。
- **分彩风险**（中奖注数多时派彩被摊薄）：EV 的 d 是随机变量，保守做法用
  分位数而非均值（Hausch–Ziemba–Rubinstein 1981）；v1 用均值口径并在
  词典/页面上如实标注"未建模分彩风险"。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

CST = timezone(timedelta(hours=8))  # 传统足彩业务时区：北京时间

# 官方池码（胜/平/负）——与 selections 种子及 settlement 判定同口径
POOL_WDL_CODES: tuple[str, str, str] = ("3", "1", "0")
_WDL_TO_HAD = {"3": "h", "1": "d", "0": "a"}
_HAD_TO_WDL = {v: k for k, v in _WDL_TO_HAD.items()}
# 传统足彩规则返奖率（研究 03 §0：64% 当期 + 1% 调节基金）
POOL_RETURN_RATE = 0.65
# 冷门阈值：公众份额 < 25% 计冷选项（v2 判定/生成器口径，前端同值镜像；
# evaluation/pool_replay 复验同源引用——单一正典在数据域，api 层再分发）
COLD_SHARE_MAX = 0.25
# 池场次 ↔ 竞彩场次映射的开赛时间容差（同一场比赛两源时间口径差）
_MATCH_KICKOFF_TOLERANCE_SECONDS = 2 * 3600


@dataclass
class PoolMatchInput:
    """一期一场对阵（ingest 输入；欧赔为期次页"99 家平均欧指"当前态）。"""

    match_seq: int
    source_match_id: str | None
    league: str
    kickoff_utc: str
    home_team: str
    away_team: str
    euro_odds: tuple[float | None, float | None, float | None]


@dataclass
class PoolShareInput:
    """一期一场三向公众份额（estimated=第三方人气代理 / published=官方公布）。"""

    match_seq: int
    shares: dict[str, float]  # 官方池码 → 份额（0-1）
    votes: dict[str, int] | None = None  # 票数（可选，量级参考）


@dataclass
class PoolSyncStats:
    """一次彩池同步的统计。"""

    source: str = ""
    observed_at: str = ""
    period_nos: list[str] = field(default_factory=list)
    pages: int = 0
    matches: int = 0
    share_rows: int = 0
    missing_shares: int = 0
    parse_version: str = ""


# --- 写路径（ingest / AI 代采） ---


def upsert_pool_period(
    conn: sqlite3.Connection,
    market_code: str,
    period_no: str,
    sales_deadline: str | None,
) -> int:
    """按 (market, period_no) 幂等落期次；截止时间以最新同步为准刷新。"""
    conn.execute(
        """
        INSERT INTO pool_periods (market_code, period_no, sales_deadline)
        VALUES (?, ?, ?)
        ON CONFLICT (market_code, period_no)
        DO UPDATE SET sales_deadline = excluded.sales_deadline
        """,
        (market_code, period_no, sales_deadline),
    )
    row = conn.execute(
        "SELECT id FROM pool_periods WHERE market_code = ? AND period_no = ?",
        (market_code, period_no),
    ).fetchone()
    if row is None:  # pragma: no cover - INSERT 成功后必存在
        raise RuntimeError(f"pool_period upsert failed: {market_code}/{period_no}")
    return int(row["id"])


def replace_pool_matches(
    conn: sqlite3.Connection, pool_period_id: int, matches: list[PoolMatchInput]
) -> int:
    """一期对阵当前态整组刷新（场序唯一；欧赔为页面当前值，非时序）。"""
    conn.execute("DELETE FROM pool_matches WHERE pool_period_id = ?", (pool_period_id,))
    conn.executemany(
        """
        INSERT INTO pool_matches
        (pool_period_id, match_seq, source_match_id, league, kickoff_utc,
         home_team, away_team, euro_odds_h, euro_odds_d, euro_odds_a)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                pool_period_id,
                m.match_seq,
                m.source_match_id,
                m.league,
                m.kickoff_utc,
                m.home_team,
                m.away_team,
                *m.euro_odds,
            )
            for m in matches
        ],
    )
    return len(matches)


def insert_public_shares(
    conn: sqlite3.Connection,
    pool_period_id: int,
    rows: list[PoolShareInput],
    *,
    origin: str,
    source: str,
    captured_at: str,
) -> int:
    """追加一版三向份额快照（append-only 语义，读取取最新 captured_at）。"""
    params: list[tuple[object, ...]] = []
    for row in rows:
        for code in POOL_WDL_CODES:
            if code not in row.shares:
                continue
            meta = (
                json.dumps({"votes": row.votes}, ensure_ascii=False)
                if row.votes
                else None
            )
            params.append(
                (
                    pool_period_id,
                    row.match_seq,
                    code,
                    row.shares[code],
                    origin,
                    source,
                    captured_at,
                    meta,
                )
            )
    conn.executemany(
        """
        INSERT INTO public_shares
        (pool_period_id, match_seq, selection_code, share, origin, source,
         captured_at, meta)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        params,
    )
    return len(params)


def upsert_pool_state(
    conn: sqlite3.Connection,
    pool_period_id: int,
    *,
    sales_amount: float | None,
    rollover_in: float | None,
    prize_tiers: dict[str, object] | None,
    published_at: str | None,
    source: str,
) -> None:
    """
    写入/刷新一期资金状态（pool_states 为"最新状态"表，同值重放幂等）。

    用于 AI 代采（票 43 兜底层）：代理读官方公布销量/滚存 → 结构化 → 本入口，
    source 记 ``agent``；官方四源直接 GET 全不可得（见票 43 实证）。
    """
    existing = conn.execute(
        "SELECT sales_amount, rollover_in, prize_tiers, published_at"  # noqa: S608 常量拼接
        + " FROM pool_states WHERE pool_period_id = ?",
        (pool_period_id,),
    ).fetchone()
    tiers_json = json.dumps(prize_tiers, ensure_ascii=False) if prize_tiers else None
    if existing is None:
        conn.execute(
            """
            INSERT INTO pool_states
            (pool_period_id, sales_amount, rollover_in, prize_tiers, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (pool_period_id, sales_amount, rollover_in, tiers_json, published_at),
        )
        return
    # 同值幂等；新值覆盖（状态表语义：最新公布为准）
    same = (
        existing["sales_amount"] == sales_amount
        and existing["rollover_in"] == rollover_in
        and existing["prize_tiers"] == tiers_json
        and existing["published_at"] == published_at
    )
    if not same:
        conn.execute(
            """
            UPDATE pool_states
            SET sales_amount = ?, rollover_in = ?, prize_tiers = ?, published_at = ?
            WHERE pool_period_id = ?
            """,
            (sales_amount, rollover_in, tiers_json, published_at, pool_period_id),
        )


def record_pool_sync_run(conn: sqlite3.Connection, stats: PoolSyncStats) -> int:
    """写一次同步元信息行（append-only 触发器拦改删；最新行即状态）。"""
    cur = conn.execute(
        """
        INSERT INTO pool_sync_runs
        (source, observed_at, period_nos, pages, matches, share_rows,
         missing_shares, parse_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats.source,
            stats.observed_at or utc_now_iso(),
            json.dumps(stats.period_nos),
            stats.pages,
            stats.matches,
            stats.share_rows,
            stats.missing_shares,
            stats.parse_version,
            utc_now_iso(),
        ),
    )
    conn.commit()
    if not cur.lastrowid:
        raise RuntimeError("pool_sync_runs INSERT 未产生 rowid")
    return int(cur.lastrowid)


def latest_pool_sync_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次彩池同步元信息（无同步史返回 None）。"""
    return conn.execute(
        "SELECT * FROM pool_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def pool_period_id(
    conn: sqlite3.Connection, market_code: str, period_no: str
) -> int | None:
    """期号 → pool_period_id（无该期次返回 None）。"""
    row = conn.execute(
        "SELECT id FROM pool_periods WHERE market_code = ? AND period_no = ?",
        (market_code, period_no),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def pool_period_count(conn: sqlite3.Connection) -> int:
    """已采集期次总数（同步状态端点用）。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM pool_periods").fetchone()
    return int(row["n"]) if row is not None else 0


def pool_period_deadline(conn: sqlite3.Connection, pool_period_id: int) -> str | None:
    """一期销售截止时间（无则 None）。"""
    row = conn.execute(
        "SELECT sales_deadline FROM pool_periods WHERE id = ?", (pool_period_id,)
    ).fetchone()
    return (
        str(row["sales_deadline"])
        if row is not None and row["sales_deadline"]
        else None
    )


# --- 读路径（API 视图组装） ---


def list_pool_periods(
    conn: sqlite3.Connection, market_code: str = "ttt14"
) -> list[sqlite3.Row]:
    """期次列表（含场次数/最新份额快照时点/资金状态/开赛窗口）。"""
    return conn.execute(
        """
        SELECT p.id, p.period_no, p.sales_deadline,
               (SELECT COUNT(*) FROM pool_matches m WHERE m.pool_period_id = p.id)
                   AS match_count,
               (SELECT MAX(captured_at) FROM public_shares s
                 WHERE s.pool_period_id = p.id) AS shares_captured_at,
               st.sales_amount, st.rollover_in, st.published_at AS sales_published_at,
               (SELECT MIN(kickoff_utc) FROM pool_matches m
                 WHERE m.pool_period_id = p.id) AS first_kickoff,
               (SELECT MAX(kickoff_utc) FROM pool_matches m
                 WHERE m.pool_period_id = p.id) AS last_kickoff
        FROM pool_periods p
        LEFT JOIN pool_states st ON st.pool_period_id = p.id
        WHERE p.market_code = ?
        ORDER BY p.period_no DESC
        """,
        (market_code,),
    ).fetchall()


def pool_matches_for_period(
    conn: sqlite3.Connection, pool_period_id: int
) -> list[sqlite3.Row]:
    """一期对阵（按场序）。"""
    return conn.execute(
        """
        SELECT match_seq, source_match_id, league, kickoff_utc, home_team, away_team,
               euro_odds_h, euro_odds_d, euro_odds_a
        FROM pool_matches WHERE pool_period_id = ? ORDER BY match_seq
        """,
        (pool_period_id,),
    ).fetchall()


def latest_shares_for_period(
    conn: sqlite3.Connection, pool_period_id: int
) -> dict[int, dict[str, float]]:
    """每场最新一版估计份额（官方池码 → 份额）。"""
    rows = conn.execute(
        """
        SELECT s.match_seq, s.selection_code, s.share
        FROM public_shares s
        JOIN (
            SELECT match_seq, MAX(captured_at) AS latest
            FROM public_shares
            WHERE pool_period_id = ? AND origin = 'estimated'
            GROUP BY match_seq
        ) latest ON latest.match_seq = s.match_seq AND latest.latest = s.captured_at
        WHERE s.pool_period_id = ? AND s.origin = 'estimated'
        """,
        (pool_period_id, pool_period_id),
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for row in rows:
        out.setdefault(int(row["match_seq"]), {})[str(row["selection_code"])] = float(
            row["share"]
        )
    return out


def pool_state_for_period(
    conn: sqlite3.Connection, pool_period_id: int
) -> sqlite3.Row | None:
    """一期资金状态（无 AI 代采/官方数据时 None）。"""
    return conn.execute(
        "SELECT * FROM pool_states WHERE pool_period_id = ?", (pool_period_id,)
    ).fetchone()


def shares_captured_at(conn: sqlite3.Connection, pool_period_id: int) -> str | None:
    """一期最新份额快照的观测时点（无快照 None）。"""
    row = conn.execute(
        "SELECT MAX(captured_at) AS latest FROM public_shares WHERE pool_period_id = ?",
        (pool_period_id,),
    ).fetchone()
    return str(row["latest"]) if row is not None and row["latest"] else None


def parimutuel_odds(share: float) -> float:
    """
    估计派彩赔率（抽水折算）：返奖率 / 公众份额。

    share → 0 时赔率发散，调用方需用 :func:`parimutuel_ev` 的护栏。
    """
    return POOL_RETURN_RATE / share


def parimutuel_ev(prob: float, share: float) -> float | None:
    """
    彩池口径单场 EV = p × (返奖率/share) − 1；缺份额返回 None。

    护栏：share<=0（无分布数据）→ None（诚实降级，不伪造无穷赔率）。
    未建模：price impact（个人量级可忽略）与分彩风险（派彩是随机变量，
    v1 用均值口径——见模块 docstring 三修正说明）。
    """
    if share <= 0:
        return None
    return prob * parimutuel_odds(share) - 1.0


def devig_euro_odds(
    odds: tuple[float | None, float | None, float | None],
) -> tuple[float, float, float] | None:
    """期次页三向"99 家平均欧指"去水 → 概率（Shin，与欧共识同法）。"""
    home, draw, away = odds
    if home is None or draw is None or away is None:
        return None
    if home <= 0 or draw <= 0 or away <= 0:
        return None
    return cast("tuple[float, float, float]", om.shin_implied((home, draw, away)))


# 前缀匹配的最短长度护栏（过短的缩写名前缀会误命中他队）
_NAME_PREFIX_MIN_CHARS = 2


def _names_match(pool_name: str, fixture_name: str) -> bool:
    """源B 缩写名 vs 库内全名的宽松匹配（互为前缀，去空白）。"""
    a = pool_name.replace(" ", "")
    b = fixture_name.replace(" ", "")
    if a == b:
        return True
    return (_prefix(a, b)) or (_prefix(b, a))


def _prefix(short: str, long_name: str) -> bool:
    """判断 short 是否为 long_name 的足够长前缀。"""
    return len(short) >= _NAME_PREFIX_MIN_CHARS and long_name.startswith(short)


def match_fixture_id(
    conn: sqlite3.Connection,
    kickoff_utc: str,
    home_team: str,
    away_team: str,
) -> int | None:
    """
    池场次 → 竞彩 fixture_id（业务日窗口 + 主客名宽松匹配；无映射 None）。

    场次 SQL 归 fixtures 仓储（ADR 0008）：候选集用 fx_store 的业务日查询，
    匹配（时间容差 + 名字前缀）在纯 Python 层完成。
    """
    kickoff = datetime.fromisoformat(kickoff_utc)
    base = kickoff.astimezone(CST).date()
    candidates = fx_store.fixtures_for_business_dates(
        conn, [(base + timedelta(days=offset)).isoformat() for offset in (-1, 0, 1)]
    )
    for row in candidates:
        candidate_kickoff = datetime.fromisoformat(str(row["kickoff_utc"]))
        if abs((candidate_kickoff - kickoff).total_seconds()) > (
            _MATCH_KICKOFF_TOLERANCE_SECONDS
        ):
            continue
        if _names_match(home_team, str(row["home_team"])) and _names_match(
            away_team, str(row["away_team"])
        ):
            return int(row["id"])
    return None


def model_prob_for_fixture(
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> dict[str, float] | None:
    """
    映射成功时取 as_of 前最新 ML Forecast 的 had 边际（h/d/a）。

    与票 41 注级快照同读取口径（防时间泄漏：issued_at <= as_of）。
    """
    from goalx_backend.modelling.forecast import (  # noqa: PLC0415 局部导入避免环
        forecast_matrix_from_payload,
        latest_forecast_asof,
    )

    row = latest_forecast_asof(conn, fixture_id, as_of, track="ml")
    if row is None:
        return None
    payload = json.loads(str(row["payload"]))
    matrix = forecast_matrix_from_payload(payload)
    had = matrix.had()
    return {s: had[s] for s in SELECTIONS}
