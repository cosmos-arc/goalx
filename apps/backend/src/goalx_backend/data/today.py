"""今日页读模型：竞彩 vs 欧洲共识对照（票 22/36；EV 偏差标记票 08 口径）。"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from pydantic import BaseModel, Field

from goalx_backend import odds_math as om
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import mapping, quote_evidence
from goalx_backend.db import utc_now_iso
from goalx_backend.markets import SELECTIONS

EV_FLAG_THRESHOLD = 0.05  # 今日页 EV 偏差标记阈值（票 08 report 口径）
MIN_BOOKS_FOR_CONSENSUS = 3  # books 少于该数标记样本不足
LOW_CONFIDENCE_FLAG = "low_confidence"  # 共识分母 <4 的低置信标记（票 39）
# 错价桶只读标注（misprice-bucket-display 票 01，2026-11-01 用户裁决）：
# 用户裁决的观察价段——主胜/客胜任一价含界落 [2.0,2.6] 即标（平局不计）。
# 出处注记（correctness F1）：十年回测发现的是特征桶族聚合效应（|t|
# 2.0-2.6，1-2pp 量级），本价段无直接回测，标注是攒前向读数的观察面；
# 只做展示不进决策面，前向读数攒够后再裁是否升级。
PRICE_BAND = (2.0, 2.6)
PRICE_BAND_FLAG = "price_band_2_0_2_6"


class SelectionTriple(BaseModel):
    """主/平/客三元组（赔率、概率或 EV）。"""

    h: float | None = None
    d: float | None = None
    a: float | None = None


class HadQuoteStatus(BaseModel):
    """
    今日行内嵌的 had 资格摘要（票 35 判定，票 36 消费）。

    票 08 五时钟（只记录展示，stale_source 判定仍只看调盘龄）：
    jc_source/jc_age=价格持续未变；jc_observed/_age=最近成功确认观测；
    sale_observed/_age=销售状态最近确认；eu_age=国际参考最近观测；
    pair_gap=两源配对时差。
    """

    as_of: str
    status: str  # valid | unknown | rejected
    reasons: list[str] = Field(default_factory=list)
    sale_state: str | None = None
    single_eligible: bool | None = None
    jc_source_updated_at: str | None = None
    jc_age_seconds: float | None = None
    jc_observed_at: str | None = None
    jc_observed_age_seconds: float | None = None
    sale_observed_at: str | None = None
    sale_age_seconds: float | None = None
    eu_age_seconds: float | None = None
    pair_gap_seconds: float | None = None
    eu_books: int = 0


class StaleLineView(BaseModel):
    """陈盘信号（票 48 纯派生只读）：距上次调盘时长 + sharp 参考漂移。"""

    as_of: str
    jc_last_move: str
    minutes_since_move: int
    drift: float | None = None
    drift_selection: str | None = None
    sharp_ref: str | None = None


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
    stale_line: StaleLineView | None = None


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
    duck_con: quote_evidence.DuckCon | None = None,
) -> list[TodayFixtureView]:
    """
    组装竞彩场次对照表（竞彩 vs 欧洲共识、EV、资格判定、books 数、调盘时点）。

    ``days>1`` 时覆盖 ``[business_date, business_date+days-1]`` 的业务日窗口
    （工作台 v2 票 01），行内 ``business_date`` 标记归属日供前端按日分组。
    ``duck_con``（票 02）：请求域 corpus 只读连接 + 每场 srct 链 sid 由本层
    解析注入（欧赔共识/had 判定/陈盘锚三面同源同连接）；缺席 → EU 段空值，
    JC 段不受影响。
    """
    moment = as_of or utc_now_iso()
    fixtures = fx_store.fixtures_for_business_dates(
        conn, business_dates_from(business_date, days)
    )
    # 批量预取（票 05）：窗口内全部已链 sid 的 as-of 逐书行一次取齐，
    # 共识/had 判定/陈盘 current 端复用——28 场×N 查收敛 1 批查
    sid_by_fixture = {
        int(f["id"]): mapping.srct_sid_for_fixture(conn, int(f["id"])) for f in fixtures
    }
    prefetched = (
        quote_evidence.srct_snapshot_rows_bulk(
            duck_con, sorted({s for s in sid_by_fixture.values() if s}), moment
        )
        if duck_con is not None
        else {}
    )
    rows: list[TodayFixtureView] = []
    for fixture in fixtures:
        fixture_id = int(fixture["id"])
        jc = fx_store.latest_odds_by_selection(conn, fixture_id, "had", "sporttery")
        sid = sid_by_fixture[fixture_id]
        eu_rows = prefetched.get(sid) if sid is not None else None
        books = quote_evidence.eu_book_odds(
            conn, fixture_id, duck_con=duck_con, sid=sid, rows=eu_rows
        )
        jc_odds = SelectionTriple(**{s: jc.get(s, (None,))[0] for s in SELECTIONS})
        in_price_band = any(
            price is not None and PRICE_BAND[0] <= price <= PRICE_BAND[1]
            for price in (jc_odds.h, jc_odds.a)
        )
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
            joined=mapping.has_srct_link(conn, fixture_id),
            jc_odds=jc_odds,
            jc_updated_at=max((at for _, at in jc.values()), default=None),
        )
        signal = quote_evidence.stale_line_signal(
            conn, fixture_id, as_of=moment, duck_con=duck_con, sid=sid, rows=eu_rows
        )
        if signal is not None:
            view.stale_line = StaleLineView(
                as_of=signal.as_of,
                jc_last_move=signal.jc_last_move,
                minutes_since_move=signal.minutes_since_move,
                drift=signal.drift,
                drift_selection=signal.drift_selection,
                sharp_ref=signal.sharp_ref,
            )
        verdict = quote_evidence.adjudicate_had_quote(
            conn, fixture_id, moment, duck_con=duck_con, sid=sid, rows=eu_rows
        )
        view.had_quote = HadQuoteStatus(
            as_of=moment,
            status=verdict.status,
            reasons=verdict.reasons,
            sale_state=verdict.sale_state,
            single_eligible=verdict.single_eligible,
            jc_source_updated_at=verdict.jc_source_updated_at,
            jc_age_seconds=verdict.jc_age_seconds,
            jc_observed_at=verdict.jc_observed_at,
            jc_observed_age_seconds=verdict.jc_observed_age_seconds,
            sale_observed_at=verdict.sale_observed_at,
            sale_age_seconds=verdict.sale_age_seconds,
            eu_age_seconds=verdict.eu_age_seconds,
            pair_gap_seconds=verdict.pair_gap_seconds,
            eu_books=verdict.eu_books,
        )
        assembled = om.consensus_probs(books, SELECTIONS)
        if assembled is not None:
            view.books, probs = assembled
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
        if in_price_band:
            view.flags.append(PRICE_BAND_FLAG)
        if not view.joined:
            view.flags.append("not_joined")
        if view.books and view.books < MIN_BOOKS_FOR_CONSENSUS:
            view.flags.append("few_books")
        # 共识分母护栏（票 39）：books <4 时共识可信度不足，打低置信标记
        # （前端与 few_books 合并为一个显示位；阈值待人追认，见 odds_math）。
        if view.books and om.consensus_low_confidence(view.books):
            view.flags.append(LOW_CONFIDENCE_FLAG)
        rows.append(view)
    return rows


def board_coverage(conn: sqlite3.Connection, business_date: str) -> tuple[int, int]:
    """
    今日板覆盖（晨检第 8 项）：(总场数, 已链场数)。

    已链 = 有 srct 链（欧赔输入面与今日页 joined 同口径）。
    """
    total = conn.execute(
        """
            SELECT COUNT(DISTINCT f.id) FROM fixtures f
            JOIN match_codes mc ON mc.fixture_id = f.id
              AND mc.kind = 'jingcai' AND mc.business_date = ?
        """,
        (business_date,),
    ).fetchone()[0]
    joined = conn.execute(
        """
            SELECT COUNT(DISTINCT f.id) FROM fixtures f
            JOIN match_codes mc ON mc.fixture_id = f.id
              AND mc.kind = 'jingcai' AND mc.business_date = ?
            WHERE EXISTS (
                SELECT 1 FROM source_match_links l
                WHERE l.fixture_id = f.id AND l.source = 'srct'
            )
        """,
        (business_date,),
    ).fetchone()[0]
    return int(total), int(joined)


# ---- 票 07：首页无候选六桶归因（report-audit-20261103） ----
# 桶判定规则与 quote_evidence 裁决原因一一对应——单一真相不新造第二套
# 健康口径；报告六类措辞 = 桶 label。可执行映射 = 下列 frozensets（原因
# →桶），完整映射表文本见 ATTRIBUTION_CALIBER（进契约描述，勿在此
# 复写第三份）。未映射新原因兜底进"有价但证据不合格"并经 reasons_seen
# 浮出；已停售/已开赛入 window_closed 脚注面（不入六桶但必须可见）；
# positive_candidates=valid 且有正 EV 的对照面（"没有候选"前提不成立时
# 如实亮出）。
WINDOW_CLOSED_REASONS = frozenset({"sale_stopped", "kickoff_passed"})
NOT_ON_SALE_REASONS = frozenset({"sale_not_on_sale", "sale_window_closed"})
NOT_COLLECTED_REASONS = frozenset(
    {"jc_no_quote", "sale_status_unknown", "sale_status_unknown_value"}
)
EU_CUT_REASONS = frozenset(
    {"eu_no_quote", "eu_no_valid_books", "eu_observed_at_unknown"}
)
EVIDENCE_UNQUALIFIED_REASONS = frozenset(
    {
        "stale_source",
        "jc_observed_at_unknown",
        "jc_source_time_unknown",
        "jc_three_way_incomplete",
        "single_eligibility_unknown",
    }
)
MAX_BUCKET_SAMPLES = 50  # 桶样本上限（下钻展示用，非全量清单）

ATTRIBUTION_CALIBER = (
    "六桶=report-audit-20261103 报告措辞；桶判定与 quote_evidence 裁决原因"
    "一一对应（未开售←sale_not_on_sale/sale_window_closed；数据没采到←"
    "jc_no_quote/sale_status_unknown*；国际参考断供←eu_no_quote/"
    "eu_no_valid_books/eu_observed_at_unknown；有价但证据不合格←stale_source"
    "/jc 观测与三向类，未映射新原因兜底进此桶并经 reasons_seen 浮出；"
    "信息齐确实无优势←判定 valid 且无正 EV；有候选来不及执行=空桶占位）。"
    "已停售/已开赛入 window_closed 脚注面不入六桶；positive_candidates="
    "valid 且有正 EV（今日确实有候选的对照面）。对账：board_coverage 与"
    "晨检第 8 项同函数同数；eu_cut 桶按判定原因计（已链但无有效书也入），"
    "与晨检链覆盖为间接对账（eu_cut ≥ 未链差额）。days>1 时窗口合并为一组桶。"
)


class AttributionSample(BaseModel):
    """桶内一场样本（下钻列表行）。"""

    fixture_id: int
    match_code: str
    home_team: str
    away_team: str
    kickoff_utc: str
    reasons: list[str] = Field(default_factory=list)


class AttributionBucket(BaseModel):
    """一个归因桶：计数 + 样本 + 本日出现过的裁决原因（对账用）。"""

    key: str
    label: str
    count: int = 0
    samples: list[AttributionSample] = Field(default_factory=list)
    reasons_seen: list[str] = Field(default_factory=list)


class NoCandidateAttribution(BaseModel):
    """首页六桶归因（票 07）：今日（按玩法/业务日）为何没有候选。"""

    business_date: str
    market_code: str
    as_of: str
    total: int
    buckets: list[AttributionBucket]  # 恒六桶（空桶=0 计数占位）
    window_closed: AttributionBucket  # 已停售/已开赛（脚注面，不入六桶）
    positive_candidates: AttributionBucket  # valid 且有正 EV（候选对照面）
    # [总场数, 已链场数]——晨检第 8 项同函数同数（days>1 时跨窗口日求和）
    board_coverage: list[int]
    # ok | degraded：degraded=corpus 读面缺席（被写方锁占/库缺席）——
    # 此时 eu_cut 桶可能混入"读面缺席"而非真断供，对账先看本字段
    corpus_reader: str = "ok"
    caliber: str


def _reasons_of(row: TodayFixtureView) -> list[str]:
    return list(row.had_quote.reasons) if row.had_quote else []


def _has_any(reasons: list[str], wanted: frozenset[str]) -> bool:
    return any(reason in wanted for reason in reasons)


def _has_positive_ev(row: TodayFixtureView) -> bool:
    return row.ev is not None and any(
        value is not None and value > 0 for value in (row.ev.h, row.ev.d, row.ev.a)
    )


def _reason_bucket_key(reasons: list[str]) -> str | None:
    """
    裁决原因 → 五桶 key（无命中返回 None 交给 valid/EV 分流）。

    evidence_unqualified 显式消费其 frozenset（correctness F7）：防未来
    新原因与 valid 状态共现时被兜底误吞——映射权威与路由一致。
    """
    for wanted, key in (
        (WINDOW_CLOSED_REASONS, "window_closed"),
        (NOT_ON_SALE_REASONS, "not_on_sale"),
        (NOT_COLLECTED_REASONS, "not_collected"),
        (EU_CUT_REASONS, "eu_cut"),
        (EVIDENCE_UNQUALIFIED_REASONS, "evidence_unqualified"),
    ):
        if _has_any(reasons, wanted):
            return key
    return None


def _bucket_key_for(row: TodayFixtureView) -> str:
    """一场 → 桶 key（固定优先级首匹配；映射表见模块 docstring 注记）。"""
    reason_key = _reason_bucket_key(_reasons_of(row))
    if reason_key is not None:
        return reason_key
    status = row.had_quote.status if row.had_quote else "unknown"
    if status != "valid":
        # 有价但证据不合格：stale/观测未知/三向不全/单固未知 + 未映射
        # 新原因兜底（经 reasons_seen 浮出，不静默不误归他桶）
        return "evidence_unqualified"
    if _has_positive_ev(row):
        return "positive_candidates"
    return "no_edge"


def no_candidate_attribution(
    conn: sqlite3.Connection,
    rows: list[TodayFixtureView],
    business_date: str,
    *,
    as_of: str,
    market_code: str = "had",
    days: int = 1,
    corpus_reader: str = "ok",
) -> NoCandidateAttribution:
    """
    六桶归因聚合：``build_today_view`` 行 → 桶计数与样本（纯派生只读）。

    分类按固定优先级首匹配（一场只进一桶，``_bucket_key_for``）：停售/
    开赛 → 未开售 → 数据没采到 → 国际参考断供 → 有价但证据不合格 →
    valid 按 EV 分无优势/有候选。未映射的新裁决原因兜底进"有价但证据
    不合格"并经 reasons_seen 浮出。同一 fixture 多 match_code 只计一次
    （与 board_coverage 的 DISTINCT 同口径）；board_coverage 跨窗口日
    求和（days>1 时 total==coverage[0] 可对账）。
    """
    definitions: list[tuple[str, str]] = [
        ("not_on_sale", "未开售"),
        ("not_collected", "数据没采到"),
        ("eu_cut", "国际参考断供"),
        ("evidence_unqualified", "有价但证据不合格"),
        ("no_edge", "信息齐确实无优势"),
        ("missed_deadline", "有候选来不及执行"),
    ]
    buckets_by_key: dict[str, AttributionBucket] = {
        key: AttributionBucket(key=key, label=label)
        for key, label in [
            *definitions,
            ("window_closed", "已停售/已开赛"),
            ("positive_candidates", "信息齐且有正 EV 候选"),
        ]
    }

    def admit(row: TodayFixtureView) -> None:
        bucket = buckets_by_key[_bucket_key_for(row)]
        bucket.count += 1
        if len(bucket.samples) < MAX_BUCKET_SAMPLES:
            bucket.samples.append(
                AttributionSample(
                    fixture_id=row.fixture_id,
                    match_code=row.match_code,
                    home_team=row.home_team,
                    away_team=row.away_team,
                    kickoff_utc=row.kickoff_utc,
                    reasons=_reasons_of(row),
                )
            )
        for reason in _reasons_of(row):
            if reason not in bucket.reasons_seen:
                bucket.reasons_seen.append(reason)

    # match_codes 唯一键不含 fixture 维（correctness F4）：同场同日双码
    # 双计会破坏 total 与 board_coverage(DISTINCT) 的同源对账——按
    # fixture_id 去重，一场只进桶一次
    deduped = {row.fixture_id: row for row in rows}
    for row in deduped.values():
        admit(row)
    coverage = [
        sum(part)
        for part in zip(
            *(
                board_coverage(conn, day)
                for day in business_dates_from(business_date, days)
            ),
            strict=True,
        )
    ]
    return NoCandidateAttribution(
        business_date=business_date,
        market_code=market_code,
        as_of=as_of,
        total=len(deduped),
        buckets=[buckets_by_key[key] for key, _ in definitions],
        window_closed=buckets_by_key["window_closed"],
        positive_candidates=buckets_by_key["positive_candidates"],
        board_coverage=coverage,
        corpus_reader=corpus_reader,
        caliber=ATTRIBUTION_CALIBER,
    )
