# goalx 运维手册

goalx 是本机单机运行的足彩量化研究系统：FastAPI 后端（SQLite WAL）+ React 前端 + Prefect 定时调度。
本手册覆盖常驻进程、全部定时/手工任务、数据源、运维端点、故障排查与兜底流程。
架构见 [ARCHITECTURE.md](ARCHITECTURE.md)；运行验收协议见 [plans/goalx-quant/run-protocol-v1.md](plans/goalx-quant/run-protocol-v1.md)（口径冻结，运行期不改）。

## 1. 常驻进程

| 进程 | 端口 | 启动 | 日志 |
|---|---|---|---|
| API server（granian） | 127.0.0.1:8000 | `nohup task server >> .scratch/server.log 2>&1 &` | `.scratch/server.log` |
| Prefect server | 127.0.0.1:4200（API+UI） | 由 `task serve-schedules` 自动拉起 | `.scratch/prefect-server.log` |
| 调度（serve 19 deployment） | — | `nohup task serve-schedules >> .scratch/prefect-serve.log 2>&1 &` | `.scratch/prefect-serve.log` |
| Web dev server（vite） | 5173 | `nohup task dev >> .scratch/web.log 2>&1 &` | `.scratch/web.log` |

**为什么是 server + serve 两进程**：Prefect ≥3.7 的临时 server 不运行 scheduler，单进程 `flow.serve()` 的 schedule 永不触发（2026-09 上线初期连续两天 0 触发的根因）。`task serve-schedules` 先探活并按需启动专用 server（`PREFECT_API_URL=http://127.0.0.1:4200/api`），再启动 serve；server 存活独立于 serve，serve 重启不影响已排程 run。

健康检查：

```bash
curl -sf http://127.0.0.1:8000/readyz                  # 就绪：业务库可读 + schema==迁移链末版（v41）
curl -sf http://127.0.0.1:8000/api/v1/fixtures/today   # API 业务面
curl -sf http://127.0.0.1:4200/api/health              # Prefect
pgrep -f goalx_backend.schedules                       # 调度进程
```

**readyz 职责边界**：只证业务库存在可读 + schema 版本与部署迁移链一致（低/高都 503 拒服务）；不做 corpus 扫描、研究库或数据新鲜度检查——那由晨检（`uv run --no-sync python -m goalx_backend.cli morning-check` 十四项；09:05 由本机侧自动化定时执行，非 deployment，不入库）与页面降级表达。`/healthz` 只证进程活着并带运行指纹（git_sha/dirty/schema）。

重启顺序：先启 Prefect server（serve-schedules 自带），再启 API server；两者无依赖也可并行。Mac 睡眠会暂停进程，漏跑窗口如实计入分母（协议允许）；长期无人值守时改 `caffeinate -s task serve-schedules` 或迁 launchd/远端，flow 与 deployment 不用动。

## 2. 定时调度（19 个服务 deployment，Asia/Shanghai）

全量装配 21 项，默认服务 19 项，其余 2 项已停用（`eu-odds-closing` 欧赔聚合 / `srcb-collect` 源B——2026-09-25/26 裁决，国际赔率与欧指时序全走源T；serve 仍注册、恢复清单排除）。清单由 `datasets.py` 注册表推导（`RESUME_DEPLOYMENTS`），不再手抄。

组合任务（`schedules.py` 手写装配）：

| deployment | cron | 内容 | 成本 |
|---|---|---|---|
| daily-capture/protocol-v1 | `0 10,19 * * *` | 竞彩快照 → DC 预测 → 新鲜度双口径影子快照（欧赔聚合段已于 2026-09-25 撤除） | 0 |
| daily-capture/protocol-v1-window | `30 12,16 * * *` | 可购窗观测（on_sale 快照，计算器端点 1 请求/次） | 0 |
| draw-results-sync/protocol-v1 | `*/30 0-5,18-23 * * *` | 赛果自动同步（官方 uniform 主链）；无待出赛果零成本跳过 | 0 |
| draw-results-sync/protocol-v1-sweep | `0 8 * * *` | 同上，早场补扫收尾 | 0 |
| weekly-refresh/protocol-v1 | `10 6 * * 1` | fdhist 幂等重导（周末赛果）→ DC 周训练（五大+N1） | 0 |
| daily-wrap/protocol-v1 | `30 23 * * *` | 结算批跑 → 漏斗日快照 → M3 评测 → 冻结对账 → CLV 对账（源T cid177 收盘锚）→ 报价账本 sweep → 账务核查 → 冻结汇总（含税后面） | 0 |

注册表任务（`datasets.py` DatasetSpec 推导）：

| deployment | cron | 内容 | 成本 |
|---|---|---|---|
| guardian-sync | `40 9 * * *` | 卫报新闻语料（500 请求/日预算，断点续跑） | 免费 |
| pool-snapshot | `20 10,16,22 * * *` | 彩池期次/对阵/官方上期彩果（2026-09 起官方源） | 0 |
| srct-night | `0 1 * * *` | 源T 夜间批量回填推进（预算/熔断/断点续传，摘要落语料台账） | 语料请求预算 |
| official-reconcile | `30 8 * * *` | 赛果日终审计（双参照源，不落事实；pending_manual 告警） | 0 |
| understat-sync | `20 9 * * *` | Understat xG 特征（五大当前季） | 0 |
| clubelo-sync | `10 9 * * *` | Clubelo Elo 每日快照（单请求全量） | 0 |
| odds-anchor-dense | `*/5 * * * *` | 双锚临场采样（零候选零请求；开售轮询带除外） | 0 |
| track2-snapshots | `*/10 * * * *` | track2 时点采样（开球前 24h/12h，零候选零请求） | 0 |
| intel-collect | `40 10,22 * * *` | 情报采集（内部推导 + 伤停源） | 0 |
| scout-line | `50 10,22 * * *` | scout（GLM）读情报出三项 → forecasts(track=llm) | GLM 订阅额度 |
| srct-shift | `*/30 8-23,0 * * *` | 源T 当期采集四类快照（在售清单发现+开售/每日/临场，快照键幂等，夜间让位） | 语料请求预算 |
| srct-remat | `10 8 * * *` | 源T silver 增量物化（rowid 水位+触及分区重写；护栏命中自动全量兜底） | 0 |
| gold-rebuild | `40 8 * * *` | market_quote 保鲜 + gold 全量重算（赶在 09:05 晨检 gold 新鲜度检查之前）。market/gold 两重段各起 spawn 子进程 + duckdb 资源帽（BuildGuards，见 CONTEXT.md）；余量不足/子进程硬死/撞锁 → deferred 让位明日自愈（2026-11-06 内存收口，全链 38min→~6min） | 0 |

全部 flow 幂等、append-only（快照重复即去重计数）。Prefect UI：http://127.0.0.1:4200 。频率调整只改 `schedules.py`（组合任务）或 `datasets.py`（注册表任务）的 cron。

**M3 degraded 语义**：daily-wrap 内 M3 评测是独立异常边界——报告读 corpus 追加窗口可撞并发读 EOFError（写完重读正常，非永久损坏），降级只标记 `m3.degraded` 进返回体，不拖其后的冻结对账/CLV/账本回填/账务核查；**失败不得记成通过**。freshness_shadow 同款边界（挂采集流后拍快照，影子失败不拖采集）。

## 3. 手工 CLI 命令

**统一从仓库根目录跑**（`db_path=data/goalx.db`、`observations_dir=data/observations` 都是相对 cwd 的路径，从子目录跑会静默空跑/建错库）；或直接用 Taskfile 包装的同名 task。完整命令清单由 `datasets.py` 注册表推导，`uv run --no-sync python -m goalx_backend.cli --help` 为准（本节只列常用）。

**启动与迁移**（拉新代码后）：`task db-migrate`（schema 到迁移链末版，readyz 低/高都拒服务）→ 按既有重启方式换 serve 栈（**按 PID 直杀全栈再起新栈**——`pkill task` 包装进程会留孤儿 Runner 抢 Prefect 领活，曾致多栈并存）→ 重启后复核恰好一棵进程树（`pgrep -f goalx_backend.schedules` 全树列表）。

采集与同步（与 flow 同一实现，共享证据契约）：

```bash
task ingest-jingcai                 # 竞彩全玩法快照（原始证据 gzip 落盘 + 入库）
uv run --no-sync python -m goalx_backend.cli ingest-odds    # 欧赔一次（走 credit 护栏）
uv run --no-sync python -m goalx_backend.cli closing-snapshot
uv run --no-sync python -m goalx_backend.cli sync-draw-results   # 赛果手动同步
uv run --no-sync python -m goalx_backend.cli pool-sync           # 彩池手动同步
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

## 4. 数据源（2026-11-06 现状）

| 源 | 用途 | 配置（`GOALX_` 前缀 env / .env） | 状态 |
|---|---|---|---|
| sporttery calculator/uniform 族 | 竞彩报价/销售状态/单固 + 赛果落事实主链（官方 void 兜底） | `sporttery_calculator_url` 等 | 常规自动 |
| **源T 六端点**（语料主链） | 国际书商赔率轨迹/赛果/xG/阵容：夜间批量回填+当期采集+silver 物化+gold；欧赔共识与 CLV 收盘锚（cid177）全走此链 | 语料树 `GOALX_CORPUS_ROOT`（repo 外 `~/goalx-data/`） | srct-night/srct-shift/srct-remat/gold-rebuild 四项自动 |
| 官方开奖 | 唯一事实源 | — | UI/API 人工录入，更正必带原因 |
| 体彩官方（传统足彩） | 彩池期次/对阵/上期彩果（2026-09 起官方源） | — | 每日三次快照，自动 |
| 官方彩池销量/滚存 | 无自动源 | — | AI 代采（见 §8） |
| 卫报 open platform | 新闻语料（football 段 ~20 万篇） | `guardian_api_key`（免费 500 请求/日） | 每日 09:40 快照 |
| Understat | xG 特征（五大当前季） | — | 每日 09:20 快照 |
| Clubelo | Elo 评级 | — | 每日 09:10 快照 |
| football-data.co.uk | 五大+N1 历史底座（PSC/AvgC 收盘价） | `fd_base_url` | 周一 06:10 幂等重导 |
| 伤停情报源（formation 页族） | 情报采集（伤停） | — | 每日两次 |
| GLM（智谱） | scout 三项/追问/融合 | LLM 订阅 | 熔断三级 60/80/95% |
| The Odds API | ~~欧赔聚合~~ **停采备用**（2026-09-25 裁决：国际赔率全走源T；读侧旧命名空间仅历史行） | `odds_api_key` | 已停用任务 `eu-odds-closing`，不服务 |
| 源B（欧指变化）/PropLine | ~~时序/互备~~ **停采备用**（2026-09-26 终局） | — | 已停用任务 `srcb-collect`，历史语料留档 |

数据资产与备份见 §9（语料树/研究库/模型工件的配对关系）。

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
3. **玩法页** `/markets/had`（胜平负，EV×置信排序）、`/markets/goals`（进球，比分矩阵推导）、`/markets/pool`（14场/任9，期次切换+三档额度）、`/markets/pool4`（四场进球）。
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
| 彩池/赛果源不可达 | 赛果链三级降级：源T silver 缺席→uniform 官方族兜底→AI 代采（§8）→页面骨架态（如实呈现，不伪造）。 |
| 页面数据与调度日志不一致 | 先看同步状态端点（`/draw-sync/status`、`/pool-sync/status`）再下结论；快照 append-only，重跑幂等。 |

## 8. AI 代采（最差情况兜底）

适用：源直连不可达，或无自动源的数据（官方彩池销量/滚存）。由用户发令发起，agent 用浏览器读取后结构化入库：

- 官方彩池销量/滚存 → `POST /api/v1/pool-states`（唯一为代采暴露的写入口）。
- 赛果兜底 → `POST /api/v1/draw-results`（与人工录入同通道，来源字段如实标注）。
- 原则：不改源时间、不用演示数据冒充实采；原始页面快照随证据归档到 `data/observations/`。

## 9. 数据与备份

| 路径 | 内容 | 备份策略 |
|---|---|---|
| `data/goalx.db` | 全部业务数据（SQLite WAL） | 定期冷拷贝：`sqlite3 data/goalx.db ".backup '<目标>'"`（在线一致性快照，不停写） |
| `data/goalx-research.db` | 研究库（backtest runs/predictions/metrics，2026-10 起 23 run / 53 万 predictions） | 同上 `.backup`；run 不可重取（对应代码版本已演进），随库备份 |
| `data/observations/` | 原始响应 gzip + 哈希（append-only 证据，可重放） | 随库备份；丢失后不可再生成 |
| `data/models/` | DC 模型工件（基准+bootstrap）——**对应历史研究 run 的冻结模型产物**：run 重放要求与 predictions 配对的当时模型工件 | 冷拷贝归档；重建（`train-models`）产出的是新工件，不能替代历史 run 的配对证据 |
| `~/goalx-data/checkpoint.db` | corpus 台账（raw_artifacts sha 索引 + 夜间批量/采集状态） | 冷拷贝（128M+）；与 raw 树配对——台账在而文件缺=对账面破洞 |
| `~/goalx-data/raw/` | corpus 原始证据树（7.6G+，gzip per-artifact） | 全量冷拷贝（外置盘/异地优先——语料积累数月，采集成本不可重取）；最小集=checkpoint.db + 所需数据集目录 |

**恢复演练记录**：2026-11-04 空目录演练一次全过（备份快照→空目录恢复→三读验证：账目可开 v41 / 研究 run #23 可读 / bfdata 原始证据 gzip 解压+sha 对账 PASS），记录页 [report-audit-20261104/run-acceptance.md](plans/report-audit-20261104/run-acceptance.md)。
演练脚注：`read_raw/verify_raw` 的 `ext` 参数须与采集时写入的扩展名一致（如 bfdata 是 `.js`）——台账不存 ext，恢复读取按数据集约定传参。

关键不变量：开奖结果唯一事实源、更正原子重算+冲正、快照/运行记录 append-only（库内触发器强制）、金额为浮点元记账（真金流水以账务核查兜底）。

## 10. 验收与监控

- 合格销售日按 run-protocol-v1 口径累计，人工介入记录在 [plans/goalx-quant/run37-report.md](plans/goalx-quant/run37-report.md)。
- 日常看三处：Prefect UI（run 成功/失败）、验证页（前瞻 skill 曲线）、资金页（真金流水）。
- 改口径前先读协议 v1："启动后不因结果改口径"。
