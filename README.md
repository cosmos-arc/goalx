# goalx

个人大陆足彩分析与记录系统：外部数据采集 → 概率建模 → 价值与资格判定 → 纸面投注与结算复盘。
FastAPI 后端 + React 前端 monorepo，单机本地运行、人工下单，[MIT](LICENSE) 许可。

## 功能特性

- **数据采集**：竞彩全玩法官方快照/赛果/彩池，国际书商赔率轨迹语料（raw→silver→gold 三层），xG/Elo/新闻等特征源；
- **双线预测**：ML 线（Dixon-Coles + 10×10 比分矩阵）与 LLM 情报线，真金资格只读 ML 轨；
- **判定与诊断**：证据新鲜度、多书共识（Shin 去水）、EV 诊断、资格判定（可投/拒绝/拒绝原因可见）；
- **投注闭环**：建议 → 锁定 → 结算 → 复盘，全程纸面优先；真金票据赛前建建议、赛后人工回录，**无自动下单**；
- **测量工具**：前瞻验证（skill）、CLV 收盘对账、价格时间线与价格存活等预注册实验。

## 技术栈

| 层 | 选型 |
| --- | --- |
| Backend | Python 3.13, FastAPI, Pydantic v2, granian (Rust ASGI), loguru |
| Frontend | React 19, TanStack Router/Query, Tailwind CSS 4, Vite 8, TypeScript ~6.0 |
| Package managers | uv（Python，单 lockfile workspace）+ bun（Web，isolated linker） |
| Quality | ruff（lint+format）、basedpyright（strict）、Biome 2、tsc -b（strict）、pytest 9 / vitest 4、Playwright + axe |
| Contract | `contracts/openapi/v1.json` 单一事实源：redocly recommended-strict lint、openapi-typescript 生成 Web 类型、后端一致性测试 |
| CI/Hooks | GitHub Actions（fail-closed gate、CodeQL、gitleaks、OSV）、pre-commit（local 模式 ruff、gitleaks、conventional commits、no-commit-to-main） |
| Task runner | go-task（`Taskfile.yml`） |

## 快速开始

```bash
brew install uv go-task        # 一次性；node 用 .node-version 对应版本，bun 按 packageManager 锁定
task bootstrap                 # uv sync + bun install + Playwright chromium + pre-commit hooks
task server                    # API @ http://127.0.0.1:8000
task dev                       # Web @ http://127.0.0.1:5173（/api 代理到后端）
```

日常连续运行另加 `task serve-schedules`（定时调度，专用 Prefect server + serve 两进程，详见 [RUNBOOK](docs/RUNBOOK.md)）；拉新代码后先 `task db-migrate`。

## 开发与测试

| 命令 | 作用 |
| --- | --- |
| `task check` | 本地全量门禁（含 e2e；CI 的 security 工作流除外） |
| `task fmt` / `task lint` | ruff 格式化 / 检查 |
| `task test` / `task test-fast` | 后端测试（含 90% 覆盖率门禁 / 不含覆盖率） |
| `task type` | basedpyright strict |
| `task web-lint` / `task web-type` / `task web-coverage` / `task web-build` / `task web-e2e` | 前端各道检查 |
| `task contract-export` / `task contract-codegen` | OpenAPI 契约导出 / Web 类型再生成 |
| `task pre-commit-run` | 全仓跑一遍 git hooks |

修改 API 后：`task contract-export && task contract-codegen`，把 `contracts/` 与 `apps/web/src/api/generated/` 的 diff 一起提交（CI 的 `check-contract` 会校验无漂移）。采集/训练/回测/审计的全部 CLI 命令以 `uv run --no-sync python -m goalx_backend.cli --help` 为准（注册表推导）。

## 项目结构

```text
apps/backend/    # goalx_backend：领域包 data / modelling / evaluation / betting + api/cli/flows 交付层
apps/web/        # @goalx/web：React 19 + TanStack Router/Query + Tailwind 4 工作台
contracts/       # openapi/v1.json——API 契约单一事实源
docs/            # 架构/运维/使用手册/ADR/数据流；docs/plans/ 为历史收官档（零改写）
.github/         # CI（fail-closed gate、CodeQL、gitleaks、OSV、契约防漂移、base=main 守卫）
Taskfile.yml     # go-task 任务入口（全部开发命令经 task）
```

## 文档导览

按序读完即可建立全貌（均为仓库内文件，克隆即得）：

1. 本 README——定位、特性、快速启动；
2. [CONTEXT.md](CONTEXT.md)——领域术语表，先统一语言；
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)——架构总览：数据流、领域包、运行形态、决策索引；
4. [docs/RUNBOOK.md](docs/RUNBOOK.md)——运维手册：常驻进程、定时调度、手工命令、数据源、健康检查、备份恢复与 AI 代采兜底；
5. [docs/USER-GUIDE.md](docs/USER-GUIDE.md)——产品使用手册（使用者视角：全链操作、入口身份、失败恢复、指标判读）；
6. [docs/adr/](docs/adr/)——12 条不可逆技术决策及其理由；
7. [AGENTS.md](AGENTS.md)——仓库工作约定（面向 AI agent，人读同样适用）。

历史与证据：全部收官票板（goalx-quant 主线、十年回测、M3 融合、两轮报告审计等）在
[docs/plans/](docs/plans/) 按家族归档；V1 运行验收记录见
[docs/plans/report-audit-20261104/run-acceptance.md](docs/plans/report-audit-20261104/run-acceptance.md)（只追加）；
能力边界以 [docs/plans/capability-registry.md](docs/plans/capability-registry.md) 为单一认知面。
`.scratch/`（gitignored）是本地工作记忆：进行中的票、日志与探针数据，克隆不含，以 docs/plans 为准。

指标口径以 [docs/plans/goalx-quant/glossary.md](docs/plans/goalx-quant/glossary.md) 正式版词典为准（页面内另有 tooltip 词典）；结算与更正口径（单关无效退款、串关无效腿按 1、更正必带原因原子重算+冲正）见 RUNBOOK §5 与 CONTEXT.md 词条。

## 当前状态（V1，2026-11-06）

V1 功能范围已冻结（冻结矩阵见 capability-registry 与 USER-GUIDE §2）：HAD 今日/研究/单关/串关/纸面锁定结算复盘 = 正式核心；前瞻验证与价格测量 = 正式测量工具；非 HAD = 研究入口；彩池 = 研究构票；LLM/融合 = 辅助参考。live 回录 = 赛前建建议 + 赛后人工回录，不开自动真实执行。经济性判读（≥30 销售日价格实验）独立于软件验收，另线按预注册协议推进。

历史阶段叙述（M1 数据地基 / M2 ML 线与回测的手册式展开）已归档至 git 历史与
[docs/plans/goalx-quant/](docs/plans/goalx-quant/)、[docs/plans/trusted-paper-handoff.md](docs/plans/trusted-paper-handoff.md)；
现行操作一律以 RUNBOOK（运维）与 USER-GUIDE（使用）为准。

## 许可证

[MIT](LICENSE)
