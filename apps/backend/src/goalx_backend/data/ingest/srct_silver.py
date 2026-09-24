"""
源T silver 层第一张表：fixture_universe（票 55/56 线切片 14）。

silver 四件套（ADR-0011 决策 4）之骨架：从 bronze 批量重物化、带版本戳
（SILVER_VERSION，投影规则变更时递增）、season/competition 分区（季为
外层——查询主裁剪维度是季，切片 14 裁定并回写 ADR）、文件内按 kickoff+sid
排序、重建幂等（整树重写：目标分区写 tmp 后
原子替换，缺席旧分区删除——联赛更名/赛季滚动天然清理）。

fixture_universe（spec story 12）：sid × 联赛 × 赛季 × 开球 × 主客 ×
赛果——轨迹数据的 join 地基、对账的场次全集。唯一输入 = bronze day_page
行（一行=一日 CorpusScope 完场清单），本地重物化零网络。

开球解析（实测钉死，2026-09 真树六日页）：页日期 D 的标签只有两种形态——
`D日HH:MM`（晚间场）与 `(D+1)日HH:MM`（凌晨场）；其余视为异常跳行计数。
开球为站点墙钟（北京），无时区——研究面统一口径使用（ADR-0010 三时间：
published_at 语义）。赛季按开球日 8 月界切（8/1-次年 7/31，与夜班季窗
一致）；欧战资格赛/正赛同名随行——stage 列按开球月窗口派生（7/8 月=
资格赛，9 月起=正赛）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 dc_model/xg_dc 先例）

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct
from goalx_backend.db import utc_now_iso

SILVER_VERSION = "silver_fixture_v1"
FIXTURE_DATASET = "fixture_universe"
SEASON_START_MONTH = 8  # 赛季 8 月界切（与夜班季窗 8/1-7/31 同界）
# 欧战正赛（联赛阶段）9 月开打，7/8 月同名行 = 资格赛（research/20 §九）
EURO_COMPETITIONS = ("欧冠杯", "欧罗巴杯")
EURO_QUALIFIER_MONTHS = (7, 8)
STAGE_LEAGUE = "league"
STAGE_QUALIFIER = "qualifier"
STAGE_MAIN = "main"
_SCORE_PARTS = 2  # "h-a" 两段

_KICKOFF_RE = re.compile(r"(\d{1,2})日(\d{2}):(\d{2})")

_FIXTURE_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("league", pa.string()),
        pa.field("kickoff", pa.timestamp("ms")),  # 站点墙钟（北京），naive
        pa.field("home", pa.string()),
        pa.field("away", pa.string()),
        pa.field("home_goals", pa.int16()),
        pa.field("away_goals", pa.int16()),
        pa.field("stage", pa.string()),  # league/qualifier/main
        pa.field("day", pa.string()),  # 页日期（审计回溯：开球由页日+标签解出）
    ]
)


@dataclass
class SilverFixtureReport:
    """一次 fixture_universe 重物化报告（CLI/coverage 摘要口径）。"""

    silver_version: str = SILVER_VERSION
    rows: int = 0
    day_pages: int = 0  # 消费的 bronze 日页行数
    skipped_anomaly: int = 0  # 开球标签两形态之外的行（跳行留痕）
    partitions: int = 0
    stale_partitions_removed: int = 0
    seasons: dict[str, int] = field(default_factory=dict)
    built_at: str = ""


def resolve_kickoff(day: date, label: str) -> datetime | None:
    """页日期 + 开球标签 → 开球时刻（两形态之外 None，异常留痕）。"""
    found = _KICKOFF_RE.fullmatch(label)
    if found is None:
        return None
    label_day, hour, minute = (int(g) for g in found.groups())
    if label_day == day.day:
        anchor = day
    elif label_day == (day + timedelta(days=1)).day:
        anchor = day + timedelta(days=1)  # 凌晨场：标签日 = 页日期+1
    else:
        return None
    return datetime(anchor.year, anchor.month, anchor.day, hour, minute)


def season_of(kickoff: datetime) -> str:
    """开球 → 赛季键（8 月界切，目录名安全形 "2025-26"）。"""
    year = kickoff.year if kickoff.month >= SEASON_START_MONTH else kickoff.year - 1
    return f"{year}-{(year + 1) % 100:02d}"


def _stage(league: str, kickoff: datetime) -> str:
    """联赛行 league；欧战按开球月窗口分资格赛/正赛。"""
    if league not in EURO_COMPETITIONS:
        return STAGE_LEAGUE
    return STAGE_QUALIFIER if kickoff.month in EURO_QUALIFIER_MONTHS else STAGE_MAIN


def _match_score(score: str) -> tuple[int, int] | None:
    """比分 "h-a" → (主, 客)； malformed 返回 None（异常口径与开球一致）。"""
    parts = score.split("-")
    if len(parts) != _SCORE_PARTS:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def fixture_rows(store: CorpusStore) -> tuple[list[dict[str, object]], int, int]:
    """
    Bronze day_page（当前解析器版本）→ 规范 fixture 行。

    两级去重：日页行按页日期取 fetched_at 最新（append-only 重跑回补）；
    场次 sid 全站唯一——同 sid 跨日页重复出现时同样取最新页（站点极少
    重挂场次，防御性去重）。开球两形态/比分解析失败均跳行计数留痕。
    返回 (行集, 日页数, 异常跳行数)。
    """
    bronze_rows = [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, srct.DAY_DATASET)
        if row.get("parser_version") == srct.BRONZE_VERSIONS[srct.DAY_DATASET]
    ]
    latest_day: dict[str, dict[str, object]] = {}
    for row in bronze_rows:  # append-only 顺序即时间序，后行覆盖前行
        latest_day[str(row["sid"])] = row
    latest_match: dict[str, dict[str, object]] = {}
    fixtures: list[dict[str, object]] = []
    skipped = 0
    # 倒序遍历（最新页先注册先赢）；终序由上游 kickoff+sid 排序统一
    for day_str, row in sorted(latest_day.items(), reverse=True):
        day = date.fromisoformat(day_str)
        payload = row.get("payload")
        matches = payload.get("matches", []) if isinstance(payload, dict) else []
        for match in matches:
            sid = str(match["sid"])
            if sid in latest_match:  # 旧页重挂行：最新页已收编
                continue
            latest_match[sid] = match
            kickoff = resolve_kickoff(day, str(match.get("kickoff_label", "")))
            goals = _match_score(str(match.get("score", "")))
            if kickoff is None or goals is None:
                skipped += 1
                continue
            fixtures.append(
                {
                    "sid": sid,
                    "league": str(match["league"]),
                    "kickoff": kickoff,
                    "home": str(match["home"]),
                    "away": str(match["away"]),
                    "home_goals": goals[0],
                    "away_goals": goals[1],
                    "stage": _stage(str(match["league"]), kickoff),
                    "day": day_str,
                }
            )
    return fixtures, len(latest_day), skipped


def build_fixture_universe(store: CorpusStore) -> SilverFixtureReport:
    """重物化 silver fixture_universe（幂等：同输入字节级重建）。"""
    fixtures, day_pages, skipped = fixture_rows(store)
    report = SilverFixtureReport(
        day_pages=day_pages,
        skipped_anomaly=skipped,
        built_at=utc_now_iso(),
    )
    fixtures.sort(key=lambda r: (r["kickoff"], str(r["sid"])))  # 文件内序
    root = _fixture_root(store)
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in fixtures:
        season = season_of(cast(datetime, row["kickoff"]))
        grouped.setdefault((season, str(row["league"])), []).append(row)
    target_dirs: set[Path] = set()
    for (season, league), rows in sorted(grouped.items()):
        part = root / f"season={season}" / f"competition={league}"
        write_partition(part, rows, _FIXTURE_SCHEMA)
        target_dirs.add(part)
        report.seasons[season] = report.seasons.get(season, 0) + len(rows)
    report.rows = len(fixtures)
    report.partitions = len(target_dirs)
    report.stale_partitions_removed = remove_stale(root, target_dirs)
    root.mkdir(parents=True, exist_ok=True)  # 空语料也要落 _meta（建库即审计）
    meta = {
        "silver_version": SILVER_VERSION,
        "bronze_version": srct.BRONZE_VERSIONS[srct.DAY_DATASET],
        "built_at": report.built_at,
        "rows": report.rows,
        "day_pages": report.day_pages,
        "skipped_anomaly": report.skipped_anomaly,
        "partitions": report.partitions,
        "seasons": report.seasons,
    }
    (root / "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def _fixture_root(store: CorpusStore) -> Path:
    return store.root / "silver" / srct.SRCT_PROVIDER / FIXTURE_DATASET


def write_partition(
    part: Path, rows: list[dict[str, object]], schema: pa.Schema
) -> None:
    """一分区一 parquet 文件（tmp 原子替换；排序已在上游统一完成）。"""
    part.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=schema)
    tmp = part / "data.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(part / "data.parquet")


def remove_stale(root: Path, target: set[Path]) -> int:
    """删除不在目标集里的旧分区（重建幂等的清理半边；silver 各数据集共用）。"""
    removed = 0
    if not root.exists():
        return removed
    for season_dir in sorted(root.iterdir()):
        if not season_dir.is_dir():
            continue  # _meta.json 等文件不动
        for part in sorted(season_dir.iterdir()):
            if part.is_dir() and part not in target:
                for file in part.iterdir():
                    file.unlink()
                part.rmdir()
                removed += 1
        if season_dir.is_dir() and not any(season_dir.iterdir()):
            season_dir.rmdir()
    return removed


# ---- xg_observation（切片 16）：47 键统计按场 silver，源T 单源口径 ----

XG_DATASET = "xg_observation"
XG_SILVER_VERSION = "silver_xg_v1"
# 标题 xG 扁平列取 EXPECTED_GOALS（预期进球，全五味之总）；其余四味留在
# stats 列内按 kind 保留（键集逐年演进，贴源不砍）
_HEADLINE_KIND = "EXPECTED_GOALS"

_STAT_ITEM = pa.struct(
    [
        pa.field("kind", pa.string()),
        pa.field("name", pa.string()),
        pa.field("home_value", pa.string()),  # 贴源原串（int/float 落串）
        pa.field("away_value", pa.string()),
    ]
)
_XG_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("has_xg", pa.bool_()),
        pa.field("xg_home", pa.float64()),  # EXPECTED_GOALS 数值化（缺/坏 None）
        pa.field("xg_away", pa.float64()),
        pa.field("stats", pa.list_(_STAT_ITEM)),  # 全键保留（无 xG 场不丢 40+ 键）
        pa.field(
            "fetched_at", pa.string()
        ),  # 观测时刻（bronze 行信封，observed_at 语义）
    ]
)


@dataclass
class SilverXgReport:
    """一次 xg_observation 重物化报告（coverage 摘要口径）。"""

    silver_version: str = XG_SILVER_VERSION
    rows: int = 0
    has_xg_rows: int = 0  # coverage：has_xg=True 场数
    bad_xg_values: int = 0  # EXPECTED_GOALS 在场但值非数值（留 None 计数）
    headline_missing: int = 0  # has_xg=True 但标题键缺席（口径背离信号）
    orphan_sids: int = 0  # 无 fixture 元数据的场（落 _unknown 分区不丢）
    partitions: int = 0
    stale_partitions_removed: int = 0
    seasons: dict[str, int] = field(default_factory=dict)
    built_at: str = ""


def _to_float(value: object) -> float | None:
    """数值化容错（贴源串/数值均可；失败 None）。"""
    if value is None:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _headline_xg(
    stats: list[dict[str, object]],
) -> tuple[float | None, float | None, bool]:
    """标题 xG：(home, away, 值坏)；键不在场返回 (None, None, False)。"""
    headline = next((i for i in stats if i.get("kind") == _HEADLINE_KIND), None)
    if headline is None:
        return None, None, False
    home, away = (
        _to_float(headline.get("home_value")),
        _to_float(headline.get("away_value")),
    )
    return home, away, home is None or away is None


def _latest_stats_rows(store: CorpusStore) -> dict[str, dict[str, object]]:
    """当前解析器版本的 match_stats 行，sid→最新（append-only 后行胜出）。"""
    latest: dict[str, dict[str, object]] = {}
    for row in store.read_bronze(srct.SRCT_PROVIDER, srct.STATS_DATASET):
        if row.get("parser_version") != srct.BRONZE_VERSIONS[srct.STATS_DATASET]:
            continue
        latest[str(row["sid"])] = row
    return latest


def _stat_items(stats: list[dict[str, object]]) -> list[dict[str, str | None]]:
    """47 键贴源投影：值转串、None 保留（键集演进零假设）。"""
    return [
        {
            "kind": str(i.get("kind")),
            "name": str(i.get("name")),
            "home_value": None
            if i.get("home_value") is None
            else str(i.get("home_value")),
            "away_value": None
            if i.get("away_value") is None
            else str(i.get("away_value")),
        }
        for i in stats
    ]


def build_xg_observations(store: CorpusStore) -> SilverXgReport:
    """
    重物化 silver xg_observation（幂等；输入=bronze match_stats 当前版本）。

    一行=一场：has_xg 布尔 + 标题 xG 扁平列 + 全部 47 键 stats 贴源保留。
    赛季/联赛分区取自 fixture_universe 的 sid 元数据（join 地基，spec
    story 12）；无元数据的场落 `season=_unknown/competition=_unknown`
    （不丢数据，计数留痕——含"真孤儿"与 fixture 侧开球/比分解析跳行
    两类，审计看 fixture 报告的 skipped_anomaly 可分口径）。观测时刻 =
    bronze 行信封 fetched_at（列名同义）。
    """
    report = SilverXgReport(built_at=utc_now_iso())
    latest = _latest_stats_rows(store)
    meta = {str(r["sid"]): r for r in fixture_rows(store)[0]}
    rows: list[dict[str, object]] = []
    unknown: list[dict[str, object]] = []
    for sid, bronze in sorted(latest.items()):
        payload = bronze.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        stats = payload_dict.get("stats", [])
        has_xg = bool(payload_dict.get("has_xg"))
        xg_home, xg_away, bad = _headline_xg(stats)
        if bad:
            report.bad_xg_values += 1
        if has_xg and not any(i.get("kind") == _HEADLINE_KIND for i in stats):
            # has_xg 口径（任一 XG 键在场）与标题键背离——coverage 对账信号
            report.headline_missing += 1
        row: dict[str, object] = {
            "sid": sid,
            "has_xg": has_xg,
            "xg_home": xg_home,
            "xg_away": xg_away,
            "stats": _stat_items(stats),
            "fetched_at": str(bronze.get("fetched_at")),
        }
        if has_xg:
            report.has_xg_rows += 1
        fixture = meta.get(sid)
        if fixture is None:
            report.orphan_sids += 1
            unknown.append(row)
        else:
            rows.append(row)
    root = store.root / "silver" / srct.SRCT_PROVIDER / XG_DATASET
    target_dirs: set[Path] = set()
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        fixture = meta[str(row["sid"])]
        season = season_of(cast(datetime, fixture["kickoff"]))
        grouped.setdefault((season, str(fixture["league"])), []).append(row)
    if unknown:
        grouped.setdefault(("_unknown", "_unknown"), []).extend(unknown)
    for (season, league), part_rows in sorted(grouped.items()):
        part_rows.sort(key=lambda r: str(r["sid"]))
        part = root / f"season={season}" / f"competition={league}"
        write_partition(part, part_rows, _XG_SCHEMA)
        target_dirs.add(part)
        report.seasons[season] = report.seasons.get(season, 0) + len(part_rows)
    report.rows = len(rows) + len(unknown)
    report.partitions = len(target_dirs)
    report.stale_partitions_removed = remove_stale(root, target_dirs)
    root.mkdir(parents=True, exist_ok=True)
    meta_out = {
        "silver_version": XG_SILVER_VERSION,
        "bronze_version": srct.BRONZE_VERSIONS[srct.STATS_DATASET],
        "built_at": report.built_at,
        "rows": report.rows,
        "has_xg_rows": report.has_xg_rows,
        "orphan_sids": report.orphan_sids,
        "bad_xg_values": report.bad_xg_values,
        "headline_missing": report.headline_missing,
        "partitions": report.partitions,
        "seasons": report.seasons,
    }
    (root / "_meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
