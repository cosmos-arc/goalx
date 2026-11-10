"""
09:05 晨检代码化：25 项只读红绿面。

（ingest-orchestration 票 01 起；票 08 加第 14 项；auto-paper 票 04 加第
15 项；identity 票 02 加第 16 项；autoresearch 票 04 加第 17 项；
autoresearch 票 14 加第 18 项；autoresearch 票 19 加第 19 项；
票 34 加第 20 项断点哨兵；autoresearch 票 17 加第 21 项；
vision-build 票 05 加第 22 项教训库复审到期；vision-build 票 08 加第
23 项收缩双钟与第 24 项四信号重扫唤醒；vision-build 票 06 加第 25 项
RAG 两索引落后条数。）

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

import duckdb
import httpx

from goalx_backend import db as db_mod
from goalx_backend.betting import account, funnel, summary
from goalx_backend.betting import store as bets_store
from goalx_backend.config import Settings
from goalx_backend.data import (
    corpus_duckdb,
    gold_reader,
    mapping,
    pool,
    price_ledger,
    quote_evidence,
    silver,
    today,
)
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import zucai_official
from goalx_backend.data.ingest.shell import browser_ssl_context
from goalx_backend.data.ingest.srct_shift import GAP_HORIZON
from goalx_backend.evaluation import (
    anchor_sentinel,
    contraction,
    edge_scan,
    lessons,
    rag,
)
from goalx_backend.modelling import forecast

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
# 赛前拍近窗（票 14 观察项）：与夜班/收班新鲜界同 26h 一昼夜口径
_PREMATCH_RECENT_HOURS = 26
# 项 20 断点哨兵（票 34）：对账面无新周行的黄线（周级定拍，8 天=错过
# 一个周一仍无周报即黄）
_SENTINEL_STALE_DAYS = 8
# 新闻 PIT 夜拍新鲜界（票 17）：26h 同口径——每晚不开=永久缺口，
# 断夜黄（degraded）不红（ponytail 睡眠漏跑协议允许，缺夜可见即可）
_NEWS_PIT_FRESH_HOURS = 26
# 项 25 RAG 卫报索引落后黄线（vision-build 票 06）：教训库小（>0 即黄）；
# 卫报大库留真回填余量（>500 篇落后=增量入口 CLI 该跑了；全量回填
# 前常态黄=待办可见，非故障）
_RAG_GUARDIAN_LAG_DEGRADED = 500


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


def check_stack(*, api_port: int = 8000, prefect_port: int = 4201) -> CheckItem:
    """项 1 栈活性：Prefect 4201（goalx 专端口，ditto 占 4200）+ API 8000 双端口。"""
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
    auto_note = "/".join(
        f"{f.strategy} {f.bets_n}" for f in face.strategies if f.strategy != "manual"
    )
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
        f"账户行 {auto_note}"
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


def check_alias_coverage(conn: sqlite3.Connection, *, business_date: str) -> CheckItem:
    """
    项 16 覆盖守门（identity 票 02）：最近业务日 no_mapping > 0 即红。

    31 队静默洞数月无人知的教训：容忍度=0，任何 no_mapping 行都附队名列单
    （canonical + 模型侧断名）。覆盖账无行=守门失效，黄面自报；账存在但
    落后 >1 天且无 no_mapping=黄（陈账的 0 不冒充新鲜；竞彩连日空板会
    良性触发，人工看单即辨）。
    """
    latest = forecast.latest_coverage_date(conn)
    if latest is None:
        return CheckItem(
            "alias_coverage",
            DEGRADED,
            "覆盖账无行（forecast_daily 未落账或库早于 v42）",
        )
    names = forecast.no_mapping_fixtures(conn, business_date=latest)
    if names:
        shown = "；".join(names[:_CRITICAL_NAME_CAP])
        return CheckItem(
            "alias_coverage",
            FAIL,
            f"{latest} no_mapping {len(names)} 场：{shown}"
            + ("…" if len(names) > _CRITICAL_NAME_CAP else ""),
        )
    try:
        lag_days = (
            datetime.fromisoformat(business_date) - datetime.fromisoformat(latest)
        ).days
    except ValueError:
        # 写侧 --date 无 argparse 校验（cli forecast --date 2026-11-7 可入库），
        # 坏串只降级本项不炸整检（correctness F1：晨检防缺数据也防自身崩）
        return CheckItem("alias_coverage", DEGRADED, f"覆盖账业务日坏串：{latest!r}")
    if lag_days > 1:
        return CheckItem(
            "alias_coverage",
            DEGRADED,
            f"{latest} no_mapping 0，但覆盖账落后 {lag_days} 天",
        )
    return CheckItem("alias_coverage", OK, f"{latest} no_mapping 0")


def check_edge_scan(conn: sqlite3.Connection, *, now: datetime) -> CheckItem:
    """
    项 17 探索轨扫描器绿（autoresearch 票 04/02）：s1/s2/s4 最新留痕 26h 内。

    观察项（degraded 不红）：wrap 夜拍断线/首跑前如实黄——发现台账与
    周报都吃这两张留痕表，断流须晨检可见。只查日频扫描器（s5 按期次
    兑付节奏，其 created_at 随近窗刷新，作新鲜度信号会失真）。
    """
    parts: list[str] = []
    worst = OK
    for scanner in ("s1", "s2", "s4"):  # 日频面（s5 期次节奏不进新鲜度）
        latest = edge_scan.latest_observation_at(conn, scanner)
        age = _age_hours(latest, now)
        if age is None or age > _FRESH_HOURS:
            worst = DEGRADED
            parts.append(f"{scanner}={latest or '无留痕'}")
        else:
            parts.append(f"{scanner} {age:.1f}h")
    return CheckItem("edge_scan", worst, "; ".join(parts))


def check_prematch_snapshots(store: CorpusStore, *, now: datetime) -> CheckItem:
    """
    项 18 赛前拍快照落库计数（autoresearch 票 14；观察项只报数不判红）。

    E-15 前向真值供给面的日频可见性：拍挂当期班（srct-shift */30），零拍
    日可能是无近开球 JC 场（国际比赛周等合法态），不设红黄——连续性验收
    （≥7 日）走票面运行期读数。数源=corpus_store.prematch_snapshot_face
    （只认 match_detail_prematch 专表的 @pre- 拍键——独立复核 W1 分表）。
    """
    face = store.prematch_snapshot_face(
        since_iso=(now - timedelta(hours=_PREMATCH_RECENT_HOURS)).isoformat(
            timespec="seconds"
        )
    )
    total = int(cast("int", face["total"]))
    if total == 0:
        return CheckItem(
            "prematch_snapshots", OK, "未开始积累（拍挂当期班 srct-shift）"
        )
    return CheckItem(
        "prematch_snapshots",
        OK,
        f"累计 {total} 拍 · 近{_PREMATCH_RECENT_HOURS}h "
        + f"{int(cast('int', face['recent']))} 拍 · 最新 {face['latest_fetched_at']}",
    )


def check_anchor_sentinel(conn: sqlite3.Connection, *, now: datetime) -> CheckItem:
    """
    项 20 断点哨兵（autoresearch 票 34，设计稿 13-sentinel-design §六）。

    任一轴最新周 alert → FAIL（红）；任一轴 warn/insufficient 或对账面
    >8 天无新周行 → DEGRADED（黄，含未首跑——不绿冒充；insufficient=
    该轴 n<10 无从判，设计稿 §三「晨检黄不红」）；否则 OK。detail=四轴
    `axis=值(n)` 汇总（check_edge_scan 同款字符串风格）。告警动作到此
    为止=留痕+红面，不自动处置。
    """
    face = anchor_sentinel.latest_week_face(conn)
    if not face:
        return CheckItem("anchor_sentinel", DEGRADED, "哨兵未首跑（无周报行）")
    parts: list[str] = []
    worst = OK
    for axis in anchor_sentinel.AXES:
        row = face.get(axis)
        if row is None:
            worst = DEGRADED
            parts.append(f"{axis}=missing")
            continue
        status = str(row["status"])
        metrics: dict[str, Any] = row["metrics"]
        metric = (
            metrics.get("rate")
            if axis
            in (
                anchor_sentinel.AXIS_COVERAGE_SECOND,
                anchor_sentinel.AXIS_COVERAGE_ANCHOR,
            )
            else metrics.get("median")
            if axis == anchor_sentinel.AXIS_DEV_ODDS
            else metrics.get("mean")
        )
        value = "" if metric is None else f" {float(metric):.3f}"
        parts.append(f"{axis}={status}{value}(n={row['n_fixtures']})")
        if status == "alert":
            worst = FAIL
        elif status in ("warn", "insufficient") and worst != FAIL:
            worst = DEGRADED
    latest_computed = max(str(row["computed_at"]) for row in face.values())
    age = _age_hours(latest_computed, now)
    if age is None or age > _SENTINEL_STALE_DAYS * 24:
        if worst != FAIL:
            worst = DEGRADED
        age_note = "坏时间戳" if age is None else f"{age:.0f}h"
        parts.append(f"对账面 {age_note} 无新周行（>8 天线）")
    return CheckItem("anchor_sentinel", worst, "; ".join(parts))


def check_srct_season_rhythm(
    duck_con: duckdb.DuckDBPyConnection | None, *, now: datetime
) -> CheckItem:
    """
    项 19 源T 当季锚书入库节奏（autoresearch 票 19；观察项只报数不判红）。

    账本 §4.1#7 放宽注记的积累面：cid177（SRCT_PINNACLE_BOOK）轨迹当季
    在库场次 + 最新观测——S4 有分辨面（corpus as-of 接线后）的数据前提
    可见性。计数不设阈值（赛季进行态，节奏靠逐日对比）；断供探测归语料
    新鲜度项。duck 桥缺席/视图降级=黄（与 S4 as-of 读数同源降级，不
    冒充）；季无行=绿零（空真：未开始积累）。
    """
    if duck_con is None:
        return CheckItem(
            "srct_season_rhythm", DEGRADED, "corpus 桥缺席（S4 as-of 面同源降级）"
        )
    # 季键按北京墙钟（#298 复核 info）：UTC now 在 8 月界切窗（北京
    # 8/1 00:00-07:59 = UTC 7/31）会取到上季；定拍 09:05 免疫，手动跑受影响
    season = silver.season_of(now.astimezone(fx_store.CST))
    face = quote_evidence.srct_anchor_season_face(duck_con, season=season)
    if face is None:
        return CheckItem("srct_season_rhythm", DEGRADED, f"{season} 视图缺席/查询降级")
    latest = face["latest_observed"] or "无"
    return CheckItem(
        "srct_season_rhythm",
        OK,
        f"{season} cid177 {face['n_sids']} 场在库 · 最新观测 {latest}",
    )


def check_news_pit_beats(store: CorpusStore, *, now: datetime) -> CheckItem:
    """
    项 21 新闻 PIT 夜拍新鲜度（autoresearch 票 17；断夜黄不红）。

    每晚不开=永久缺口（账本 §4.1#11 原话）——已开拍后最新 ok 拍超 26h
    即黄（ponytail 睡眠漏跑协议允许、如实记；缺夜的观测由次夜窗重含兜
    住，但拍纪元已晚——PIT 语义上属晚到，不回灌）。未开拍（无拍行）=
    部署前合法态，绿面如实标注。数源=corpus_store.news_pit_beats。
    """
    beats = store.news_pit_beats(limit=1)
    if not beats:
        return CheckItem("news_pit_beats", OK, "未开拍（拍挂 daily-wrap 23:30，票 17）")
    latest = beats[0]
    if latest["status"] != "ok":
        return CheckItem(
            "news_pit_beats",
            DEGRADED,
            f"最新拍 {latest['beat_date']} 状态 {latest['status']}（降级留痕）",
        )
    age = _age_hours(str(latest["observed_at"]), now)
    if age is None or age > _NEWS_PIT_FRESH_HOURS:
        detail = f"最新 ok 拍 {latest['beat_date']} 已 {age}h（超 26h 缺夜）"
        return CheckItem("news_pit_beats", DEGRADED, detail)
    rows = store.news_pit_state()
    return CheckItem(
        "news_pit_beats",
        OK,
        f"最新拍 {latest['beat_date']} {age:.1f}h 前 · "
        + f"累计观测 {int(cast('int', rows.get('total_articles') or 0))} 行",
    )


def check_lessons_review_due(
    lessons_dir: Path | None = None, *, now: datetime | None = None
) -> CheckItem:
    """
    项 22 教训库复审到期（vision-build 票 05；只报不改，到期=黄不红）。

    读 git 内受控 markdown（docs/plans/lessons/，evaluation.lessons）——
    晨检首个非库数据源形态（读的是版本库文件而非 sqlite/语料）。到期
    清单=人裁输入面（12 月未复现→建议降置信；置信翻转/归档=status/
    confidence 人改，脚本零写入）。目录缺席/条目破损=黄（受控文档
    可见性，不绿冒充，也不炸整检）。
    """
    resolved = now or datetime.now(UTC)
    today = resolved.astimezone(fx_store.CST).date()
    try:
        loaded = lessons.load_lessons(lessons_dir)
    except (lessons.LessonFormatError, OSError, RuntimeError) as exc:
        return CheckItem("lessons_review_due", DEGRADED, f"教训库不可读：{exc}")
    active_n = sum(1 for lesson in loaded if lesson.status == "active")
    due = [
        lesson
        for lesson in loaded
        if lesson.status != "archived"
        and lesson.review_due is not None
        and lesson.review_due <= today
    ]
    if due:
        ids = "、".join(lesson.id for lesson in due)
        detail = (
            f"复审到期 {len(due)} 条：{ids}"
            + "（只报不改；12 月未复现→建议降置信，翻转/归档=人改）"
        )
        return CheckItem("lessons_review_due", DEGRADED, detail)
    return CheckItem(
        "lessons_review_due",
        OK,
        f"{len(loaded)} 条在库 · active {active_n} · 复审到期 0",
    )


def check_rag_index_lag(
    conn: sqlite3.Connection,
    store: CorpusStore,
    *,
    lessons_dir: Path | None = None,
) -> CheckItem:
    """
    项 25 RAG 两索引落后条数（vision-build 票 06；只读红线：零 embed 零写入）。

    教训库（docs/plans/lessons/）与卫报语料（bronze）在当前模型口径下
    未嵌条数=增量入口（CLI rag-embed）该跑的量。教训库小（>0 即黄）；
    卫报大库 >500 黄（真回填前的常态黄=待办可见）。教训库不可读=黄
    （项 22 同款兜底）。晨检只报不嵌（rag.pending_count 只数行；
    模型身份串经 rag.current_model_id() 零成本取用，不构造模型）。
    """
    model_id = rag.current_model_id()
    try:
        lesson_items = rag.lesson_texts(lessons_dir)
    except (lessons.LessonFormatError, OSError, RuntimeError) as exc:
        return CheckItem("rag_index_lag", DEGRADED, f"教训库不可读：{exc}")
    lesson_lag = rag.pending_count(
        conn,
        namespace=rag.NAMESPACE_LESSON,
        items=lesson_items,
        model_id=model_id,
    )
    guardian_lag = rag.pending_count(
        conn,
        namespace=rag.NAMESPACE_GUARDIAN,
        items=rag.guardian_article_texts(store),
        model_id=model_id,
    )
    detail = (
        f"落后条数（{model_id}）：教训 {lesson_lag} / 卫报 {guardian_lag}"
        + "（增量入口：goalx rag-embed --namespace lessons|guardian）"
    )
    if lesson_lag > 0 or guardian_lag > _RAG_GUARDIAN_LAG_DEGRADED:
        return CheckItem("rag_index_lag", DEGRADED, detail)
    return CheckItem("rag_index_lag", OK, detail)


def check_contraction_clocks(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> CheckItem:
    """
    项 23 收缩双钟（vision-build 票 08）：触发告警面，黄不红待人 CLI。

    钟只建议不执行（prereg 附录 B）：主钟/探索钟越门且现级未及建议级
    =黄（待人 CLI 落痕）；已收缩至建议级或更深=绿（钟读数照报，告警
    已处置）；insufficient/unanchored=绿只报。只读——读数账由人 CLI/
    月读写（晨检红线）。
    """
    resolved = now or datetime.now(UTC)
    clv = contraction.clv_clock_reading(conn, now=resolved)
    exploration = contraction.exploration_clock_reading(conn, now=resolved)
    level = contraction.current_level(conn)
    clv_face = (
        f"mean={clv.mean:.4f},n={clv.n}" if clv.mean is not None else f"n={clv.n}"
    )
    parts = [
        f"级={level.value}",
        f"主钟={'触发' if clv.fired else clv.status}({clv_face})",
        f"探索钟={'触发' if exploration.fired else exploration.status}"
        + f"(幸存者={exploration.survivors},发现={exploration.new_findings})",
    ]
    pending: list[str] = []
    if clv.fired and contraction.level_rank(level) < contraction.level_rank(
        contraction.ContractionLevel.L1
    ):
        pending.append(f"主钟建议 {contraction.ContractionLevel.L1.value}")
    if exploration.fired and contraction.level_rank(level) < contraction.level_rank(
        contraction.ContractionLevel.L2
    ):
        pending.append(f"探索钟建议 {contraction.ContractionLevel.L2.value}")
    if pending:
        parts.append("待人 CLI：" + "、".join(pending))
    return CheckItem(
        "contraction_clocks", DEGRADED if pending else OK, "；".join(parts)
    )


def check_rescan_signals(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> CheckItem:
    """
    项 24 四信号重扫唤醒（vision-build 票 08）：触发=黄（人裁执行重扫）。

    信号触发只告警不自动恢复（prereg 附录 C——恢复仍走人 CLI 相邻步进）；
    未接线面如实 unwired（不伪造绿）。只读。
    """
    resolved = now or datetime.now(UTC)
    faces = contraction.rescan_signals(conn, now=resolved)
    parts = [f"{face.name}={face.status}" for face in faces]
    fired = [face for face in faces if face.status == "fired"]
    if fired:
        parts.append("唤醒重扫（人裁执行）")
    return CheckItem("rescan_signals", DEGRADED if fired else OK, "；".join(parts))


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
    """跑全部 25 项（只读；网络面第 10 项官方当期端点 + 第 13 项本机 /healthz）。"""
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
                check_alias_coverage(
                    conn, business_date=fx_store.beijing_business_date(resolved_now)
                ),
                check_bets(conn),
                check_paper_account(conn),
                check_freshness_shadow(conn, now=resolved_now),
                check_edge_scan(conn, now=resolved_now),
                check_prematch_snapshots(store, now=resolved_now),
                check_anchor_sentinel(conn, now=resolved_now),
                check_news_pit_beats(store, now=resolved_now),
                check_lessons_review_due(now=resolved_now),
                check_contraction_clocks(conn, now=resolved_now),
                check_rescan_signals(conn, now=resolved_now),
                check_rag_index_lag(conn, store, lessons_dir=lessons.lessons_dir()),
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
        # 项 19 源T 当季锚书入库节奏（票 19）：短命只读 duck 桥（reader 缺席/
        # 被写方锁占降级 None → 黄）；放队尾——duck 连接不与 sqlite 项交叠。
        with corpus_duckdb.reader(settings) as duck_con:
            items.append(check_srct_season_rhythm(duck_con, now=resolved_now))
    finally:
        conn.close()
        store.close()
    return items
