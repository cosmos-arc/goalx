"""
goalx 命令行入口：迁移与一次性采集/结算任务。

日常定时采集走 Prefect deployments；本 CLI 覆盖初始化与手工补跑：

    uv run python -m goalx_backend.cli migrate
    uv run python -m goalx_backend.cli ingest-jingcai
    uv run python -m goalx_backend.cli ingest-odds
    uv run python -m goalx_backend.cli ingest-hist
    uv run python -m goalx_backend.cli settle
"""

from __future__ import annotations

import argparse
import sys

from loguru import logger

from goalx_backend.config import get_settings
from goalx_backend.db import connect, migrate
from goalx_backend.ingest import fdhist, oddsapi, sporttery
from goalx_backend.ingest.oddsapi import polite_client
from goalx_backend.services import run_settlement


def _cmd_migrate() -> None:
    """执行 schema 迁移。"""
    conn = connect()
    version = migrate(conn)
    conn.close()
    logger.info("schema at version {}", version)


def _cmd_ingest_jingcai() -> None:
    """手动拉一次竞彩快照。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            payload = sporttery.fetch_calculator_payload(settings, client)
        stats = sporttery.store_matches(conn, sporttery.parse_matches(payload))
        logger.info(
            "matches={} snapshots={} dup={}",
            stats.matches,
            stats.snapshots,
            stats.duplicate_snapshots,
        )
    finally:
        conn.close()


def _cmd_ingest_odds() -> None:
    """手动拉一次欧赔(走 credit 护栏)。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = oddsapi.fetch_and_store_odds(conn, settings, client)
        logger.info(
            "events={} snapshots={} credits={} unmatched={}",
            stats.events,
            stats.snapshots,
            stats.credits_used,
            stats.unmatched,
        )
    finally:
        conn.close()


def _cmd_ingest_hist() -> None:
    """导入历史底座(五大三季)。"""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        with polite_client() as client:
            stats = fdhist.import_history(conn, settings, client)
        logger.info(
            "rows={} written={} skipped={}", stats.rows, stats.written, stats.skipped
        )
    finally:
        conn.close()


def _cmd_settle() -> None:
    """手动结算批跑。"""
    conn = connect()
    try:
        migrate(conn)
        logger.info("settlement: {}", run_settlement(conn))
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数。"""
    parser = argparse.ArgumentParser(prog="goalx", description="goalx 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="执行 schema 迁移")
    sub.add_parser("ingest-jingcai", help="手动拉一次竞彩快照")
    sub.add_parser("ingest-odds", help="手动拉一次欧赔(走 credit 护栏)")
    sub.add_parser("ingest-hist", help="导入五大三季历史底座")
    sub.add_parser("settle", help="手动结算批跑")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。"""
    args = build_parser().parse_args(argv)
    handlers = {
        "migrate": _cmd_migrate,
        "ingest-jingcai": _cmd_ingest_jingcai,
        "ingest-odds": _cmd_ingest_odds,
        "ingest-hist": _cmd_ingest_hist,
        "settle": _cmd_settle,
    }
    handlers[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
