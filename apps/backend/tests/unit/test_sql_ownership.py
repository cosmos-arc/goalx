"""ADR-0008 执法：表 SQL 只出现在归属包（db/migrations 拥有 DDL 与种子）。"""

from __future__ import annotations

import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "goalx_backend"
INFRA = {"db.py", "migrations.py"}  # schema_migrations/DDL 归 infra，不受限

OWNERS: dict[str, set[str]] = {
    "data": {
        "fixtures",
        "teams",
        "competitions",
        "match_codes",
        "odds_snapshots",
        "sale_statuses",
        "quote_observations",
        "draw_results",
        "draw_result_revisions",
        "draw_sync_runs",
        "hist_matches",
        "cost_ledger",
        # 票 43：彩池域表归 data/pool.py
        "pool_periods",
        "pool_states",
        "public_shares",
        "pool_matches",
        "pool_sync_runs",
    },
    "modelling": {"forecasts", "team_aliases"},
    "evaluation": {
        "backtest_runs",
        "backtest_predictions",
        "backtest_bets",
        "backtest_metrics",
        "clv_records",
        "haircut_calibrations",
    },
    "betting": {
        "bets",
        "bet_legs",
        "bet_slips",
        "combinations",
        "pool_picks",
        "settlements",
        "settlement_revisions",
        "bankroll_events",
    },
}
TABLE = re.compile(r"(?:FROM|JOIN|INTO|UPDATE)\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
# 仅匹配大写动词：仓库 SQL 关键字全大写，避开 "Insert or update a..." 类 docstring
SQL_START = re.compile(
    r"""^\s*(?:[rbfuRBFU]{0,2})(?:"""
    + '"""'
    + r"""|'''|"|')\s*(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b"""
)
SQL_KEYWORDS = {
    "set",
    "values",
    "or",
    "and",
    "on",
    "as",
    "with",
    "not",
    "exists",
    "select",
    "index",
    "table",
    "all",
    "union",
    "where",
    "from",
    "by",
}


def sql_tables(path: Path) -> set[str]:
    """文件里 SQL 语句字符串（以动词开头）中出现的表名；docstring 不误伤。"""
    tables: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type != tokenize.STRING or not SQL_START.match(tok.string):
                continue
            tables.update(
                m.group(1).lower()
                for m in TABLE.finditer(tok.string)
                if m.group(1).lower() not in SQL_KEYWORDS
            )
    return tables


def test_table_sql_stays_in_owning_package() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC)
        if "__pycache__" in rel.parts or path.name in INFRA:
            continue
        pkg = rel.parts[0] if len(rel.parts) > 1 else "(top)"
        for table in sql_tables(path):
            owner = next((p for p, ts in OWNERS.items() if table in ts), None)
            if owner is None:
                violations.append(f"{path}: 表 {table} 未登记归属(新表请更新 OWNERS)")
            elif pkg != owner:
                violations.append(f"{path}: 表 {table} 归 {owner} 包(ADR-0008)")
    assert not violations, "\n".join(violations)
