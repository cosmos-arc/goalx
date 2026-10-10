"""
gold match_features 消费侧单一读数口（backtest-decade 票 13）。

构建面 data/gold.py 传递依赖 data.ingest（corpus_gate 配对引擎等），
importlinter 分层执法下 evaluation 层不可直达；而 ADR-0011 裁定的消费
形态（duckdb 只读视图）需要一个归属包内（ADR-0008，match_features
登记 owner=data）、且下层可达的读数落点——本模块即该叶子：零 ingest
依赖，SQL 全字面量（表 SQL 归属静态审真实可见，不走 f-string 盲区）。

卫生语义在此单一入口生效：已赛行 + ``admin_excluded=false``（训练面
与预测面同一份行集，spec S14）；PSC 成熟度由 gold 快照承载（未成熟行
odds 列整族置空=无基准），消费侧不二次判。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any

import duckdb
from loguru import logger

from goalx_backend.data.corpus_store import (
    GOLD_DATASET,
    GOLD_PROVIDER,
    CorpusStore,
)
from goalx_backend.data.leagues import FD_TO_LEAGUE

# 十年引擎取数列（收盘族按候选链原样携带，era 声明正典；投影归调用方）
_DECADE_SQL = """
    SELECT sid, league,
           strftime(CAST(kickoff AS DATE), '%Y-%m-%d') AS day,
           season, home, away, home_goals, away_goals, era,
           psc_home, psc_draw, psc_away,
           avgc_home, avgc_draw, avgc_away,
           close1x2_h, close1x2_d, close1x2_a,
           close1x2_cons_h, close1x2_cons_d, close1x2_cons_a,
           version
    FROM match_features
    WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
      AND admin_excluded = false
      AND league IN (SELECT unnest(?))
      AND (? IS NULL OR CAST(kickoff AS DATE) <= ?)
    ORDER BY league, day, sid
"""


def read_gold_meta(store: CorpusStore) -> dict[str, Any]:
    """Gold `_meta.json`（版本/构建戳/输入 digest；缺席=空 dict 如实入档）。"""
    path = store.gold_path(GOLD_PROVIDER, GOLD_DATASET) / "_meta.json"
    try:
        meta: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(meta)  # type: ignore[arg-type]


def fetch_decade_rows(
    duck_con: duckdb.DuckDBPyConnection,
    competitions: Sequence[str],
    *,
    end: str | None,
) -> tuple[list[tuple[Any, ...]], str | None, str, int]:
    """
    引擎行集（fd 联赛码过滤；end 含端点、None=数据尾）。

    返回 (行集, gold 版本, 视图状态, 重复 sid 数)。无 start 下界——窗口
    前历史行是训练热身池，start 门由引擎的预测面施加。重复 sid（上游
    腐败/未来多分区布局）按 (league, day, sid) 定序取首行去重并计数，
    保持审计面 UNIQUE(run_id, match_key) 与下注面身份一致（correctness
    F3）。视图缺席（未建桥/空语料）= 合法初生态返回 "missing" 由调用方
    入档，不冒充零过滤数据；**只捕视图缺席形态**（Catalog/IOException，
    gold.py 同款）——参数/转换错误 fail-loud，不冒充基础设施缺失（F2）。
    """
    try:
        leagues = [FD_TO_LEAGUE[code] for code in competitions]
    except KeyError as exc:
        raise ValueError(
            f"未知联赛码 {exc.args[0]!r}（合法码见 data/leagues.LEAGUE_TO_FD）"
        ) from None
    if end is not None:
        try:
            date.fromisoformat(end)
        except ValueError as exc:
            raise ValueError(f"end={end!r} 非法：须零填充 YYYY-MM-DD") from exc
    try:
        rows = duck_con.execute(_DECADE_SQL, [leagues, end, end]).fetchall()
    except (duckdb.CatalogException, duckdb.IOException) as exc:
        logger.warning("match_features 视图不可用（先跑 gold-build + 建桥）: {}", exc)
        return [], None, "missing", 0
    seen: dict[str, tuple[Any, ...]] = {}
    duplicates = 0
    for row in rows:
        sid = str(row[0])
        if sid in seen:
            duplicates += 1
            continue
        seen[sid] = row
    version = next((str(row[-1]) for row in rows if row[-1] is not None), None)
    return list(seen.values()), version, "ok", duplicates


# 报告面取数列（票 15 解锁判定）：era 收盘族 + AH/OU 收盘族（中位数共识）
# + 赛果。同一卫生入口（已赛 + admin_excluded=false）+ 11 重叠联赛范围
# （语料含杯赛/挪超等 CorpusScope 外联赛，correctness F3——报告口径=
# 十年 11 联赛，与引擎面同范围）。
_MARKET_FACE_SQL = """
    SELECT sid, league,
           strftime(CAST(kickoff AS DATE), '%Y-%m-%d') AS day,
           season, era, home_goals, away_goals,
           psc_home, psc_draw, psc_away,
           avgc_home, avgc_draw, avgc_away,
           close1x2_h, close1x2_d, close1x2_a,
           close1x2_cons_h, close1x2_cons_d, close1x2_cons_a,
           ah_close_line_med, ah_close_home_water_med, ah_close_away_water_med,
           ou_close_line_med, ou_close_over_water_med, ou_close_under_water_med
    FROM match_features
    WHERE home_goals IS NOT NULL AND away_goals IS NOT NULL
      AND admin_excluded = false
      AND league IN (SELECT unnest(?))
    ORDER BY league, day, sid
"""


def fetch_market_face_rows(
    duck_con: duckdb.DuckDBPyConnection,
) -> list[dict[str, Any]]:
    """
    全量报告行（票 15：era 收盘 + AH/OU 收盘族中位数 + 赛果，dict 行）。

    口径=11 重叠联赛（与引擎面同范围；语料含杯赛/挪超等 CorpusScope
    外联赛，报告不采）。视图缺席（未建桥/空语料）抛 RuntimeError（报告
    面非引擎面，缺席即运维前置缺失，不落"诚实空"报告）。sid 去重对齐
    引擎面（定序取首）。
    """
    leagues = [FD_TO_LEAGUE[code] for code in sorted(FD_TO_LEAGUE)]
    try:
        result = duck_con.execute(_MARKET_FACE_SQL, [leagues])
        names = [str(d[0]) for d in result.description]
        rows = result.fetchall()
    except (duckdb.CatalogException, duckdb.IOException) as exc:
        raise RuntimeError(
            "match_features 视图不可用（先跑 gold-build + 建桥）"
        ) from exc
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        seen.setdefault(str(row[0]), dict(zip(names, row, strict=True)))
    return list(seen.values())
