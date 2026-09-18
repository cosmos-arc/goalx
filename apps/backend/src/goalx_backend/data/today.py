"""今日页读模型：竞彩 vs 欧洲共识对照（票 22/36；EV 偏差标记票 08 口径）。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from pydantic import BaseModel, Field

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import quote_evidence
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

EV_FLAG_THRESHOLD = 0.05  # 今日页 EV 偏差标记阈值（票 08 report 口径）
MIN_BOOKS_FOR_CONSENSUS = 3  # books 少于该数标记样本不足


class SelectionTriple(BaseModel):
    """主/平/客三元组（赔率、概率或 EV）。"""

    h: float | None = None
    d: float | None = None
    a: float | None = None


class HadQuoteStatus(BaseModel):
    """今日行内嵌的 had 资格摘要（票 35 判定，票 36 消费）。"""

    as_of: str
    status: str  # valid | unknown | rejected
    reasons: list[str] = Field(default_factory=list)
    sale_state: str | None = None
    single_eligible: bool | None = None
    jc_source_updated_at: str | None = None
    eu_books: int = 0


class TodayFixtureView(BaseModel):
    """场次列表页一行：竞彩 vs 欧洲共识对照（票 22/36；票 wb-01 加业务日）。"""

    fixture_id: int
    match_code: str
    business_date: str
    competition: str
    tier: str
    home_team: str
    away_team: str
    kickoff_utc: str
    is_single: bool
    joined: bool
    jc_odds: SelectionTriple
    jc_updated_at: str | None = None
    books: int = 0
    eu_prob: SelectionTriple | None = None
    ev: SelectionTriple | None = None
    flags: list[str] = Field(default_factory=list)
    had_quote: HadQuoteStatus | None = None


def business_dates_from(start: str, days: int) -> list[str]:
    """从起始业务日向后展开 ``days`` 个日历日（票 wb-01：3 日窗口）。"""
    if days <= 1:
        return [start]
    begin = date.fromisoformat(start)
    return [(begin + timedelta(days=offset)).isoformat() for offset in range(days)]


def build_today_view(
    conn: sqlite3.Connection,
    business_date: str,
    *,
    as_of: str | None = None,
    days: int = 1,
) -> list[TodayFixtureView]:
    """
    组装竞彩场次对照表（竞彩 vs 欧洲共识、EV、资格判定、books 数、调盘时点）。

    ``days>1`` 时覆盖 ``[business_date, business_date+days-1]`` 的业务日窗口
    （工作台 v2 票 01），行内 ``business_date`` 标记归属日供前端按日分组。
    """
    moment = as_of or utc_now_iso()
    rows: list[TodayFixtureView] = []
    for fixture in fx_store.fixtures_for_business_dates(
        conn, business_dates_from(business_date, days)
    ):
        fixture_id = int(fixture["id"])
        jc = fx_store.latest_odds_by_selection(conn, fixture_id, "had", "sporttery")
        books = fx_store.eu_book_odds(conn, fixture_id)
        jc_odds = SelectionTriple(**{s: jc.get(s, (None,))[0] for s in SELECTIONS})
        view = TodayFixtureView(
            fixture_id=fixture_id,
            match_code=str(fixture["match_code"]),
            business_date=str(fixture["business_date"]),
            competition=str(fixture["competition_name"]),
            tier=str(fixture["competition_tier"]),
            home_team=str(fixture["home_team"]),
            away_team=str(fixture["away_team"]),
            kickoff_utc=str(fixture["kickoff_utc"]),
            is_single=bool(fixture["is_single"]),
            joined=fixture["odds_api_event_id"] is not None,
            jc_odds=jc_odds,
            jc_updated_at=max((at for _, at in jc.values()), default=None),
        )
        verdict = quote_evidence.adjudicate_had_quote(conn, fixture_id, moment)
        view.had_quote = HadQuoteStatus(
            as_of=moment,
            status=verdict.status,
            reasons=verdict.reasons,
            sale_state=verdict.sale_state,
            single_eligible=verdict.single_eligible,
            jc_source_updated_at=verdict.jc_source_updated_at,
            eu_books=verdict.eu_books,
        )
        if all(s in books for s in SELECTIONS):
            consensus = om.consensus_odds([books[s] for s in SELECTIONS])
        else:
            consensus = None
        if consensus is not None:
            probs = om.shin_implied(consensus)
            view.books = max(len(prices) for prices in books.values())
            view.eu_prob = SelectionTriple(
                **{s: round(p, 4) for s, p in zip(SELECTIONS, probs, strict=True)}
            )
            view.ev = SelectionTriple(
                **{
                    s: round(om.expected_value(p, jc[s][0]), 4)
                    if s in jc and jc[s][0]
                    else None
                    for s, p in zip(SELECTIONS, probs, strict=True)
                }
            )
            ev_values = [v for v in (view.ev.h, view.ev.d, view.ev.a) if v is not None]
            if ev_values and max(abs(v) for v in ev_values) >= EV_FLAG_THRESHOLD:
                view.flags.append("ev_deviation")
        if not view.joined:
            view.flags.append("not_joined")
        if view.books and view.books < MIN_BOOKS_FOR_CONSENSUS:
            view.flags.append("few_books")
        rows.append(view)
    return rows
