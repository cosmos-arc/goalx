"""表→归属域的规范登记（ADR-0008）：执法测试与 schema 文档导出共用。"""

from __future__ import annotations

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
        # 票 44：官方赛果事实源观测 + 对账 + 覆盖维表（reconcile/uniform）
        "uniform_result_observations",
        "draw_reconciliation_runs",
        "source_coverage",
        # 票 45：Understat xG 特征层（ingest/understat.py）
        "understat_matches",
        "understat_sync_runs",
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
    # 票 09/11：LLM 线域（M3）——divergences 脚手架表移交 llm
    "llm": {"intel_observations", "divergences", "review_items", "blind_reviews"},
}

# 文档呈现顺序（按数据→模型→情报→投注→评测的管线方向）
DOMAIN_ORDER: tuple[str, ...] = ("data", "modelling", "llm", "betting", "evaluation")

# 未登记表 = migrations 播种/脚手架（markets/selections 种子、ev_assessments
# 预留），无归属包写它们；文档归入「infra/种子」组。
