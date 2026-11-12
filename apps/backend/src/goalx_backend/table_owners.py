"""表→归属域的规范登记（ADR-0008）：SQL 归属执法测试消费。"""

from __future__ import annotations

OWNERS: dict[str, set[str]] = {
    "data": {
        "fixtures",
        "teams",
        "competitions",
        "match_codes",
        # 票 77：跨源场次映射物化（data/mapping.py）
        "source_match_links",
        "odds_snapshots",
        "sale_statuses",
        # 票 08：新鲜度双口径影子测量（data/quote_evidence.py）
        "freshness_shadow",
        # measure-07：同步报价账本（data/price_ledger.py，A 线仪器）
        "price_deviation_ledger",
        "quote_observations",
        "had_quote_confirmations",
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
        # 票 74：clubelo Elo 评级层（ingest/clubelo.py）
        "elo_ratings",
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
        # PR#125：月度探针账本（同 checkpoint 库，任一月正证据即升全深）
        "srct_season_probe_months",
        # 票 65 当期班：sid 建档（联赛/开球/scope 资格）
        "srct_shift_matches",
        # 票 71 JC 当期拍：matchId 建档（kickoff/收口旗）
        "jc_shift_matches",
        # 票 67 JC 回填日账（done 日不重枚举）
        "jc_backfill_days",
        "srct_deep_backfill_days",
        # 票 55/56 切片 14/15：corpus.duckdb 银层四视图（data 包建视图并查询）
        "fixture_universe",
        "xg_observation",
        "odds_change_event",
        "bookmaker",
        "srct_night_summaries",
        # autoresearch 票 18：detail 银层视图（data/coach_crosscheck 只读消费）
        "match_detail",
        # 票 78：自算 Elo 逐场赛前值（ingest/elo_silver.py 物化）
        "elo_self",
        # 票 79：卫报语料游标状态机 + 500/日请求账本（checkpoint 库）
        "guardian_sync_state",
        "guardian_request_days",
        # autoresearch 票 17：新闻 PIT 前向夜拍游标 + 逐拍摘要
        # （ingest/news_pit.py，同 checkpoint 库）
        "news_pit_state",
        "news_pit_beats",
        # 2026-09-30：bronze sid 索引 + 文件锚台账（解析层防重 O(1) 载体）
        "bronze_sids",
        "bronze_sid_backfill",
        # 票 11：增量物化状态（水位+计数护栏快照；DDL 在 corpus_store，
        # 消费方 data/ingest/srct_remat.py）
        "srct_remat_state",
        # odds-read-switch 票 03：remat 互斥锁 + live 锚定分区 sidecar
        "srct_remat_lock",
        "srct_live_sid_partitions",
        # backtest-decade A4：行政判赛排除表（data/hygiene.py 单一取数口）
        "admin_match_exclusions",
        # backtest-decade B：gold 特征面（data/gold.py 物化，语料树 parquet）
        "match_features",
        # autoresearch 票 18：TM 教练任职层 + 换帅事件派生面
        # （ingest/tm_coach.py 采集；data/coach_changes.py 派生重建）
        "tm_coach_tenures",
        "tm_club_seasons",
        "coach_change_events",
        "coach_change_samples",
        "tm_coach_sync_runs",
        # vision-build 票 09：六轴 ② 阵型族条件面 + ④ 底蕴硬层四因子
        # （data/formations.py + data/pedigree.py 派生重建）
        "formation_family_facet",
        "club_pedigree",
        "face_derive_state",
        # mining-loop 票 09：源T 任期/队籍 silver 两表（data/srct_tenure_silver
        # 全量重建；票 10 身份切换的读缝底座，v61）
        "srct_coach_tenures",
        "srct_club_seasons",
    },
    "modelling": {"forecasts", "team_aliases", "forecast_coverage"},
    "evaluation": {
        "backtest_runs",
        "backtest_predictions",
        "backtest_bets",
        "backtest_metrics",
        "clv_records",
        "haircut_calibrations",
        # auto-paper 票 01：决策锚基准冻结（evaluation/baseline_freeze.py）
        "market_baseline_snapshots",
        # auto-paper 票 02：前瞻准确率评分行（evaluation/accuracy.py）
        "forecast_scores",
        # autoresearch 票 01/04：探索轨发现台账 + 扫描器观测留痕
        # （evaluation/edge_scan.py）
        "edge_findings",
        "edge_observations",
        # autoresearch 票 34：断点哨兵周级留痕（evaluation/anchor_sentinel.py）
        "anchor_sentinel_weekly",
        # vision-build 票 02：假设注册表两表（evaluation/hypothesis_registry.py）
        "hypothesis_registry",
        "hypothesis_attempts",
        # vision-build 票 08：收缩双钟两账（evaluation/contraction.py）
        "contraction_transitions",
        "contraction_clock_readings",
        # vision-build 票 03：周批 agent 三账（evaluation/weekly_agent.py；
        # 锚账写口在 contraction.ensure_exploration_anchor）
        "llm_proposal_costs",
        "weekly_agent_state",
        "exploration_clock_anchor",
        # vision-build 票 04 缝回填：四信号触发史账（evaluation/
        # contraction.py 属主——读口 B.3/晨检；写口=月读 record_rescan_signals）
        "rescan_signal_events",
        # vision-build 票 06：RAG 向量表两索引共载（evaluation/rag.py 属主：
        # 写口=embed_pending 增量管线 CLI；读口=DuckDB 余弦 search+晨检落后数）
        "rag_embeddings",
        # vision-build 票 07：影子重训触发台账（evaluation/shadow.py 属主：
        # 写口=run_shadow_retrain_beat；读口=月读⑤ retrain_event_counts）
        "shadow_retrain_events",
        # vision-build 票 11：challenger 质量门两账（evaluation/quality_gate.py
        # 属主：冻结面写口=freeze_gate_spec（注册拍）；门账写口=
        # evaluate_quality_gate（人 CLI）；读口=promote 硬门+晨检 26 项+月读⑤）
        "quality_gate_specs",
        "quality_gate_events",
    },
    "betting": {
        "bets",
        "bet_legs",
        # auto-paper 票 03：auto 锚执行台账（写口=probes auto-v2；史行含已退役 auto-v1）
        "auto_paper_runs",
        "bet_slips",
        "bet_price_timeline",
        "combinations",
        "pool_picks",
        "settlements",
        "settlement_revisions",
        "bankroll_events",
        "funnel_days",
        "funnel_fixture_days",
        "track2_snapshots",
        "frozen_decisions",
        "frozen_decision_divergences",
        # vision-build 票 01：策略版本状态机两表（betting/versioning.py）
        "strategy_versions",
        "version_transitions",
    },
    # 票 09/11：LLM 线域（M3）——divergences 脚手架表移交 llm
    "llm": {"intel_observations", "divergences", "review_items", "blind_reviews"},
}

# 未登记表 = migrations 播种（markets/selections 种子），无归属包写它们；
# ev_assessments 脚手架已 DROP（票 48：派生信号不落表）。
