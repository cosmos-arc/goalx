"""
源T silver 增量物化（票 11，A 线参考链运维）。

2026-10-22 二轮报告裁决顺带诊断的定稿：srct-silver/srct-odds 物化无调度
纯手动（全量 srct-odds ≥3h20m，夜班新采永远进不了语料）。本模块给
fixture_universe + odds_change_event 两条 A 线依赖面接增量 + 定拍
（deployment `srct-remat` 08:10，夜班 01:00-08:00 收口后、23:30 wrap 前）。

增量语义（与全量维护口径的字节级契约）：

- **判据**：checkpoint `raw_artifacts` 提交序高水位（rowid，见
  corpus_store srct_remat_state 注释）——快照后新提交的 raw 工件定
  增量；夜班/当期班拍键（`sid@open` 等）剥后缀归 sid 一并入账。
- **odds 增量**：新工件 sid（含新日页解锁的孤儿 sid，`_unknown` 随新
  日页恒入触及集）按现行 fixture 元数据归 (season, competition) 分区；
  触及分区内的**全部** sid 经前缀扫描过滤账本重导（选取/排序/转换/
  写入与全量同一路径）——触及分区的输出与全量重建逐字节一致，未触及
  分区零改写。分区级记账（门④恒等式等）口径不变、范围=本轮触及集。
- **fixture 全量维持**：build_fixture_universe 实测分钟级以下
  （fixture_rows 0.82s @ 4.8 万行真树），不另做增量（票面"先量后裁"）。
  xg_observation 同为全量但按水位门控（match_stats 有新工件才重建，
  实测读取面 ~2.5 分钟）。
- **护栏→全量兜底**：无状态行（首次）/ bronze sid 计数与水位加账不平
  （无 raw 佐证的 bronze 变动=中断回补/外部重建）/ 解析器版本漂移 /
  夜班摘要报 bronze_repaired——一律走全量（bookmaker+odds 同
  `goalx srct-odds` 现行为），增量只做有判据可解的推进。
- **幂等**：零新工件时秒级返回、silver 树零改写（fixture/xg/odds 全
  门控跳过）；水位仅整轮成功后推进。
- **降级不打断**：单面失败记 degraded 继续其余面；任一面失败不推进
  水位（下轮重做，全路径幂等）。odds 增量中途异常时触及分区可能留
  "部分推进"中间态（单分区文件仍原子完整、树永不损坏，仅本轮未收口，
  ≤一天窗口由下轮重写自愈）。增量面落 `_incremental.json`
  （树侧审计），全量 `_meta.json` 只由全量路径写。

不在本票范围（如实记）：market_quote/match_detail/match_analysis 三面
维持手动全量（非 A 线依赖）；bookmaker 字典增量随全量维护口径刷新；
duckdb 桥视图为路径绑定（分区重写即时可见，无需重建）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import cast

import psutil
from loguru import logger

from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore, RawArtifact
from goalx_backend.data.ingest import srct, srct_odds, srct_silver
from goalx_backend.db import utc_now_iso

# 全量物化内存余量下限（票 13）：16GB+6GB swap 常驻栈 ~4.5GB 的机器上，
# 全量 odds 物化实测峰值 ~4.4GB 且有采样间隙尖峰——无人值守护栏全量低于
# 此余量必被 memorystatus SIGKILL（10-22~24 四次实证，静默断链）；`--full`
# 显式人值守不拦。低于即 deferred 降级（水位不推下轮重做），人按停栈
# 安静全量口径处理。
FULL_REMAT_MIN_HEADROOM_BYTES = 6 * 1024**3


def _headroom_bytes() -> int:
    """系统可挪用内存余量（可用物理 + swap 空闲；模块级供测试放宽）。"""
    return psutil.virtual_memory().available + psutil.swap_memory().free


# remat 消费面（水位快照/门控判据均限此集；shift_entry/夜班台账不是 silver 输入）
REMAT_DATASETS: tuple[str, ...] = (
    srct.DAY_DATASET,
    srct.STATS_DATASET,
    *srct.ODDS_FAMILY,
)
_UNKNOWN_PARTITION = ("_unknown", "_unknown")


@dataclass
class RematReport:
    """一轮增量物化摘要（deployment 日志/晨检口径；odds 面另有树侧 _incremental.json）。"""  # noqa: E501

    # odds 面五态：skip=零新工件 | incremental | full（护栏/首跑/强制）|
    # degraded（增量中途异常——触及分区或部分推进，水位不推进下轮重做）|
    # deferred（票 13 护栏全量余量不足——零写入早退，停栈安静全量后人跑）
    mode: str = "skip"
    guard_reason: str = ""  # mode=full 时的护栏命中原因（人读）
    watermark: str = ""  # 本轮起点快照（max fetched_at 审计面）
    last_rowid: int = 0  # 本轮起点提交序高水位
    new_raw: dict[str, int] = field(default_factory=dict)  # 各数据集新工件数
    affected_partitions: list[str] = field(default_factory=list)
    target_sids: int = 0
    fixture: dict[str, object] = field(default_factory=dict)  # 全量报告/空=跳过
    xg: dict[str, object] = field(default_factory=dict)
    odds: dict[str, object] = field(default_factory=dict)
    bookmaker: dict[str, object] = field(default_factory=dict)  # 仅全量路径
    degraded: list[str] = field(default_factory=list)  # 失败面留痕（不打断）
    built_at: str = ""


def _partition_of(sid: str, meta: dict[str, dict[str, object]]) -> tuple[str, str]:
    """Sid → 现行 (season, competition) 分区（无 fixture 元数据归 _unknown）。"""
    fixture = meta.get(sid)
    if fixture is None:
        return _UNKNOWN_PARTITION
    kickoff = cast("datetime", fixture["kickoff"])
    return (silver.season_of(kickoff), str(fixture["league"]))


def _count_guard_reason(
    store: CorpusStore,
    new_raw: dict[str, list[RawArtifact]],
    recorded: dict[str, dict[str, object]],
) -> str:
    """
    Bronze 计数护栏：任一 odds 族数据集出现水位无法解释的 bronze 变动。

    判据（每数据集独立）：版本漂移 / 计数倒退（外部重建）/ 增量超过新
    工件数可解释上限（"无 raw 佐证"的新 bronze sid——正常写径每个新
    bronze sid 至少占一个新 raw 键；中断回补不吃新键）。命中即返回人读
    原因，调用方落全量兜底。残留窗口（手动回补与既有 sid 拍键同窗混合）
    记票 Answer，全量维护口径兜底。
    """
    for dataset in srct.ODDS_FAMILY:
        rec = recorded.get(dataset)
        if rec is None:
            continue  # 该数据集无前账（首跑整体已走全量，此处不重复拦）
        version = srct.BRONZE_VERSIONS[dataset]
        count = store.bronze_sid_count(srct.SRCT_PROVIDER, dataset, version)
        rec_version = str(rec.get("version"))
        rec_count = int(cast("int", rec.get("count", -1)))
        new_keys = len(new_raw.get(dataset, []))
        if rec_version != version:
            return f"{dataset}: 解析器版本 {rec_version}→{version}"
        if count < rec_count:
            return f"{dataset}: bronze sid 计数倒退 {rec_count}→{count}（外部重建？）"
        if count > rec_count + new_keys:
            return (
                f"{dataset}: bronze 增量 {count - rec_count} 超新工件 {new_keys}"
                "（无 raw 佐证的回补/重建）"
            )
    return ""


def _night_repaired_since(store: CorpusStore, since: str) -> bool:
    """
    快照窗内有夜班 bronze 回补（回补不吃新 raw 键，水位判据结构性不可见）。

    窗判据=摘要 ended_at > 上次状态行 updated_at：跨过上次 remat 的夜班
    （started_at 在前、ended_at 在后）也入窗——started_at 判据会漏掉
    overrun 过 08:10 的夜班回补（correctness-review F4），误触发方向
    只是多跑一次全量，无害。limit=30 夜对日拍节奏余量充足；超长缺勤窗
    内更早的回补可能漏看（罕见；残留由全量维护口径兜底）。
    """
    for summary in store.night_summaries(limit=30):
        if since and not str(summary["ended_at"]) > since:
            continue
        if cast("int", summary["bronze_repaired"]) > 0:
            return True
    return False


def _write_incremental_meta(
    store: CorpusStore, report: RematReport, odds_report: srct_odds.SilverOddsReport
) -> None:
    """增量运行面 `_incremental.json`（树侧审计；全量 `_meta.json` 不在此写）。"""
    root = store.silver_path(srct.SRCT_PROVIDER, srct_odds.ODDS_DATASET)
    payload: dict[str, object] = {
        "mode": report.mode,
        "built_at": report.built_at,
        "watermark": report.watermark,
        "last_rowid": report.last_rowid,
        "new_raw": report.new_raw,
        "affected_partitions": report.affected_partitions,
        "target_sids": report.target_sids,
        "odds": asdict(odds_report),
    }
    (root / "_incremental.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _run_incremental_odds(
    store: CorpusStore,
    report: RematReport,
    new_raw: dict[str, list[RawArtifact]],
    meta: dict[str, dict[str, object]],
    *,
    live_sids: set[str] | None = None,
) -> None:
    """触及集推导 + 过滤账本 + 分区重写（字节级=全量切片；异常记降级）。"""
    live_sids = live_sids if live_sids is not None else set()
    new_day_dates = {art.key for art in new_raw.get(srct.DAY_DATASET, [])}
    new_odds_sids = {
        art.key.split("@", 1)[0]
        for dataset in srct.ODDS_FAMILY
        for art in new_raw.get(dataset, [])
    }
    day_sids = {sid for sid, row in meta.items() if str(row["day"]) in new_day_dates}
    partitions = {_partition_of(sid, meta) for sid in new_odds_sids | day_sids}
    if day_sids:
        # 新日页解锁孤儿：_unknown 必须重写（迁出行驻留），与迁入分区成对；
        # 重挂场次的"迁移出分区"（旧页联赛/开球界）同并入——排除新日页
        # 复算旧元数据可得其原分区（correctness-review F1：真实↔真实
        # 迁移无 _unknown 式特判，漏触及=旧分区残留该 sid 行，树与全量分叉）
        day_rows = silver.latest_bronze_rows(
            store,
            srct.SRCT_PROVIDER,
            srct.DAY_DATASET,
            srct.BRONZE_VERSIONS[srct.DAY_DATASET],
        )
        old_meta = {
            str(row["sid"]): row
            for row in srct_silver.fixture_rows_from(
                {k: v for k, v in day_rows.items() if k not in new_day_dates}
            )[0]
        }
        partitions.update(_partition_of(sid, old_meta) for sid in day_sids)
    if day_sids or (new_odds_sids & live_sids):
        # _unknown 成对重写（票 11 F1 语义 + 票 03 扩充）：新日页解锁或
        # 当期班兜底锚定的迁出同样要求 _unknown 清驻留——漏触及=旧分区
        # 残留该 sid 行，树与全量分叉
        partitions.add(_UNKNOWN_PARTITION)
    report.affected_partitions = sorted(
        f"season={season}/competition={competition}"
        for season, competition in partitions
    )
    ledgers: dict[str, dict[str, int]] = {}
    for dataset in srct.ODDS_FAMILY:
        version = srct.BRONZE_VERSIONS[dataset]
        bronze_set = store.bronze_sids(srct.SRCT_PROVIDER, dataset, version)
        targets = {sid for sid in bronze_set if _partition_of(sid, meta) in partitions}
        report.target_sids += len(targets)
        ledgers[dataset] = silver.latest_bronze_ledger_filtered(
            store, srct.SRCT_PROVIDER, dataset, version, targets
        )
    try:
        odds_report = srct_odds.build_odds_change_events(
            store, ledgers=ledgers, clean_stale=False, write_meta=False, meta=meta
        )
        _prune_drained_partitions(store, partitions, odds_report.partition_keys)
    except Exception as exc:
        logger.warning("srct-remat odds 增量降级：{}", exc)
        report.degraded.append(f"odds_incremental: {type(exc).__name__}")
        report.mode = "degraded"
        return
    report.odds = asdict(odds_report)
    _write_incremental_meta(store, report, odds_report)


def _prune_drained_partitions(
    store: CorpusStore,
    affected: set[tuple[str, str]],
    written_keys: list[str],
) -> None:
    """
    触及集内零行分区的定向清理（全量 remove_stale 语义的增量切片）。

    触及=本轮判定需要重导的分区；产出零行（孤儿全部迁出/联赛面清空）时
    全量重建不会留下该分区目录，增量同样必须删——未触及分区绝不动。
    删除核=silver.delete_partition_dir（与 remove_stale 一份语义）。
    """
    drained: set[tuple[str, str]] = set()
    for key in written_keys:
        season_w, _, competition_w = key.partition("/")
        drained.add((season_w, competition_w))
    root = store.silver_path(srct.SRCT_PROVIDER, srct_odds.ODDS_DATASET)
    for season, competition in affected - drained:
        silver.delete_partition_dir(
            root / f"season={season}" / f"competition={competition}"
        )


def _guard_reason(
    store: CorpusStore,
    *,
    full: bool,
    state: dict[str, object] | None,
    new_raw: dict[str, list[RawArtifact]],
    recorded: dict[str, dict[str, object]],
) -> str:
    """全量兜底触发原因（空串=增量可解）。"""
    if full:
        return "强制全量（--full）"
    if state is None:
        return "无状态行（首次运行）"
    count_reason = _count_guard_reason(store, new_raw, recorded)
    if count_reason:
        return count_reason
    if _night_repaired_since(store, str(state["updated_at"])):
        return "夜班摘要报 bronze_repaired"
    return ""


def _deferred_low_headroom(
    report: RematReport, *, full: bool, headroom_fn: Callable[[], int] | None
) -> bool:
    """票 13：护栏全量前余量探测（--full 显式人值守不拦）；命中写降级返 True。"""
    if full or not report.guard_reason:
        return False
    try:
        headroom = headroom_fn() if headroom_fn is not None else _headroom_bytes()
    except OSError:
        # 探测自身不许成为 08:10 无人值守路径的新死因（票 13 correctness
        # F1）——fail-open 照走全量，宁可回到护栏前行为也不静默断链
        logger.warning("srct-remat 余量探测失败（OSError），跳过余量检查照走全量")
        return False
    if headroom >= FULL_REMAT_MIN_HEADROOM_BYTES:
        return False
    report.mode = "deferred"
    report.degraded.append(f"full_deferred_low_headroom: {headroom / 1024**3:.1f}GiB")
    logger.warning(
        "srct-remat 护栏全量推迟：余量 {:.1f}GiB < {}GiB（停栈安静全量；水位不推下轮重做）",  # noqa: E501
        headroom / 1024**3,
        FULL_REMAT_MIN_HEADROOM_BYTES // 1024**3,
    )
    return True


def run_remat(
    store: CorpusStore,
    *,
    full: bool = False,
    headroom_fn: Callable[[], int] | None = None,
) -> RematReport:
    """
    一轮增量物化：水位快照 → 门控重建（fixture/xg/odds）→ 成功才推水位。

    full=True 强制全量（维护口径；护栏命中自动同路）。store 由调用方开合
    （cli/flow 共用；测试注入临时树）。headroom_fn 注入余量探测（测试用；
    缺省真实 psutil——护栏场景余量不足全量直接 deferred，见票 13）。
    """
    report = RematReport(built_at=utc_now_iso())
    state = store.remat_state()
    recorded: dict[str, dict[str, object]] = (
        json.loads(str(state["bronze_sid_counts"])) if state else {}
    )
    last_rowid = cast("int", state["last_rowid"]) if state else 0
    # 起点快照先于任何读取/重建：跑动中抵达的行 rowid 更大，下轮必可见
    report.last_rowid, report.watermark = store.raw_artifacts_high_water(
        srct.SRCT_PROVIDER
    )
    new_raw = store.raw_artifacts_since(srct.SRCT_PROVIDER, REMAT_DATASETS, last_rowid)
    report.new_raw = {dataset: len(rows) for dataset, rows in new_raw.items()}

    report.guard_reason = _guard_reason(
        store, full=full, state=state, new_raw=new_raw, recorded=recorded
    )
    # 票 13：无人值守护栏全量先探余量，不足即 deferred——早退于任何阶段
    # 写之前，语料停上一份完好态，摘要可见
    if _deferred_low_headroom(report, full=full, headroom_fn=headroom_fn):
        return report
    day_new = bool(new_raw.get(srct.DAY_DATASET))
    stats_new = bool(new_raw.get(srct.STATS_DATASET))
    odds_new = any(new_raw.get(dataset) for dataset in srct.ODDS_FAMILY)

    # ---- fixture（全量维持，门控：day_page 有新工件） ----
    if day_new or full:
        try:
            report.fixture = asdict(srct_silver.build_fixture_universe(store))
        except Exception as exc:
            logger.warning("srct-remat fixture 降级：{}", exc)
            report.degraded.append(f"fixture: {type(exc).__name__}")
    # 合并元数据（票 03）：日页 fixture 行 ∪ 当期班兜底——增量/全量同出处
    meta = {str(row["sid"]): row for row in srct_silver.fixture_rows_with_live(store)}
    live_sids = {str(row["sid"]) for row in srct_silver.shift_live_rows(store)} - {
        str(row["sid"]) for row in srct_silver.fixture_rows(store)[0]
    }

    # ---- xg（全量路径，门控：match_stats 有新工件） ----
    if stats_new or full:
        try:
            report.xg = asdict(srct_silver.build_xg_observations(store))
        except Exception as exc:
            logger.warning("srct-remat xg 降级：{}", exc)
            report.degraded.append(f"xg: {type(exc).__name__}")

    # ---- odds（增量主路径 / 全量兜底 / 零新工件跳过） ----
    if report.guard_reason:
        report.mode = "full"
        try:
            report.bookmaker = asdict(srct_odds.build_bookmakers(store))
            report.odds = asdict(srct_odds.build_odds_change_events(store))
        except Exception as exc:
            logger.warning("srct-remat odds 全量降级：{}", exc)
            report.degraded.append(f"odds_full: {type(exc).__name__}")
    elif odds_new or day_new:
        report.mode = "incremental"
        _run_incremental_odds(store, report, new_raw, meta, live_sids=live_sids)

    if report.degraded:
        logger.warning(
            "srct-remat 有降级面 {}：水位不推进（下轮重做）", report.degraded
        )
        return report
    counts: dict[str, dict[str, object]] = {
        dataset: {
            "version": srct.BRONZE_VERSIONS[dataset],
            "count": store.bronze_sid_count(
                srct.SRCT_PROVIDER, dataset, srct.BRONZE_VERSIONS[dataset]
            ),
        }
        for dataset in srct.ODDS_FAMILY
    }
    store.upsert_remat_state(
        report.watermark, report.last_rowid, json.dumps(counts, ensure_ascii=False)
    )
    return report
