# goalx

个人大陆足彩分析与记录系统，FastAPI backend + React web monorepo；本地运行、人工下单。

## 新开发者导览

按序读完即可建立全貌（均为仓库内文件，克隆即得）：

1. 本 README——定位、Stack、快速启动；
2. [CONTEXT.md](CONTEXT.md)——领域术语表，先统一语言；
3. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)——架构总览：数据流、领域包、运行形态、决策索引；
4. [docs/RUNBOOK.md](docs/RUNBOOK.md)——运维手册：常驻进程、19 面定时调度、手工命令、数据源、健康检查、备份恢复与 AI 代采兜底；
5. [docs/USER-GUIDE.md](docs/USER-GUIDE.md)——产品使用手册（使用者视角：全链操作、入口身份、失败恢复、指标判读）；
6. [docs/adr/](docs/adr/)——12 条不可逆技术决策及其理由；
7. [AGENTS.md](AGENTS.md)——仓库工作约定（面向 AI agent，人读同样适用）。

历史与证据：全部收官票板（goalx-quant 主线、十年回测、M3 融合、两轮报告审计等）在
[docs/plans/](docs/plans/) 按家族归档；V1 运行验收记录见
[docs/plans/report-audit-20261104/run-acceptance.md](docs/plans/report-audit-20261104/run-acceptance.md)（只追加）；
能力边界以 [docs/plans/capability-registry.md](docs/plans/capability-registry.md) 为单一认知面。
`.scratch/`（gitignored）是本地工作记忆：进行中的票、日志与探针数据，克隆不含，以 docs/plans 为准。

## 当前状态（V1，2026-11-06）

V1 功能范围已冻结（冻结矩阵见 capability-registry 与 USER-GUIDE §2）：HAD 今日/研究/单关/串关/纸面锁定结算复盘 = 正式核心；前瞻验证与价格测量 = 正式测量工具；非 HAD = 研究入口；彩池 = 研究构票；LLM/融合 = 辅助参考。live 回录 = 赛前建建议 + 赛后人工回录，不开自动真实执行。经济性判读（≥30 销售日价格实验）独立于软件验收，另线按 prereg 协议推进。

历史阶段叙述（M1 数据地基 / M2 ML 线与回测的手册式展开）已归档至 git 历史与
[docs/plans/goalx-quant/](docs/plans/goalx-quant/)、[docs/plans/trusted-paper-handoff.md](docs/plans/trusted-paper-handoff.md)；
现行操作一律以 RUNBOOK（运维）与 USER-GUIDE（使用）为准。

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

## 运行入口

日常运行入口三件（详见 RUNBOOK）：`task server`（API）+ `task dev`（web）+ `task serve-schedules`（19 面调度，两进程姿势）；拉新代码后先 `task db-migrate`。采集/训练/回测/审计的全部 CLI 命令以 `uv run --no-sync python -m goalx_backend.cli --help` 为准（注册表推导）。

结算与更正口径（单关无效退款、串关无效腿按 1、更正必带原因原子重算+冲正）在 RUNBOOK §5 与 CONTEXT.md 词条；指标判读以 [docs/plans/goalx-quant/glossary.md](docs/plans/goalx-quant/glossary.md) 正式版为准。
