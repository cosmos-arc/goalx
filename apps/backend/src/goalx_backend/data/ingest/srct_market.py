"""
源T silver 票 64：规格 v2 四新数据集的 silver 物化 + 字典身份扩展。

- **market_quote**（asian_odds + over_down 两 bronze → 一报价表）：一行=
  (sid, market, cid, multi) 的初/即时/终三组盘口水位（贴源串+归一线值）。
  亚盘/大小球多庄页的 companyID 与 changeDetail 同空间（页内逐行链回
  changeDetail）——bookmaker_id 沿 design-15 双空间防线：`srct:ah:{cid}` /
  `srct:ou:{cid}`，与 1x2 联合空间隔离防撞名。
- **match_detail** / **match_analysis**：detail 详情页与 analysis 特征页的
  按场摘要（bounded 数据集单文件；analysis 贴源数组行只计形状，语义化归
  特征线消费端）。
- **bookmaker 字典扩展**（srct_odds.build_bookmakers 吸收多庄页）：站点
  自带遮罩短名（"澳*"）入 name_zh_masked——cid=8 基准亚盘家自此有代称化
  身份；match_count 按 (sid, cid) 出现计。
- 幂等：tmp 原子替换 + 陈旧分区清理（同 srct_odds 范式）；选版口径=
  latest_bronze_rows（当前 parser_version、后行胜出）。
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
# （pyarrow 无官方 stub，同 srct_odds 先例）

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import cast

import pyarrow as pa
import pyarrow.parquet as pq

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds, srct_silver
from goalx_backend.db import utc_now_iso

MARKET_DATASET = "market_quote"
DETAIL_DATASET = "match_detail"
ANALYSIS_DATASET = "match_analysis"
MARKET_SILVER_VERSION = "silver_market_v1"
DETAIL_SILVER_VERSION = "silver_detail_v1"
ANALYSIS_SILVER_VERSION = "silver_analysis_v1"
# 多庄页 market 标记与 bookmaker 空间（design-15 双空间防线沿用）
MARKET_AH = "ah"
MARKET_OU = "ou"

_QUOTE_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("market", pa.string()),  # ah / ou
        pa.field("cid", pa.string()),
        pa.field("bookmaker_id", pa.string()),  # srct:ah:{cid} / srct:ou:{cid}
        pa.field("multi", pa.string()),  # 盘1/盘2/…（多盘行）
        pa.field("observed_at", pa.timestamp("ms", tz="UTC")),  # bronze fetched_at
        pa.field(
            "kickoff", pa.timestamp("ms", tz="UTC")
        ),  # fixture 冗余列（孤儿 None）
        pa.field("open_line_raw", pa.string()),
        pa.field("open_line", pa.float64()),  # 主队视角归一（受让为负）
        pa.field("open_home_water", pa.float64()),
        pa.field("open_away_water", pa.float64()),
        pa.field("latest_line_raw", pa.string()),
        pa.field("latest_line", pa.float64()),
        pa.field("latest_home_water", pa.float64()),
        pa.field("latest_away_water", pa.float64()),
        pa.field("close_line_raw", pa.string()),
        pa.field("close_line", pa.float64()),
        pa.field("close_home_water", pa.float64()),
        pa.field("close_away_water", pa.float64()),
    ]
)

_DETAIL_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("kickoff", pa.timestamp("ms", tz="UTC")),
        pa.field("observed_at", pa.timestamp("ms", tz="UTC")),
        pa.field("home", pa.string()),
        pa.field("away", pa.string()),
        pa.field("venue", pa.string()),
        pa.field("weather", pa.string()),
        pa.field("temperature", pa.string()),
        pa.field("referee", pa.string()),
        pa.field("home_formation", pa.string()),
        pa.field("away_formation", pa.string()),
        pa.field("home_coach", pa.string()),
        pa.field("away_coach", pa.string()),
        pa.field("has_xg", pa.bool_()),
        pa.field("xg_home", pa.float64()),
        pa.field("xg_away", pa.float64()),
        pa.field("tech_rows", pa.int16()),
        pa.field("events", pa.int16()),
        pa.field("home_starters", pa.int16()),
        pa.field("away_starters", pa.int16()),
        pa.field("home_bench", pa.int16()),
        pa.field("away_bench", pa.int16()),
    ]
)

_ANALYSIS_SCHEMA = pa.schema(
    [
        pa.field("sid", pa.string()),
        pa.field("kickoff", pa.timestamp("ms", tz="UTC")),
        pa.field("observed_at", pa.timestamp("ms", tz="UTC")),
        pa.field("recent_home", pa.int16()),  # 近况行数（形状；语义化归消费端）
        pa.field("recent_away", pa.int16()),
        pa.field("recent_home_split", pa.int16()),
        pa.field("recent_away_split", pa.int16()),
        pa.field("h2h", pa.int16()),
        pa.field("odds_compare_asian", pa.int16()),
        pa.field("odds_compare_eu", pa.int16()),
        pa.field("standings_home", pa.int16()),
        pa.field("standings_away", pa.int16()),
        pa.field("future_home", pa.int16()),
        pa.field("future_away", pa.int16()),
        pa.field("next_home_gap_days", pa.int16()),  # 未来首场相隔天数（密度）
        pa.field("next_away_gap_days", pa.int16()),
    ]
)


@dataclass
class SilverMarketReport:
    """一次 market_quote 重物化报告。"""

    silver_version: str = MARKET_SILVER_VERSION
    quotes_ah: int = 0
    quotes_ou: int = 0
    books_ah: int = 0  # 出现的独立书商数（cid 去重）
    books_ou: int = 0
    bad_line_values: int = 0  # 线串未识别（line=None，行保留）
    bad_waters: int = 0
    orphan_sids: int = 0
    partitions: int = 0
    stale_partitions_removed: int = 0
    built_at: str = ""


@dataclass
class SilverFaceReport:
    """一次 detail/analysis 摘要重物化报告（两数据集同口径）。"""

    silver_version: str
    rows: int = 0
    orphan_sids: int = 0
    built_at: str = ""


def _fetched_ms(bronze: dict[str, object]) -> int | None:
    """Bronze fetched_at（ISO）→ epoch 毫秒。"""
    return srct_odds.fetched_ms(bronze.get("fetched_at"))


def _kickoff_ms(
    meta: dict[str, dict[str, object]], sid: str
) -> tuple[int | None, str, str]:
    """Fixture 元数据 → (kickoff 毫秒, season, competition)；孤儿 (None,_unknown)。"""
    fixture = meta.get(sid)
    if fixture is None:
        return None, "_unknown", "_unknown"
    kickoff = cast("datetime", fixture["kickoff"])
    return (
        srct_odds.beijing_ms(kickoff),
        srct_silver.season_of(kickoff),
        str(fixture["league"]),
    )


def _quote_row(
    market: str,
    book: dict[str, object],
    bronze: dict[str, object],
    kickoff_ms: int | None,
    report: SilverMarketReport,
) -> dict[str, object]:
    """一书一盘 bronze 行 → 报价行（三组线值归一；坏值 None 计数留痕）。"""
    row: dict[str, object] = {
        "sid": bronze.get("sid"),
        "market": market,
        "cid": str(book.get("cid")),
        "bookmaker_id": f"srct:{market}:{book.get('cid')}",
        "multi": str(book.get("multi") or "盘1"),
        "observed_at": _fetched_ms(bronze),
        "kickoff": kickoff_ms,
    }
    for prefix, key in (("open", "initial"), ("latest", "latest"), ("close", "close")):
        quote = cast("dict[str, object]", book.get(key) or {})
        line_raw = quote.get("line")
        line = srct_odds.normalize_line(str(line_raw) if line_raw is not None else None)
        home = srct_silver.to_float(quote.get("home_water"))
        away = srct_silver.to_float(quote.get("away_water"))
        if line_raw is not None and line is None:
            report.bad_line_values += 1
        if line_raw is not None and (home is None or away is None):
            report.bad_waters += 1
        row[f"{prefix}_line_raw"] = line_raw
        row[f"{prefix}_line"] = line
        row[f"{prefix}_home_water"] = home
        row[f"{prefix}_away_water"] = away
    return row


def build_market_quotes(
    store: CorpusStore, *, chunk_rows: int = srct_odds.STREAM_CHUNK_ROWS
) -> SilverMarketReport:
    """重物化 silver market_quote（幂等；asian_odds+over_down bronze 当前版本）。"""
    report = SilverMarketReport(built_at=utc_now_iso())
    meta = {str(r["sid"]): r for r in srct_silver.fixture_rows(store)[0]}
    root = store.root / "silver" / srct.SRCT_PROVIDER / MARKET_DATASET
    out = srct_odds.StreamingPartitions(root, _QUOTE_SCHEMA, chunk_rows)
    sids = sorted(
        set(srct_silver.latest_bronze_rows(store, srct.ASIANODDS_DATASET))
        | set(srct_silver.latest_bronze_rows(store, srct.OVERDOWN_DATASET)),
        key=lambda s: _kickoff_ms(meta, s)[:1] or (0,),  # 孤子垫后稳定序
    )
    for sid in sids:
        kickoff_ms, season, competition = _kickoff_ms(meta, sid)
        if kickoff_ms is None:
            report.orphan_sids += 1
        for market, dataset in (
            (MARKET_AH, srct.ASIANODDS_DATASET),
            (MARKET_OU, srct.OVERDOWN_DATASET),
        ):
            bronze = srct_silver.latest_bronze_rows(store, dataset).get(sid)
            if bronze is None:
                continue
            payload = bronze.get("payload")
            books = (
                cast("list[dict[str, object]]", payload.get("books", []))
                if isinstance(payload, dict)
                else []
            )
            for book in books:
                out.append(
                    season,
                    competition,
                    _quote_row(market, book, bronze, kickoff_ms, report),
                )
                if market == MARKET_AH:
                    report.quotes_ah += 1
                else:
                    report.quotes_ou += 1
    seen_books: set[tuple[str, str]] = set()
    for dataset, market in (
        (srct.ASIANODDS_DATASET, MARKET_AH),
        (srct.OVERDOWN_DATASET, MARKET_OU),
    ):
        for bronze in srct_silver.latest_bronze_rows(store, dataset).values():
            payload = bronze.get("payload")
            books = (
                cast("list[dict[str, object]]", payload.get("books", []))
                if isinstance(payload, dict)
                else []
            )
            seen_books.update((market, str(b.get("cid"))) for b in books)
    report.books_ah = sum(1 for m, _ in seen_books if m == MARKET_AH)
    report.books_ou = sum(1 for m, _ in seen_books if m == MARKET_OU)
    report.partitions, report.stale_partitions_removed = out.close()
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": MARKET_SILVER_VERSION,
            "bronze_versions": {
                "asian_odds": srct.BRONZE_VERSIONS[srct.ASIANODDS_DATASET],
                "over_down": srct.BRONZE_VERSIONS[srct.OVERDOWN_DATASET],
            },
            "built_at": report.built_at,
            "quotes_ah": report.quotes_ah,
            "quotes_ou": report.quotes_ou,
            "orphan_sids": report.orphan_sids,
        },
    )
    return report


def _xg_values(
    tech: list[dict[str, object]],
) -> tuple[bool, float | None, float | None]:
    """Detail 技统行 → (has_xg, xg_home, xg_away)（预期进球/xG 命名行）。"""
    for row in tech:
        name = str(row.get("name") or "")
        if "xg" in name.lower() or "预期进球" in name:
            return (
                True,
                srct_silver.to_float(row.get("home")),
                srct_silver.to_float(row.get("away")),
            )
    return False, None, None


def _next_gap_days(rows: list[list[str]]) -> int | None:
    """未来首场的"相隔 N 天"（密度特征；缺形态 None）。"""
    for cells in rows:
        for cell in reversed(cells):  # 相隔列在行尾
            digits = "".join(ch for ch in str(cell) if ch.isdigit())
            if "天" in str(cell) and digits:
                return int(digits)
    return None


def build_detail_faces(store: CorpusStore) -> SilverFaceReport:
    """重物化 silver match_detail（幂等；detail bronze 当前版本按场摘要）。"""
    report = SilverFaceReport(DETAIL_SILVER_VERSION, built_at=utc_now_iso())
    meta = {str(r["sid"]): r for r in srct_silver.fixture_rows(store)[0]}
    rows: list[dict[str, object]] = []
    for sid, bronze in sorted(
        srct_silver.latest_bronze_rows(store, srct.DETAIL_DATASET).items()
    ):
        payload = bronze.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        meta_payload = cast("dict[str, object]", payload_dict.get("meta") or {})
        lineup = cast("dict[str, list[object]]", payload_dict.get("lineup") or {})
        kickoff_ms, _, _ = _kickoff_ms(meta, sid)
        if kickoff_ms is None:
            report.orphan_sids += 1
        tech = cast("list[dict[str, object]]", payload_dict.get("tech") or [])
        has_xg, xg_home, xg_away = _xg_values(tech)
        rows.append(
            {
                "sid": sid,
                "kickoff": kickoff_ms,
                "observed_at": _fetched_ms(bronze),
                "home": meta_payload.get("home"),
                "away": meta_payload.get("away"),
                "venue": meta_payload.get("venue"),
                "weather": meta_payload.get("weather"),
                "temperature": meta_payload.get("temperature"),
                "referee": meta_payload.get("referee"),
                "home_formation": meta_payload.get("home_formation"),
                "away_formation": meta_payload.get("away_formation"),
                "home_coach": meta_payload.get("home_coach"),
                "away_coach": meta_payload.get("away_coach"),
                "has_xg": has_xg,
                "xg_home": xg_home,
                "xg_away": xg_away,
                "tech_rows": len(tech),
                "events": len(cast("list[object]", payload_dict.get("events") or [])),
                "home_starters": len(lineup.get("home_starters") or []),
                "away_starters": len(lineup.get("away_starters") or []),
                "home_bench": len(lineup.get("home_bench") or []),
                "away_bench": len(lineup.get("away_bench") or []),
            }
        )
    root = store.root / "silver" / srct.SRCT_PROVIDER / DETAIL_DATASET
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "data.parquet.tmp"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_DETAIL_SCHEMA), tmp, compression="zstd"
    )
    tmp.replace(root / "data.parquet")
    report.rows = len(rows)
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": DETAIL_SILVER_VERSION,
            "bronze_version": srct.BRONZE_VERSIONS[srct.DETAIL_DATASET],
            "built_at": report.built_at,
            "rows": report.rows,
            "orphan_sids": report.orphan_sids,
        },
    )
    return report


def build_analysis_faces(store: CorpusStore) -> SilverFaceReport:
    """重物化 silver match_analysis（幂等；analysis bronze 当前版本按场摘要）。"""
    report = SilverFaceReport(ANALYSIS_SILVER_VERSION, built_at=utc_now_iso())
    meta = {str(r["sid"]): r for r in srct_silver.fixture_rows(store)[0]}
    rows: list[dict[str, object]] = []
    for sid, bronze in sorted(
        srct_silver.latest_bronze_rows(store, srct.ANALYSIS_DATASET).items()
    ):
        payload = bronze.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}
        arrays = cast("dict[str, list[object]]", payload_dict.get("arrays") or {})
        future = cast(
            "dict[str, list[list[str]]]", payload_dict.get("future_fixtures") or {}
        )
        kickoff_ms, _, _ = _kickoff_ms(meta, sid)
        if kickoff_ms is None:
            report.orphan_sids += 1
        rows.append(
            {
                "sid": sid,
                "kickoff": kickoff_ms,
                "observed_at": _fetched_ms(bronze),
                "recent_home": len(arrays.get("h_data") or []),
                "recent_away": len(arrays.get("a_data") or []),
                "recent_home_split": len(arrays.get("h2_data") or []),
                "recent_away_split": len(arrays.get("a2_data") or []),
                "h2h": len(arrays.get("v_data") or []),
                "odds_compare_asian": len(arrays.get("Vs_hOdds") or []),
                "odds_compare_eu": len(arrays.get("Vs_eOdds") or []),
                "standings_home": len(arrays.get("homeScoreStr") or []),
                "standings_away": len(arrays.get("guestScoreStr") or []),
                "future_home": len(future.get("home") or []),
                "future_away": len(future.get("away") or []),
                "next_home_gap_days": _next_gap_days(future.get("home") or []),
                "next_away_gap_days": _next_gap_days(future.get("away") or []),
            }
        )
    root = store.root / "silver" / srct.SRCT_PROVIDER / ANALYSIS_DATASET
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / "data.parquet.tmp"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_ANALYSIS_SCHEMA), tmp, compression="zstd"
    )
    tmp.replace(root / "data.parquet")
    report.rows = len(rows)
    srct_silver.write_dataset_meta(
        root,
        {
            "silver_version": ANALYSIS_SILVER_VERSION,
            "bronze_version": srct.BRONZE_VERSIONS[srct.ANALYSIS_DATASET],
            "built_at": report.built_at,
            "rows": report.rows,
            "orphan_sids": report.orphan_sids,
        },
    )
    return report
