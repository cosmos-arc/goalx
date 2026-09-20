# 数据库与领域模型

> 面向"下一个不了解库的会话"：表结构全列、领域实体映射、表间关系一页建立心智模型。
> 术语定义以根目录 [CONTEXT.md](../CONTEXT.md) 为单一事实源，本文不重复定义，只补"落在哪张表、归哪个包、什么生命周期"。
>
> **生成块**：`<!-- schema-doc:BEGIN/END -->` 标记内的内容（全列表格/ER 图/唯一键/触发器）由
> `task schema-doc-export` 从迁移后的 schema 生成，**不要手改**——改了会被
> `test_schema_doc.py` 断言红叉。迁移加列/加表后：重跑导出 + 在对应域章节补注解。
> 表的写权限归归属包（ADR-0008，`table_owners.py` 登记，执法测试见 `test_sql_ownership.py`）。

## 实体 ↔ 表 映射矩阵

生命周期口径：**append** = 只增不改；**append+幂等** = 只增 + 唯一键吸收重复；**upsert** = 最新值覆盖；**种子** = migrations 播种基本不动。

| CONTEXT.md 实体 | 表 | 归属包 | 生命周期 |
| --- | --- | --- | --- |
| Competition | `competitions` | data | append |
| Fixture | `fixtures` | data | append（映射列可 upsert） |
| MatchCode | `match_codes` | data | append+幂等 (kind,business_date,code) |
| Market / Selection | `markets` / `selections` | infra 种子 | 种子 |
| OddsSnapshot | `odds_snapshots` | data | append+幂等，触发器禁改删 |
| （报价证据） | `quote_observations` | data | append+幂等 raw_sha256 |
| （销售状态） | `sale_statuses` | data | append |
| PoolPeriod | `pool_periods` | data(pool) | append+幂等 |
| PoolState | `pool_states` | data(pool) | upsert（最新公布） |
| PublicShare | `public_shares` | data(pool) | append |
| （池对阵） | `pool_matches` | data(pool) | append+幂等 |
| Forecast | `forecasts` | modelling | append+幂等 (fixture,track,content_hash) |
| （历史底座） | `hist_matches` | data | append+幂等 |
| IntelObservation | `intel_observations` | llm | append+幂等，触发器禁改删 |
| Divergence | `divergences` | llm | append |
| （复核队列） | `review_items` | llm | append+幂等，open→done |
| （盲评） | `blind_reviews` | llm | append+幂等 (cycle,fixture) |
| Bet | `bets` | betting | append，状态机推进 |
| BetLeg | `bet_legs` | betting | append |
| BetSlip | `bet_slips` | betting | append |
| Combination | `combinations` | betting | append（票提交物化） |
| （池票选场） | `pool_picks` | betting | append |
| Settlement | `settlements` | betting | append+幂等 |
| （结算更正） | `settlement_revisions` | betting | append |
| Bankroll(BankrollEvent) | `bankroll_events` | betting | append |
| DrawResult | `draw_results` | data(results) | upsert 可更正 |
| （开奖更正） | `draw_result_revisions` | data(results) | append |
| （同步运行） | `draw_sync_runs` / `pool_sync_runs` | data | append |
| CostLedger | `cost_ledger` | data(results) | append |
| （回测） | `backtest_runs/-predictions/-bets/-metrics` | evaluation | append |
| （CLV） | `clv_records` | evaluation | append+幂等 |
| （折价校准） | `haircut_calibrations` | evaluation | append |
| EVAssessment | `ev_assessments` | （脚手架，未投产） | — |

无表实体：MarketGroup（口径约定，无存储）、MatchIntel（票 09 废弃，由 IntelObservation 承接）、EvidenceSummary（视图态，渲染时组装）、DecisionKey（betting 包函数口径）、ClosingLine（odds_snapshots.purpose='closing' 切片）。

## 表间关系总览（ER）

只画主键与外键边；全列细节见下文各表。可空 FK 用 `}|o`（零或多），非空 `}o`（一或多）。

<!-- schema-doc:BEGIN:er -->
erDiagram
    backtest_bets {
        INTEGER id PK
    }
    backtest_metrics {
        INTEGER id PK
    }
    backtest_predictions {
        INTEGER id PK
    }
    backtest_runs {
        INTEGER id PK
    }
    bankroll_events {
        INTEGER id PK
    }
    bet_legs {
        INTEGER id PK
    }
    bet_slips {
        INTEGER id PK
    }
    bets {
        INTEGER id PK
    }
    blind_reviews {
        INTEGER id PK
    }
    clv_records {
        INTEGER id PK
    }
    combinations {
        INTEGER id PK
    }
    competitions {
        INTEGER id PK
    }
    cost_ledger {
        INTEGER id PK
    }
    divergences {
        INTEGER id PK
    }
    draw_reconciliation_runs {
        INTEGER id PK
    }
    draw_result_revisions {
        INTEGER id PK
    }
    draw_results {
        INTEGER id PK
    }
    draw_sync_runs {
        INTEGER id PK
    }
    ev_assessments {
        INTEGER id PK
    }
    fixtures {
        INTEGER id PK
    }
    forecasts {
        INTEGER id PK
    }
    haircut_calibrations {
        INTEGER id PK
    }
    hist_matches {
        INTEGER id PK
    }
    intel_observations {
        INTEGER id PK
    }
    markets {
        TEXT code PK
    }
    match_codes {
        INTEGER id PK
    }
    odds_snapshots {
        INTEGER id PK
    }
    pool_matches {
        INTEGER id PK
    }
    pool_periods {
        INTEGER id PK
    }
    pool_picks {
        INTEGER id PK
    }
    pool_states {
        INTEGER pool_period_id PK
    }
    pool_sync_runs {
        INTEGER id PK
    }
    public_shares {
        INTEGER id PK
    }
    quote_observations {
        INTEGER id PK
    }
    review_items {
        INTEGER id PK
    }
    sale_statuses {
        INTEGER id PK
    }
    selections {
        INTEGER id PK
    }
    settlement_revisions {
        INTEGER id PK
    }
    settlements {
        INTEGER id PK
    }
    source_coverage {
        INTEGER id PK
    }
    team_aliases {
        INTEGER id PK
    }
    teams {
        INTEGER id PK
    }
    understat_matches {
        INTEGER id PK
    }
    understat_sync_runs {
        INTEGER id PK
    }
    uniform_result_observations {
        INTEGER id PK
    }
    backtest_bets }o--|| backtest_runs : run_id
    backtest_metrics }o--|| backtest_runs : run_id
    backtest_predictions }o--|| hist_matches : hist_match_id
    backtest_predictions }o--|| backtest_runs : run_id
    bankroll_events }|o--|| bet_slips : slip_id
    bankroll_events }|o--|| bets : bet_id
    bet_legs }o--|| selections : market_code
    bet_legs }o--|| selections : selection_code
    bet_legs }|o--|| odds_snapshots : snapshot_id
    bet_legs }o--|| fixtures : fixture_id
    bet_legs }o--|| bets : bet_id
    bet_slips }|o--|| pool_periods : pool_period_id
    bets }|o--|| bet_slips : slip_id
    blind_reviews }o--|| fixtures : fixture_id
    clv_records }o--|| fixtures : fixture_id
    clv_records }o--|| bets : bet_id
    combinations }o--|| bet_slips : slip_id
    divergences }o--|| fixtures : fixture_id
    draw_result_revisions }o--|| fixtures : fixture_id
    draw_results }o--|| fixtures : fixture_id
    ev_assessments }|o--|| pool_periods : pool_period_id
    ev_assessments }|o--|| fixtures : fixture_id
    fixtures }o--|| teams : away_team_id
    fixtures }o--|| teams : home_team_id
    fixtures }o--|| competitions : competition_id
    forecasts }o--|| fixtures : fixture_id
    intel_observations }o--|| fixtures : fixture_id
    match_codes }o--|| fixtures : fixture_id
    odds_snapshots }o--|| selections : market_code
    odds_snapshots }o--|| selections : selection_code
    odds_snapshots }|o--|| quote_observations : observation_id
    odds_snapshots }o--|| fixtures : fixture_id
    pool_matches }o--|| pool_periods : pool_period_id
    pool_periods }o--|| markets : market_code
    pool_picks }|o--|| fixtures : fixture_id
    pool_picks }o--|| bet_slips : slip_id
    pool_states }|o--|| pool_periods : pool_period_id
    public_shares }o--|| pool_periods : pool_period_id
    review_items }o--|| fixtures : fixture_id
    sale_statuses }|o--|| quote_observations : observation_id
    sale_statuses }o--|| fixtures : fixture_id
    selections }o--|| markets : market_code
    settlement_revisions }o--|| settlements : settlement_id
    settlements }|o--|| bet_slips : slip_id
    settlements }|o--|| bets : bet_id
    team_aliases }o--|| teams : team_id
    understat_matches }|o--|| fixtures : fixture_id
    uniform_result_observations }|o--|| fixtures : fixture_id
<!-- schema-doc:END:er -->

## data 域（fixtures / results / pool / ingest 支撑）

### 赛程与市场骨架

#### `teams` / `competitions`

队伍与赛事典：canonical_name 唯一语义；competitions.tier = tier1/tier2 投入分层（Tier1 主动、Tier2 被覆盖），odds_api_sport_key 是欧赔源 join 键。

#### `fixtures`

场次主表，UTC 记时。odds_api_event_id/join_method/joined_at 记欧赔事件映射（auto/manual）；stage 预留杯赛阶段（analyst 复核触发依据之一）。

#### `match_codes`

官方销售编号（竞彩"周六001"/胜负彩期次场号），business_date = 北京业务日。source_match_id 承载源站场次 id（澳客 formation 零映射靠它）。 UNIQUE(kind, business_date, code) 幂等入库。

#### `markets` / `selections`（infra 种子）

玩法（had/hhad/crs/ttg/…）与选项字典，migrations 播种；业务表 FK 引用保证口径一致，无归属包写它们。

### 报价与销售证据

#### `odds_snapshots`

报价快照 append-only：source（sporttery=竞彩、odds_api:<book>=欧赔）、purpose（live_capture/closing）。observation_id 指向原始观测存证。**触发器禁 UPDATE/DELETE**——CLV 与回测的分母铁证。

#### `quote_observations`

原始报价观测（证据层，票 35）：endpoint+parse_version+raw_sha256 存证，两次捕获共用一次观测。append-only。

#### `sale_statuses`

销售状态/单固资格时序（竞彩快照解析），append。

### 开奖与结算事实

#### `draw_results`

开奖**唯一事实源**：upsert 可更正（更正必须走 revision 留痕）。void 场次标记在此。

#### `draw_result_revisions`

开奖更正留痕（previous/replacement/reason 全存 JSON），append。

#### `draw_sync_runs` / `pool_sync_runs`

官方 uniform 赛果 / 源B 彩池同步的运行日志（fetched/imported/pending_manual
计数），append。票 44 切换（2026-09-20）前赛果同步源为源D。

#### `uniform_result_observations`（票 44）

源A uniform 族官方赛果**观测**：append-only，UNIQUE(match_id, observed_at)
同跑幂等；poolStatus 迁移（空→Payout/Refund）多跑多行留痕。join 键 =
match_codes.source_match_id（一跳确定性）。终态观测（比分或官方 void）经
ingest/uniform 落 draw_results 事实（2026-09-20 用户裁决切换官方为事实源，
源D 降审计）。

#### `draw_reconciliation_runs`（票 44）

对账运行日志（参照源 sporttery.cn/openfootball vs 事实：一致/比分不一致/
void 冲突/缺果计数 + 待人工清单），append。切换官方为事实源的门槛依据。

#### `source_coverage`（票 44，定则 4）

每源每覆盖日"看到了什么"的现态维表（UPSERT，非证据表）——空≠无：
absent 断言仅当 coverage_status='covered'。coverage_date 语义随源
（uniform=matchDate、源D=业务日、openfootball=赛季键、understat=起始年）。

#### `understat_matches`（票 45）

Understat 五大联赛逐场 xG 特征现态表（按源 match_id UPSERT）：逐场
xG/xGA（含点球）与 npxG/npxGA（去点球）、本季**开球日严格早于本场**的
prior_* 累计（防前视红线，同日场次互不可见）、源自带 forecast{w,d,l}
对标基准（仅已赛场次携带）。三时间：datetime_utc=event_time、
observed_at/first_seen_at=本机观测、源不提供发布时间（无列，不伪造）。
fixture_id 为竞彩确定性 join（±1 日 + 双队名解析唯一命中，否则 NULL）。

#### `understat_sync_runs`（票 45）

xG 特征同步运行日志（逐联赛×赛季计数与 join 计数），append。

### 彩池（data/pool.py）

#### `pool_periods` / `pool_matches`

胜负彩/任9 期次与对阵。pool_matches.euro_odds_* 是期次页三向欧指（概率兜底口径）；source_match_id = 澳客场次 id（伤停直爬零映射）。

#### `pool_states` / `public_shares`

销量/滚存（官方公布，upsert 最新）；公众份额快照（源B 人气，append，meta 带注数等量级参考）。

### 历史底座与成本

#### `hist_matches`

football-data.co.uk 历史比赛（五大+N1，含 PSC/AvgC 收盘价）——DC 训练与回测分母。append+幂等导入。

#### `cost_ledger`

系统成本台账（数据源 credit、LLM 折算金额），append；category+note 是口径维度（如 llm_call 的 note=model/surface/purpose）。

## modelling 域

#### `forecasts`

三轨预测 append-only：track = ml（DC 比分矩阵 payload）/ llm（scout/analyst 三项+rationale，analyst 行带 revision_of）/ fused（log-pool，payload 引用双源 id）。同 (fixture, content_hash) 幂等吸收。**ml 轨是真钱资格唯一口径（票 04 冻结），任何轨不可覆写。**

#### `team_aliases`

队名别名（竞彩名 ↔ 训练域名 ↔ odds_api 名对齐），append。

## llm 域（M3）

#### `intel_observations`

情报存证 append-only：kind/form/h2h/伤停、collector 三源（internal-fdhist / okooo-formation / sina-injury）。UNIQUE(fixture_id, collector, raw_hash) 幂等——双源同情报各自成行（互校验）。**触发器禁改删。**

#### `divergences`

ML×LLM JS 散度日志（metric=js_had_ml_llm），append（重复计算自然累积）。

#### `review_items`

复核队列：route=pre_match（gate JS>0.06 Tier1）/post_settle（赛后一对一错）；UNIQUE(fixture_id, route)。结论三分类只进评测集，不改预测工件（票 05 冻结）。

#### `blind_reviews`

盲评双周匿名二选一（choice=ml/llm），UNIQUE(cycle, fixture_id) 幂等。

## betting 域

#### `bets`

注单（mode=paper/live 隔离；market_kind=fixed/pool）。snap_prob_*/snap_ev_* 是锁注时点概率快照（票 41，防时间泄漏）。状态机 open→won/lost/void/partial。

#### `bet_legs` / `bet_slips` / `combinations` / `pool_picks`

腿（引用锁定 OddsSnapshot）/ 票 / 复式组合物化（任9 笛卡尔积落行）/ 池票选场。

#### `settlements` / `settlement_revisions`

结算记录（单关返本、串关无效腿按 1 继续）与更正留痕。

#### `bankroll_events`

资金事件 append-only（出入金/结算兑付），balance_after 链式核对；**只真金记录进 Bankroll**。

## evaluation 域

#### `backtest_runs` / `backtest_predictions` / `backtest_bets` / `backtest_metrics`

回测工件四件套：run（label/params/summary）、prediction（含 had/fair 概率与 model_fingerprint）、bet（含 kelly/EV）、metric（scope=overall/联赛/赛季）。append。

#### `clv_records`

CLV 对账：taken_odds vs close_prob（pinnacle 主锚/betfair 辅/consensus 兜底，close_basis 分层）。append+幂等。

#### `haircut_calibrations`

EV 折价校准（按 scope/market 的 haircut 分位数）。

## infra / 脚手架

#### `ev_assessments`

票 03 预留脚手架，无业务代码写入；EVAssessment 实际口径在视图与 betting 包函数。

---

*以下各表全列由导出器生成（marker 块内勿手改）：*

<!-- schema-doc:BEGIN:table:teams -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `canonical_name` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`canonical_name`)`
<!-- schema-doc:END:table:teams -->

<!-- schema-doc:BEGIN:table:competitions -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `name` | TEXT | NOT NULL |
| `tier` | TEXT | NOT NULL，DEFAULT 'tier2' |
| `odds_api_sport_key` | TEXT | — |
| `api_football_league_id` | INTEGER | — |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`name`)`
<!-- schema-doc:END:table:competitions -->

<!-- schema-doc:BEGIN:table:fixtures -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `competition_id` | INTEGER | NOT NULL，FK→competitions.id |
| `kickoff_utc` | TEXT | NOT NULL |
| `home_team_id` | INTEGER | NOT NULL，FK→teams.id |
| `away_team_id` | INTEGER | NOT NULL，FK→teams.id |
| `stage` | TEXT | — |
| `odds_api_event_id` | TEXT | — |
| `odds_api_sport_key` | TEXT | — |
| `join_method` | TEXT | — |
| `joined_at` | TEXT | — |

唯一键 `UNIQUE(`competition_id`, `kickoff_utc`, `home_team_id`, `away_team_id`)`
<!-- schema-doc:END:table:fixtures -->

<!-- schema-doc:BEGIN:table:match_codes -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `kind` | TEXT | NOT NULL |
| `business_date` | TEXT | NOT NULL |
| `code` | TEXT | NOT NULL |
| `source_match_id` | TEXT | — |
| `is_single` | INTEGER | — |

唯一键 `UNIQUE(`kind`, `business_date`, `code`)`
<!-- schema-doc:END:table:match_codes -->

<!-- schema-doc:BEGIN:table:markets -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `code` | TEXT | PK |
| `name` | TEXT | NOT NULL |
| `kind` | TEXT | NOT NULL |

唯一键 `UNIQUE(`code`)`
<!-- schema-doc:END:table:markets -->

<!-- schema-doc:BEGIN:table:selections -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `market_code` | TEXT | NOT NULL，FK→markets.code |
| `code` | TEXT | NOT NULL |
| `label` | TEXT | NOT NULL |

唯一键 `UNIQUE(`market_code`, `code`)`
<!-- schema-doc:END:table:selections -->

<!-- schema-doc:BEGIN:table:odds_snapshots -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `market_code` | TEXT | NOT NULL，FK→selections.market_code |
| `selection_code` | TEXT | NOT NULL，FK→selections.code |
| `source` | TEXT | NOT NULL |
| `purpose` | TEXT | NOT NULL，DEFAULT 'live_capture' |
| `odds` | REAL | NOT NULL |
| `captured_at` | TEXT | NOT NULL |
| `meta` | TEXT | — |
| `created_at` | TEXT | NOT NULL |
| `observed_at` | TEXT | — |
| `source_updated_at` | TEXT | — |
| `observation_id` | INTEGER | FK→quote_observations.id |

唯一键 `UNIQUE(`fixture_id`, `market_code`, `selection_code`, `source`, `captured_at`, `odds`)`

append-only 触发器：`odds_snapshots_no_delete`、`odds_snapshots_no_update`
<!-- schema-doc:END:table:odds_snapshots -->

<!-- schema-doc:BEGIN:table:quote_observations -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `purpose` | TEXT | NOT NULL，DEFAULT 'live' |
| `observed_at` | TEXT | NOT NULL |
| `source_updated_at` | TEXT | — |
| `snapshot_at` | TEXT | — |
| `endpoint` | TEXT | — |
| `parse_version` | TEXT | NOT NULL |
| `raw_sha256` | TEXT | NOT NULL |
| `raw_ref` | TEXT | — |
| `summary` | TEXT | — |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`quote_observations_no_delete`、`quote_observations_no_update`
<!-- schema-doc:END:table:quote_observations -->

<!-- schema-doc:BEGIN:table:sale_statuses -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `market_code` | TEXT | — |
| `sale_state` | TEXT | NOT NULL |
| `single_eligible` | INTEGER | — |
| `observed_at` | TEXT | NOT NULL |
| `source_updated_at` | TEXT | — |
| `observation_id` | INTEGER | FK→quote_observations.id |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`sale_statuses_no_delete`、`sale_statuses_no_update`
<!-- schema-doc:END:table:sale_statuses -->

<!-- schema-doc:BEGIN:table:draw_results -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `home_goals` | INTEGER | NOT NULL |
| `away_goals` | INTEGER | NOT NULL |
| `half_home_goals` | INTEGER | — |
| `half_away_goals` | INTEGER | — |
| `void` | INTEGER | NOT NULL，DEFAULT 0 |
| `void_reason` | TEXT | — |
| `source` | TEXT | NOT NULL |
| `published_at` | TEXT | — |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`fixture_id`)`
<!-- schema-doc:END:table:draw_results -->

<!-- schema-doc:BEGIN:table:draw_result_revisions -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `previous` | TEXT | NOT NULL |
| `replacement` | TEXT | NOT NULL |
| `reason` | TEXT | NOT NULL |
| `recorded_at` | TEXT | NOT NULL |

append-only 触发器：`draw_result_revisions_no_delete`、`draw_result_revisions_no_update`
<!-- schema-doc:END:table:draw_result_revisions -->

<!-- schema-doc:BEGIN:table:draw_sync_runs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |
| `business_dates` | TEXT | NOT NULL |
| `pages` | INTEGER | NOT NULL |
| `fetched` | INTEGER | NOT NULL |
| `imported` | INTEGER | NOT NULL |
| `unchanged` | INTEGER | NOT NULL |
| `unmatched` | INTEGER | NOT NULL |
| `pending_manual` | TEXT | NOT NULL |
| `parse_version` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`draw_sync_runs_no_delete`、`draw_sync_runs_no_update`
<!-- schema-doc:END:table:draw_sync_runs -->

<!-- schema-doc:BEGIN:table:uniform_result_observations -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `match_id` | INTEGER | NOT NULL |
| `match_num_str` | TEXT | NOT NULL |
| `match_date` | TEXT | NOT NULL |
| `business_date` | TEXT | — |
| `fixture_id` | INTEGER | FK→fixtures.id |
| `league_id` | INTEGER | — |
| `league_name` | TEXT | — |
| `result_status` | TEXT | NOT NULL |
| `pool_status` | TEXT | — |
| `full_score_raw` | TEXT | — |
| `half_score_raw` | TEXT | — |
| `home_goals` | INTEGER | — |
| `away_goals` | INTEGER | — |
| `half_home_goals` | INTEGER | — |
| `half_away_goals` | INTEGER | — |
| `win_flag` | TEXT | — |
| `odds_h` | TEXT | — |
| `odds_d` | TEXT | — |
| `odds_a` | TEXT | — |
| `goal_line` | TEXT | — |
| `betting_single` | INTEGER | — |
| `void_flag` | INTEGER | NOT NULL，DEFAULT 0 |
| `void_reason` | TEXT | — |
| `observed_at` | TEXT | NOT NULL |
| `parse_version` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`match_id`, `observed_at`)`

append-only 触发器：`uniform_result_observations_no_delete`、`uniform_result_observations_no_update`
<!-- schema-doc:END:table:uniform_result_observations -->

<!-- schema-doc:BEGIN:table:draw_reconciliation_runs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |
| `business_dates` | TEXT | NOT NULL |
| `compared` | INTEGER | NOT NULL，DEFAULT 0 |
| `consistent` | INTEGER | NOT NULL，DEFAULT 0 |
| `score_mismatch` | INTEGER | NOT NULL，DEFAULT 0 |
| `void_mismatch` | INTEGER | NOT NULL，DEFAULT 0 |
| `missing_result` | INTEGER | NOT NULL，DEFAULT 0 |
| `unmatched` | INTEGER | NOT NULL，DEFAULT 0 |
| `pending_manual` | TEXT | NOT NULL |
| `parse_version` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`draw_reconciliation_runs_no_delete`、`draw_reconciliation_runs_no_update`
<!-- schema-doc:END:table:draw_reconciliation_runs -->

<!-- schema-doc:BEGIN:table:source_coverage -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `coverage_date` | TEXT | NOT NULL |
| `league_key` | TEXT | NOT NULL，DEFAULT '' |
| `league_name` | TEXT | — |
| `match_count` | INTEGER | NOT NULL，DEFAULT 0 |
| `coverage_status` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |
| `updated_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`source`, `coverage_date`, `league_key`)`
<!-- schema-doc:END:table:source_coverage -->

<!-- schema-doc:BEGIN:table:understat_matches -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `match_id` | TEXT | NOT NULL |
| `league` | TEXT | NOT NULL |
| `season` | TEXT | NOT NULL |
| `datetime_utc` | TEXT | NOT NULL |
| `home_team_id` | TEXT | NOT NULL |
| `home_team` | TEXT | NOT NULL |
| `away_team_id` | TEXT | NOT NULL |
| `away_team` | TEXT | NOT NULL |
| `is_result` | INTEGER | NOT NULL，DEFAULT 0 |
| `goals_home` | INTEGER | — |
| `goals_away` | INTEGER | — |
| `xg_home` | REAL | — |
| `xg_away` | REAL | — |
| `npxg_home` | REAL | — |
| `npxg_away` | REAL | — |
| `prior_npxg_home` | REAL | — |
| `prior_npxga_home` | REAL | — |
| `prior_matches_home` | INTEGER | — |
| `prior_npxg_away` | REAL | — |
| `prior_npxga_away` | REAL | — |
| `prior_matches_away` | INTEGER | — |
| `forecast_w` | REAL | — |
| `forecast_d` | REAL | — |
| `forecast_l` | REAL | — |
| `fixture_id` | INTEGER | FK→fixtures.id |
| `first_seen_at` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`match_id`)`
<!-- schema-doc:END:table:understat_matches -->

<!-- schema-doc:BEGIN:table:understat_sync_runs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |
| `seasons` | TEXT | NOT NULL |
| `matches` | INTEGER | NOT NULL |
| `results` | INTEGER | NOT NULL |
| `joined` | INTEGER | NOT NULL |
| `unmatched` | INTEGER | NOT NULL |
| `failed` | INTEGER | NOT NULL |
| `parse_version` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`understat_sync_runs_no_delete`、`understat_sync_runs_no_update`
<!-- schema-doc:END:table:understat_sync_runs -->

<!-- schema-doc:BEGIN:table:pool_periods -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `market_code` | TEXT | NOT NULL，FK→markets.code |
| `period_no` | TEXT | NOT NULL |
| `sales_deadline` | TEXT | — |

唯一键 `UNIQUE(`market_code`, `period_no`)`
<!-- schema-doc:END:table:pool_periods -->

<!-- schema-doc:BEGIN:table:pool_matches -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `pool_period_id` | INTEGER | NOT NULL，FK→pool_periods.id |
| `match_seq` | INTEGER | NOT NULL |
| `source_match_id` | TEXT | — |
| `league` | TEXT | — |
| `kickoff_utc` | TEXT | NOT NULL |
| `home_team` | TEXT | NOT NULL |
| `away_team` | TEXT | NOT NULL |
| `euro_odds_h` | REAL | — |
| `euro_odds_d` | REAL | — |
| `euro_odds_a` | REAL | — |

唯一键 `UNIQUE(`pool_period_id`, `match_seq`)`
<!-- schema-doc:END:table:pool_matches -->

<!-- schema-doc:BEGIN:table:pool_states -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `pool_period_id` | INTEGER | PK，FK→pool_periods.id |
| `sales_amount` | REAL | — |
| `rollover_in` | REAL | — |
| `prize_tiers` | TEXT | — |
| `published_at` | TEXT | — |
<!-- schema-doc:END:table:pool_states -->

<!-- schema-doc:BEGIN:table:public_shares -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `pool_period_id` | INTEGER | NOT NULL，FK→pool_periods.id |
| `match_seq` | INTEGER | NOT NULL |
| `selection_code` | TEXT | NOT NULL |
| `share` | REAL | NOT NULL |
| `origin` | TEXT | NOT NULL |
| `source` | TEXT | NOT NULL |
| `captured_at` | TEXT | NOT NULL |
| `meta` | TEXT | — |

唯一键 `UNIQUE(`pool_period_id`, `match_seq`, `selection_code`, `origin`, `source`, `captured_at`)`
<!-- schema-doc:END:table:public_shares -->

<!-- schema-doc:BEGIN:table:pool_sync_runs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `source` | TEXT | NOT NULL |
| `observed_at` | TEXT | NOT NULL |
| `period_nos` | TEXT | NOT NULL |
| `pages` | INTEGER | NOT NULL |
| `matches` | INTEGER | NOT NULL |
| `share_rows` | INTEGER | NOT NULL |
| `missing_shares` | INTEGER | NOT NULL |
| `parse_version` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

append-only 触发器：`pool_sync_runs_no_delete`、`pool_sync_runs_no_update`
<!-- schema-doc:END:table:pool_sync_runs -->

<!-- schema-doc:BEGIN:table:hist_matches -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `competition` | TEXT | NOT NULL |
| `season` | TEXT | NOT NULL |
| `match_date` | TEXT | NOT NULL |
| `home_team` | TEXT | NOT NULL |
| `away_team` | TEXT | NOT NULL |
| `fthg` | INTEGER | NOT NULL |
| `ftag` | INTEGER | NOT NULL |
| `ftr` | TEXT | NOT NULL |
| `psc_home` | REAL | — |
| `psc_draw` | REAL | — |
| `psc_away` | REAL | — |
| `avgc_home` | REAL | — |
| `avgc_draw` | REAL | — |
| `avgc_away` | REAL | — |

唯一键 `UNIQUE(`competition`, `season`, `match_date`, `home_team`, `away_team`)`
<!-- schema-doc:END:table:hist_matches -->

<!-- schema-doc:BEGIN:table:cost_ledger -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `occurred_at` | TEXT | NOT NULL |
| `category` | TEXT | NOT NULL |
| `units` | REAL | NOT NULL，DEFAULT 1 |
| `amount_cny` | REAL | NOT NULL，DEFAULT 0 |
| `note` | TEXT | — |
| `meta` | TEXT | — |
<!-- schema-doc:END:table:cost_ledger -->

<!-- schema-doc:BEGIN:table:forecasts -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `track` | TEXT | NOT NULL |
| `model_version` | TEXT | NOT NULL |
| `issued_at` | TEXT | NOT NULL |
| `content_hash` | TEXT | NOT NULL |
| `payload` | TEXT | NOT NULL |

唯一键 `UNIQUE(`fixture_id`, `content_hash`)`

append-only 触发器：`forecasts_no_delete`、`forecasts_no_update`
<!-- schema-doc:END:table:forecasts -->

<!-- schema-doc:BEGIN:table:team_aliases -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `team_id` | INTEGER | NOT NULL，FK→teams.id |
| `source` | TEXT | NOT NULL |
| `alias` | TEXT | NOT NULL |

唯一键 `UNIQUE(`source`, `alias`)`
<!-- schema-doc:END:table:team_aliases -->

<!-- schema-doc:BEGIN:table:intel_observations -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `kind` | TEXT | NOT NULL |
| `text` | TEXT | NOT NULL |
| `source` | TEXT | NOT NULL |
| `collected_at` | TEXT | NOT NULL |
| `collector` | TEXT | NOT NULL |
| `raw_payload` | TEXT | NOT NULL |
| `raw_hash` | TEXT | NOT NULL |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`fixture_id`, `collector`, `raw_hash`)`

append-only 触发器：`intel_observations_no_delete`、`intel_observations_no_update`
<!-- schema-doc:END:table:intel_observations -->

<!-- schema-doc:BEGIN:table:divergences -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `metric` | TEXT | NOT NULL |
| `reference` | TEXT | NOT NULL |
| `value` | REAL | NOT NULL |
| `computed_at` | TEXT | NOT NULL |
<!-- schema-doc:END:table:divergences -->

<!-- schema-doc:BEGIN:table:review_items -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `route` | TEXT | NOT NULL |
| `js_value` | REAL | — |
| `status` | TEXT | NOT NULL，DEFAULT 'open' |
| `verdict` | TEXT | — |
| `note` | TEXT | — |
| `created_at` | TEXT | NOT NULL |
| `decided_at` | TEXT | — |

唯一键 `UNIQUE(`fixture_id`, `route`)`
<!-- schema-doc:END:table:review_items -->

<!-- schema-doc:BEGIN:table:blind_reviews -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `cycle` | TEXT | NOT NULL |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `choice` | TEXT | NOT NULL |
| `note` | TEXT | — |
| `created_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`cycle`, `fixture_id`)`
<!-- schema-doc:END:table:blind_reviews -->

<!-- schema-doc:BEGIN:table:bets -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `slip_id` | INTEGER | FK→bet_slips.id |
| `mode` | TEXT | NOT NULL |
| `market_kind` | TEXT | NOT NULL |
| `purchased` | INTEGER | NOT NULL，DEFAULT 0 |
| `stake` | REAL | NOT NULL |
| `placed_at` | TEXT | — |
| `created_at` | TEXT | NOT NULL |
| `status` | TEXT | NOT NULL，DEFAULT 'open' |
| `payout` | REAL | — |
| `profit` | REAL | — |
| `settled_at` | TEXT | — |
| `strategy_version` | TEXT | — |
| `actual_stake` | REAL | — |
| `snap_prob_consensus` | REAL | — |
| `snap_prob_model` | REAL | — |
| `snap_ev_consensus` | REAL | — |
| `snap_ev_model` | REAL | — |
<!-- schema-doc:END:table:bets -->

<!-- schema-doc:BEGIN:table:bet_legs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `bet_id` | INTEGER | NOT NULL，FK→bets.id |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `market_code` | TEXT | NOT NULL，FK→selections.market_code |
| `selection_code` | TEXT | NOT NULL，FK→selections.code |
| `locked_odds` | REAL | NOT NULL |
| `snapshot_id` | INTEGER | FK→odds_snapshots.id |
| `meta` | TEXT | — |
| `actual_odds` | REAL | — |
<!-- schema-doc:END:table:bet_legs -->

<!-- schema-doc:BEGIN:table:bet_slips -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `mode` | TEXT | NOT NULL |
| `source` | TEXT | NOT NULL，DEFAULT 'manual' |
| `pool_period_id` | INTEGER | FK→pool_periods.id |
| `placed_at` | TEXT | — |
| `note` | TEXT | — |
| `created_at` | TEXT | NOT NULL |
<!-- schema-doc:END:table:bet_slips -->

<!-- schema-doc:BEGIN:table:combinations -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `slip_id` | INTEGER | NOT NULL，FK→bet_slips.id |
| `seq` | INTEGER | NOT NULL |
| `stake` | REAL | NOT NULL |
| `selections` | TEXT | NOT NULL |
| `hit` | INTEGER | — |
| `payout` | REAL | — |

唯一键 `UNIQUE(`slip_id`, `seq`)`
<!-- schema-doc:END:table:combinations -->

<!-- schema-doc:BEGIN:table:pool_picks -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `slip_id` | INTEGER | NOT NULL，FK→bet_slips.id |
| `match_seq` | INTEGER | NOT NULL |
| `fixture_id` | INTEGER | FK→fixtures.id |
| `selection_code` | TEXT | NOT NULL |

唯一键 `UNIQUE(`slip_id`, `match_seq`, `selection_code`)`
<!-- schema-doc:END:table:pool_picks -->

<!-- schema-doc:BEGIN:table:settlements -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `bet_id` | INTEGER | FK→bets.id |
| `slip_id` | INTEGER | FK→bet_slips.id |
| `status` | TEXT | NOT NULL |
| `stake` | REAL | NOT NULL |
| `payout` | REAL | NOT NULL |
| `profit` | REAL | NOT NULL |
| `detail` | TEXT | NOT NULL |
| `computed_at` | TEXT | NOT NULL |

唯一键 `UNIQUE(`bet_id`)`

唯一键 `UNIQUE(`slip_id`)`
<!-- schema-doc:END:table:settlements -->

<!-- schema-doc:BEGIN:table:settlement_revisions -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `settlement_id` | INTEGER | NOT NULL，FK→settlements.id |
| `previous` | TEXT | — |
| `replacement` | TEXT | NOT NULL |
| `reason` | TEXT | NOT NULL |
| `recorded_at` | TEXT | NOT NULL |

append-only 触发器：`settlement_revisions_no_delete`、`settlement_revisions_no_update`
<!-- schema-doc:END:table:settlement_revisions -->

<!-- schema-doc:BEGIN:table:bankroll_events -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `occurred_at` | TEXT | NOT NULL |
| `kind` | TEXT | NOT NULL |
| `amount_cny` | REAL | NOT NULL |
| `balance_after` | REAL | NOT NULL |
| `bet_id` | INTEGER | FK→bets.id |
| `slip_id` | INTEGER | FK→bet_slips.id |
| `note` | TEXT | — |
<!-- schema-doc:END:table:bankroll_events -->

<!-- schema-doc:BEGIN:table:backtest_runs -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `label` | TEXT | NOT NULL |
| `params` | TEXT | NOT NULL |
| `status` | TEXT | NOT NULL，DEFAULT 'running' |
| `created_at` | TEXT | NOT NULL |
| `finished_at` | TEXT | — |
| `summary` | TEXT | — |
<!-- schema-doc:END:table:backtest_runs -->

<!-- schema-doc:BEGIN:table:backtest_predictions -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `run_id` | INTEGER | NOT NULL，FK→backtest_runs.id |
| `hist_match_id` | INTEGER | NOT NULL，FK→hist_matches.id |
| `competition` | TEXT | NOT NULL |
| `season` | TEXT | NOT NULL |
| `match_date` | TEXT | NOT NULL |
| `home_team` | TEXT | NOT NULL |
| `away_team` | TEXT | NOT NULL |
| `had_probs` | TEXT | NOT NULL |
| `fair_probs` | TEXT | NOT NULL |
| `fair_source` | TEXT | NOT NULL |
| `model_fingerprint` | TEXT | NOT NULL |
| `train_window_end` | TEXT | NOT NULL |

唯一键 `UNIQUE(`run_id`, `hist_match_id`)`
<!-- schema-doc:END:table:backtest_predictions -->

<!-- schema-doc:BEGIN:table:backtest_bets -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `run_id` | INTEGER | NOT NULL，FK→backtest_runs.id |
| `kind` | TEXT | NOT NULL |
| `competition` | TEXT | NOT NULL |
| `placed_week` | TEXT | NOT NULL |
| `stake` | REAL | NOT NULL |
| `legs` | TEXT | NOT NULL |
| `ev` | REAL | NOT NULL |
| `kelly` | REAL | — |
| `status` | TEXT | NOT NULL |
| `payout` | REAL | NOT NULL |
| `profit` | REAL | NOT NULL |
| `detail` | TEXT | — |
<!-- schema-doc:END:table:backtest_bets -->

<!-- schema-doc:BEGIN:table:backtest_metrics -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `run_id` | INTEGER | NOT NULL，FK→backtest_runs.id |
| `scope` | TEXT | NOT NULL |
| `metrics` | TEXT | NOT NULL |

唯一键 `UNIQUE(`run_id`, `scope`)`
<!-- schema-doc:END:table:backtest_metrics -->

<!-- schema-doc:BEGIN:table:clv_records -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `bet_id` | INTEGER | NOT NULL，FK→bets.id |
| `fixture_id` | INTEGER | NOT NULL，FK→fixtures.id |
| `market_code` | TEXT | NOT NULL |
| `selection_code` | TEXT | NOT NULL |
| `taken_odds` | REAL | NOT NULL |
| `close_prob` | REAL | NOT NULL |
| `clv_prob` | REAL | NOT NULL |
| `close_source` | TEXT | NOT NULL |
| `minutes_to_kickoff` | REAL | — |
| `computed_at` | TEXT | NOT NULL |
| `close_basis` | TEXT | — |

唯一键 `UNIQUE(`bet_id`, `fixture_id`)`
<!-- schema-doc:END:table:clv_records -->

<!-- schema-doc:BEGIN:table:haircut_calibrations -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `scope` | TEXT | NOT NULL |
| `market_code` | TEXT | NOT NULL |
| `haircut` | REAL | NOT NULL |
| `n_samples` | INTEGER | NOT NULL |
| `quartiles` | TEXT | NOT NULL |
| `source` | TEXT | NOT NULL |
| `computed_at` | TEXT | NOT NULL |
| `n_fixtures` | INTEGER | NOT NULL，DEFAULT 0 |
| `method_version` | TEXT | NOT NULL，DEFAULT 'shin_mean_v0' |

唯一键 `UNIQUE(`scope`, `market_code`)`
<!-- schema-doc:END:table:haircut_calibrations -->

<!-- schema-doc:BEGIN:table:ev_assessments -->
| 列 | 类型 | 约束 |
| --- | --- | --- |
| `id` | INTEGER | PK |
| `fixture_id` | INTEGER | FK→fixtures.id |
| `pool_period_id` | INTEGER | FK→pool_periods.id |
| `market_code` | TEXT | NOT NULL |
| `selection_code` | TEXT | NOT NULL |
| `ev` | REAL | NOT NULL |
| `ci_low` | REAL | — |
| `ci_high` | REAL | — |
| `cost_adjusted_ev` | REAL | — |
| `kelly_fraction` | REAL | — |
| `created_at` | TEXT | NOT NULL |
<!-- schema-doc:END:table:ev_assessments -->
