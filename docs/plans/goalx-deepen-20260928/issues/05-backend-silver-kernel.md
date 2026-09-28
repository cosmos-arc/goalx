# 05 backend：silver 内核归位 corpus 层 + silver_path 唯一落点

Status: resolved
Blocked by: None（可立即开工；建议排在 04 后——两票都动 srct_odds，串行少撞）

**交付（用户视角）**：两张赔率轨迹表（jc sp_change_event × srct odds_change_event）
的事件流语义有且只有一份实现；新增一个 silver 数据集不再手抄 parquet 写舞步；语料树
磁盘布局只有一个住所。

## 现状

- `_keep_value_changes` **同名同义两份**：srct_odds.py:354-383（泛型版）与
  jc_silver.py:117-143（dict 版，文档自认「与 odds_change_event 同构」）——
  A→B→A/心跳事件流不变量两份 implementation 在等漂移。
- parquet 原子写舞步（tmp 写 + replace）手抄 ×6：srct_silver.py:210-219（唯一
  helper `write_partition`）、srct_odds.py:646-650 **及** 431-451（第二套增量写类）、
  srct_market.py:365-369 / 423-427、jc_silver.py:360-364。
- 共享工具（write_dataset_meta / remove_stale / latest_bronze_* / iter_selected_rows /
  to_float / season_of）住在 srct_silver.py——一个数据集主人兼职全家族图书馆：
  srct_odds 7 处、srct_market/jc_silver/elo_silver/archive538 横向引用。
- CorpusStore 有 raw_path()/bronze_path() 却无 silver_path()：silver/{provider}/
  {dataset} 布局在 srct_silver.py:206、elo_silver.py:221-222、corpus_gate.py:500-502
  （自称「路径布局知识的唯一落点」，实不是）、corpus_duckdb.py:42-84 四处重拼；
  corpus_gate.py:539-543 还在自行重推 raw 布局。

## 方案

内核上移为 corpus 层一个 deep module，作为各 builder 的共同 interface：分区原子写、
幂等重建、事件流 keep、bronze 迭代、版本戳。CorpusStore 增 silver_path()，路径布局
第三个归所收齐。事件流不变量收敛为一份。

## 实现决策

- 纯搬迁优先：各 builder 的 build_* 入口签名不变，内部改引内核。
- 两版 keep 合一：以 srct_odds 泛型版为体，jc dict 形态走适配。
- corpus_gate / corpus_duckdb 改走 silver_path() / raw_path()，删自拼路径。
- 内核模块归 corpus 层（corpus_store 同级），不归 data/ingest 某源名下。

## 测试接缝

- builder 入口是既有测试面：test_ingest_srct_odds / test_ingest_jc_silver /
  test_ingest_srct_silver / test_ingest_srct_market / elo 相关全绿 = 等价证据。
- 新增一个钉不变量的测试：同一组轨迹输入（含 A→B→A、心跳、缺列）喂合并后的 keep，
  jc 与 srct 两形态输出同构。
- CorpusStore.silver_path() 并入 test_corpus.py 既有形态。

## 范围外

- 不动 silver 四件 canonical 的 schema（ADR-0011）；不动 corpus_gate 的四职责拆分
  （报告/枚举/阈值/渲染混杂，map 暂不动清单）；checkpoint 表双登记问题归 02/03。

## 不变量与人裁决项

- 不变量：重建幂等（同 bronze 重跑 silver 零行差）；事件流语义迁移前后逐行相等
  （用既有 fixture 钉死）；对账账目口径不变（gate ④ 入账语义）。
- 人裁决：内核模块命名（起领域名如「silver builder 内核」；若成新概念，CONTEXT.md
  随实现补词条）。

## 验收

- [x] `_keep_value_changes` 一份 + 两形态同构测试（A→B→A/心跳/缺列，test_silver_kernel）
- [x] parquet 写舞步两形一处（write_partition + write_dataset_file ×4）；srct_silver 不再兼职家族图书馆（共享工具迁 data/silver.py，六 builder 改引）
- [x] silver_path() 落地，gate/duckdb/各 builder 自拼路径删除（review 抓到 xg 内联根与 archive538 两处漏改已补，src 内自拼归零验证）
- [x] builder 既有测试全绿；同 bronze 重跑 silver 零行差（既有 fixture 幂等测试钉死，786 过 / 覆盖 92.18%）

## Answer

PR #112（合 main b89e728）。内核 `data/silver.py`（corpus 层，corpus_store
同级）：keep_value_changes 合一（time_of/order_of/value_of/account_row 注入，
account_row 含被心跳丢弃行——srct 口径保留）；write_partition +
write_dataset_file 两形写舞步；共享工具迁入（provider/version 参数注入——
data 层不 import data.ingest，层序合法）；CorpusStore 增 silver_path()/
raw_dir()，gate/duckdb/六 builder 自拼路径删除（duckdb 视图清单三元组化）。
srct_odds 流式写类（多行组）独立保留——形态真实不同。CONTEXT.md 增
SilverKernel 词条。

**留观察（review 记录）**：provider/dataset/version 三元组在 7 处调用点
重复出现（轻度 Data Clump，层序约束所致）——后续新增数据集若痛，收一个
小的 dataset 描述对象。

**同批落地的相邻票**：PR #113（b0acb76）= 用户提速令独立提交（25/min +
2.4s±0.8s，票 04 期间混入、本票期间确认为用户指令，docstring/测试钉值
同步；官方域 3.2s 间距不动）。

## Comments
