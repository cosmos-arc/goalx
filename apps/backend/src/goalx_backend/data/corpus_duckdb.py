"""
CorpusStore 接入面（ADR-0011 决策 6）：duckdb/corpus.duckdb 只读桥。

建库 = 银层 Parquet 的视图面（read_parquet + hive 分区列，随数据集逐张
扩）；消费 = `connect()` 以 READ_ONLY 打开 corpus.duckdb 并 ATTACH 运行面
goalx.db（TYPE sqlite, READ_ONLY）——跨面对账（fdhist×源T 等，切片 17）
单引擎单 SQL 可达。运行面永不写：READ_ONLY ATTACH 由 DuckDB 拒绝写路径，
corpus.duckdb 本体只读打开同样拒绝（消费侧任何写路径被拒）。

路径解析：视图引用语料树内 Parquet 的**绝对路径**（read_parquet 按 CWD
解析，不能依赖调用方 CWD）；语料树整体迁移后重建一次即恢复。ATTACH 的
运行面路径在 connect() 时按 Settings 解析（不烙进 duckdb 文件——迁移
零残留）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from loguru import logger

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import (
    archive538,
    jc,
    jc_silver,
    srct,
    srct_market,
    srct_odds,
    srct_silver,
)

CORPUS_DUCKDB_NAME = "corpus.duckdb"
# 运行面 ATTACH 别名（infra 常量，非表名）
RUNNING_FACE_ALIAS = "goalx"
# 银层视图清单（视图名, 语料树 silver 相对路径；随数据集逐张扩：
# 538 静态表+odds/字典=切片 15/16，四件套齐）
_SILVER_VIEWS: tuple[tuple[str, str], ...] = (
    (
        srct_silver.FIXTURE_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_silver.FIXTURE_DATASET}",
    ),
    (
        srct_silver.XG_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_silver.XG_DATASET}",
    ),
    (
        "archive_538",
        f"{archive538.PROVIDER}/{archive538.DATASET}",
    ),
    (
        srct_odds.ODDS_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_odds.ODDS_DATASET}",
    ),
    (
        srct_odds.BOOKMAKER_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_odds.BOOKMAKER_DATASET}",
    ),
    (
        srct_market.MARKET_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_market.MARKET_DATASET}",
    ),
    (
        srct_market.DETAIL_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_market.DETAIL_DATASET}",
    ),
    (
        srct_market.ANALYSIS_DATASET,
        f"{srct.SRCT_PROVIDER}/{srct_market.ANALYSIS_DATASET}",
    ),
    (
        jc_silver.SP_EVENT_DATASET,
        f"{jc.JC_PROVIDER}/{jc_silver.SP_EVENT_DATASET}",
    ),
)


def corpus_duckdb_path(root: Path) -> Path:
    """corpus.duckdb 约定路径（树内 duckdb/）。"""
    return Path(root) / "duckdb" / CORPUS_DUCKDB_NAME


def build_corpus_duckdb(store: CorpusStore) -> Path:
    """
    建/重建 corpus.duckdb：全部 silver 视图（幂等，CREATE OR REPLACE）。

    空语料（该数据集尚无 parquet）跳过建视图并告警——首夜数据落地前
    `srct-silver` 可跑不炸，视图下次重建自愈。
    """
    store.ensure_tree()  # duckdb/ 目录随首建落地
    path = corpus_duckdb_path(store.root)
    con = duckdb.connect(str(path))
    try:
        for view, rel in _SILVER_VIEWS:
            silver = store.root / "silver" / rel
            if not any(silver.glob("**/*.parquet")):
                logger.warning("corpus.duckdb：{} 无 parquet，跳过建视图", view)
                continue
            glob = silver.as_posix() + "/**/*.parquet"
            con.execute(
                # 视图名/路径来自模块常量与语料树根，非用户输入
                f"""
                CREATE OR REPLACE VIEW {view} AS
                SELECT * FROM read_parquet('{glob}', hive_partitioning = true)
                """  # noqa: S608
            )
    finally:
        con.close()
    return path


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    """
    消费侧连接：corpus.duckdb 只读 + 运行面 goalx 只读 ATTACH。

    只进不出——连接上任何写（视图 INSERT、goalx.* 写、建库建表）都会被
    DuckDB 拒绝。调用方负责 close()。
    """
    con = duckdb.connect(str(corpus_duckdb_path(settings.corpus_root)), read_only=True)
    face = settings.db_path.resolve().as_posix()
    con.execute(
        f"""ATTACH IF NOT EXISTS '{face}'
        AS {RUNNING_FACE_ALIAS} (TYPE sqlite, READ_ONLY)"""
    )
    return con


def hist_matches_count(con: duckdb.DuckDBPyConnection) -> int | None:
    """运行面 hist_matches 行数（跨面冒烟；表缺席/库缺席返回 None）。"""
    try:
        row = con.execute("SELECT count(*) FROM goalx.hist_matches").fetchone()
    except duckdb.Error:
        return None
    return int(row[0]) if row is not None else None


def srct_pinnacle_closing_triplet(
    con: duckdb.DuckDBPyConnection,
    home: str,
    away: str,
    kickoff_utc: str,
    *,
    bookmaker_id: str = "srct:1x2:177",
) -> tuple[float, float, float] | None:
    """
    竞彩场次 → 源T 主锚盘前三向收盘（票 75 CLV 收盘锚接管的数据面）。

    场次匹配两步确定性键（禁自信合并，定则 1）：
    ① home/away 中文队名 + 北京日期 == fixture_universe；
    ② 兜底 kickoff 时刻精确（北京 naive）+ home 精确（解命名变体）；
    两步不中返回 None（CorpusScope 外场结构性无锚，调用方诚实 skip）。
    资格线 = published_at（源自报时点）≤ kickoff，取最新一笔。

    ponytail: odds_change_event 为 3,000 万行级 parquet 视图、sid 无索引，
    单查秒级内——日频对账×个位数腿可接受；腿数上量后按 sid 分区物化。
    """
    kickoff = datetime.fromisoformat(kickoff_utc.replace("Z", "+00:00"))
    if kickoff.tzinfo is None:  # 防御：naive 串按 UTC 解释
        kickoff = kickoff.replace(tzinfo=UTC)
    beijing = kickoff.astimezone(UTC).replace(tzinfo=None) + timedelta(hours=8)
    sid_row = con.execute(
        """
        SELECT sid FROM fixture_universe
        WHERE home = ? AND away = ? AND CAST(kickoff AS DATE) = ?
        ORDER BY sid LIMIT 1
        """,
        [home, away, beijing.date().isoformat()],
    ).fetchone()
    if sid_row is None:
        sid_row = con.execute(
            """
            SELECT sid FROM fixture_universe
            WHERE kickoff = ? AND home = ? ORDER BY sid LIMIT 1
            """,
            [beijing, home],
        ).fetchone()
    if sid_row is None:
        return None
    event = con.execute(
        """
        SELECT odds_home, odds_draw, odds_away FROM odds_change_event
        WHERE sid = ? AND bookmaker_id = ? AND published_at <= ?
        ORDER BY published_at DESC LIMIT 1
        """,
        [sid_row[0], bookmaker_id, kickoff],
    ).fetchone()
    if event is None or any(price is None for price in event):
        return None
    return float(event[0]), float(event[1]), float(event[2])
