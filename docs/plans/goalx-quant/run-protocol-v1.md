# 运行协议 v1(票 37 工程验收,冻结于 2026-09-13)

目的:证明真实赛前采集连续运行、纸面流程可用、成本可计。**不证明长期盈利,不购买套餐,不真钱投注。**

本协议自用户启动连续运行之日起生效;启动前参数可再修订,启动后不因结果改口径(交接契约)。

## 1. 范围(最小非空,五大之内)

| 项 | 冻结值 |
|---|---|
| 欧赔 sport 范围 | `soccer_epl,soccer_italy_serie_a`(英超+意甲) |
| 选择理由 | 五大中最小非空组合:覆盖周末+周中赛程日,E0/I1 模型与 hist 底座齐备,2026-09-13 在售 10 场(英超3+意甲7) |
| 竞彩 | 全玩法照采(sporttery 免费),资格判定只对 had |
| 明确排除 | 其余联赛/杯赛的场次照常出现在今日页,但无欧赔证据(显示 not_joined/unknown),不纳入候选 |

范围由 `GOALX_ODDS_API_SPORT_SCOPE` 冻结(已写入 .env);空值=动态发现全部足球,每次≈16 credits,禁止在运行期改回。

## 2. 数据源与版本

| 源 | 用途 | 解析版本 | 时间语义 |
|---|---|---|---|
| sporttery calculator | 竞彩报价/销售状态/单固 | `sporttery_calculator_v2` | captured_at=调盘时间,observed_at=本机收到 |
| The Odds API h2h/eu | 欧赔共识/收盘 | `oddsapi_h2h_v2` | last_update=源更新,observed_at=本机收到 |
| 官方开奖 | 唯一事实源 | manual 导入(API/UI),更正必带原因 | published_at 可空不伪造 |
| Forecast(ML 线) | 评分概率 | 工件指纹(model_version=dc-*) | issued_at<开赛才算赛前 |

## 3. 窗口与节奏(每销售日,北京时间)

| 时刻 | 动作 | 成本 |
|---|---|---|
| 10:00 / 19:00 | `ingest-jingcai` + `forecast` | 0 |
| 10:05 / 19:05 | `ingest-odds`(范围冻结) | 2 credits/次 |
| 每 30 分钟 | `closing-snapshot`(35 分钟窗;无窗口内已 join 场次时自动零成本跳过) | 0~2 |
| 赛果公布后 | UI/API 录入 DrawResult(来源核验)→ 自动结算+冲正 | 0 |
| 每日收尾 | `clv-reconcile` + `audit-ledger`(只读) | 0 |

新鲜度 300s/两源时差 300s 为工程初值,运行期不放宽。

## 4. 预算(既有免费档,不抬限额)

- 月 480 / 日 40 credits(护栏已实现,超限抛错不绕过)。
- 范围冻结后预估 ≤10 credits/销售日;3 个合格销售日 ≈ 30 credits。
- 已用(2026-09-13):月内 80(冻结前旧口径,每次 16);9-13 当日 48 已尽,当日欧赔采集如实记为受阻。

## 5. 纳入/拒绝与策略语义

- 单关候选:had 判定 valid 且 single_eligible=true;串关:两腿同一 as_of 判定非拒绝且不同场。判定只消费票 35 共享证据。
- 锁定:paper 锁定须服务器再校验(停售/过期拒绝);live 回录事后可入账,前瞻排除。
- 无正 EV 信号时不建策略注;验证流程用诊断纸面,strategy_version=`diagnostic-paper-v0`,在验证样本中独立排除策略/盈利资格。
- Forecast 评分集合:每场赛前最后一条(ml track),不按是否下注挑选(票 34 规则)。

## 6. 运行方式(待用户授权,本次不自动创建调度)

连续运行可选其一(手册见 run37-runbook.md):
1. Prefect deployment(定义已交付,`prefect deploy` 后由用户启动 schedule);
2. 手动:每天按 §3 节奏跑 task 命令(启动成本低,3 天验收足够)。

## 7. 证据记录

- 自动:quote_observations(原始证据 gzip+哈希)、odds_snapshots/sale_statuses(append-only)、cost_ledger(credits/金额)、settlements/draw_result_revisions/settlement_revisions(结算与更正)、clv_records(closing 对账)。
- 人工:仅「结果录入来源核验」与「人工介入分钟数」记入 run37-report.md 表格;不改源时间、不用测试数据冒充实采。
