"""
报价证据判定（票 35）：按指定 as-of 取证，返回 有效/未知/拒绝 + 原因。

这是「报价与验证之间的最小交接契约」的实现：验证任务（票 34）与页面
（票 36）只消费本模块的判定，服务器锁定时再次校验；不各自解读快照的
时间语义。

时间规则（docs/plans/trusted-paper-handoff.md）：
- 任何用于决策的本机观测必须不晚于该决策时点（observed_at <= as_of）；
- 新鲜度与两源时差工程初值各 5 分钟，可配置但不自动放宽；
- 源时间未知/无法证明旧观测时间的记录只用于观察，不进正式候选；
- 旧数据 captured_at 按源解释：odds_api* = 观测时间；sporttery = 源
  调盘时间（其 observed_at 不可证明 → 状态 unknown，不倒填资格）。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
from loguru import logger

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

DuckCon = duckdb.DuckDBPyConnection


# 快照行统一类型（odds-read-switch 票 02）：sqlite3.Row（legacy odds_api
# 快照）与 corpus 伪行 dict 同走 str 键取列，消费逻辑共用不分源
SnapshotRow = sqlite3.Row | dict[str, Any]


DEFAULT_FRESHNESS_SECONDS = 300.0
DEFAULT_PAIR_GAP_SECONDS = 300.0

VALID = "valid"
REJECTED = "rejected"
UNKNOWN = "unknown"

# --- 源T 欧赔读侧（odds-read-switch 票 02） ---

# corpus 共识书集上限：按 bookmaker 维度全局覆盖数取 top-N（克隆去重缓议，
# 记入票 02 歧义账）；sharp 主锚 cid177 永不因截断出局
SRCT_EU_BOOK_CAP = 12
SRCT_MARKET = "1x2"
# 源T 1x2d 百家行主锚（bookmaker 字典 space=1x2；sharp 锚活路径，票75
# 收盘锚同书）。原票 75 段内定义，票 02 上移至读侧常量区（消费在桥+锚）
SRCT_PINNACLE_BOOK = "srct:1x2:177"
# had 判定 EU 段新鲜度门（票 02 人裁决默认）：corpus 行 admitted = 本机
# 观测年龄 ≤60min，替代 odds_api 快照新鲜度语义；pair-gap（300s 两源时差
# 门）对 corpus 行不适用——srct 的 price-since（published_at）跨日不动，
# 时差门结构性误杀，观察新鲜度已由本门兜住
EU_AGE_MAX_SECONDS = 3600.0


def parse_ts(value: str) -> datetime:
    """ISO 时间串 → aware datetime（naive 按 UTC 解释）。"""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def seconds_between(later: str, earlier: str) -> float:
    """两 ISO 串的秒差（later − earlier；naive 按 UTC 解释）。"""
    return (parse_ts(later) - parse_ts(earlier)).total_seconds()


# 可比身份白名单（票 03）：漂移对账（betting.frozen）与账本侧主指标
# （data.delay_survival）共用——跨类相减=参考切换伪装漂移/伪装存活。
IDENTITY_COMPARABLE_CLASSES: tuple[str, ...] = ("anchor", "consensus")


def reference_identity_class(ref: str | None) -> str:
    """
    参考取法身份类：anchor=单主锚，consensus=欧共识，NULL=unknown，其余 other。

    report-audit-20261027 票 03 口径：anchor 覆盖 entry ``pinnacle`` 与
    源T 主锚两条馈线（asof/closing 标签）；跨类相减=参考切换伪装漂移，
    两端同身份才算可比。raw 身份串存列，本函数只给分类判定。
    """
    if ref is None:
        return "unknown"
    if ref.startswith("eu_consensus"):
        return "consensus"
    if ref == "pinnacle" or ref.startswith("srct"):
        return "anchor"
    return "other"


def effective_observed_at(row: SnapshotRow) -> str | None:
    """本机观测时间；旧行按源解释（odds_api*=captured_at，sporttery=未知）。"""
    observed = row["observed_at"]
    if observed:
        return str(observed)
    if str(row["source"]).startswith("odds_api:"):
        return str(row["captured_at"])
    return None


def effective_source_updated_at(row: SnapshotRow) -> str | None:
    """源自报更新时间；sporttery 的 captured_at 即调盘时间（源语义）。"""
    updated = row["source_updated_at"]
    if updated:
        return str(updated)
    if str(row["source"]) == "sporttery":
        return str(row["captured_at"])
    return None


@dataclass
class HadQuoteVerdict:
    """一个 fixture 在某 as-of 时点的 had 报价判定（交接契约的载荷）。"""

    fixture_id: int
    as_of: str
    kickoff_utc: str
    # 证据链走完且无降级原因才保持 valid；mark_unknown/reject 只降不升
    status: str = VALID  # valid | rejected | unknown
    reasons: list[str] = field(default_factory=list)
    sale_state: str | None = None
    single_eligible: bool | None = None
    jc_odds: dict[str, float] = field(default_factory=dict)
    jc_source_updated_at: str | None = None
    jc_age_seconds: float | None = None
    eu_books: int = 0
    eu_probabilities: dict[str, float] | None = None
    eu_fair_odds: dict[str, float] | None = None
    eu_source_updated_at: str | None = None
    pair_gap_seconds: float | None = None
    sources: list[str] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        """记录阻断原因并置为 rejected（只降不升）。"""
        self.status = REJECTED
        self.reasons.append(reason)

    def mark_unknown(self, reason: str) -> None:
        """记录证据不足原因；valid 降为 unknown，rejected 不动。"""
        if self.status == VALID:
            self.status = UNKNOWN
        self.reasons.append(reason)


def latest_per_selection(rows: list[SnapshotRow]) -> dict[str, SnapshotRow]:
    """按 captured_at 取每选项最新一行（并列时 id 大者胜）。"""
    latest: dict[str, SnapshotRow] = {}
    for row in rows:
        sel = str(row["selection_code"])
        current = latest.get(sel)
        key = (str(row["captured_at"]), int(row["id"]))
        if current is None or key >= (str(current["captured_at"]), int(current["id"])):
            latest[sel] = row
    return latest


def had_snapshot_rows(
    conn: sqlite3.Connection,
    fixture_id: int,
    *,
    market_code: str = "had",
    purpose: str | None = None,
) -> list[SnapshotRow]:
    """一场比赛某市场的欧赔快照行（odds_api 源，按 captured_at,id 稳定排序）。"""
    sql = """
        SELECT * FROM odds_snapshots
        WHERE fixture_id = ? AND market_code = ? AND source LIKE 'odds_api:%'
    """
    params: tuple[str | int, ...] = (fixture_id, market_code)
    if purpose is not None:
        sql += " AND purpose = ?"
        params = (*params, purpose)
    sql += " ORDER BY captured_at, id"
    return conn.execute(sql, params).fetchall()


def srct_snapshot_rows_asof(
    duck_con: DuckCon,
    sid: str,
    as_of: str,
    *,
    max_age_seconds: float | None = None,
    exclusive: bool = False,
) -> list[SnapshotRow]:
    """
    源T 逐书 as-of 最新三向（odds-read-switch 票 02 读侧桥）。

    逐书取 ``published_at`` 最新一行（并列按 observed_at 决胜），admission
    = ``observed_at`` ≤ as_of（重放语义：本机未看到的不进 as-of 决策）；
    ``max_age_seconds`` 同口径对 observed_at 施下界。任一价 NULL 的书剔除
    （三向不完整）。共识书集按 bookmaker 维度全局覆盖 top-N 截断
    （cid177 主锚豁免）。产出伪快照行（dict，键同 odds_snapshots 语义列：
    source/selection_code/odds/captured_at/observed_at/source_updated_at），
    时间统一 UTC iso 秒（与 as_of 字符串比较兼容）。corpus 异常降级空表
    并告警（EU 段空值，不炸调用方——corpus_anchor 同款降级哲学）。

    ponytail: sid 无索引、逐场单查百毫秒级——今日页 28 场 ≈ 秒级可接受；
    页面变慢后按 sid 批量聚合一次拿全。
    """
    cutoff = parse_ts(as_of)
    compare = "<" if exclusive else "<="
    age_clause = ""
    age_param: list[object] = []
    if max_age_seconds is not None:
        floor = cutoff - timedelta(seconds=max_age_seconds)
        age_clause = "AND observed_at >= ?"
        age_param = [floor]
    try:
        rows = duck_con.execute(
            f"""
            SELECT bookmaker_id, odds_home, odds_draw, odds_away,
                   published_at, observed_at
            FROM (
                SELECT bookmaker_id, odds_home, odds_draw, odds_away,
                       published_at, observed_at, source_order,
                       ROW_NUMBER() OVER (
                           PARTITION BY bookmaker_id
                           ORDER BY published_at DESC NULLS LAST,
                                    observed_at DESC NULLS LAST,
                                    source_order DESC NULLS LAST
                       ) AS rn
                FROM odds_change_event
                WHERE sid = ? AND market = ? AND observed_at {compare} ?
                      {age_clause}
            ) ranked
            WHERE rn = 1
            """,  # noqa: S608 compare/age_clause 为常量白名单插值（<,<=/固定子句）
            [sid, SRCT_MARKET, cutoff, *age_param],
        ).fetchall()
    except duckdb.Error as exc:
        logger.warning("srct eu rows degraded: {}", exc)
        return []
    by_book: dict[str, tuple[float, float, float, str, str]] = {}
    for book, home, draw, away, published, observed in rows:
        if home is None or draw is None or away is None or observed is None:
            continue
        pub_iso = (
            published.astimezone(UTC).isoformat(timespec="seconds")
            if published is not None
            else None
        )
        obs_iso = observed.astimezone(UTC).isoformat(timespec="seconds")
        by_book[str(book)] = (
            float(home),
            float(draw),
            float(away),
            pub_iso or obs_iso,
            obs_iso,
        )
    books = _cap_srct_books(duck_con, by_book)
    return _pseudo_rows(books)


def srct_snapshot_rows_bulk(
    duck_con: DuckCon,
    sids: list[str],
    as_of: str,
) -> dict[str, list[SnapshotRow]]:
    """
    Bulk 版 :func:`srct_snapshot_rows_asof`（odds-read-switch 票 05 批查）。

    一次 SQL 取多 sid 的逐书 as-of 最新三向（今日页 28 场×4 查 → 1 批查）。
    书集截断逐 sid 独立同规（top-N 覆盖、cid177 豁免、维度滞后降级
    保全量）；空 sid 集或 corpus 异常返回空 dict。
    """
    if not sids:
        return {}
    cutoff = parse_ts(as_of)
    try:
        rows = duck_con.execute(
            """
            SELECT sid, bookmaker_id, odds_home, odds_draw, odds_away,
                   published_at, observed_at
            FROM (
                SELECT sid, bookmaker_id, odds_home, odds_draw, odds_away,
                       published_at, observed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY sid, bookmaker_id
                           ORDER BY published_at DESC NULLS LAST,
                                    observed_at DESC NULLS LAST,
                                    source_order DESC NULLS LAST
                       ) AS rn
                FROM odds_change_event
                WHERE sid IN (SELECT unnest(?)) AND market = ?
                      AND observed_at <= ?
            ) ranked
            WHERE rn = 1
            """,
            [sids, SRCT_MARKET, cutoff],
        ).fetchall()
    except duckdb.Error as exc:
        logger.warning("srct eu bulk rows degraded: {}", exc)
        return {}
    by_sid: dict[str, dict[str, tuple[float, float, float, str, str]]] = {}
    for sid, book, home, draw, away, published, observed in rows:
        if home is None or draw is None or away is None or observed is None:
            continue
        pub_iso = (
            published.astimezone(UTC).isoformat(timespec="seconds")
            if published is not None
            else None
        )
        obs_iso = observed.astimezone(UTC).isoformat(timespec="seconds")
        by_sid.setdefault(str(sid), {})[str(book)] = (
            float(home),
            float(draw),
            float(away),
            pub_iso or obs_iso,
            obs_iso,
        )
    return {
        sid: _pseudo_rows(_cap_srct_books(duck_con, by_book))
        for sid, by_book in by_sid.items()
    }


def _pseudo_rows(
    books: dict[str, tuple[float, float, float, str, str]],
) -> list[SnapshotRow]:
    """逐书三向 → 伪快照行（单/批两桥共用，键序与排序规则一致）。"""
    pseudo: list[SnapshotRow] = []
    for seq, (book, (h, d, a, source_time, observed_iso)) in enumerate(
        sorted(books.items())
    ):
        for sel, price in zip(SELECTIONS, (h, d, a), strict=True):
            pseudo.append(
                {
                    "id": seq,
                    "source": book,
                    "selection_code": sel,
                    "odds": price,
                    "captured_at": source_time,
                    "observed_at": observed_iso,
                    "source_updated_at": source_time,
                }
            )
    return pseudo


# 维度覆盖缓存（票 05 性能）：08:10 全量重建前覆盖数不变，10min TTL
# 足够新鲜；进程内单线程（API 事件环/拍班子进程各自一份），无锁
_DIM_TTL_SECONDS = 600.0


@dataclass
class _DimCache:
    """进程内维度缓存（可变单槽：count 表 + 取数时刻）。"""

    counts: dict[str, int] = field(default_factory=dict)
    at: float = 0.0


_dim_cache = _DimCache()


def _dim_match_counts(duck_con: DuckCon) -> dict[str, int]:
    """Bookmaker 维度 book→覆盖数（TTL 缓存；查询异常空表=降级保全量）。"""
    now = time.monotonic()
    if _dim_cache.counts and now - _dim_cache.at < _DIM_TTL_SECONDS:
        return _dim_cache.counts
    try:
        rows = duck_con.execute(
            "SELECT bookmaker_id, match_count FROM bookmaker"
        ).fetchall()
    except duckdb.Error as exc:
        logger.warning("srct dim ranking degraded: {}", exc)
        return {}
    _dim_cache.counts = {str(b): int(c) for b, c in rows}
    _dim_cache.at = now
    return _dim_cache.counts


def _cap_srct_books(
    duck_con: DuckCon, by_book: dict[str, tuple[float, float, float, str, str]]
) -> dict[str, tuple[float, float, float, str, str]]:
    """书集截断：bookmaker 维度全局覆盖 top-N；cid177 主锚豁免不截。"""
    if len(by_book) <= SRCT_EU_BOOK_CAP:
        return by_book
    dim = _dim_match_counts(duck_con)
    if not dim:
        # 维度整体缺席/查询异常 → 覆盖排序无据，降级保全量（F3 空表塌缩
        # 防线）。部分缺席属常态——odds 流书数 > 维度全量覆盖面，缺者按
        # 覆盖 0 排尾部参与截断，不触发保全量
        logger.warning(
            "srct book cap: dim unavailable, keeping all {} books", len(by_book)
        )
        return by_book
    keep: set[str] = {SRCT_PINNACLE_BOOK} & set(by_book)
    ranked = sorted(by_book, key=lambda book: (-dim.get(book, 0), book))
    for book in ranked:
        if len(keep) >= SRCT_EU_BOOK_CAP:
            break
        keep.add(book)
    return {book: by_book[book] for book in sorted(keep)}


def _eu_rows_asof(  # noqa: PLR0913 行源接缝集随 as-of/源切换口径累加（同 books_complete_asof）
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    duck_con: DuckCon | None,
    sid: str | None = None,
    purpose: str | None = None,
    exclusive: bool = False,
    max_age_seconds: float | None = None,
) -> list[SnapshotRow]:
    """
    EU 行源统一分发（odds-read-switch 票 02）。

    ``duck_con``+``sid`` 双给 → corpus 桥（as-of 已过滤，**corpus 唯一
    真值，空即空不回退**——防历史评测口径混源）；sid 由调用方经
    ``mapping.srct_sid_for_fixture`` 解析后传入（票 75 先例：本模块不
    依赖 mapping，分层契约钉死）。缺任一 → legacy odds_snapshots（单测/
    演示/9-25 前历史行，行未过滤由调用方体过滤）。purpose 行（closing
    等 odds_api 专用语义）不走 corpus。
    """
    if duck_con is not None and sid is not None and purpose is None:
        return srct_snapshot_rows_asof(
            duck_con,
            sid,
            as_of,
            max_age_seconds=max_age_seconds,
            exclusive=exclusive,
        )
    return had_snapshot_rows(conn, fixture_id, purpose=purpose)


def books_complete_asof(  # noqa: PLR0913 票35 接缝集随 as-of/源切换口径累加（同 run_shift 先例）
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    market_code: str = "had",
    purpose: str | None = None,
    exclusive: bool = False,
    max_age_seconds: float | None = None,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> dict[str, dict[str, float]]:
    """
    as_of 时点可用的各 book 完整三向最新报价（本包统一的报价读取口径）。

    行源（odds-read-switch 票 02）：``duck_con`` 给定且有 srct 链 →
    corpus 源T 逐书 as-of；否则 legacy odds_snapshots（odds_api 历史/
    单测/演示口径）。资格按行判定：观测时间（旧行按源解释）须不晚于
    as_of（exclusive=True 时严格早于）；给 max_age_seconds 时还须不早于
    as_of − 该窗。每 book 按 captured_at 取各选项最新行，三向不完整的
    book 剔除。
    """
    source_rows: list[SnapshotRow] = (
        rows
        if rows is not None
        else _eu_rows_asof(
            conn,
            fixture_id,
            as_of,
            duck_con=duck_con,
            sid=sid,
            purpose=purpose,
            exclusive=exclusive,
            max_age_seconds=max_age_seconds,
        )
    )
    by_book: dict[str, list[SnapshotRow]] = {}
    for row in source_rows:
        observed = effective_observed_at(row)
        if observed is None:
            continue
        if (observed >= as_of) if exclusive else (observed > as_of):
            continue
        if (
            max_age_seconds is not None
            and seconds_between(as_of, observed) > max_age_seconds
        ):
            continue
        by_book.setdefault(str(row["source"]), []).append(row)
    books: dict[str, dict[str, float]] = {}
    for book, book_rows in by_book.items():
        latest = latest_per_selection(book_rows)
        if set(latest) != set(SELECTIONS):
            continue
        books[book] = {sel: float(r["odds"]) for sel, r in latest.items()}
    return books


def eu_consensus_asof(
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    market_code: str = "had",
    duck_con: DuckCon | None = None,
    sid: str | None = None,
) -> dict[str, float] | None:
    """as_of 前最新欧赔完整三向共识的 Shin 概率（评分基准，非成交口径）。"""
    books = books_complete_asof(
        conn, fixture_id, as_of, market_code=market_code, duck_con=duck_con, sid=sid
    )
    if not books:
        return None
    consensus = om.consensus_odds(
        [{b: books[b][s] for b in sorted(books)} for s in SELECTIONS]
    )
    if consensus is None:
        return None
    probs = om.shin_implied(consensus)
    return dict(zip(SELECTIONS, probs, strict=True))


def eu_book_odds(
    conn: sqlite3.Connection,
    fixture_id: int,
    *,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> dict[str, dict[str, float]]:
    """
    Latest per-book odds for a fixture: ``selection -> {book: odds}``.

    自 fixtures.py 迁入（odds-read-switch 票 02：欧赔读 SQL 归本包单一
    出处）。行源同 ``_eu_rows_asof``——corpus 有链走源T，否则 legacy
    ``odds_api:%``（as_of 取当下，不限年龄=「最新已知」口径不变）。
    """
    source_rows: list[SnapshotRow] = (
        rows
        if rows is not None
        else _eu_rows_asof(conn, fixture_id, utc_now_iso(), duck_con=duck_con, sid=sid)
    )
    latest: dict[str, dict[str, tuple[str, float]]] = {}
    for row in source_rows:
        sel = str(row["selection_code"])
        book = str(row["source"])
        latest.setdefault(sel, {})[book] = (str(row["captured_at"]), float(row["odds"]))
    return {
        sel: {book: captured[1] for book, captured in books.items()}
        for sel, books in latest.items()
    }


def eu_book_quotes(
    conn: sqlite3.Connection,
    fixture_id: int,
    *,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
) -> dict[str, dict[str, tuple[float, str]]]:
    """
    Latest per-book quotes with capture time: ``book -> {selection: (odds, at)}``.

    工作台 v2 票 02（研究页逐书赔率明细）；行源/迁入同 :func:`eu_book_odds`，
    ``(fixture, market, book, selection)`` 取最新一条。
    """
    rows = _eu_rows_asof(conn, fixture_id, utc_now_iso(), duck_con=duck_con, sid=sid)
    latest: dict[str, dict[str, tuple[float, str]]] = {}
    for row in rows:
        book = str(row["source"])
        latest.setdefault(book, {})[str(row["selection_code"])] = (
            float(row["odds"]),
            str(row["captured_at"]),
        )
    return latest


def market_rows_with_competition(
    conn: sqlite3.Connection, *, market_code: str = "had"
) -> list[sqlite3.Row]:
    """
    全量比赛的市场快照行（sporttery + odds_api），带联赛名。

    haircut 批量配对用；按 captured_at,id 稳定排序。
    """
    # business_date 列（odds-read-switch 票 05）：haircut 分代口径的切换键
    return conn.execute(
        """
        SELECT f.id AS fixture_id, c.name AS competition,
               s.id AS id, s.selection_code, s.source, s.odds, s.captured_at,
               s.observed_at, s.source_updated_at,
               (SELECT MAX(mc.business_date) FROM match_codes mc
                WHERE mc.fixture_id = f.id AND mc.kind = 'jingcai'
               ) AS business_date
        FROM odds_snapshots s
        JOIN fixtures f ON f.id = s.fixture_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE s.market_code = ?
          AND (s.source = 'sporttery' OR s.source LIKE 'odds_api:%')
        ORDER BY s.captured_at, s.id
        """,
        (market_code,),
    ).fetchall()


def adjudicate_had_quote(  # noqa: PLR0913 判定接缝集随 as-of/源切换/批量预取累加
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
    max_pair_gap_seconds: float = DEFAULT_PAIR_GAP_SECONDS,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> HadQuoteVerdict:
    """
    判定某时点 had 报价是否可作正式候选依据。

    status=valid 仅表示「报价证据链完整且新鲜」；single_eligible 单独
    返回——单关正式候选还须它为 True，未知即拒绝（票 35 验收 4）。
    """
    fixture = fx_store.get_fixture(conn, fixture_id)
    kickoff = str(fixture["kickoff_utc"]) if fixture else None
    verdict = HadQuoteVerdict(
        fixture_id=fixture_id, as_of=as_of, kickoff_utc=kickoff or ""
    )
    if fixture is None:
        verdict.reject("fixture_not_found")
        return verdict
    if parse_ts(as_of) >= parse_ts(kickoff or ""):
        verdict.reject("kickoff_passed")
        return verdict

    sale = fx_store.latest_sale_status_asof(conn, fixture_id, "had", as_of)
    if sale is None:
        verdict.mark_unknown("sale_status_unknown")
    else:
        verdict.sale_state = str(sale["sale_state"])
        if sale["sale_state"] == "stopped":
            verdict.reject("sale_stopped")
            return verdict
        if sale["sale_state"] == "not_on_sale":
            # 票 09：未开售≠停售≠未知——不可购同样短路，但归因独立
            verdict.reject("sale_not_on_sale")
            return verdict
        if sale["sale_state"] == "unknown":
            verdict.mark_unknown("sale_status_unknown_value")
        if sale["single_eligible"] is not None:
            verdict.single_eligible = bool(sale["single_eligible"])
        else:
            verdict.mark_unknown("single_eligibility_unknown")

    _adjudicate_jc(conn, verdict, as_of, freshness_seconds)
    if verdict.status == REJECTED:
        return verdict
    _adjudicate_eu(
        conn,
        verdict,
        as_of,
        max_pair_gap_seconds=max_pair_gap_seconds,
        duck_con=duck_con,
        sid=sid,
        rows=rows,
    )
    return verdict


def _adjudicate_jc(
    conn: sqlite3.Connection,
    verdict: HadQuoteVerdict,
    as_of: str,
    freshness_seconds: float,
) -> None:
    rows = conn.execute(
        """
        SELECT * FROM odds_snapshots
        WHERE fixture_id = ? AND market_code = 'had' AND source = 'sporttery'
        ORDER BY captured_at, id
        """,
        (verdict.fixture_id,),
    ).fetchall()
    if not rows:
        verdict.mark_unknown("jc_no_quote")
        return
    eligible = [r for r in rows if (t := effective_observed_at(r)) and t <= as_of]
    if not eligible:
        # 有报价但无法证明 as_of 前已观测（含旧行 observed_at 未知）
        verdict.mark_unknown("jc_observed_at_unknown")
        return
    latest = latest_per_selection(eligible)
    if set(latest) != set(SELECTIONS):
        verdict.reject("jc_three_way_incomplete")
        return
    for sel, row in latest.items():
        verdict.jc_odds[sel] = float(row["odds"])
    source_times = [
        t for t in (effective_source_updated_at(r) for r in latest.values()) if t
    ]
    if not source_times:
        verdict.mark_unknown("jc_source_time_unknown")
        return
    verdict.jc_source_updated_at = max(source_times)
    age = seconds_between(as_of, verdict.jc_source_updated_at)
    verdict.jc_age_seconds = age
    if age > freshness_seconds:
        verdict.reject("stale_source")
    verdict.sources.append("sporttery")


def _book_asof(
    book: str,
    book_rows: list[SnapshotRow],
    jc_source_updated_at: str | None,
    *,
    max_pair_gap_seconds: float,
) -> tuple[dict[str, float] | None, str | None, str]:
    """
    一个 book 的 as-of 校验：返回 ``(prices, source_time, note)``。

    prices 为 None 时 note 说明剔除原因（三向不完整/源时间未知/超配对窗）。
    """
    latest = latest_per_selection(book_rows)
    if set(latest) != set(SELECTIONS):
        return None, None, f"{book}:three_way_incomplete"
    source_times = [
        t for t in (effective_source_updated_at(r) for r in latest.values()) if t
    ]
    if not source_times:
        return None, None, f"{book}:source_time_unknown"
    book_time = max(source_times)
    if jc_source_updated_at is not None:
        gap = abs(seconds_between(book_time, jc_source_updated_at))
        if gap > max_pair_gap_seconds:
            return None, book_time, f"{book}:pair_gap_exceeded"
    return (
        {sel: float(r["odds"]) for sel, r in latest.items()},
        book_time,
        "",
    )


def _collect_valid_books(
    verdict: HadQuoteVerdict,
    eligible: list[SnapshotRow],
    *,
    max_pair_gap_seconds: float,
) -> dict[str, dict[str, float]]:
    """按 book 校验并把通过者写入 verdict 的 notes/时差/源时间字段。"""
    by_book: dict[str, list[SnapshotRow]] = {}
    for row in eligible:
        by_book.setdefault(str(row["source"]), []).append(row)
    valid_books: dict[str, dict[str, float]] = {}
    book_notes: list[str] = []
    for book, book_rows in sorted(by_book.items()):
        prices, book_time, note = _book_asof(
            book,
            book_rows,
            verdict.jc_source_updated_at,
            max_pair_gap_seconds=max_pair_gap_seconds,
        )
        if note:
            book_notes.append(note)
        if prices is None or book_time is None:
            continue
        if verdict.jc_source_updated_at is not None:
            gap = abs(seconds_between(book_time, verdict.jc_source_updated_at))
            if verdict.pair_gap_seconds is None or gap > verdict.pair_gap_seconds:
                verdict.pair_gap_seconds = gap
        valid_books[book] = prices
        if (
            verdict.eu_source_updated_at is None
            or book_time > verdict.eu_source_updated_at
        ):
            verdict.eu_source_updated_at = book_time
    verdict.reasons.extend(book_notes)
    return valid_books


def _adjudicate_eu(
    conn: sqlite3.Connection,
    verdict: HadQuoteVerdict,
    as_of: str,
    *,
    max_pair_gap_seconds: float,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> None:
    corpus = duck_con is not None and sid is not None
    source_rows: list[SnapshotRow] = (
        rows
        if rows is not None
        else _eu_rows_asof(
            conn,
            verdict.fixture_id,
            as_of,
            duck_con=duck_con,
            sid=sid,
            max_age_seconds=EU_AGE_MAX_SECONDS if corpus else None,
        )
    )
    if not source_rows:
        verdict.mark_unknown("eu_no_quote")
        return
    eligible = [
        r for r in source_rows if (t := effective_observed_at(r)) and t <= as_of
    ]
    if not eligible:
        verdict.mark_unknown("eu_observed_at_unknown")
        return
    valid_books = _collect_valid_books(
        verdict,
        eligible,
        # corpus 行 pair-gap 不适用（见 EU_AGE_MAX_SECONDS 注释）——
        # 300s 两源时差门是 odds_api 快照时代语义，对 30min 拍点结构性误杀
        max_pair_gap_seconds=float("inf") if corpus else max_pair_gap_seconds,
    )
    if not valid_books:
        verdict.mark_unknown("eu_no_valid_books")
        return
    consensus = om.consensus_odds(
        [{b: valid_books[b][s] for b in sorted(valid_books)} for s in SELECTIONS]
    )
    if consensus is None:  # 完整三向已校验，理论不可达
        raise RuntimeError("consensus incomplete after three-way validation")
    probs = om.shin_implied(consensus)
    verdict.eu_books = len(valid_books)
    verdict.eu_probabilities = {
        s: round(p, 6) for s, p in zip(SELECTIONS, probs, strict=True)
    }
    verdict.eu_fair_odds = {
        s: round(1.0 / p, 4) for s, p in zip(SELECTIONS, probs, strict=True)
    }
    verdict.sources.extend(sorted(valid_books))


# --- 票 48：陈盘信号（纯派生，零 schema——append-only 原料可重放） ---

# 主锚书（票 40 语义：单书 Shin）。与 evaluation/clv.PINNACLE_SOURCE 同值
# 复制——分层禁止 data 上行 import evaluation，改动须两处同步。
# odds-read-switch 票 02：活路径主锚 = 源T cid177（SRCT_PINNACLE_BOOK，
# 票 75 收盘锚同书）；odds_api 值保留给历史行/legacy 路径（9-25 摘除）。
SHARP_ANCHOR_SOURCE = "odds_api:pinnacle"
# 定格端参考新鲜度上界：p0 过旧会把竞彩已吸收的冻前移动混入漂移（夸大），
# 宁缺毋滥。12h 覆盖隔夜采集间隔；票 47 停售锚落地后临场窗口天然新鲜。
SHARP_FREEZE_MAX_AGE_SECONDS = 12 * 3600


@dataclass(frozen=True)
class StaleLineSignal:
    """
    一场比赛的陈盘信号（as-of 决策时点的只读派生视图）。

    分钟数 = 竞彩距上次调盘（sporttery captured_at = 源调盘时间）；
    drift = sharp 参考（pinnacle 主锚，缺则欧共识兜底，双端同法才可比）
    在"竞彩定格时刻 → as_of"区间内漂移最大的选项的概率变化（带符号，
    正=该向概率上行）。任一端无参考点 → drift=None（只报陈旧时长）。
    """

    as_of: str
    jc_last_move: str
    minutes_since_move: int
    drift: float | None = None
    drift_selection: str | None = None
    sharp_ref: str | None = None


def _probs_from_books(books: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """欧共识三向 Shin（books_complete_asof 的兜底参考口径）。"""
    if not books:
        return None
    consensus = om.consensus_odds(
        [{b: books[b][s] for b in sorted(books)} for s in SELECTIONS]
    )
    if consensus is None:
        return None
    return dict(zip(SELECTIONS, om.shin_implied(consensus), strict=True))


def sharp_probs_asof(  # noqa: PLR0913 锚接缝集随 as-of/源切换/批量预取累加
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    max_age_seconds: float | None = None,
    anchor_only: bool = False,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> tuple[dict[str, float], str] | None:
    """
    As_of 时点的 sharp 参考概率与取法身份（pinnacle 主锚优先）。

    身份串含共识书集（排序拼接）——双端按书集精确判等同法，避免同数
    不同书误判（评审修正）。anchor_only=True 只认 pinnacle 主锚（缺=
    None，不走共识兜底——同步报价账本的单锚口径，measure-07）。
    行源随 books_complete_asof（票 02）：corpus 路径主锚 = cid177，
    身份串用书 id（双端同源恒等）；legacy 路径身份 "pinnacle" 不变。
    """
    books = books_complete_asof(
        conn,
        fixture_id,
        as_of,
        max_age_seconds=max_age_seconds,
        duck_con=duck_con,
        sid=sid,
        rows=rows,
    )
    # 身份串契约：legacy="pinnacle"（持久化值不动）；srct=书 id（双端同源恒等）
    for anchor_id, identity in (
        (SHARP_ANCHOR_SOURCE, "pinnacle"),
        (SRCT_PINNACLE_BOOK, SRCT_PINNACLE_BOOK),
    ):
        anchor = books.get(anchor_id)
        if anchor is not None:
            probs = dict(
                zip(
                    SELECTIONS,
                    om.shin_implied(tuple(anchor[s] for s in SELECTIONS)),
                    strict=True,
                )
            )
            return probs, identity
    if anchor_only:
        return None
    consensus = _probs_from_books(books)
    if consensus is not None:
        return consensus, f"eu_consensus[{','.join(sorted(books))}]"
    return None


def stale_line_signal(
    conn: sqlite3.Connection,
    fixture_id: int,
    *,
    as_of: str,
    duck_con: DuckCon | None = None,
    sid: str | None = None,
    rows: list[SnapshotRow] | None = None,
) -> StaleLineSignal | None:
    """
    陈盘信号（票 48）：距竞彩上次调盘的时长 + sharp 参考自定格起的漂移。

    无竞彩 had 快照 → None（无信号可言）。纯读 odds_snapshots（SQL 归
    本包），同 as_of 重算恒同值（重放确定性，验收项）。sharp 端行源随
    ``duck_con`` 切 corpus（票 02）；双端同源同法，漂移可比性不破。
    """
    # observed_at 门槛（评审修正）：本机未看到的调盘不进 as-of 决策（重放
    # 语义）；旧行无 observed_at 时按 captured_at 解释（源调盘时间，票 35 口径）
    jc_rows = conn.execute(
        """
        SELECT captured_at FROM odds_snapshots
        WHERE fixture_id = ? AND market_code = 'had' AND source = 'sporttery'
          AND COALESCE(observed_at, captured_at) <= ?
        ORDER BY captured_at DESC, id DESC LIMIT 1
        """,
        (fixture_id, as_of),
    ).fetchall()
    if not jc_rows:
        return None
    jc_last_move = str(jc_rows[0]["captured_at"])
    signal = StaleLineSignal(
        as_of=as_of,
        jc_last_move=jc_last_move,
        minutes_since_move=max(0, int(seconds_between(as_of, jc_last_move) // 60)),
    )
    frozen = sharp_probs_asof(
        conn,
        fixture_id,
        jc_last_move,
        max_age_seconds=SHARP_FREEZE_MAX_AGE_SECONDS,
        duck_con=duck_con,
        sid=sid,
    )
    current = sharp_probs_asof(
        conn, fixture_id, as_of, duck_con=duck_con, sid=sid, rows=rows
    )
    if frozen is None or current is None or frozen[1] != current[1]:
        # 双端取法不一致（主锚中途出现/消失）→ 漂移不可比，只报时长
        return signal
    p0, p1 = frozen[0], current[0]
    selection = max(SELECTIONS, key=lambda s: abs(p1[s] - p0[s]))
    return replace(
        signal,
        drift=round(p1[selection] - p0[selection], 4),
        drift_selection=selection,
        sharp_ref=current[1],
    )


# --- 源T 主锚收盘证据（票 75 CLV 收盘锚接管；corpus.duckdb 只读面） ---

# SRCT_PINNACLE_BOOK 已上移至读侧常量区（票 02）


def srct_pinnacle_closing_triplet(
    con: duckdb.DuckDBPyConnection,
    home: str,
    away: str,
    kickoff_utc: str,
    *,
    sid: str | None = None,
) -> tuple[float, float, float] | None:
    """
    竞彩场次 → 源T 主锚盘前三向收盘（票 75 CLV 收盘锚接管的数据面）。

    场次匹配优先走物化链（票 77 source_match_links，``sid`` 直传即跳过
    匹配）；无链时兜底两步确定性键（禁自信合并，定则 1）：
    ① home/away 中文队名 + 北京日期 == fixture_universe；
    ② 兜底 kickoff 时刻精确（北京 naive）+ home 精确（解命名变体）；
    两步不中返回 None（CorpusScope 外场结构性无锚，调用方诚实 skip）。
    资格线 = published_at（源自报时点）≤ kickoff，取最新一笔。
    库/视图缺席（首夜 silver 落地前重建视图等）同样降级 None 并告警，
    不打断对账主流程（票 75 验收 5）。连接由调用方开合（编排层经
    corpus_duckdb.connect，ADR-0008 分层：本模块不依赖 ingest）。

    ponytail: odds_change_event 为 3,000 万行级 parquet 视图、sid 无索引，
    单查秒级内——日频对账×个位数腿可接受；腿数上量后按 sid 分区物化。
    """
    return srct_pinnacle_asof_triplet(
        con, home, away, kickoff_utc, as_of=kickoff_utc, sid=sid
    )


def srct_pinnacle_asof_triplet(
    con: duckdb.DuckDBPyConnection,
    home: str,
    away: str,
    kickoff_utc: str,
    *,
    as_of: str,
    sid: str | None = None,
) -> tuple[float, float, float] | None:
    """
    源T 主锚三向 as-of 报价（measure-07 账本参考面）：截止 as_of 的最新一笔。

    收盘=as_of 取 kickoff（srct_pinnacle_closing_triplet 同语义）；
    as-of 语义保证事后计算无前视。异常降级 None（同收盘版惯例）。
    """
    try:
        return _srct_pinnacle_asof(con, home, away, kickoff_utc, as_of, sid)
    except duckdb.Error as exc:
        logger.warning("srct as-of 锚查询降级: {}", exc)
        return None


def _srct_pinnacle_asof(
    con: duckdb.DuckDBPyConnection,
    home: str,
    away: str,
    kickoff_utc: str,
    as_of: str,
    sid: str | None,
) -> tuple[float, float, float] | None:
    """查询体：sid 解析（链直传/两步兜底）+ published_at ≤ as_of 最新一笔。"""
    beijing = fx_store.beijing_naive(kickoff_utc)
    cutoff = parse_ts(as_of)
    if sid is None:
        sid = _resolve_sid_fallback(con, home, away, beijing)
    if sid is None:
        return None
    event = con.execute(
        """
        SELECT odds_home, odds_draw, odds_away FROM odds_change_event
        WHERE sid = ? AND bookmaker_id = ? AND published_at <= ?
        ORDER BY published_at DESC LIMIT 1
        """,
        [sid, SRCT_PINNACLE_BOOK, cutoff],
    ).fetchone()
    if event is None or any(price is None for price in event):
        return None
    return float(event[0]), float(event[1]), float(event[2])


def _resolve_sid_fallback(
    con: duckdb.DuckDBPyConnection, home: str, away: str, beijing: datetime
) -> str | None:
    """
    无链兜底：两步确定性键查询（票 75 语义）。

    多命中=歧义 → 拒绝并告警（report-audit-20261027 票 05：不再
    ORDER BY sid 任选首行——确定性任意选择仍是任意选择，歧义显式
    拒绝并把候选集留审计痕迹）。

    命名注记：这是确定性精确匹配的兜底路径，非模糊匹配（词条 _Avoid_）。
    """

    def _rows(sql: str, params: list[object]) -> list[Any]:
        return con.execute(sql, params).fetchall()

    day_rows = _rows(
        """
        SELECT sid FROM fixture_universe
        WHERE home = ? AND away = ? AND CAST(kickoff AS DATE) = ?
        """,
        [home, away, beijing.date().isoformat()],
    )
    kickoff_rows = _rows(
        """
        SELECT sid FROM fixture_universe
        WHERE kickoff = ? AND home = ?
        """,
        [beijing, home],
    )
    if len(day_rows) == 1:
        return str(day_rows[0][0])
    if len(kickoff_rows) == 1:
        # 第一步零匹配走原第二步路径；同日多场（correctness I4）第一步
        # 歧义但精确 kickoff 唯一 → 消歧接受
        return str(kickoff_rows[0][0])
    ambiguous = day_rows if day_rows else kickoff_rows
    if len(ambiguous) > 1:
        logger.warning(
            "sid 兜底多候选拒绝: home={} away={} kickoff={} candidates={}",
            home,
            away,
            beijing.isoformat(),
            [str(r[0]) for r in ambiguous],
        )
    return None


def jc_sp_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    JC 官方 SP 行（uniform 彩果，void=0 三向全；票 17 配对面）。

    表归属取数口（ADR-0008）：evaluation 侧（haircut 十年重估）经此引，
    不散写第二份 SQL。betting_single 供单关 overround 分层。
    """
    return conn.execute(
        """
        SELECT fixture_id, league_name, match_date,
               odds_h, odds_d, odds_a, betting_single
        FROM uniform_result_observations
        WHERE fixture_id IS NOT NULL AND void_flag = 0
          AND odds_h IS NOT NULL AND odds_d IS NOT NULL AND odds_a IS NOT NULL
          AND trim(odds_h) <> '' AND trim(odds_d) <> '' AND trim(odds_a) <> ''
          AND CAST(odds_h AS REAL) > 1.0
          AND CAST(odds_d AS REAL) > 1.0
          AND CAST(odds_a AS REAL) > 1.0
        ORDER BY id
        """
    ).fetchall()


def jc_sp_overround_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    全量 JC SP 面的 overround 行（不需 fixture 链；1.129 基线对照）。

    odds 列是 TEXT：数值比较必须 CAST（TEXT 与数字比是字典序，
    'abc'>1 为真——correctness F1）；坏值行在此拦下不出 NULL overround。
    """
    return conn.execute(
        """
        SELECT 1.0 / odds_h + 1.0 / odds_d + 1.0 / odds_a AS overround,
               betting_single
        FROM uniform_result_observations
        WHERE void_flag = 0
          AND trim(odds_h) <> '' AND trim(odds_d) <> '' AND trim(odds_a) <> ''
          AND CAST(odds_h AS REAL) > 1.0
          AND CAST(odds_d AS REAL) > 1.0
          AND CAST(odds_a AS REAL) > 1.0
        """
    ).fetchall()
