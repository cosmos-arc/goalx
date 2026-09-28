# 02 backend：数据集注册表骨架 + 首批数据集入册（expand）

Status: resolved
Blocked by: 01

**交付（用户视角）**：首批 2-3 个定拍数据集的排程、CLI 入口、恢复清单全部由一份声明
推导出来；`--only`、off-gate 闸门、serve 行为与今天逐一相同（行为零变化是本票的
可验证交付）。

## 现状

新增/改一个定拍数据集要摊 7-10 个文件（实测三例：卫报票 79 = 9 文件；自算 Elo 票 78 =
7 文件；fdhist 批 = 11 文件）：

源模块 → tasks.py 包装函数 → flows.py `@flow` 壳（17 个 flow 里 15 个体相同构「调
tasks 函数、记日志、返回 stats」）→ schedules.py deployments 字典（:226-244）**加**
RESUME_DEPLOYMENTS 手抄清单（:268-284）→ cli.py subparser + `_cmd_*` handler +
dispatch 字典（:915-1246，自注释「随票累加的平铺 argparse」）→ config.py 字段 →
table_owners.py 登记 →（corpus 源再加 corpus_store.py 的 ensure_tree 与 `_checkpoint`
**两处**表登记，:212-241）。

已付过的账：RESUME 清单漏项 = 静默不注册；off-gate 原稿插位在 selected 固化后属无效
位（schedules.py:245-247 注释自证）；cli corpus handler 各自重搭 CorpusStore + TLS
客户端直调实现（~12 处），tasks 侧另有 6 处同构样板。

## 方案（本票 = expand + 首批 migrate）

建立一份声明式数据集注册记录：名字、cron、任务函数（住所仍是 tasks）、cli 参数、
checkpoint 表清单。cli 子命令、flow/deployment、RESUME 清单、checkpoint 表登记由它
推导。首批收 2-3 个形态代表数据集入册并删其手写 wiring；其余数据集的手写 wiring
**原样保留并存**（03 全量搬），CI 全程绿。

## 实现决策

- 注册记录独立成模块，形状从简：一个 dataclass 列表，不搞插件系统/装饰器注册魔法。
- 同构 flow 壳由注册表生成（flow 工厂）；非同构的（daily-wrap 等）保留手写，不硬塞。
- RESUME 清单 = 注册表推导 + 判死面例外表（eu-odds-closing / srcb-collect 显式列出）。
- config.py **不**收进每数据集分节（改动面大收益小，YAGNI）。
- 每个数据集的入册 = 注册 → 推导生成 → **同票内删该数据集旧手写段**（逐数据集
  expand–contract，不留双份）。

## 测试接缝

- 注册表模块一个规格断言测试（先例：test_ingest_srct_spec.py 的规格断言形态）：每条
  记录产出 deployment 名/cron 正确、cli dispatch 命中同一 tasks 函数、RESUME 推导含
  例外表。
- 既有 cli/tasks 测试全绿即等价性证据；schedules/flows 维持 coverage 豁免现状。

## 范围外

- 其余数据集搬迁（03）；不取消任何 ADR-0005 裁定的层（生成的仍是真实 flow +
  deployment）；不动 table_owners 执法机制与 config.py 结构；run_night 13 参接缝宽度
  不动（map 暂不动清单）。

## 不变量与人裁决项

- 不变量：首批入册前后 serve 清单 diff 为零（deployment 名、cron 逐一相等）；
  `--only` 语义不变；GOALX_SRCT_NIGHT_OFF 闸门行为不变（摘除仍须在 selected 固化前）。
- 人裁决：首批收哪 2-3 个（建议三个形态代表：guardian 纯 corpus 定拍 +
  pool-snapshot 运行面 + srct-night 带 off-gate）。

## 验收

- [x] 注册模块 + 规格断言测试落地（deployment 名/cron、cli dispatch→同一 tasks 函数、RESUME 推导）
- [x] 首批 2-3 个数据集三处接线（cli/flow/schedules）由注册表推导，其手写段删除
- [x] serve 清单 diff 为零；`--only` 与 off-gate 行为不变（金标测试 + cron 多重集对账）
- [x] 既有测试全绿 + 全量 `task lint` 绿（778 过 / 覆盖 92.15% / importlinter 1 kept）

## Answer

PR #109（分支 refactor/deepen-02-dataset-registry，单提交 d79c5c2）。新模块
`datasets.py`：DatasetSpec（名/cron/task/cli 参数/checkpoint 表）推导 cli
子命令、flow（工厂）、deployment、RESUME；首批三形态代表入册并删手写
wiring（guardian-sync / pool-snapshot / srct-night，票面建议的三代表照单）。

**实现口径（与票面三处有意偏差，均已在 PR 声明）**：

1. checkpoint 表登记 = 规格核对而非运行时推导——corpus_store 在 data 层，
   反向 import 顶层注册表违反 ADR-0008 分层；落地为 corpus_store 建表
   单处登记（双抄消除）+ `checkpoint_table_names()`（解析失败即炸），
   记录表名在 test_datasets_registry 交叉断言。核对面单向（注册表漏填
   测试不红）——03 收口时若痛再收紧。
2. RESUME = 手写余量 + `resume_names()` 追加；判死面例外表以
   `schedules.DEAD_DEPLOYMENTS = ("eu-odds-closing", "srcb-collect")`
   显式落形（+不相交断言），resume=False 字段形状留给 03 注册判死面时。
3. `goalx srct-night` 手工运行 failed 超 20 条时截断为采样（旧 CLI 全量、
   定拍一直截断——本就分叉，收敛到防爆量一侧并声明）。

**接线增量**：importlinter 增 datasets 层（api/server 与 tasks 之间，ADR-0008
执法节补记）；CONTEXT.md 增 ScheduledDataset 词条；`tasks.srct_night_run`
吸收 cli 缝注入口（settings/client/now_fn/sleeper/seasons/jc_phase +
list_mode/no_window，`# noqa: PLR0913`）；`tasks.pool_snapshot` 返回
stats_dict 载荷（三记录同构 dict 契约，flow/cli 输出零变化）。

**行为零变化证据**：`test_datasets_registry.py` 金标钉 17 deployment 名→cron
（与 main 版 cron 多重集机械对账一致）；--only/off-gate 代码原样；cli help
文案与 argparse 默认值逐字保留（`git show main:` 旧 parser 比对，review
sub-agent 复核）。deployment 字典键序为推导面排尾的新序——键序非契约
（serve 对各 deployment 独立注册）。

## Comments
