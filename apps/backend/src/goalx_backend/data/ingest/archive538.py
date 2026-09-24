"""
538 SPI/xG 终版档案入库（票 55/56 切片 16）：一次性静态资产进语料树。

源站 2023-06 停更，Wayback 2023-06-25 快照=字节级终版（research/21 §一
留档，官方 repo 明示 CC BY 4.0 可本地永久保存）。原档 CSV 整档进
raw/{provider=538}/spi_matches/{key=terminal}；silver 静态小表 =
CorpusScope 15 联赛映射过滤后的行（league 字面量与源T CorpusScope 对齐，
含 xG 全零的土/比/苏/瑞/挪——SPI/prob 参照列仍在）。

xG 覆盖实测口径（research/21 覆盖矩阵）：五大 2016-2023 ≈100%、英冠
2017-2023、荷甲仅 2020-2023、葡超 2017-2023、欧冠 2016-2023、欧联
2017-2023；土/比/苏/瑞/挪全程 0%（has_xg=False 如实记录）。附赠维度
nsxG（non-shot xG）Understat 无。team1/team2 是队名字符串——跨源身份
绑定仍后置（定则 1），对账 join 归切片 17。

赛季键按开球日 8 月界切（与夜班季窗/silver 同界）；夏季历联赛（挪超/瑞超）
的 538 行落在跨年季键下属已知标签偏差（该五联赛 xG 全零，只影响 SPI-only
行的分区标签，不影响任何 xG 对账）。
"""

from __future__ import annotations

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 srct_silver 先例）
import csv
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow as pa

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest.srct_silver import (
    remove_stale,
    season_of,
    write_partition,
)
from goalx_backend.db import utc_now_iso

PROVIDER = "538"
DATASET = "spi_matches"
ARCHIVE_KEY = "terminal"  # Wayback 终版（2023-06-25 快照）
SILVER_VERSION = "silver_538_v1"
# research/21 留档位置（.scratch 不进库；入库后重建从树内 raw 读，零依赖）
DEFAULT_CSV_PATH = Path(".scratch/goalx-quant/research/21-538-spi_matches_terminal.csv")

# 538 联赛名 → CorpusScope 字面量（十五项；欧会杯沿排除）
LEAGUE_MAP: dict[str, str] = {
    "Barclays Premier League": "英超",
    "Spanish Primera Division": "西甲",
    "German Bundesliga": "德甲",
    "Italy Serie A": "意甲",
    "French Ligue 1": "法甲",
    "English League Championship": "英冠",
    "Dutch Eredivisie": "荷甲",
    "Portuguese Liga": "葡超",
    "UEFA Champions League": "欧冠杯",
    "UEFA Europa League": "欧罗巴杯",
    "Turkish Turkcell Super Lig": "土超",
    "Belgian Jupiler League": "比甲",
    "Scottish Premiership": "苏超",
    "Swedish Allsvenskan": "瑞超",
    "Norwegian Tippeligaen": "挪超",
}

_FLOAT_COLUMNS = (
    "spi1",
    "spi2",
    "prob1",
    "prob2",
    "probtie",
    "proj_score1",
    "proj_score2",
    "xg1",
    "xg2",
    "nsxg1",
    "nsxg2",
    "adj_score1",
    "adj_score2",
)
_INT_COLUMNS = ("score1", "score2")

_538_SCHEMA = pa.schema(
    [
        pa.field("kickoff", pa.timestamp("ms")),  # 原档仅日期，零点占位
        pa.field("league_cn", pa.string()),
        pa.field("season_key", pa.string()),
        pa.field("team1", pa.string()),
        pa.field("team2", pa.string()),
        *(pa.field(name, pa.float64()) for name in _FLOAT_COLUMNS),
        *(pa.field(name, pa.int16()) for name in _INT_COLUMNS),
        pa.field("has_xg", pa.bool_()),
        pa.field("archive_season", pa.string()),  # 原 538 season 标签（历年制）
    ]
)


@dataclass
class Archive538Report:
    """一次 538 档案入库/重建报告。"""

    silver_version: str = SILVER_VERSION
    raw_ingested: bool = False  # 本次是否新落 raw（树内已有则 False）
    archive_rows: int = 0  # 原档总行数
    rows: int = 0  # CorpusScope 过滤后行数
    bad_dates: int = 0  # 坏日期跳行（终版实测 0）
    has_xg_rows: int = 0
    partitions: int = 0
    stale_partitions_removed: int = 0
    seasons: dict[str, int] = field(default_factory=dict)
    built_at: str = ""


def _silver_root(store: CorpusStore) -> Path:
    return store.root / "silver" / PROVIDER / DATASET


def _num(value: str) -> float | None:
    """空串→None；数值化失败→None（坏值不炸静态入库）。"""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: str) -> int | None:
    """整型化：非整数值（"2.7"）返回 None，不静默截断。"""
    num = _num(value)
    if num is None or num != int(num):
        return None
    return int(num)


def _scoped_rows(
    content: bytes,
) -> tuple[list[dict[str, object]], int, int]:
    """
    原档字节 → CorpusScope 行集（贴源值类型化）。

    返回 (行集, 总行数, 坏日期跳行数)——坏日期与坏数值同口径：跳行留痕
    不炸静态入库（终版固定档实测零坏行，防御性）。
    """
    rows: list[dict[str, object]] = []
    total = 0
    bad_dates = 0
    for raw in csv.DictReader(io.StringIO(content.decode("utf-8-sig"))):
        total += 1
        league_cn = LEAGUE_MAP.get(raw.get("league", ""))
        if league_cn is None:
            continue
        try:
            kickoff = datetime.fromisoformat(raw.get("date", ""))
        except ValueError:
            bad_dates += 1
            continue
        xg1, xg2 = _num(raw.get("xg1", "")), _num(raw.get("xg2", ""))
        row: dict[str, object] = {
            "kickoff": kickoff,
            "league_cn": league_cn,
            "season_key": season_of(kickoff),
            "team1": raw.get("team1", ""),
            "team2": raw.get("team2", ""),
            "has_xg": xg1 is not None and xg2 is not None,
            "archive_season": raw.get("season", ""),
        }
        for name in _FLOAT_COLUMNS:
            row[name] = _num(raw.get(name, ""))
        for name in _INT_COLUMNS:
            row[name] = _int(raw.get(name, ""))
        rows.append(row)
    rows.sort(key=lambda r: (str(r["kickoff"]), str(r["team1"])))
    return rows, total, bad_dates


def ingest_archive_538(
    store: CorpusStore, csv_path: Path | None = None
) -> Archive538Report:
    """
    一次性入库：原档进 raw + CorpusScope silver 小表（幂等重建）。

    csv_path 缺省取 research/21 留档；树内已有 raw 而源档缺席时从 raw
    重建 silver（静态资产自持，换机不依赖 .scratch）。
    """
    report = Archive538Report(built_at=utc_now_iso())
    store.ensure_tree()
    source = csv_path if csv_path is not None else DEFAULT_CSV_PATH
    if store.has(PROVIDER, DATASET, ARCHIVE_KEY):
        content = store.read_raw(PROVIDER, DATASET, ARCHIVE_KEY, ext=".csv")
    elif source.is_file():
        content = source.read_bytes()
        store.ingest_raw(PROVIDER, DATASET, ARCHIVE_KEY, content, ext=".csv")
        report.raw_ingested = True
    else:
        msg = f"538 原档不在语料树且源档缺席：{source}"
        raise FileNotFoundError(msg)
    rows, archive_total, bad_dates = _scoped_rows(content)
    report.archive_rows = archive_total
    report.bad_dates = bad_dates
    report.rows = len(rows)
    report.has_xg_rows = sum(1 for r in rows if r["has_xg"])
    root = _silver_root(store)
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault((str(row["season_key"]), str(row["league_cn"])), []).append(
            row
        )
    target_dirs: set[Path] = set()
    for (season, league), part_rows in sorted(grouped.items()):
        part = root / f"season={season}" / f"competition={league}"
        write_partition(part, part_rows, _538_SCHEMA)
        target_dirs.add(part)
        report.seasons[season] = report.seasons.get(season, 0) + len(part_rows)
    report.partitions = len(target_dirs)
    report.stale_partitions_removed = remove_stale(root, target_dirs)
    root.mkdir(parents=True, exist_ok=True)
    (root / "_meta.json").write_text(
        json.dumps(
            {
                "silver_version": SILVER_VERSION,
                "archive": "wayback-terminal-2023-06-25",
                "license": "CC BY 4.0",
                "built_at": report.built_at,
                "archive_rows": report.archive_rows,
                "rows": report.rows,
                "bad_dates": report.bad_dates,
                "has_xg_rows": report.has_xg_rows,
                "partitions": report.partitions,
                "seasons": report.seasons,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return report
