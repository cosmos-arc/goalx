"""
源D 结果页数据获取：对账审计源（域名见配置 caiguo_base_url）。

票 42 起为赛果自动同步源；票 44 切换后官方 sporttery.cn uniform 族升结算
事实源（2026-09-20 用户裁决，系统未上线免观察期），本模块不再落库——
只做第三方对账。

数据源实证（2026-09-18，详见票 42 Answer 的四源对比）：
- URL 模式：``?e=YYYY-MM-DD`` 业务日参数（入口 301 → 同参数路径，需跟随一次
  重定向；响应 GB18030 编码）；
- 访问限制仅要求浏览器 User-Agent（默认 curl UA → HTTP 567）；无 cookie/JS
  挑战（源D 的另两个子域才有 ``__tst_status`` 挑战，本模块不用它们）；
- 每行 ``<tr id="a<fid>" status="4">``：销售编号（周XNNN，与源A 网关的
  matchNumStr 同口径、同业务日语义）+ 全场比分（pk 块 clt1/clt3）+ 半场比分
  （第二个 red 单元格）+ 胜平负字母（交叉校验用）；
- 时延：当日晨完场比赛在上午已带比分（页面缓存标记 MISS）。

口径守则（票 44 审计角色）：
- 只取 status="4"（完场）且比分可解析、胜平负字母自洽的行作对账参照；
- 页面异常行（比分缺列/字母不自洽/半场越界）→ 待人工清单（页面异常可能
  藏结果）；未完场行（status≠4）静默跳过——缺果与 void 由官方事实源负责；
- 与库内赛果不一致 → 对账 mismatch 条目（不冲正，人工裁决）；
- 审计窗口 = 近 7 天已开赛场次的业务日（含已落果日——对账要覆盖旧事实，
  与官方同步的"仅待出"候选推导（uniform.candidate_business_dates）语义不同）。

网络层只做一件薄事（带 UA 的 GET + 一次重定向跟随）；解析与匹配是纯函数，
测试用固定 fixture HTML，不打真实外网。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.reconcile import (
    ReconcileStats,
    ReferenceResult,
    add_manual,
    reconcile_draw_results,
    record_reconciliation_run,
    upsert_source_coverage,
)

SOURCE = "500.com"
PARSE_VERSION = "caiguo_live_v1"
# 行状态：0=未开赛（含延期/取消，页面无独立标记）；1/2/3=进行中；4=完场。
FINISHED_STATUS = "4"
# 胜平负字母（had 口径）→ (主胜 needed, 客胜 needed) 的判定字母表
_HAD_LETTER = {"胜": "h", "平": "d", "负": "a"}
# 跟随重定向上限（实证：入口 301 → /?e= 一次即到目标页）
_MAX_REDIRECT_HOPS = 3

_ROW_RE = re.compile(
    r'<tr id="a(?P<fid>\d+)"[^>]*\bstatus="(?P<status>\d+)"[^>]*>(?P<body>.*?)</tr>',
    re.S,
)
_CODE_RE = re.compile(r'check_id\[\]" value="\d+" />(?P<code>周\S+?\d+)</td>')
# 全场比分：pk 块的 clt1/clt3 链接文本
_FULL_RE = re.compile(
    r'class="pk">.*?clt1"\s*>(?P<home>\d+)</a><span>-</span>'
    + r'<a[^>]*class="clt3"\s*>(?P<away>\d+)</a>',
    re.S,
)
# 半场比分：全场比分之后的第一个 red "N - N" 单元格
_HALF_RE = re.compile(
    r'<td align="center" class="red">\s*(?P<home>\d+)\s*-\s*(?P<away>\d+)\s*</td>'
)
_HAD_CELL_RE = re.compile(
    r'<td align="center" class="red">\s*(?P<letter>[胜平负])\s*</td>'
)


@dataclass
class ParsedResult:
    """一条完场赛果（竞彩编号 + 全/半场比分）。"""

    fid: str
    code: str
    business_date: str
    home_goals: int
    away_goals: int
    half_home_goals: int | None
    half_away_goals: int | None


@dataclass
class SkippedRow:
    """一行未纳入自动导入的场次及原因（待人工清单条目）。"""

    fid: str
    code: str
    reason: str


@dataclass
class ParsedPage:
    """一个业务日页面的解析结果。"""

    business_date: str
    finished: list[ParsedResult] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)


@dataclass
class FetchedPage:
    """一次页面拉取：解码后的 HTML 与原始字节。"""

    business_date: str
    html: str
    raw: bytes


# 审计窗口：近 7 天已开赛场次的业务日（含已落果日）
_AUDIT_LOOKBACK_DAYS = 7


def _browser_headers() -> dict[str, str]:
    """访问限制只认浏览器 UA（实证：curl 默认 UA → 567）。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }


def fetch_jczq_page(
    client: httpx.Client, settings: Settings, business_date: str
) -> FetchedPage:
    """拉取一个业务日的竞彩页面（跟随一次 301 到 ``/?e=``，GB18030 解码）。"""
    url = settings.caiguo_base_url
    response = client.get(
        url, params={"e": business_date}, headers=_browser_headers(), timeout=25.0
    )
    hops = 0
    while (
        response.status_code in (301, 302, 303, 307, 308) and hops < _MAX_REDIRECT_HOPS
    ):
        target = str(response.headers.get("Location", ""))
        if not target:
            break
        next_url = httpx.URL(target)
        if next_url.is_relative_url:
            next_url = response.url.join(next_url)
        response = client.get(next_url, headers=_browser_headers(), timeout=25.0)
        hops += 1
    response.raise_for_status()
    return FetchedPage(
        business_date=business_date,
        html=response.content.decode("gb18030", errors="replace"),
        raw=response.content,
    )


def _had_letter(home: int, away: int) -> str:
    """按全场比分推 had 结果字母（h/d/a）。"""
    if home > away:
        return "h"
    if home < away:
        return "a"
    return "d"


def parse_page(page: FetchedPage) -> ParsedPage:
    """
    解析一个业务日页面 → 完场赛果 + 待人工清单（纯函数）。

    只认 ``status="4"`` 且全场比分可解析、胜平负字母自洽的行；其余
    （未赛/延期/无效场次/进行中/比分缺列/字母不自洽）进 skipped。
    """
    parsed = ParsedPage(business_date=page.business_date)
    for match in _ROW_RE.finditer(page.html):
        fid = match.group("fid")
        body = match.group("body")
        code_match = _CODE_RE.search(body)
        code = code_match.group("code") if code_match else f"fid:{fid}"
        if match.group("status") != FINISHED_STATUS:
            parsed.skipped.append(SkippedRow(fid=fid, code=code, reason="not_finished"))
            continue
        full = _FULL_RE.search(body)
        if full is None:
            parsed.skipped.append(
                SkippedRow(fid=fid, code=code, reason="score_missing")
            )
            continue
        home, away = int(full.group("home")), int(full.group("away"))
        # 胜平负字母自洽性（口径异常 → 人工，不落库）
        had_cell = _HAD_CELL_RE.search(body)
        if had_cell is not None and _HAD_LETTER.get(
            had_cell.group("letter")
        ) != _had_letter(home, away):
            parsed.skipped.append(
                SkippedRow(fid=fid, code=code, reason="had_letter_mismatch")
            )
            continue
        half = _HALF_RE.search(body)
        half_home = int(half.group("home")) if half else None
        half_away = int(half.group("away")) if half else None
        if half_home is not None and (
            half_home > home or half_away is None or half_away > away
        ):
            # 半场比分越界（页面异常）→ 人工
            parsed.skipped.append(
                SkippedRow(fid=fid, code=code, reason="half_score_invalid")
            )
            continue
        parsed.finished.append(
            ParsedResult(
                fid=fid,
                code=code,
                business_date=page.business_date,
                home_goals=home,
                away_goals=away,
                half_home_goals=half_home,
                half_away_goals=half_away,
            )
        )
    return parsed


def _fixture_id_for_code(
    conn: sqlite3.Connection, business_date: str, code: str
) -> int | None:
    """竞彩编号 → fixture_id（无对应场次返回 None）。"""
    row = conn.execute(
        """
        SELECT mc.fixture_id FROM match_codes mc
        WHERE mc.kind = 'jingcai' AND mc.business_date = ? AND mc.code = ?
        """,
        (business_date, code),
    ).fetchone()
    return int(row["fixture_id"]) if row is not None else None


def recent_business_dates(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    days: int = _AUDIT_LOOKBACK_DAYS,
) -> list[str]:
    """近 N 天已开赛竞彩场次的业务日集合（审计窗口：含已落果日）。"""
    current = now or datetime.now(UTC)
    floor = (current - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT DISTINCT mc.business_date
        FROM match_codes mc
        JOIN fixtures f ON f.id = mc.fixture_id
        WHERE mc.kind = 'jingcai'
          AND f.kickoff_utc <= ?
          AND f.kickoff_utc >= ?
        ORDER BY mc.business_date
        """,
        (current.isoformat(timespec="seconds"), floor),
    ).fetchall()
    return [str(row["business_date"]) for row in rows]


def audit_draw_results(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    business_dates: list[str] | None = None,
    now: datetime | None = None,
) -> ReconcileStats:
    """
    源D 审计入口：拉取窗口业务日页面 → 解析 → 匹配 → 对账（不落库）。

    - business_dates 缺省取 recent_business_dates（近 7 天含已落果日；
      需零请求跳过语义时由调用方先查空）；
    - 完场行匹配竞彩场次后作参照，与 draw_results 事实比对：不一致 →
      mismatch 条目（人工裁决）；库内缺失 → missing 条目（官方侧漏时可见）；
    - 页面异常行进待人工清单；未完场行静默跳过（事实源职责在官方）。
    """
    now_dt = now or datetime.now(UTC)
    observed_at = now_dt.isoformat(timespec="seconds")
    dates = business_dates
    if dates is None:
        dates = recent_business_dates(conn, now=now_dt)
    stats = ReconcileStats(
        source=SOURCE, observed_at=observed_at, business_dates=list(dates)
    )
    refs: list[ReferenceResult] = []
    for business_date in dates:
        page = fetch_jczq_page(client, settings, business_date)
        parsed = parse_page(page)
        # 定则 4（票 44）：源D 每业务日的覆盖现态（页面级，无联赛细分）
        upsert_source_coverage(
            conn,
            source=SOURCE,
            coverage_date=business_date,
            match_count=len(parsed.finished) + len(parsed.skipped),
            coverage_status="covered",
            observed_at=observed_at,
        )
        for skip in parsed.skipped:
            if skip.reason != "not_finished":
                add_manual(stats, business_date, skip.code, f"page_{skip.reason}")
        for result in parsed.finished:
            fixture_id = _fixture_id_for_code(conn, business_date, result.code)
            if fixture_id is None:
                stats.unmatched += 1
                continue
            refs.append(
                ReferenceResult(
                    fixture_id=fixture_id,
                    business_date=business_date,
                    code=result.code,
                    home_goals=result.home_goals,
                    away_goals=result.away_goals,
                    half_home_goals=result.half_home_goals,
                    half_away_goals=result.half_away_goals,
                )
            )
    reconcile_draw_results(conn, refs, stats)
    record_reconciliation_run(conn, stats, parse_version=PARSE_VERSION)
    return stats
