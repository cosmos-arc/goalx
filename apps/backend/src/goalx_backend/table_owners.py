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
        # 票 49 采集先行：源B欧指变化时序（ingest/srcb.py）
        "srcb_change_rows",
        "srcb_change_runs",
        # 票 55 切片 11：CorpusStore 语料树 checkpoint（data/corpus_store.py，
        # 语料树独立 SQLite，不进运行面 migrations）
        "raw_artifacts",
        # 票 55 切片 13：夜班台账（日级状态 + 每夜摘要，同 checkpoint 库）
        "srct_day_status",
        # 票 18：老季深度判定（同 checkpoint 库，跨夜不重探）
        "srct_season_depth",
        # 票 65 当期班：sid 建档（联赛/开球/scope 资格）
        "srct_shift_matches",
        # 票 71 JC 当期拍：matchId 建档（kickoff/收口旗）
        "jc_shift_matches",
        # 票 67 JC 回填日账（done 日不重枚举）
        "jc_backfill_days",
        # 票 55/56 切片 14/15：corpus.duckdb 银层四视图（data 包建视图并查询）
        "fixture_universe",
        "xg_observation",
        "odds_change_event",
        "bookmaker",
        "srct_night_summaries",
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

# 未登记表 = migrations 播种（markets/selections 种子），无归属包写它们；
# ev_assessments 脚手架已 DROP（票 48：派生信号不落表）。
