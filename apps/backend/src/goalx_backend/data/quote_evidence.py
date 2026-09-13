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
from dataclasses import dataclass, field
from datetime import UTC, datetime

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.markets import SELECTIONS

DEFAULT_FRESHNESS_SECONDS = 300.0
DEFAULT_PAIR_GAP_SECONDS = 300.0

VALID = "valid"
REJECTED = "rejected"
UNKNOWN = "unknown"


def _parse_ts(value: str) -> datetime:
    """ISO 时间串 → aware datetime（naive 按 UTC 解释）。"""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _seconds_between(later: str, earlier: str) -> float:
    return (_parse_ts(later) - _parse_ts(earlier)).total_seconds()


def effective_observed_at(row: sqlite3.Row) -> str | None:
    """本机观测时间；旧行按源解释（odds_api*=captured_at，sporttery=未知）。"""
    observed = row["observed_at"]
    if observed:
        return str(observed)
    if str(row["source"]).startswith("odds_api:"):
        return str(row["captured_at"])
    return None


def effective_source_updated_at(row: sqlite3.Row) -> str | None:
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


def latest_per_selection(rows: list[sqlite3.Row]) -> dict[str, sqlite3.Row]:
    """按 captured_at 取每选项最新一行（并列时 id 大者胜）。"""
    latest: dict[str, sqlite3.Row] = {}
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
) -> list[sqlite3.Row]:
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


def books_complete_asof(
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    market_code: str = "had",
    purpose: str | None = None,
    exclusive: bool = False,
    max_age_seconds: float | None = None,
) -> dict[str, dict[str, float]]:
    """
    as_of 时点可用的各 book 完整三向最新报价（本包统一的报价读取口径）。

    资格按行判定：观测时间（旧行按源解释）须不晚于 as_of（exclusive=True
    时严格早于）；给 max_age_seconds 时还须不早于 as_of − 该窗。每 book 按
    captured_at 取各选项最新行，三向不完整的 book 剔除。
    """
    by_book: dict[str, list[sqlite3.Row]] = {}
    for row in had_snapshot_rows(
        conn, fixture_id, market_code=market_code, purpose=purpose
    ):
        observed = effective_observed_at(row)
        if observed is None:
            continue
        if (observed >= as_of) if exclusive else (observed > as_of):
            continue
        if (
            max_age_seconds is not None
            and _seconds_between(as_of, observed) > max_age_seconds
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
    conn: sqlite3.Connection, fixture_id: int, as_of: str, *, market_code: str = "had"
) -> dict[str, float] | None:
    """as_of 前最新欧赔完整三向共识的 Shin 概率（评分基准，非成交口径）。"""
    books = books_complete_asof(conn, fixture_id, as_of, market_code=market_code)
    if not books:
        return None
    consensus = om.consensus_odds(
        [{b: books[b][s] for b in sorted(books)} for s in SELECTIONS]
    )
    if consensus is None:
        return None
    probs = om.shin_implied(consensus)
    return dict(zip(SELECTIONS, probs, strict=True))


def market_rows_with_competition(
    conn: sqlite3.Connection, *, market_code: str = "had"
) -> list[sqlite3.Row]:
    """
    全量比赛的市场快照行（sporttery + odds_api），带联赛名。

    haircut 批量配对用；按 captured_at,id 稳定排序。
    """
    return conn.execute(
        """
        SELECT f.id AS fixture_id, c.name AS competition,
               s.id AS id, s.selection_code, s.source, s.odds, s.captured_at,
               s.observed_at, s.source_updated_at
        FROM odds_snapshots s
        JOIN fixtures f ON f.id = s.fixture_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE s.market_code = ?
          AND (s.source = 'sporttery' OR s.source LIKE 'odds_api:%')
        ORDER BY s.captured_at, s.id
        """,
        (market_code,),
    ).fetchall()


def adjudicate_had_quote(
    conn: sqlite3.Connection,
    fixture_id: int,
    as_of: str,
    *,
    freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
    max_pair_gap_seconds: float = DEFAULT_PAIR_GAP_SECONDS,
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
    if _parse_ts(as_of) >= _parse_ts(kickoff or ""):
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
        if sale["sale_state"] == "unknown":
            verdict.mark_unknown("sale_status_unknown_value")
        if sale["single_eligible"] is not None:
            verdict.single_eligible = bool(sale["single_eligible"])
        else:
            verdict.mark_unknown("single_eligibility_unknown")

    _adjudicate_jc(conn, verdict, as_of, freshness_seconds)
    if verdict.status == REJECTED:
        return verdict
    _adjudicate_eu(conn, verdict, as_of, max_pair_gap_seconds=max_pair_gap_seconds)
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
    age = _seconds_between(as_of, verdict.jc_source_updated_at)
    verdict.jc_age_seconds = age
    if age > freshness_seconds:
        verdict.reject("stale_source")
    verdict.sources.append("sporttery")


def _book_asof(
    book: str,
    book_rows: list[sqlite3.Row],
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
        gap = abs(_seconds_between(book_time, jc_source_updated_at))
        if gap > max_pair_gap_seconds:
            return None, book_time, f"{book}:pair_gap_exceeded"
    return (
        {sel: float(r["odds"]) for sel, r in latest.items()},
        book_time,
        "",
    )


def _collect_valid_books(
    verdict: HadQuoteVerdict,
    eligible: list[sqlite3.Row],
    *,
    max_pair_gap_seconds: float,
) -> dict[str, dict[str, float]]:
    """按 book 校验并把通过者写入 verdict 的 notes/时差/源时间字段。"""
    by_book: dict[str, list[sqlite3.Row]] = {}
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
            gap = abs(_seconds_between(book_time, verdict.jc_source_updated_at))
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
) -> None:
    rows = had_snapshot_rows(conn, verdict.fixture_id)
    if not rows:
        verdict.mark_unknown("eu_no_quote")
        return
    eligible = [r for r in rows if (t := effective_observed_at(r)) and t <= as_of]
    if not eligible:
        verdict.mark_unknown("eu_observed_at_unknown")
        return
    valid_books = _collect_valid_books(
        verdict, eligible, max_pair_gap_seconds=max_pair_gap_seconds
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
