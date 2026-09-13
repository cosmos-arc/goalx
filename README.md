# goalx

个人大陆足彩分析与记录系统，FastAPI backend + React web monorepo；本地运行、人工下单。

## 新开发者导览

按序读完即可建立全貌（均为仓库内文件，克隆即得）：

1. 本 README——定位、Stack、运行手册（下文）；
2. [CONTEXT.md](CONTEXT.md)——领域术语表，先统一语言；
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)——架构总览：数据流、领域包、运行形态、决策索引；
4. [docs/plans/goalx-quant/spec.md](docs/plans/goalx-quant/spec.md)——当前生效计划（可信纸面闭环）与五张实施票；
5. [docs/adr/](docs/adr/)——8 条不可逆技术决策及其理由；
6. [AGENTS.md](AGENTS.md)——仓库工作约定（面向 AI agent，人读同样适用）。

历史与证据：wayfinder 设计地图、运行协议与票 37 验收报告、spec v1.0 与 9 篇调研见
[docs/plans/goalx-quant/](docs/plans/goalx-quant/)；2026-09 目标/设计审视见
[docs/plans/goalx-review-20260913/](docs/plans/goalx-review-20260913/)。
`.scratch/`（gitignored）是本地工作记忆：进行中的票、日志与探针数据，克隆不含，以 docs/plans 为准。

## 当前状态与下一阶段（2026-09-13）

M1/M2已实现数据采集、DC模型、合成报价回测与部分页面/API；结算与资金生命周期纠偏已实现并通过本地全量门禁。验证统计、赛前证据、浏览器纸面全流程与持续运行仍未验收。当前阶段为**可信纸面闭环**：

1. 结算规则与资金生命周期纠偏已实现：无效腿、已购约束、事务回录、更正冲正及只读旧账核查；
2. 补齐报价观测、匹配与可购买状态；
3. 修正验证统计与赛前证据边界；
4. 完成had纸面用户闭环；
5. 验收至少3个有目标比赛的销售日及真实纸面/收盘对账，报告成本与缺失。

保留现有技术栈与had-only范围；完整LLM融合、奖池优化、其他玩法、新框架和订阅暂缓。
代码/CI通过不替代真实使用证据；短期运行不替代整赛季及既定纸面通过条件。
结算修复已合并；剩余工作按“报价证据→验证统计→页面闭环→真实运行”串行交接，
具体职责、样本口径和可直接交给实施模型的指令见[可信纸面实施安排](docs/plans/trusted-paper-handoff.md)。
计划与实施票已转正至 [docs/plans/goalx-quant/](docs/plans/goalx-quant/)
（spec v1.1、issues/ 实施票、运行协议与验收报告）；`.scratch/` 仅存进行中的本地工作记忆。
以下命令是组件运行入口，不代表纸面验证或持续运行已经验收。

## Stack

| 层 | 选型 |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic v2, granian (Rust ASGI), loguru |
| Frontend | React 19, TanStack Router/Query, Tailwind CSS 4, Vite 8, TypeScript ~6.0 |
| Package managers | uv（Python，单 lockfile workspace）+ bun（Web，isolated linker） |
| Quality | ruff（lint+format）、basedpyright（strict）、Biome 2、tsc -b（strict）、pytest 9 / vitest 4、Playwright + axe |
| Contract | `contracts/openapi/v1.json` 单一事实源：redocly recommended-strict lint、openapi-typescript 生成 Web 类型、后端一致性测试 |
| CI/Hooks | GitHub Actions（fail-closed gate、CodeQL、gitleaks、OSV）、pre-commit（local 模式 ruff、gitleaks、conventional commits、no-commit-to-main） |
| Task runner | go-task（`Taskfile.yml`） |

## Quickstart

```bash
brew install uv go-task        # 一次性；node 用 .node-version 对应版本，bun 按 packageManager 锁定
task bootstrap                 # uv sync + bun install + Playwright chromium + pre-commit hooks
task server                    # API @ http://127.0.0.1:8000
task dev                       # Web @ http://127.0.0.1:5173（/api 代理到后端）
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `task check` | 本地全量门禁（含 e2e；CI 的 security 工作流除外） |
| `task test` / `task test-fast` | 后端测试（含 90% 覆盖率门禁 / 不含覆盖率） |
| `task type` | basedpyright strict |
| `task web-coverage` | Web 单测 + 覆盖率阈值 |
| `task web-e2e` | Playwright 冒烟 + a11y |
| `task contract-export` | 从应用重新导出 OpenAPI 契约（变更后提交 diff） |
| `task contract-codegen` | 重新生成 Web API 类型 |
| `task pre-commit-run` | 全仓跑一遍 git hooks |

修改 API 后：`task contract-export && task contract-codegen`，把 `contracts/` 与 `apps/web/src/api/generated/` 的 diff 一起提交（CI 的 `check-contract` 会校验无漂移）。

## M1 运行手册（数据地基）

M1 = 记录复盘工具：竞彩采集 → 欧赔对照 → 投注建议/回录 → 开奖导入 → 结算 → 复盘。
数据落 `data/goalx.db`（SQLite WAL，已 gitignore）；凭据在 `.env`（见 `.env.template`）。

### 一次性初始化

```bash
task db-migrate        # 建/升级 schema（当前 v5 含报价证据层）
task ingest-hist       # 五大 2023-26 三季回测底座（~5,257 行，幂等可重跑）
```

### 每日采集

```bash
task ingest-jingcai    # 竞彩官方全玩法快照（append-only + 原始响应证据）
task ingest-odds       # The Odds API 欧赔 + join（逐请求 credit 预留/退回）
```

采集证据（票 35）：每次 HTTP 观测的脱敏原始响应 gzip 存档于
`data/observations/`（哈希入 `quote_observations`）；时间语义按源解释——
sporttery 的调盘时间=源更新时间，The Odds API 的 `last_update`=源时间、
本机收到响应时间为 `observed_at`；未知时间可空、不倒填。

`GET /api/v1/fixtures/{id}/had-quote?as_of=…` 返回该时点 had 报价的
有效/未知/拒绝判定、原因、age、两源时差与单固资格（新鲜度/配对时差
默认各 300s，可查询参数调整）——锁定与验证共用这一判定。

销售期高频轮询走 Prefect（ADR 0005），部署定义见 `prefect.yaml`：

```bash
uv run prefect server start                             # 本地 server
uv run prefect work-pool create goalx-local --type process
uv run prefect deploy --all                             # 按 prefect.yaml 注册
uv run prefect worker start --pool goalx-local
```

flows：`jingcai-snapshot` / `eu-odds-snapshot` / `eu-odds-closing` /
`fd-history-import` / `weekly-train` / `forecast-daily` / `settlement-sweep`。
欧赔类部署每次运行 ≈8 credits，默认 cron 合计 ≤3 次/日（日预算 40）。

### 纸面流程（目标与当前缺口）

1. 今日页（`task dev` → `/`）：竞彩 vs 欧洲共识（Shin 去晦）、EV、books、调盘时点；
2. 当前建注仅有API，今日/投注页建建议入口待补；投注页（`/bets`）可勾选已有建议回录；
3. 赛后：投注页导入官方比分（DrawResult，唯一事实源）→「结算批跑」；
4. 复盘列表展示状态/盈亏；资金页（`/bankroll`）仅受已购live影响；重复回录拒绝，更正保留历史并按兑付差额冲正。

正确口径：单关无效退款；串关无效腿赔率按1，其余腿按原报价与赛果计奖，
2串1剩一腿继续计奖，全无效才整单退款。相关引擎与回归已修正。
奖池奖金未实现：live池票API拒绝，paper组合草稿保持待结算，不保存0元已完成。
[无效场次官方说明](https://www.gdlottery.cn/html/ticaidongtai/20240108/89644.html)。

### 手工补跑

`task settle`（结算批跑）、`task audit-ledger`（只读旧账与更正历史）、
`uv run python -m goalx_backend.cli baseline-compare`（PSC/AvgC 分期质检 +
对照 run，票 34）、`uv run python -m goalx_backend.cli --help`。

验证口径（票 34）：`/api/v1/validation/progress` 的市场 skill 只来自前瞻
评分集合（冻结赛前 Forecast × 同期市场基准，`/api/v1/validation/forward-skill`），
不读取任何历史回测 run；CLV 按单关/2串1 × paper/live 分组报告，分母为去重
后的唯一 Bet（串关票级联合概率，声明独立性假设）；无复核=未评估、整赛季
未验收前不通过。

### 开奖更正与旧账核查

已有库升级前先运行 `task audit-ledger` 保存核查结果，再按正常升级流程备份库并执行
`task db-migrate`。v4只新增两个append-only审计表，不重算或改写旧资金流水。
核查报告列出重复/缺失注金、未购兑付、票据绑定、余额和当前规则下结算差异；
发现异常需凭真实票据人工处理，系统不自动补购、删账或修复。报告同时支持v3旧库。

通过 `POST /api/v1/draw-results` 更正已有事实时，给对应结果项传入
`correction_reason`；相同事实重试不会重复记账。旧新事实记录在
`draw_result_revisions`，重算历史在 `settlement_revisions`；冲正流水的note关联更正ID，
`task audit-ledger`可查询。结果更正、结算更新及差额流水在同一事务内完成。
半场事实被撤回时，受影响Bet退回open并冲回旧兑付，Settlement以partial保留待核状态；
补全后重新计奖。若旧账本身不一致则拒绝更正，避免顺带修复历史异常。
当前页面尚未提供更正原因输入，先使用API；完整页面流程在后继票验收。

未购建议可以保存反事实结果，但不进入正式收益曲线、已结注数或CLV。
paper/live分别统计、注/腿分母、赛前时点与验证资格仍由后继票完善。

## M2 运行手册（ML 线 + 回测）

M2 = 概率基座 + 验证：DC 分池训练 → 在售场次 Forecast（哈希存证）→
walk-forward 回测 → 指标/haircut 校准/CLV 对账 → 验证页（`/validation`）。
schema v3 追加 backtest/haircut/clv 表；模型工件落 `data/models/`（gitignored）。

### 一次性初始化

```bash
task db-migrate                                     # v3：回测/校准/CLV 表
uv run python -m goalx_backend.cli ingest-hist      # 五大 5 季（含 2122/2223 暖机，~8,900 行）
uv run python -m goalx_backend.cli train-models --bootstrap 50   # 五大 DC + bootstrap（~30s）
# 可选 Tier2：荷甲需先导入 N1 历史（fdhist competitions 参数）再 train-models --competitions E0 D1 SP1 I1 F1 N1
```

### 每日/每周

```bash
uv run python -m goalx_backend.cli forecast         # 每日：在售场次 ML Forecast（幂等，跳过清单入日志）
uv run python -m goalx_backend.cli train-models     # 重估工件；完整配置/依赖版本身份待修
```

Prefect flows：`weekly-train` / `forecast-daily`（与 M1 采集 flows 并存）。

### 回测与校准（票 28-30）

```bash
uv run python -m goalx_backend.cli calibrate-haircut   # 竞彩 vs 欧共识 → haircut 分布（样本<30 回落默认）
uv run python -m goalx_backend.cli backtest --label m2   # 三季 walk-forward（--haircut auto 用校准值）
```

当前实现优先PSC、缺失用AvgC，经Shin得到市场概率p；模拟十进制赔率
`O_sim=(1-haircut)/p`。EV≥1.5% + 1/4 Kelly（参考资金单注1%上限），
每周每联赛至多一笔2串1，复用Settlement，训练窗防前视断言常开。
这是合成报价模型实验，尚未重现真实销售资格、共同购买时点或动态资金纪律。
[ADR 0007](docs/adr/0007-euro-close-proxy-backtest-baseline.md)已修订来源质量和使用边界，代码待对齐。

**偏离记录（had-only）**：裁决原为模拟 had+hhad+ttg，但实现期实测发现
1X2→比分矩阵反推（penaltyblog goal_expectancy）在总进球维度系统性欠分散
（6,174 场对照：ttg 桶 5 隐含 7.1% vs 实际 9.1%；hhad 实际 33% < 隐含
40%），由反推派生的 hhad/ttg 模拟价会制造假 edge。v1 回测只对 had 下注
（定价直接来自fair 1X2，无反推）。恢复其他玩法还需真实报价、同义结算映射
与校准验证；国际totals/handicap接入本身不充分。

2026-09-13旧run记录RPS skill约−3.75%、投入加权ROI约−25.33%，不能作为实盘或陈盘结果。
football-data警告2025-07-23后Pinnacle报价陈旧；旧run中893条预测使用该时期PSC，
需来源/分期对照。统计相关性、玩法重复计数亦待修，精确p值暂不作为可靠结论。
[数据源质量说明](https://football-data.co.uk/data.php)。

### CLV 收盘窗口（票 32）

目标是在开球前窗口采样；当前CLI可手工运行，但可复现部署与真实窗口证据待验收。
按一个region、5 sport × 8次/日估算约40 credits/日，月480只够约12天，
且普通采集与收盘共享预算。应缩小目标赛事与窗口，不通过提高日限额绕过月限额。

```bash
uv run python -m goalx_backend.cli closing-snapshot   # 窗口内(35min)场次 purpose=closing 快照
uv run python -m goalx_backend.cli clv-reconcile      # 已结算注单 CLV 对账 + beat rate/回归报表
```

CLV_proxy = p_close − 1/买入价（概率域）。当前实现按had腿计算多book共识，
与完整票级验证不同；赛后窗口、样本分组和未知状态待修。正式200注须按唯一票级Bet，
另列腿数/场次数，单关与2串1分开；缺失closing或复核不能显示通过。
本轮不变更整赛季等长期要求，也不做真钱推荐放行。
