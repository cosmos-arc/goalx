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
  水位（下轮重做，全路径幂等）。增量面落 `_incremental.json`
  （树侧审计），全量 `_meta.json` 只由全量路径写。

不在本票范围（如实记）：market_quote/match_detail/match_analysis 三面
维持手动全量（非 A 线依赖）；bookmaker 字典增量随全量维护口径刷新；
duckdb 桥视图为路径绑定（分区重写即时可见，无需重建）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import cast

from loguru import logger

from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds, srct_silver
from goalx_backend.db import utc_now_iso

# remat 消费面（水位快照/门控判据均限此集；shift_entry/夜班台账不是 silver 输入）
REMAT_DATASETS: tuple[str, ...] = (
    srct.DAY_DATASET,
    srct.STATS_DATASET,
    srct.ODDS_DATASET,
    srct.HANDICAP_DATASET,
    srct.ASIANODDS_DATASET,
    srct.OVERDOWN_DATASET,
)
ODDS_FAMILY: tuple[str, ...] = (
    srct.ODDS_DATASET,
    srct.HANDICAP_DATASET,
    srct.ASIANODDS_DATASET,
    srct.OVERDOWN_DATASET,
)
_UNKNOWN_PARTITION = ("_unknown", "_unknown")


@dataclass
class RematReport:
    """一轮增量物化摘要（deployment 日志/晨检口径；odds 面另有树侧 _incremental.json）。"""  # noqa: E501

    mode: str = "skip"  # odds 面：skip=零新工件 | incremental | full（护栏/首跑/强制）
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
    new_raw: dict[str, list[tuple[str, str, str]]],
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
    for dataset in ODDS_FAMILY:
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
    """快照窗内有夜班 bronze 回补（回补不吃新 raw 键，水位判据结构性不可见）。"""
    for summary in store.night_summaries(limit=30):
        if since and str(summary["started_at"]) < since:
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
    new_raw: dict[str, list[tuple[str, str, str]]],
    meta: dict[str, dict[str, object]],
) -> None:
    """触及集推导 + 过滤账本 + 分区重写（字节级=全量切片；异常记降级上抛面）。"""
    new_day_dates = {key for key, _, _ in new_raw.get(srct.DAY_DATASET, [])}
    new_odds_sids = {
        key.split("@", 1)[0]
        for dataset in ODDS_FAMILY
        for key, _, _ in new_raw.get(dataset, [])
    }
    day_sids = {sid for sid, row in meta.items() if str(row["day"]) in new_day_dates}
    partitions = {_partition_of(sid, meta) for sid in new_odds_sids | day_sids}
    if day_sids:
        # 新日页解锁孤儿：_unknown 必须重写（迁出行驻留），与迁入分区成对
        partitions.add(_UNKNOWN_PARTITION)
    report.affected_partitions = sorted(
        f"season={season}/competition={competition}"
        for season, competition in partitions
    )
    ledgers: dict[str, dict[str, int]] = {}
    for dataset in ODDS_FAMILY:
        version = srct.BRONZE_VERSIONS[dataset]
        bronze_set = store.bronze_sids(srct.SRCT_PROVIDER, dataset, version)
        targets = {sid for sid in bronze_set if _partition_of(sid, meta) in partitions}
        report.target_sids += len(targets)
        ledgers[dataset] = silver.latest_bronze_ledger_filtered(
            store, srct.SRCT_PROVIDER, dataset, version, targets
        )
    try:
        odds_report = srct_odds.build_odds_change_events(
            store, ledgers=ledgers, clean_stale=False, write_meta=False
        )
    except Exception as exc:
        logger.warning("srct-remat odds 增量降级：{}", exc)
        report.degraded.append(f"odds_incremental: {type(exc).__name__}")
        report.mode = "degraded"
        return
    _prune_drained_partitions(store, partitions, odds_report.partition_keys)
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
    """
    written = set(written_keys)
    drained: set[tuple[str, str]] = set()
    for key in written:
        season_w, _, competition_w = key.partition("/")
        drained.add((season_w, competition_w))
    for season, competition in affected - drained:
        part = (
            store.silver_path(srct.SRCT_PROVIDER, srct_odds.ODDS_DATASET)
            / (f"season={season}")
            / f"competition={competition}"
        )
        if not part.exists():
            continue
        for file in part.iterdir():
            file.unlink()
        part.rmdir()
        season_dir = part.parent
        if season_dir.exists() and not any(season_dir.iterdir()):
            season_dir.rmdir()


def _guard_reason(
    store: CorpusStore,
    *,
    full: bool,
    state: dict[str, object] | None,
    new_raw: dict[str, list[tuple[str, str, str]]],
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


def run_remat(store: CorpusStore, *, full: bool = False) -> RematReport:
    """
    一轮增量物化：水位快照 → 门控重建（fixture/xg/odds）→ 成功才推水位。

    full=True 强制全量（维护口径；护栏命中自动同路）。store 由调用方开合
    （cli/flow 共用；测试注入临时树）。
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
    day_new = bool(new_raw.get(srct.DAY_DATASET))
    stats_new = bool(new_raw.get(srct.STATS_DATASET))
    odds_new = any(new_raw.get(dataset) for dataset in ODDS_FAMILY)

    # ---- fixture（全量维持，门控：day_page 有新工件） ----
    if day_new or full:
        try:
            report.fixture = asdict(srct_silver.build_fixture_universe(store))
        except Exception as exc:
            logger.warning("srct-remat fixture 降级：{}", exc)
            report.degraded.append(f"fixture: {type(exc).__name__}")
    meta = {str(row["sid"]): row for row in srct_silver.fixture_rows(store)[0]}

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
        _run_incremental_odds(store, report, new_raw, meta)

    if report.degraded:
        logger.warning(
            "srct-remat 有降级面 {}：水位不推进（下轮重做）", report.degraded
        )
        return report
    counts = {
        dataset: {
            "version": srct.BRONZE_VERSIONS[dataset],
            "count": store.bronze_sid_count(
                srct.SRCT_PROVIDER, dataset, srct.BRONZE_VERSIONS[dataset]
            ),
        }
        for dataset in ODDS_FAMILY
    }
    store.upsert_remat_state(
        report.watermark, report.last_rowid, json.dumps(counts, ensure_ascii=False)
    )
    return report
