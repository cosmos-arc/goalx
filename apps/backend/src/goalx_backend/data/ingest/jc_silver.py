"""
jc silver 切片（票 72）：jc_sp_change_event 变化事件流。

语义与书商层 odds_change_event **同构**（spec 69 story 16——消费端一套
查询口径跨两层）：

- **一行=一次官方 SP 变化**：按 (match_id, playtype, outcome[, goal_line])
  轨迹内 (published_at, source_order) 升序，值组与上一保留行全等丢弃——
  A→B→A 回摆保留、首条自然保留、末条同值心跳丢弃；
- **值组**：had/hhad=三价（hhad 另含 goalLine——**盘口变=事件**，非仅
  SP 变，story 19）；ttg=长格式 outcome 行（s0~s7 八档，值组=该档 SP，
  story 20）；
- **published_at**=updateDate+updateTime 北京钟面固定 UTC+8；**observed_at**
  =bronze fetched_at；source_order=源列表行序（同刻确定性并列规则）；
- **门④记账**：源行 = 事件 + 心跳 + 坏时间行 + 未解释缺口（必须 0）。

输入=bronze sp_history 当前版本 latest-per-matchId（拍/收口行同 sid 共存，
后行胜出=收口全量轨迹）。写入=单 parquet 文件（量级：十年 ≈4 万场 ×
几十事件，单文件有界）；确定性排序 → 幂等重建字节级一致（先例
test_ingest_srct_market）。kickoff/league 冗余列来自 jc_shift 台账与
bronze 身份字段（历史回填场 kickoff 可空）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 srct_silver/srct_odds 先例）

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, srct_silver
from goalx_backend.db import utc_now_iso

SP_EVENT_DATASET = "jc_sp_change_event"
SILVER_VERSION = "silver_jc_sp_v1"
_BEIJING = timezone(timedelta(hours=8))
_TTG_OUTCOMES = tuple(f"s{i}" for i in range(8))
_UNSORTABLE_MS = 1 << 62  # 不可解时间排序垫底（随后按 bad_time 跳行）

_SCHEMA = pa.schema(
    [
        pa.field("match_id", pa.string()),
        pa.field("playtype", pa.string()),  # had / hhad / ttg
        pa.field("outcome", pa.string()),  # ttg 档位（0~7）；had/hhad 空
        pa.field("goal_line", pa.float64()),  # hhad 盘口（受让为负，源值直读）
        pa.field("published_at", pa.timestamp("ms", tz="UTC")),  # 官方发布钟面
        pa.field("observed_at", pa.timestamp("ms", tz="UTC")),  # bronze fetched_at
        pa.field("source_order", pa.int32()),  # 源列表行序（同刻并列规则）
        pa.field("odds_h", pa.float64()),
        pa.field("odds_d", pa.float64()),
        pa.field("odds_a", pa.float64()),
        pa.field("sp", pa.float64()),  # ttg 档位 SP（长格式）
        pa.field("kickoff", pa.timestamp("ms", tz="UTC")),  # 冗余列（台账可缺）
        pa.field("league", pa.string()),
    ]
)


@dataclass
class SilverJcSpReport:
    """一次 jc_sp_change_event 重物化报告（含门④记账）。"""

    silver_version: str = SILVER_VERSION
    matches: int = 0
    events_had: int = 0
    events_hhad: int = 0
    events_ttg: int = 0
    heartbeat_dropped: int = 0
    bad_time_rows: int = 0  # updateDate/Time 不可解（不落事件留痕）
    source_rows: int = 0  # 记账分母（had+hhad 逐行；ttg 逐行×八档）
    unexplained_gap: int = 0  # 门④：必须为 0
    same_minute_conflicts: int = 0  # 同 published_at 多行组数（并列规则触发面）
    trajectories: int = 0  # 至少一条事件的 (场×玩法×档位) 轨迹数
    built_at: str = ""


def _published_ms(update_date: object, update_time: object) -> int | None:
    """updateDate('YYYY-MM-DD')+updateTime('HH:MM:SS') 北京钟面 → epoch ms。"""
    try:
        naive = datetime.strptime(f"{update_date} {update_time}", "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return int(naive.replace(tzinfo=_BEIJING).timestamp() * 1000)


def _iso_ms(raw: object) -> int | None:
    """ISO 串（fetched_at/kickoff_utc）→ epoch ms；不可解 None。"""
    try:
        return int(datetime.fromisoformat(str(raw)).timestamp() * 1000)
    except ValueError:
        return None


def _to_float(value: object) -> float | None:
    """SP 数值化（'2.30'→2.3；≤0 的封盘占位/坏值 None）。"""
    parsed = _to_float_any(value)
    return parsed if parsed is not None and parsed > 0 else None


def _to_float_any(value: object) -> float | None:
    """数值化（负值合法——hhad goalLine 受让为负；空/坏值 None）。"""
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _keep_value_changes(
    rows: list[dict[str, object]],
    report: SilverJcSpReport,
    value_of: Callable[[dict[str, object]], tuple[object, ...]],
) -> list[dict[str, object]]:
    """
    去重核（与 odds_change_event 同构）：值组与上一保留行全等丢弃。

    A→B→A 保留、首条自然保留、末条同值心跳丢弃；不可解时间行不落事件
    （bad_time 记账）。
    """
    rows.sort(
        key=lambda r: (
            r["published_ms"] if r["published_ms"] is not None else _UNSORTABLE_MS,
            r["source_order"],
        )
    )
    kept: list[dict[str, object]] = []
    last_values: tuple[object, ...] | None = None
    for row in rows:
        if row["published_ms"] is None:
            report.bad_time_rows += 1
            continue
        values = value_of(row)
        if values == last_values:
            report.heartbeat_dropped += 1
            continue
        last_values = values
        kept.append(row)
    return kept


def _note_same_minute(
    rows: list[dict[str, object]],
    value_of: Callable[[dict[str, object]], tuple[object, ...]],
    report: SilverJcSpReport,
) -> None:
    """同 published_ms 且组内值组 ≥2 种的组数（同构口径：同值心跳不计）。"""
    groups: dict[int, int] = {}
    distinct: dict[int, set[tuple[object, ...]]] = {}
    for row in rows:
        ms = row["published_ms"]
        if not isinstance(ms, int):  # 坏时间行不进并列口径（bad_time 已另记）
            continue
        groups[ms] = groups.get(ms, 0) + 1
        distinct.setdefault(ms, set()).add(value_of(row))
    report.same_minute_conflicts += sum(
        1 for ms, count in groups.items() if count > 1 and len(distinct[ms]) > 1
    )


def _emit_trajectory(
    trajectory: list[dict[str, object]],
    playtype: str,
    outcome: str | None,
    report: SilverJcSpReport,
    value_of: Callable[[dict[str, object]], tuple[object, ...]],
) -> list[dict[str, object]]:
    """一条轨迹 → 事件行（had/hhad 与 ttg 档位流共用形状）。"""
    _note_same_minute(trajectory, value_of, report)
    kept = _keep_value_changes(trajectory, report, value_of)
    for _ in kept:
        if playtype == "hhad":
            report.events_hhad += 1
        elif playtype == "had":
            report.events_had += 1
        else:
            report.events_ttg += 1
    return [
        {
            "match_id": source["match_id"],
            "playtype": playtype,
            "outcome": outcome,
            "goal_line": cast("float | None", source.get("goal_line")),
            "published_at": source["published_ms"],
            "observed_at": source.get("observed_ms"),
            "source_order": source["source_order"],
            "odds_h": source.get("h"),
            "odds_d": source.get("d"),
            "odds_a": source.get("a"),
            "sp": source.get("sp"),
            "kickoff": source.get("kickoff_ms"),
            "league": source.get("league") or "",
        }
        for source in kept
    ]


def _parse_had_like(
    playtype: str,
    rows: list[dict[str, object]],
    report: SilverJcSpReport,
) -> list[dict[str, object]]:
    """had/hhad 源行族 → 按 match_id 分轨迹 → 事件行。"""

    def value_of(source: dict[str, object]) -> tuple[object, ...]:
        triple = (source.get("h"), source.get("d"), source.get("a"))
        if playtype == "hhad":
            return (*triple, source.get("goal_line"))
        return triple

    by_match: dict[str, list[dict[str, object]]] = {}
    for source in rows:
        report.source_rows += 1
        by_match.setdefault(str(source["match_id"]), []).append(source)
    out: list[dict[str, object]] = []
    for match_id in sorted(by_match):
        out.extend(
            _emit_trajectory(by_match[match_id], playtype, None, report, value_of)
        )
    return out


def _parse_ttg(
    rows: list[dict[str, object]],
    report: SilverJcSpReport,
) -> list[dict[str, object]]:
    """Ttg 源行族 → (match_id, 档位) 八档独立轨迹 → 长格式事件行。"""

    def value_of(stream: dict[str, object]) -> tuple[object, ...]:
        return (stream.get("sp"),)

    by_stream: dict[tuple[str, str], list[dict[str, object]]] = {}
    for source in rows:
        for key in _TTG_OUTCOMES:
            report.source_rows += 1  # 八档各记一行（长格式分母口径）
            stream = {**source, "sp": source.get(key)}
            by_stream.setdefault((str(source["match_id"]), key[1:]), []).append(stream)
    out: list[dict[str, object]] = []
    for match_id, outcome in sorted(by_stream):
        out.extend(
            _emit_trajectory(
                by_stream[(match_id, outcome)], "ttg", outcome, report, value_of
            )
        )
    return out


def _sort_key(row: dict[str, object]) -> tuple[object, ...]:
    """确定性终序（重建字节级一致的前提）。"""

    def as_int(value: object) -> int:
        return int(str(value)) if value is not None else 0

    return (
        str(row["match_id"]),
        str(row["playtype"]),
        str(row["outcome"] or ""),
        as_int(row["published_at"]),
        as_int(row["source_order"]),
    )


def build_sp_change_events(store: CorpusStore) -> SilverJcSpReport:
    """
    重物化 silver jc_sp_change_event（幂等；bronze sp_history latest 行）。

    latest-per-matchId：拍/收口行同 sid 共存，文件后行胜出（收口全量
    轨迹天然覆盖在售期拍）。had→hhad→ttg 流出序即文件终序。
    """
    report = SilverJcSpReport(built_at=utc_now_iso())
    latest: dict[str, dict[str, object]] = {}
    for row in store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET):
        if row.get("parser_version") != jc.BRONZE_VERSION:
            continue
        latest[str(row["sid"])] = row  # append-only 后行胜出
    kickoffs = _kickoff_ms(store)
    had_rows: list[dict[str, object]] = []
    hhad_rows: list[dict[str, object]] = []
    ttg_rows: list[dict[str, object]] = []
    for match_id in sorted(latest):
        bronze = latest[match_id]
        report.matches += 1
        payload = bronze.get("payload")
        if not isinstance(payload, dict):
            continue
        odds_history = cast("dict[str, object]", payload.get("oddsHistory") or {})
        base = {
            "match_id": match_id,
            "league": str(odds_history.get("leagueAbbName") or ""),
            "kickoff_ms": kickoffs.get(match_id),
            "observed_ms": _iso_ms(bronze.get("fetched_at")),
        }
        for playtype, shelf in (("had", had_rows), ("hhad", hhad_rows)):
            for idx, raw in enumerate(
                cast(
                    "list[dict[str, object]]", odds_history.get(f"{playtype}List") or []
                )
            ):
                shelf.append(
                    {
                        **base,
                        "source_order": idx,
                        "published_ms": _published_ms(
                            raw.get("updateDate"), raw.get("updateTime")
                        ),
                        "h": _to_float(raw.get("h")),
                        "d": _to_float(raw.get("d")),
                        "a": _to_float(raw.get("a")),
                        "goal_line": _to_float_any(raw.get("goalLine")),
                    }
                )
        for idx, raw in enumerate(
            cast("list[dict[str, object]]", odds_history.get("ttgList") or [])
        ):
            ttg_rows.append(
                {
                    **base,
                    "source_order": idx,
                    "published_ms": _published_ms(
                        raw.get("updateDate"), raw.get("updateTime")
                    ),
                    **{key: _to_float(raw.get(key)) for key in _TTG_OUTCOMES},
                }
            )
    rows_out = [
        *_parse_had_like("had", had_rows, report),
        *_parse_had_like("hhad", hhad_rows, report),
        *_parse_ttg(ttg_rows, report),
    ]
    rows_out.sort(key=_sort_key)
    report.trajectories = len(
        {(r["match_id"], r["playtype"], r["outcome"]) for r in rows_out}
    )
    accounted = (
        report.events_had
        + report.events_hhad
        + report.events_ttg
        + report.heartbeat_dropped
        + report.bad_time_rows
    )
    report.unexplained_gap = report.source_rows - accounted
    root = store.root / "silver" / jc.JC_PROVIDER / SP_EVENT_DATASET
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "data.parquet.tmp"
    pq.write_table(
        pa.Table.from_pylist(rows_out, schema=_SCHEMA), tmp, compression="zstd"
    )
    tmp.replace(root / "data.parquet")
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": SILVER_VERSION,
            "bronze_version": jc.BRONZE_VERSION,
            "built_at": report.built_at,
            "matches": report.matches,
            "events_had": report.events_had,
            "events_hhad": report.events_hhad,
            "events_ttg": report.events_ttg,
            "heartbeat_dropped": report.heartbeat_dropped,
            "bad_time_rows": report.bad_time_rows,
            "unexplained_gap": report.unexplained_gap,
        },
    )
    return report


def _kickoff_ms(store: CorpusStore) -> dict[str, int]:
    """jc_shift 台账 → matchId → 开球 ms（历史回填场缺档不在表）。"""
    out: dict[str, int] = {}
    for match_id, row in store.jc_shift_matches().items():
        raw = row.get("kickoff_utc")
        if raw:
            ms = _iso_ms(raw)
            if ms is not None:
                out[str(match_id)] = ms
    return out
