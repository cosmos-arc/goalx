# ADR-0011: 数据层 V2——CorpusStore 混合底座

日期：2026-09-23
状态：已接受（wayfinder 地图 `.scratch/goalx-data-v2/` 票 01-10 终审；随文修订 ADR-0010）

## 背景

源数据调研收官（research/17-21 + ADR-0010）后，两份外部架构推荐（Parquet+DuckDB+SQLite
轻量方案、24 表 PostgreSQL V1 spec；代称化存档 `docs/plans/goalx-data-v2/`）与现有系统
（SQLite 单库 + raw 压缩件 + 领域包 SQL 自持，ADR-0008）三方对峙。数据层 V2 边界经
wayfinder 地图十票终审，多项难以逆转且未来读者会问"为什么这么定"。

## 决策

1. **混合底座，不上 PostgreSQL**。运行面 SQLite 一律不动（竞彩/结算/评估/既有五源）；
   语料层 = repo 外独立数据树 **CorpusStore**（默认 `~/goalx-data/`，词条见 CONTEXT.md）：
   `raw/`（压缩件+sha256）+ `bronze/`（NDJSON(gzip) append-only，行信封 provider/dataset/
   sid/fetched_at/parser_version/raw_sha）+ `silver/`（canonical Parquet，competition/season
   分区）+ `gold/`（feature 预留，schema 归模型图）+ `duckdb/`（研究查询库）+ 独立
   checkpoint SQLite。PG 的收益（>1 亿行 OLAP）在 DuckDB-over-Parquet 甜区，对单机个人
   系统是过度基建。
2. **源T raw 全留本地**。每个响应压缩落盘+sha256（不进 repo）。源T 是"容忍非合同"的
   一次性十年回填资产，解析器 bug 事后发现时 raw 是唯一重解析兜底；轻量方案"不留
   HTML"的例外条款（难再恢复的数据完整留）全覆盖本源。
3. **独立树+只读桥隔离**。爬虫故障与运行面互不波及；既有五源原样留运行面作对账基准；
   消费单向（语料→消费，消费侧永不写语料树）；竞彩运行面与 API 永不触语料层。
4. **silver canonical 最小集四件**：fixture_universe / bookmaker 字典 / odds_change_event
   （1x2+亚盘变化事件流，非快照）/ xg_observation（47 键按场，源T单源）。~157 家书商
   全量进 silver（字典标 core 6-10 家）——源T 单请求已含全量轨迹，silver 再砍书商是把
   已抓到的数据藏回 raw。竞彩官方价 cid1129 随行为书商（`srct:1129`），不建独立域；
   xG 跨源归一（understat 运行面+538 档案×源T）归消费面视图+系数表，normalized_xg
   物化不建（校准拟合是研究工作，不固化进数据层）。
5. **回填窗口 2017/18 季初起·分层深度·分三批**（修订 ADR-0010 的 2020→今）。终点
   ≈44K 场 ≈120K 请求 ≈15-16 夜班；Phase1 = 近三完整季+当季（2023/24-2025/26+当季，
   ≈47K 请求，唯一含 xG 的全数据面验证批）→ 过验证门 → Phase2 2020/21-2022/23 →
   Phase3 2017/18-2019/20（只采轨迹+亚盘两请求，老场统计页跳过）。批界取整季。
6. **接入面 = DuckDB 只读视图**：`duckdb/corpus.duckdb` 建 silver 视图 + ATTACH 运行面
   goalx.db（READ_ONLY），跨面对账单引擎单 SQL；现有 evaluation/回测零改动；冻结快照
   后置。538 档案（4363 场）一次性入语料树作静态 silver 表。
7. **实施两票 + 五条量化验证门**：goalx-quant 票 55（采集先行：CorpusStore 骨架+采集器
   +raw+bronze 随夜迭代，第一夜即可开工）、票 56（silver+对账+判门，数据积累期并行
   开发）。验证门（Phase2 放行尺子，门=报告+用户点头）：①场次对账 fdhist×源T 11 项
   重叠匹配率≥99%、缺口逐场归因；②PSC×cid177 终盘偏差<1%；③xG 对账报告+异常场清单
   （双源异构，不设硬线）；④核心书商开+收齐全率≥95%；⑤管线健康（bronze 解析≥99%、
   断点/熔断/重建幂等实测）。coverage 持续观察并入对账报告，不设独立票；扩展批不设票。
   模型栈（四模型+blender+PIT horizons）出图另议，本 ADR 只到数据层。

## 后果

- 随文修订 ADR-0010 背景行回填窗口；research/20 §九定案 5 量级同步标注修订。
- 两份外部推荐代称化存档 `docs/plans/goalx-data-v2/`：24 表清单作为 silver 逐张扩展的
  目标蓝图；其 PostgreSQL 底座、对象存储 RAW 永久保存、模型栈三处不具存档外效力
  （分别由本 ADR 决策 1/2 与模型栈出图裁决覆盖）。
- silver 逐张扩展（provider_entity_mapping、lineup/availability PIT、player 域等按消费
  需求逐张落）与 feature 面模型图回接，走后续票，不动本 ADR；推翻本 ADR 需新 ADR。
