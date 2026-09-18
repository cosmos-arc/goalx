"""
源D 结果页数据获取（票 42）：赛果自动同步源（域名见配置 caiguo_base_url）。

数据源实证（2026-09-18，详见票 42 Answer 的四源对比）：
- URL 模式：``?e=YYYY-MM-DD`` 业务日参数（入口 301 → 同参数路径，需跟随一次
  重定向；响应 GB18030 编码）；
- 访问限制仅要求浏览器 User-Agent（默认 curl UA → HTTP 567）；无 cookie/JS
  挑战（源D 的另两个子域才有 ``__tst_status`` 挑战，本模块不用它们）；
- 每行 ``<tr id="a<fid>" status="4">``：销售编号（周XNNN，与源A 网关的
  matchNumStr 同口径、同业务日语义）+ 全场比分（pk 块 clt1/clt3）+ 半场比分
  （第二个 red 单元格）+ 胜平负字母（交叉校验用）；
- 时延：当日晨完场比赛在上午已带比分（页面缓存标记 MISS）。

口径守则（ADR 0001 澄清条款，票 42）：
- 只导入 status="4"（完场）且比分可解析、胜平负字母自洽的场次；
- 无效场次/延期/进行中/未赛（status≠4）、比分缺列、字母不自洽 → 不落库，
  进待人工清单（fail-closed，人工兜底通道不动）；
- 与库内已存赛果不一致（含人工先录）→ 不自动冲正，进待人工清单；
- 落库走 import_draw_results（幂等+原子+冲正语义已在），source 用 SOURCE
  常量标记来源，published_at 置 None（源页面无逐场官方发布时点，不伪造；
  同步观测时点记 observed_at 于 draw_sync_runs）。

网络层只做一件薄事（带 UA 的 GET + 一次重定向跟随）；解析与匹配是纯函数，
测试用固定 fixture HTML，不打真实外网。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest.results import import_draw_results
from goalx_backend.db import utc_now_iso
from goalx_backend.models import DrawResultInput

SOURCE = "500.com"
PARSE_VERSION = "caiguo_live_v1"
# 行状态：0=未开赛（含延期/取消，页面无独立标记）；1/2/3=进行中；4=完场。
FINISHED_STATUS = "4"
# 胜平负字母（had 口径）→ (主胜 needed, 客胜 needed) 的判定字母表
_HAD_LETTER = {"胜": "h", "平": "d", "负": "a"}
# 近因窗口：候选业务日只回看 7 天（更久的缺果场走人工，不无限重试）
_PENDING_LOOKBACK_DAYS = 7
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


@dataclass
class CaiguoSyncStats:
    """一次同步的统计与待人工清单。"""

    source: str = SOURCE
    observed_at: str = ""
    business_dates: list[str] = field(default_factory=list)
    pages: int = 0
    fetched: int = 0  # 页面解析出的完场行数
    imported: int = 0  # 本次新落库（含同值幂等重放）场次数
    unchanged: int = 0  # 与库内已存完全一致、无需导入的场次数
    unmatched: int = 0  # 页面有、库内无对应竞彩场次的行数（范围外，不报警）
    pending_manual: list[dict[str, str]] = field(default_factory=list)


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


def candidate_business_dates(
    conn: sqlite3.Connection, now: datetime | None = None
) -> list[str]:
    """
    待出赛果场次的业务日集合（调度零成本跳过的依据）。

    已开赛（kickoff <= now）、无开奖、近 7 天内的竞彩场次所在业务日；
    空集 = 无待出赛果，本次同步可以完全不发请求。
    """
    current = now or datetime.now(UTC)
    floor = (current - timedelta(days=_PENDING_LOOKBACK_DAYS)).isoformat(
        timespec="seconds"
    )
    rows = conn.execute(
        """
        SELECT DISTINCT mc.business_date
        FROM match_codes mc
        JOIN fixtures f ON f.id = mc.fixture_id
        LEFT JOIN draw_results d ON d.fixture_id = f.id
        WHERE mc.kind = 'jingcai'
          AND f.kickoff_utc <= ?
          AND f.kickoff_utc >= ?
          AND d.id IS NULL
        ORDER BY mc.business_date
        """,
        (current.isoformat(timespec="seconds"), floor),
    ).fetchall()
    return [str(row["business_date"]) for row in rows]


def _to_input(result: ParsedResult, fixture_id: int) -> DrawResultInput:
    """解析行 → 落库输入（source 标记来源；published_at 源无官方时点置 None）。"""
    return DrawResultInput(
        fixture_id=fixture_id,
        home_goals=result.home_goals,
        away_goals=result.away_goals,
        half_home_goals=result.half_home_goals,
        half_away_goals=result.half_away_goals,
        void=False,
        void_reason=None,
        source=SOURCE,
        published_at=None,
    )


def _int_or_none(value: object) -> int | None:
    """把行值列还原为 int/None（非 int/str 视为空）。"""
    if isinstance(value, (int, str)):
        return int(value)
    return None


def _values_match(existing: sqlite3.Row, incoming: DrawResultInput) -> bool:
    """库内已存结果与新值是否逐字段一致（不含 source/published_at 追踪列）。"""
    return (
        int(existing["home_goals"]) == incoming.home_goals
        and int(existing["away_goals"]) == incoming.away_goals
        and _int_or_none(existing["half_home_goals"]) == incoming.half_home_goals
        and _int_or_none(existing["half_away_goals"]) == incoming.half_away_goals
        and bool(existing["void"]) == incoming.void
    )


def sync_draw_results(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    business_dates: list[str] | None = None,
    now: datetime | None = None,
) -> CaiguoSyncStats:
    """
    同步入口：拉取候选业务日页面 → 解析 → 匹配 → import_draw_results。

    - business_dates 缺省取 candidate_business_dates（空集零请求跳过）；
    - 与库内不一致的完场行不自动冲正，进待人工清单（ADR 0001）；
    - 成功后写 draw_sync_runs 元信息行（来源/时点/场次数/待人工清单）。
    """
    now_dt = now or datetime.now(UTC)
    stats = CaiguoSyncStats(observed_at=now_dt.isoformat(timespec="seconds"))
    dates = business_dates
    if dates is None:
        dates = candidate_business_dates(conn, now=now)
    stats.business_dates = list(dates)
    inputs: list[DrawResultInput] = []
    for business_date in dates:
        page = fetch_jczq_page(client, settings, business_date)
        stats.pages += 1
        parsed = parse_page(page)
        stats.fetched += len(parsed.finished)
        for skip in parsed.skipped:
            stats.pending_manual.append(
                {
                    "business_date": business_date,
                    "code": skip.code,
                    "reason": skip.reason,
                }
            )
        for result in parsed.finished:
            fixture_id = _fixture_id_for_code(conn, business_date, result.code)
            if fixture_id is None:
                stats.unmatched += 1
                continue
            existing = rs_store.get_draw_result(conn, fixture_id)
            if existing is not None:
                incoming = _to_input(result, fixture_id)
                if _values_match(existing, incoming):
                    stats.unchanged += 1
                    continue
                # 已存结果与源不一致：不自动冲正（人工裁决通道）
                stats.pending_manual.append(
                    {
                        "business_date": business_date,
                        "code": result.code,
                        "reason": "stored_differs",
                    }
                )
                continue
            inputs.append(_to_input(result, fixture_id))
    if inputs:
        stats.imported = import_draw_results(conn, inputs)
    record_sync_run(conn, stats)
    return stats


def record_sync_run(conn: sqlite3.Connection, stats: CaiguoSyncStats) -> int:
    """写一次同步的元信息行（append-only 语义，最新行即状态）。"""
    cur = conn.execute(
        """
        INSERT INTO draw_sync_runs
(source, observed_at, business_dates, pages, fetched, imported,
            unchanged, unmatched, pending_manual, parse_version, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            stats.source,
            stats.observed_at or utc_now_iso(),
            json.dumps(stats.business_dates, ensure_ascii=False),
            stats.pages,
            stats.fetched,
            stats.imported,
            stats.unchanged,
            stats.unmatched,
            json.dumps(stats.pending_manual, ensure_ascii=False),
            PARSE_VERSION,
            utc_now_iso(),
        ),
    )
    conn.commit()
    if not cur.lastrowid:
        raise RuntimeError("draw_sync_runs INSERT 未产生 rowid")
    return int(cur.lastrowid)


def latest_sync_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """最近一次同步的元信息（无同步史返回 None）。"""
    return conn.execute(
        "SELECT * FROM draw_sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def pending_result_count(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """已开赛、无开奖的竞彩场次数（状态端点的"待出赛果数"）。"""
    current = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM match_codes mc
        JOIN fixtures f ON f.id = mc.fixture_id
        LEFT JOIN draw_results d ON d.fixture_id = f.id
        WHERE mc.kind = 'jingcai' AND f.kickoff_utc <= ? AND d.id IS NULL
        """,
        (current,),
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def stats_dict(stats: CaiguoSyncStats) -> dict[str, Any]:
    """统计 → 可 JSON 化的 dict（flow 返回值/日志用）。"""
    return {
        "source": stats.source,
        "observed_at": stats.observed_at,
        "business_dates": stats.business_dates,
        "pages": stats.pages,
        "fetched": stats.fetched,
        "imported": stats.imported,
        "unchanged": stats.unchanged,
        "unmatched": stats.unmatched,
        "pending_manual": len(stats.pending_manual),
    }
