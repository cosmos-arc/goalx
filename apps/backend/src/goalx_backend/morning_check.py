"""
09:05 晨检代码化：15 项只读红绿面。

（ingest-orchestration 票 01 起；票 08 加第 14 项；auto-paper 票 04 加第 15 项。）

goals4 断供 4 个月零痕迹的教训：日志全绿 ≠ 数据在进——护栏防错数据，
晨检防缺数据。本模块只读不修（repair 各归其主）；网络面第 10 项漂移
对账走官方当期端点（≤2 请求）+ 第 13 项本机 /healthz（票 06 运行指纹,
dirty=黄）。CLI `goalx morning-check` 输出逐项
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from goalx_backend import db as db_mod
from goalx_backend.betting import account, funnel, summary
from goalx_backend.betting import store as bets_store
from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import (
    gold_reader,
    mapping,
    pool,
    price_ledger,
    quote_evidence,
    today,
)
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import zucai_official
from goalx_backend.data.ingest.shell import browser_ssl_context
from goalx_backend.data.ingest.srct_shift import GAP_HORIZON

OK = "ok"
DEGRADED = "degraded"
FAIL = "fail"

_FRESH_HOURS = 26
_LEDGER_DEGRADED_HOURS = 48
_CORPUS_FRESH_HOURS = 36
# gold 日频（08:40 gold-rebuild 定拍）：>36h 黄；错过两个晨检日仍无重建才红
_GOLD_FRESH_HOURS = 36
_GOLD_STALE_HOURS = 60
_GOALS4_STALE_DAYS = 7
# 票 03(d) 影子面窗口：daily-capture 日频定拍——零样本判"采集在拍而影子
# 断线"的证据窗 2 日；有拍后最新业务日落后超 2 个销售日=采样断流
_SHADOW_EXPECTED_WINDOW_DAYS = 2
_SHADOW_MAX_LAG_DAYS = 2
_SERVE_LOG_TAIL_BYTES = 200_000
_SERVE_LOG_MARKERS = ("Crashed", "CRASHED", "Traceback")
# 票 05 等源T：临界=开球 ≤2h 仍未链（视界=补缺 GAP_HORIZON 同源）
_JC_WAITING_CRITICAL = timedelta(hours=2)
_CRITICAL_NAME_CAP = 3  # 名单单列上限（多则省略号）


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
            # connect_ex 只覆盖立即失败（RST）；超时路径抛 TimeoutError
            # （correctness F1：栈僵死形态必须落红绿面不得炸整检）
            reachable = probe.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            reachable = False
        finally:
            probe.close()
        (up if reachable else down).append(label)
    both_down = len(down) == len(up) + len(down)
    status = OK if not down else (FAIL if both_down else DEGRADED)
    return CheckItem("stack", status, f"up={up} down={down}")


def check_runtime_fingerprint(
    log_path: Path,
    *,
    api_port: int = 8000,
    probe: Callable[[], dict[str, str]] | None = None,
) -> CheckItem:
    """
    运行指纹项（boundary-hardening 票 06;装配位次序见 run_morning_check）。

    serve /healthz dirty 旗 + 调度日志最近一次启动指纹的 WORKTREE DIRTY
    行。dirty=黄不红（人裁决 2026-11-02①：照跑不拒,晨检亮黄——未提交
    代码跑调度是 10-31 票 01 实录过的主库风险面,可见性兜底）。probe
    注入供测试;缺省实打 /healthz。
    """
    notes: list[str] = []
    worst = OK

    def _default_probe() -> dict[str, str]:
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f"http://127.0.0.1:{api_port}/healthz")
            response.raise_for_status()
            return dict(response.json())

    fetch = probe if probe is not None else _default_probe
    try:
        face = fetch()
        notes.append(
            f"api sha={str(face.get('git_sha'))[:12]} dirty={face.get('git_dirty')}"
        )
        if face.get("git_dirty") == "true":
            worst = DEGRADED
    except (httpx.HTTPError, ValueError):
        notes.append("healthz 不可读")  # 栈死红绿由项 1 覆盖,此处只缺面
    try:
        text = log_path.read_bytes()[-_SERVE_LOG_TAIL_BYTES:].decode(errors="replace")
    except OSError:
        text = ""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if "runtime fingerprint" in line]
    if not starts:
        notes.append("调度启动指纹行缺(栈未含票 06)")
    else:
        # 最近一次启动指纹之后的 dirty 行才是本班次的（老行跨重启不计）
        if any("WORKTREE DIRTY" in line for line in lines[starts[-1] :]):
            worst = DEGRADED
            notes.append("调度 WORKTREE DIRTY")
        notes.append(lines[starts[-1]].split(" - ")[-1].strip()[:90])
    return CheckItem("runtime_fingerprint", worst, "; ".join(notes))


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


def check_gold_freshness(store: CorpusStore, *, now: datetime) -> CheckItem:
    """
    项 12 gold 新鲜度（misprice 票 03①）：gold _meta built_at 36h 绿/60h 红。

    market_quote 停摆月余零红面的教训（票 03④ 根因）——复盘面读 gold 视图，
    gold 断build即复盘面静默退化为"全不在桶"；本面把断线晒到晨检。
    meta 缺席/坏串 = 红无built_at（不冒充新鲜）。
    """
    meta = gold_reader.read_gold_meta(store)
    age = _age_hours(meta.get("built_at"), now)
    return CheckItem(
        "gold",
        _verdict(age, green=_GOLD_FRESH_HOURS, yellow=_GOLD_STALE_HOURS),
        f"built_at={meta.get('built_at')}",
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
    """
    项 9 纸面闭环观察（观察项：只报数不判红；票 10 起并报 summary 聚合面）。

    detail 即 history 页指标卡的晨检对账面：已结=已锁定且已结算 canon，
    两模式分列不混算——人读可跨 UI/晨检对数。
    """
    faces = {face.mode: face for face in summary.bets_summary(conn)}

    def face_note(mode: str) -> str:
        face = faces[mode]
        pnl = f"{face.total_pnl:+.2f}"
        return f"{mode} 已结 {face.settled_n} 注 {pnl}/未结 {face.open_n}"

    total = bets_store.bets_total(conn)
    detail = f"累计 {total} 注（{face_note('paper')}；{face_note('live')}）"
    return CheckItem("bets", OK, detail)


def check_paper_account(conn: sqlite3.Connection) -> CheckItem:
    """
    项 15 纸面账户观察（auto-paper 票 04；只报数不判红）。

    当日新注数/账户行数/最近落注锚——auto-v1 换栈前台账可能为空，
    无行=「未开始」，不绿冒充。
    """
    face = account.paper_account_face(conn)
    auto_face = next(f for f in face.strategies if f.strategy == "auto-v1")
    manual_face = next(f for f in face.strategies if f.strategy == "manual")
    new_today, anchor = account.auto_v1_pulse(conn)
    if anchor is not None:
        zero = f"，{anchor['zero_note']}" if anchor["zero_note"] else ""
        anchor_note = (
            f"最近锚 {anchor['business_date']} {anchor['anchor_label']}"
            f"（注 {anchor['bets']}{zero}）"
        )
    else:
        anchor_note = "未开始"
    return CheckItem(
        "paper_account",
        OK,
        f"账户行 auto-v1 {auto_face.bets_n}"
        + f"/人工 {manual_face.bets_n} 注，"
        + f"现值 ¥{face.bankroll_now:.2f}，今日新 {new_today} 注；"
        + anchor_note,
    )


def check_freshness_shadow(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> CheckItem:
    """
    项 14 新鲜度双口径影子读数（票 08 观察项；票 03(d) 口径纠正）。

    规则口径=300s 调盘龄（现行 stale_source）；观测口径=同 300s 观测龄
    （最尖对照）。拍点挂 daily-capture 定拍（10:00/12:30/16:30/19:00，
    flows.py）——非 daily-wrap。无样本=「未开始积累/未验收」，不绿冒充
    测量健康：采集侧 2 日内有证据而影子零样本、或最新拍落后超两个销售
    日 = DEGRADED；详情恒带最新采样日期（防"曾经有一条便长期正常"）。
    """
    resolved = now or datetime.now(UTC)
    latest = quote_evidence.latest_freshness_shadow_date(conn)
    if latest is None:
        recent_capture = fx_store.latest_observation_time(conn)
        capturing = (
            recent_capture is not None
            and recent_capture
            >= (resolved - timedelta(days=_SHADOW_EXPECTED_WINDOW_DAYS)).isoformat()
        )
        return CheckItem(
            "freshness_shadow",
            DEGRADED if capturing else OK,
            "未开始积累/未验收（拍点=daily-capture 10:00/12:30/16:30/19:00；"
            + (
                f"采集侧最新证据 {recent_capture}，影子零样本=测量断线"
                if capturing
                else "采集侧近期亦无证据"
            )
            + "）",
        )
    summary = quote_evidence.freshness_shadow_summary(conn, latest)
    if summary is None:
        return CheckItem("freshness_shadow", OK, f"{latest} 未拍")
    comparable = summary["n_comparable"]
    today = fx_store.beijing_business_date(resolved)
    lag_days = (datetime.fromisoformat(today) - datetime.fromisoformat(latest)).days
    stale = lag_days > _SHADOW_MAX_LAG_DAYS
    return CheckItem(
        "freshness_shadow",
        DEGRADED if stale else OK,
        (f"采样断流：最新拍 {latest} 落后 {lag_days} 天；" if stale else "")
        + f"{latest} 规则新鲜 {summary['rule_fresh']}/{comparable}"
        + f" · 观测新鲜 {summary['observed_fresh']}/{comparable}"
        + f" · 分歧(规则旧/观测新) {summary['divergent_rule_stale_observed_fresh']}"
        + f" · 已拍 {summary['n']}",
    )


def check_jc_waiting(
    conn: sqlite3.Connection, *, now: datetime, business_dates: tuple[str, ...]
) -> CheckItem:
    """
    项 11 JC 在售等源T（票 05 补缺面的兜底可见性）。

    视界内（补缺同源 36h）竞彩业务日在售场无 srct 链 = 等源T（黄）；
    红面两类（correctness F1：深夜场临界窗 23:00-03:00 无观察点，晨检
    09:05 只能看到"已开球仍未链"）——①开球 ≤2h 未链（前瞻）②近 36h
    内已开球未链（昨夜残差，晨检最强信号）。票面原"范围外"桶被补缺
    设计取代：韩职类场现在也走编号直连补链（测试钉死），残差未链=真
    异常该红，不再按联赛豁免。等的是源T live 轨迹：odds_api 历史链
    单独在场不豁免（判据=srct linked，偏离 joined 广义口径有注记）。
    """
    rows = fx_store.jc_board_rows_in_horizon(
        conn,
        business_dates=business_dates,
        kickoff_from_iso=(now - GAP_HORIZON).isoformat(timespec="seconds"),
        kickoff_to_iso=(now + GAP_HORIZON).isoformat(timespec="seconds"),
    )
    linked = mapping.srct_sids_for_fixtures(conn, [int(r["fid"]) for r in rows])
    waiting = [r for r in rows if int(r["fid"]) not in linked]
    critical = [
        r
        for r in waiting
        if datetime.fromisoformat(str(r["kickoff"])) <= now + _JC_WAITING_CRITICAL
    ]
    if critical:
        status = FAIL
    elif waiting:
        status = DEGRADED
    else:
        status = OK
    played = [r for r in critical if datetime.fromisoformat(str(r["kickoff"])) <= now]
    upcoming = [r for r in critical if r not in played]
    names = [
        f"{r['code']} {r['home']}vs{r['away']}" + ("（已开球）" if r in played else "")
        for r in critical[:_CRITICAL_NAME_CAP]
    ]
    detail = (
        f"等源T {len(waiting)} 场（昨夜残差 {len(played)}，≤2h 临界 {len(upcoming)}："
        + "；".join(names)
        + ("…" if len(critical) > _CRITICAL_NAME_CAP else "")
        + "）"
        if waiting
        else f"窗内 {len(rows)} 场全链"
    )
    return CheckItem("jc_waiting", status, detail)


def check_pool_drift(
    conn: sqlite3.Connection,
    settings: Settings,
    client: httpx.Client,
    *,
    now: datetime | None = None,
) -> CheckItem:
    """
    项 10 传足当期漂移（goals4 断供探测器）。

    官方当期在售期号 vs 库内最新期号差集 → fail；官方面空但库内最新
    截止超 7 天 → fail（断供复发形态）；端点失败 → degraded。
    """
    resolved_now = now or datetime.now(UTC)
    try:
        value = zucai_official.fetch_current(client, settings)
    except (
        # HTTP/网错、success=false、200+非 JSON 体（JSONDecodeError⊂ValueError）、
        # 响应形状漂移（AttributeError）、非数字期号（int() ValueError）
        # ——correctness F2：探测器不得因端点形态漂移而崩，一律黄
        httpx.HTTPError,
        RuntimeError,
        ValueError,
        AttributeError,
        KeyError,
    ) as exc:
        return CheckItem(
            "pool_drift", DEGRADED, f"官方当期端点异常: {type(exc).__name__} {exc}"
        )
    problems: list[str] = []
    for market, face_key in (("ttt14", "sfcMatch"), ("goals4", "jqcMatch")):
        face = cast("dict[str, Any]", value.get(face_key) or {})
        official = str(face.get("lotteryDrawNum") or "")
        db_max, db_deadline = pool.latest_period_and_deadline(conn, market)
        if official:
            if not db_max or int(db_max) < int(official):
                problems.append(
                    f"{market}: 官方在售 {official} > 库内 {db_max or '无'}"
                )
        else:
            stale_days = _age_hours(db_deadline, resolved_now)
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
    """跑全部 15 项（只读；网络面第 10 项官方当期端点 + 第 13 项本机 /healthz）。"""
    resolved_now = now or datetime.now(UTC)
    log_path = serve_log or Path.cwd() / ".scratch" / "prefect-serve.log"
    items = [check_stack(), check_runtime_fingerprint(log_path)]
    # 只读红线注记：CorpusStore 冷启动会幂等自建 checkpoint 表（mkdir+DDL），
    # 语料根缺失时晨检不因此炸——接受（表结构幂等，无数据写入）
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
                check_gold_freshness(store, now=resolved_now),
                check_serve_log(log_path),
                check_today_eu(
                    conn, business_date=fx_store.beijing_business_date(resolved_now)
                ),
                check_bets(conn),
                check_paper_account(conn),
                check_freshness_shadow(conn, now=resolved_now),
                check_jc_waiting(
                    conn,
                    now=resolved_now,
                    business_dates=tuple(
                        fx_store.beijing_business_date(
                            resolved_now + timedelta(days=off)
                        )
                        for off in (
                            -1,
                            0,
                            1,
                        )  # 含昨日业务日：昨夜深夜场残差（correctness F1）
                    ),
                ),
            )
        )
        with httpx.Client(verify=browser_ssl_context()) as client:
            items.append(check_pool_drift(conn, settings, client))
    finally:
        conn.close()
        store.close()
    return items
