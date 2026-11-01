"""
09:05 晨检代码化（ingest-orchestration 票 01）：9+1 项只读红绿面。

goals4 断供 4 个月零痕迹的教训：日志全绿 ≠ 数据在进——护栏防错数据，
晨检防缺数据。本模块只读不修（repair 各归其主）；网络项仅第 10 项发
2 个官方当期请求（漂移对账）。CLI `goalx morning-check` 输出逐项
[ok|degraded|fail] + 退出码（任何 fail → 1；degraded 不红）。

阈值（票 01 人裁决项写死）：
- 夜班/收班/remat/wrap 新鲜界 26h（一昼夜 + 收班晚落余量）
- 账本 24h 绿 / 48h 黄（拍点 12:30/16:30/19:00 后应有当日行）
- 语料 36h（晨检七项既有口径）
- goals4 断供探测：官方当期面空 且 库内最新截止 >7 天 → fail
"""

from __future__ import annotations

import socket
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx

from goalx_backend import db as db_mod
from goalx_backend.betting import funnel
from goalx_backend.betting import store as bets_store
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import price_ledger, today
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import zucai_official
from goalx_backend.data.ingest.shell import browser_ssl_context

OK = "ok"
DEGRADED = "degraded"
FAIL = "fail"

_FRESH_HOURS = 26
_LEDGER_DEGRADED_HOURS = 48
_CORPUS_FRESH_HOURS = 36
_GOALS4_STALE_DAYS = 7
_SERVE_LOG_TAIL_BYTES = 200_000
_SERVE_LOG_MARKERS = ("CRASHED", "Traceback")


@dataclass(frozen=True)
class CheckItem:
    """一项检查的结果（status 三态；detail 人读数字）。"""

    name: str
    status: str
    detail: str

    @property
    def is_fail(self) -> bool:
        """红项判定（degraded 不算红——黄只看不算）。"""
        return self.status == FAIL


def _age_hours(stamp: str | None, now: datetime) -> float | None:
    """ISO 串距 now 的小时数；空/坏串 None（未知不算新鲜也不算红）。"""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed.astimezone(UTC)).total_seconds() / 3600


def _verdict(age: float | None, *, green: float, yellow: float | None = None) -> str:
    if age is None:
        return FAIL
    if age <= green:
        return OK
    if yellow is not None and age <= yellow:
        return DEGRADED
    return FAIL


def check_stack(*, api_port: int = 8000, prefect_port: int = 4200) -> CheckItem:
    """项 1 栈活性：Prefect 4200 + API 8000 双端口。"""
    up: list[str] = []
    down: list[str] = []
    for label, port in (("prefect", prefect_port), ("api", api_port)):
        probe = socket.socket()
        probe.settimeout(2.0)
        try:
            reachable = probe.connect_ex(("127.0.0.1", port)) == 0
        finally:
            probe.close()
        (up if reachable else down).append(label)
    both_down = len(down) == len(up) + len(down)
    status = OK if not down else (FAIL if both_down else DEGRADED)
    return CheckItem("stack", status, f"up={up} down={down}")


def check_night(store: CorpusStore, *, now: datetime) -> CheckItem:
    """项 2 昨夜夜班：摘要行新鲜 + 收班原因（budget/circuit=黄不红）。"""
    rows = store.night_summaries(limit=1)
    if not rows:
        return CheckItem("night", FAIL, "无夜班摘要行")
    row = rows[0]
    age = _age_hours(str(row.get("ended_at")), now)
    if age is None or age > _FRESH_HOURS:
        return CheckItem("night", FAIL, f"最新摘要过旧 ended_at={row.get('ended_at')}")
    reason = str(row.get("stop_reason"))
    status = OK if reason == "completed" else DEGRADED
    detail = f"{row.get('night_date')} {reason} 请求={row.get('requests')}"
    return CheckItem("night", status, detail)


def check_remat(store: CorpusStore, *, now: datetime) -> CheckItem:
    """项 3 remat：状态行新鲜（08:10 定拍 + 拍后钩子双保险）。"""
    state = store.remat_state()
    updated = str(state.get("updated_at")) if state else None
    age = _age_hours(updated, now)
    return CheckItem(
        "remat", _verdict(age, green=_FRESH_HOURS), f"updated_at={updated}"
    )


def check_wrap(conn: sqlite3.Connection, *, now: datetime) -> CheckItem:
    """项 4 23:30 wrap：funnel_days 最新行新鲜。"""
    age = _age_hours(funnel.latest_funnel_created_at(conn), now)
    detail = f"最新业务日={funnel.funnel_liveness(conn)}"
    return CheckItem("wrap", _verdict(age, green=_FRESH_HOURS), detail)


def check_ledger(conn: sqlite3.Connection, *, now: datetime) -> CheckItem:
    """项 5 账本：最新观测行新鲜（24h 绿/48h 黄）。"""
    latest = price_ledger.latest_observed_at(conn)
    age = _age_hours(latest, now)
    return CheckItem(
        "ledger",
        _verdict(age, green=24, yellow=_LEDGER_DEGRADED_HOURS),
        f"最新={latest}",
    )


def check_corpus(store: CorpusStore, *, now: datetime) -> CheckItem:
    """项 6 语料新鲜：raw_artifacts 最新 fetched_at 36h 内。"""
    latest = store.latest_raw_fetched_at()
    age = _age_hours(latest, now)
    return CheckItem(
        "corpus", _verdict(age, green=_CORPUS_FRESH_HOURS), f"最新工件={latest}"
    )


def check_serve_log(path: Path) -> CheckItem:
    """项 7 失败面：serve 日志尾部 CRASHED/Traceback 扫描（黄不红：采样面）。"""
    if not path.exists():
        return CheckItem("serve_log", DEGRADED, f"日志缺席 {path}")
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - _SERVE_LOG_TAIL_BYTES))
        tail = handle.read().decode("utf-8", "replace")
    counts = {marker: tail.count(marker) for marker in _SERVE_LOG_MARKERS}
    hits = sum(counts.values())
    status = OK if hits == 0 else DEGRADED
    return CheckItem(
        "serve_log", status, f"近{min(size, _SERVE_LOG_TAIL_BYTES)}B {counts}"
    )


def check_today_eu(conn: sqlite3.Connection, *, business_date: str) -> CheckItem:
    """项 8 今日页欧赔输入面（红不许静默跳过）：今日场有链即绿。"""
    total, joined = today.board_coverage(conn, business_date)
    status = DEGRADED if total == 0 else OK if joined > 0 else FAIL
    return CheckItem("today_eu", status, f"今日 {total} 场已链 {joined} 场")


def check_bets(conn: sqlite3.Connection) -> CheckItem:
    """项 9 纸面闭环观察（观察项：只报数不判红）。"""
    return CheckItem("bets", OK, f"累计 {bets_store.bets_total(conn)} 注")


def check_pool_drift(
    conn: sqlite3.Connection, settings: Settings, client: httpx.Client
) -> CheckItem:
    """
    项 10 传足当期漂移（goals4 断供探测器）。

    官方当期在售期号 vs 库内最新期号差集 → fail；官方面空但库内最新
    截止超 7 天 → fail（断供复发形态）；端点失败 → degraded。
    """
    try:
        value = zucai_official.fetch_current(client, settings)
    except (httpx.HTTPError, RuntimeError) as exc:
        return CheckItem("pool_drift", DEGRADED, f"官方当期端点不可达: {exc}")
    problems: list[str] = []
    for market, face_key in (("ttt14", "sfcMatch"), ("goals4", "jqcMatch")):
        face = cast("dict[str, Any]", value.get(face_key) or {})
        official = str(face.get("lotteryDrawNum") or "")
        db_row = conn.execute(
            (
                "SELECT MAX(period_no) AS p, MAX(sales_deadline) AS d"
                " FROM pool_periods WHERE market_code = ?"
            ),
            (market,),
        ).fetchone()
        db_max = str(db_row["p"]) if db_row and db_row["p"] else ""
        if official:
            if not db_max or int(db_max) < int(official):
                problems.append(
                    f"{market}: 官方在售 {official} > 库内 {db_max or '无'}"
                )
        else:
            stale_days = _age_hours(
                str(db_row["d"]) if db_row else None, datetime.now(UTC)
            )
            if not db_max or stale_days is None or stale_days > _GOALS4_STALE_DAYS * 24:
                problems.append(
                    f"{market}: 官方面空且库内无近期期（最新 {db_max or '无'}）"
                )
    status = FAIL if problems else OK
    return CheckItem("pool_drift", status, "; ".join(problems) or "sfc/goals4 当期在库")


def run_morning_check(
    settings: Settings,
    *,
    now: datetime | None = None,
    serve_log: Path | None = None,
) -> list[CheckItem]:
    """跑全部 10 项（只读；第 10 项发 2 个官方请求）。"""
    resolved_now = now or datetime.now(UTC)
    log_path = serve_log or Path.cwd() / ".scratch" / "prefect-serve.log"
    items = [check_stack()]
    store = CorpusStore(settings.corpus_root)
    conn = db_mod.connect(settings.db_path, readonly=True)
    try:
        items.extend(
            (
                check_night(store, now=resolved_now),
                check_remat(store, now=resolved_now),
                check_wrap(conn, now=resolved_now),
                check_ledger(conn, now=resolved_now),
                check_corpus(store, now=resolved_now),
                check_serve_log(log_path),
                check_today_eu(
                    conn, business_date=fx_store.beijing_business_date(resolved_now)
                ),
                check_bets(conn),
            )
        )
        with httpx.Client(verify=browser_ssl_context()) as client:
            items.append(check_pool_drift(conn, settings, client))
    finally:
        conn.close()
        store.close()
    return items
