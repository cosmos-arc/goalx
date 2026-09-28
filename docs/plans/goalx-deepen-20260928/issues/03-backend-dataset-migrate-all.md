# 03 backend：数据集注册全量搬迁 + 拆旧手写 wiring（migrate+contract）

Status: resolved
Blocked by: 02

**交付（用户视角）**：全部定拍数据集只在一处声明；「新增一个数据集要改 7-10 个文件」
降为「一处注册 + 源模块 + 测试」；cli/tasks/flows/schedules 回到纯 adapter，corpus
命令不再各自重搭 CorpusStore + TLS 客户端。

## 现状（02 之后的残余）

02 已建立注册表并搬入首批 2-3 个数据集；剩余数据集仍走手写 wiring：cli.py 平铺
argparse 段 + `_cmd_*` handler + dispatch 字典、flows.py 同构 flow 壳、schedules.py
deployments 字典条目 + RESUME 手抄行；cli corpus handler 里 ~12 处 CorpusStore+TLS
客户端样板与 tasks 侧 6 处同构样板并存（cli 与 flow 两份 implementation 的漂移面）。

## 方案（本票 = 剩余 migrate + contract）

剩余数据集逐批入册（每批 = 注册 → 推导 → 删该批旧手写段，批内 CI 绿）；全部入册后
contract：删除不再被引用的手写基建（平铺 argparse 段、同构 flow 模板、RESUME 手抄
清单），corpus 命令的客户端装配收敛到 tasks 侧唯一一份。

## 实现决策

- 批大小按爆炸半径定（机械搬迁，一票内 2-3 批皆可）；非同构 flow（daily-wrap 等）与
  纯手工命令保留手写，不硬塞进注册表。
- cli corpus 命令改走 tasks.*：cli 与 Prefect 同一 implementation、两个 adapter
  （01 已对同步端点证明形状）。
- 若搬迁中发现 corpus_store.py checkpoint 表双登记（ensure_tree 与 `_checkpoint`
  两处）可由注册表表清单驱动，顺带收敛；改不动则记录原因留给后续。

## 测试接缝

- 02 的规格断言测试随批扩条目即等价证据；既有 cli/tasks 测试全绿。
- 收尾跑 serve 清单 diff（与搬迁前逐一相等）。

## 范围外

- config.py 分节化与 table_owners 执法机制不动；不建 Source 抽象基类；
  flows/schedules coverage 豁免现状不动。

## 不变量与人裁决项

- 不变量：全量搬迁前后 serve 清单 diff 为零；`--only` / GOALX_SRCT_NIGHT_OFF /
  夜班-长班互斥语义不变；cli 参数向后兼容（既有命令与参数不消失）。
- 人裁决：无（首批形态已在 02 裁定）。

## 验收

- [x] 全部定拍数据集入册（12 条，含两判死面 resume=False），手写 wiring（argparse 段/同构 flow/RESUME 手抄）删除
- [x] cli corpus 命令走 tasks.*，CorpusStore+客户端装配仅 tasks 一份——**缩窄口径（人追认）**：
  「有 tasks 对应的定拍命令」（srct-shift 本票、srct-night 票 02）；~10 个手工 one-shot
  corpus 命令（srct-collect/silver/odds/market/gate、jc-*、elo-build、archive-538、
  corpus-report）的 CorpusStore+TLS 装配收敛归票 04（公共采集客户端工厂重指调用点，
  票面既定落点；tasks.py 模块文档规定无定拍运维命令保持 cli 单 adapter）
- [x] serve 清单 diff 为零（金标 17 名→cron + 与 main 两文件 cron 多重集 17=17 对账）；既有命令/参数向后兼容（help 文案与 argparse 默认值逐字保留）
- [x] 既有测试全绿 + 全量 `task lint` 绿（778 过 / 覆盖 92.16% / importlinter 1 kept）

## Answer

PR #110（合 main e7fb17d）。注册面 3→12；schedules 手写面 14→5（组合面
daily-capture/weekly/daily-wrap + 双 cron 面 draw-results-sync×2）；flows 删
9 同构壳；cli 删 4 handler（understat-sync/clubelo-sync/closing-snapshot/
srct-shift 派生）。DatasetSpec 增 resume/cli 形状位；CliArg 增 nargs/transform
+ nargs/type 互斥守卫；`tasks.srct_shift_run` 吸收 cli 缝注入口（同 srct-night）。

**留手写的非同构形状**（票面允许，注释在档）：official-reconcile cli 手写
（pending_manual 告警走查）、draw-results 双 cron 单 flow、组合面五件。

**已声明收敛**：三个 log 风格 handler 输出改 JSON stdout（与注册命令同构）；
review 修正 = eu-odds-closing 载荷回旧 3 键口径（asdict 会多 duplicate_
snapshots/unmatched 两键）、CONTEXT.md 词条补形状位、cli 门闸收口 cli_specs()
单源。

## Comments
