# goalx 运维手册

goalx 是本机单机运行的足彩量化研究系统：FastAPI 后端（SQLite WAL）+ React 前端 + Prefect 定时调度。
本手册覆盖常驻进程、全部定时/手工任务、数据源、运维端点、故障排查与兜底流程。
架构见 [ARCHITECTURE.md](ARCHITECTURE.md)；运行验收协议见 [plans/goalx-quant/run-protocol-v1.md](plans/goalx-quant/run-protocol-v1.md)（口径冻结，运行期不改）。

## 1. 常驻进程

| 进程 | 端口 | 启动 | 日志 |
|---|---|---|---|
| API server（granian） | 127.0.0.1:8000 | `nohup task server >> .scratch/server.log 2>&1 &` | `.scratch/server.log` |
| Prefect server | 127.0.0.1:4200（API+UI） | 由 `task serve-schedules` 自动拉起 | `.scratch/prefect-server.log` |
| 调度（serve 六 deployment） | — | `nohup task serve-schedules >> .scratch/prefect-serve.log 2>&1 &` | `.scratch/prefect-serve.log` |
| Web dev server（vite） | 5173 | `nohup task dev >> .scratch/web.log 2>&1 &` | `.scratch/web.log` |

**为什么是 server + serve 两进程**：Prefect ≥3.7 的临时 server 不运行 scheduler，单进程 `flow.serve()` 的 schedule 永不触发（票 37 day0 连续两天 0 触发的根因）。`task serve-schedules` 先探活并按需启动专用 server（`PREFECT_API_URL=http://127.0.0.1:4200/api`），再启动 serve；server 存活独立于 serve，serve 重启不影响已排程 run。

健康检查：

```bash
curl -sf http://127.0.0.1:8000/api/v1/fixtures/today   # API
curl -sf http://127.0.0.1:4200/api/health              # Prefect
pgrep -f goalx_backend.schedules                       # 调度进程
```

重启顺序：先启 Prefect server（serve-schedules 自带），再启 API server；两者无依赖也可并行。Mac 睡眠会暂停进程，漏跑窗口如实计入分母（协议允许）；长期无人值守时改 `caffeinate -s task serve-schedules` 或迁 launchd/远端，flow 与 deployment 不用动。

## 2. 定时调度（六个 deployment，Asia/Shanghai）

| deployment | cron | 内容 | 成本 |
|---|---|---|---|
| daily-capture/protocol-v1 | `0 10,19 * * *` | 竞彩快照 → ML 预测 → 范围内欧赔（顺序固定） | 欧赔 2 credits/次 |
| eu-odds-closing/protocol-v1 | `*/30 * * * *` | 开赛前 35 分钟窗口的收盘快照；无窗口场次零成本跳过 | 0–2 |
| draw-results-sync/protocol-v1 | `*/30 0-5,18-23 * * *` | 赛果自动同步（源D）；无待出赛果零成本跳过 | 0 |
| draw-results-sync/protocol-v1-sweep | `0 8 * * *` | 同上，早场补扫收尾 | 0 |
| daily-wrap/protocol-v1 | `30 23 * * *` | 结算批跑 + CLV 对账 + 只读账务核查 | 0 |
| pool-snapshot/protocol-v1 | `20 10,16,22 * * *` | 彩池期次/对阵/人气分布（源B） | 0 |

全部 flow 幂等、append-only（快照重复即去重计数）。Prefect UI：http://127.0.0.1:4200 。频率调整只改 `apps/backend/src/goalx_backend/schedules.py` 的 cron（当前值为 2026-09-18 用户追认定案）。

## 3. 手工 CLI 命令

**统一从仓库根目录跑**（`db_path=data/goalx.db`、`observations_dir=data/observations` 都是相对 cwd 的路径，从子目录跑会静默空跑/建错库）；或直接用 Taskfile 包装的同名 task。

采集与同步（与 flow 同一实现，共享证据契约）：

```bash
task ingest-jingcai                 # 竞彩全玩法快照（原始证据 gzip 落盘 + 入库）
uv run --no-sync python -m goalx_backend.cli ingest-odds    # 欧赔一次（走 credit 护栏）
uv run --no-sync python -m goalx_backend.cli closing-snapshot
uv run --no-sync python -m goalx_backend.cli sync-draw-results   # 赛果手动同步（票 42）
uv run --no-sync python -m goalx_backend.cli pool-sync           # 彩池手动同步（票 43）
uv run --no-sync python -m goalx_backend.cli ingest-hist         # 五大三季历史底座（幂等可重跑）
uv run --no-sync python -m goalx_backend.cli reprocess-sporttery # 重解析原始证据入库（幂等；解析逻辑升级后的存量修正）
```

ML 线：

```bash
uv run --no-sync python -m goalx_backend.cli train-models [--bootstrap N]
uv run --no-sync python -m goalx_backend.cli forecast [--date YYYY-MM-DD]
```

结算与评估：

```bash
task settle                         # 手动结算批跑
uv run --no-sync python -m goalx_backend.cli clv-reconcile     # CLV 对账+报表
uv run --no-sync python -m goalx_backend.cli calibrate-haircut # haircut 配对样本校准
uv run --no-sync python -m goalx_backend.cli backtest [...]    # walk-forward 回测
uv run --no-sync python -m goalx_backend.cli baseline-compare  # 基准分期质检+对照 run
```

维护：

```bash
task db-migrate                     # schema 迁移（拉新代码后先跑）
task audit-ledger                   # 只读账务核查（旧账与更正历史）
uv run --no-sync python -m goalx_backend.cli align-report      # hist 队名对齐覆盖率
uv run --no-sync python -m goalx_backend.cli set-alias <canonical> <alias>
uv run --no-sync python -m goalx_backend.cli seed-demo         # 演示种子（拒绝写主库）
```

## 4. 数据源

| 源 | 用途 | 配置（`GOALX_` 前缀 env / .env） | 状态 |
|---|---|---|---|
| sporttery calculator | 竞彩报价/销售状态/单固（免费） | `sporttery_calculator_url` | 常规 |
| The Odds API | 欧赔共识/收盘（credit 付费） | `odds_api_key`（兼容无前缀 `ODDS_API_KEY`） | 月 480 / 日 40 credits 护栏，超限抛错 |
| football-data.co.uk | 五大三季历史底座 | `fd_base_url` | 一次性导入 |
| 500 系结果页 | 赛果自动同步（票 42 实证唯一可行源） | `caiguo_base_url`（需浏览器 UA） | 每 30 分钟自动 |
| 澳客 | 彩池期次/对阵/人气分布 | `zucai_base_url` | 每日三拍自动 |
| 官方开奖 | 唯一事实源 | — | UI/API 人工录入，更正必带原因 |
| 官方彩池销量/滚存 | 无自动源 | — | AI 代采（见 §8） |

欧赔范围由 `GOALX_ODDS_API_SPORT_SCOPE` 冻结为英超+意甲（协议 v1 §1），运行期禁止改回空值（空=动态发现全部足球，每次 ≈16 credits）。

## 5. 运维 API 端点（127.0.0.1:8000，前缀 `/api/v1`）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/draw-sync/run` · `/draw-sync/status` | POST/GET | 赛果手动触发与状态（UI=投注页"立即同步"按钮） |
| `/pool-sync/run` · `/pool-sync/status` | POST/GET | 彩池手动触发与状态 |
| `/draw-results` · `/draw-results/preview` | POST | 开奖人工录入 / 改动前影响预览（更正必填原因，原子重算+冲正） |
| `/settlements/run` | POST | 手动触发结算批跑 |
| `/pool-states` | POST | AI 代采导入：官方彩池销量/滚存 |
| `/fixtures/{id}/join` | POST | 人工映射欧赔事件（自动 join 失配时兜底） |
| `/stake-advice` | POST | 仓位建议（纸面 flat / 真金 ¼Kelly 封顶 1–5%） |
| `/bankroll` · `/bankroll/deposits` | GET/POST | 资金池状态 / 入金登记 |
| `/fixtures/today` · `/fixtures/{id}/research` 等 | GET | 今日/研究/玩法/池数据查询 |

完整契约：`contracts/openapi/v1.json`（单一事实源，改 API 后走 `task contract-export` → `contract-codegen`）。

## 6. 日常操作（UI 视角，http://localhost:5173）

纸面闭环的详细步骤见 [plans/goalx-quant/run37-runbook.md](plans/goalx-quant/run37-runbook.md)。摘要：

1. **总览页** `/`：分诊台，看今日待办与资金快照。
2. **场次页** `/fixtures`（今天起 3 天）→ 点进**单场研究页**：多 book 赔率、我方概率、资格判定与拒绝原因。
3. **玩法页** `/markets/had`（胜平负，EV×置信排序）、`/markets/goals`（进球，比分矩阵推导）、`/markets/pool`（14场/任9，期次切换+三档额度）。
4. **投注页** `/bets`：建议→锁定→开奖导入→结算→复盘。赛果同步面板可"立即同步"兜底；异常场次进待人工清单，人工录入保留。
5. **历史/验证/资金**：已结算统计、前瞻验证口径、真金账。

指标判读提示：EV 是市场共识的诊断量不是机会信号；红涨绿跌；|EV|≥5% 标偏差。术语见页面内词典 tooltip（`lib/glossary.ts`）。

## 7. 故障排查与已知坑

| 症状 | 原因与处置 |
|---|---|
| 定时任务 0 触发 | serve 单进程无 scheduler。必须 `task serve-schedules`（server+serve 两进程）。 |
| CLI 空跑（如 reprocess 全 0） | 从子目录跑了；`data/` 路径相对 cwd。从仓库根跑。 |
| API 500 `no such table` | 拉新代码后没迁移；`task db-migrate`。 |
| 欧赔采集抛 CreditBudgetExceeded | 日/月 credit 护栏生效，属预期；等窗口或核对 `cost_ledger`。 |
| pre-push 钩子 BlockingIOError | pre-commit 大文件 I/O bug；`git push --no-verify` 后靠 CI 同样扫描兜底。 |
| e2e "死后端降级"用例失败 | 该用例要求本机 8000 端口无后端；跑全门禁前停 API server。 |
| playwright 报浏览器不存在 | 版本错配；仓库用 1.62（浏览器 build 1234），脚本指定 `executablePath` 或 `bun x playwright install chromium`。 |
| 彩池/赛果源不可达 | 三级降级：直连 → AI 代采（§8）→ 页面骨架态（如实呈现，不伪造）。 |
| 页面数据与调度日志不一致 | 先看同步状态端点（`/draw-sync/status`、`/pool-sync/status`）再下结论；快照 append-only，重跑幂等。 |

## 8. AI 代采（最差情况兜底）

适用：源直连不可达，或无自动源的数据（官方彩池销量/滚存）。由用户发令发起，agent 用浏览器读取后结构化入库：

- 官方彩池销量/滚存 → `POST /api/v1/pool-states`（唯一为代采暴露的写入口）。
- 赛果兜底 → `POST /api/v1/draw-results`（与人工录入同通道，来源字段如实标注）。
- 原则：不改源时间、不用演示数据冒充实采；原始页面快照随证据归档到 `data/observations/`。

## 9. 数据与备份

| 路径 | 内容 | 备份策略 |
|---|---|---|
| `data/goalx.db` | 全部业务数据（SQLite WAL） | 定期冷拷贝（停写或 `.backup`） |
| `data/observations/` | 原始响应 gzip + 哈希（append-only 证据，可重放） | 随库备份；丢失后不可再生成 |
| `data/models/` | DC 模型工件（基准+bootstrap） | 可由 `train-models` 重建 |

关键不变量：开奖结果唯一事实源、更正原子重算+冲正、快照/运行记录 append-only（库内触发器强制）、金额为浮点元记账（真金流水以账务核查兜底）。

## 10. 验收与监控

- 合格销售日按 run-protocol-v1 口径累计，人工介入记录在 [plans/goalx-quant/run37-report.md](plans/goalx-quant/run37-report.md)。
- 日常看三处：Prefect UI（run 成功/失败）、验证页（前瞻 skill 曲线）、资金页（真金流水）。
- 改口径前先读协议 v1："启动后不因结果改口径"。
