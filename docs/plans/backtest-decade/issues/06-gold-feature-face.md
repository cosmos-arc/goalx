# 06 Gold 特征面：语料树 gold/ 数据集（stories 6-12 一票落地）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` B 相 User Story 6-12

## 任务

语料树 `gold/` 下落 fixture 粒度特征数据集 `match_features`（一行一场、
特征列平铺 + `_meta` 版本戳 + 输入 silver digest 钉死；幂等重建）。
B 相七条 story 是同一数据集的七个面，一票落地：

- **骨架（S6）**：`data/gold.py` builder（silver 同款舞步：tmp 原子替换、
  确定性排序 → 字节级幂等、`_meta` 钉输入 digest + silver 版本）。
- **无前视断言（S7）**：逐行 `pit_max_ms` = 所用 PIT 类输入（轨迹收盘/
  1h/24h 切片的 published_at 最大值），构建断言 < 开球，违规即炸。
- **AH/OU 收盘族（S8）**：market_quote（11 家模板书商，multi=盘1）→
  终盘线/水中位数、初→终漂移、多书分歧度、cid8 亚盘锚行值；odds_
  change_event 锚轨迹 → 开球前 1h/24h 切片（1x2 cid177 + AH cid8）；
  矩阵事件相位计数（early/pre/inplay）。
- **欧赔收盘时代分层（S9）**：era 断点=1x2 轨迹起点 2023/24（常量集中
  本模块一处）；era1（psc_proxy）行 = fdhist PSC/PSH/AvgC（配对复用
  corpus_gate.match_fixtures v3 名字身份轮）；era2（trajectory）行 =
  轨迹派生收盘（cid177 锚 + 多书共识中位数归一）。两侧配对可得时并存，
  era 字段声明哪族是正典。
- **xG 源纪律（S10）**：Understat（2014+，xg 总值）主源、源T stats
  （xg_observation，2024/25+）近代源；单场严禁混源（一行一源
  xg_source 标记），配对缺席诚实留空。
- **Elo（S11）**：elo_self 赛前值按 sid 直连。
- **fdhist 26 扩展列（S12）**：O/U 均值开收 + 亚盘均值两端点 + 半场/
  技统（标签侧）按配对进特征；fdhist 全族 odds 列经 psc_mature 成熟度
  门（同一 CSV 回填滞后，未成熟=暂定置空）。

## 卫生接线（A4/A5 谓词消费，单一取数口）

- 行政判赛：配对后逐 hist 行过 `hygiene.is_admin_excluded`，命中 →
  行上 `admin_excluded=true` 且不消费该行任何 fdhist 特征（比分是真的、
  盘是真的没法证——盘面从未真正交易）；引擎/训练集过滤=票 14（C 相）。
- 成熟度：`hygiene.psc_mature`（today 可注入，入 `_meta` 审计）。

## 不变量

- era 常量唯一落点 `data/gold.py`；下游只读 era 字段。
- SQL 只经 duckdb 视图 + goalx.* ATTACH 只读；gold 写入只走语料树
  parquet（运行面永不写，ADR-0011）。
- 排除/成熟度/匹配全部复用既有单一取数口，不散写第二份语义。
- corpus.duckdb 加 gold 视图 `match_features`；OWNERS 登记 data。

## Out of scope（票面留白）

- Prefect flow 壳：silver 重建先例全是 CLI 直跑，gold 同法（spec
  "一次性 flow"的编排需求由 CLI 满足，真跑时再评估）。
- OU 锚书商（cid 空间不同族，无既有锚常量）与 OU 1h/24h 切片：
  1x2+AH 双锚已覆盖 C15 解锁判定所需，OU 切片待 C/D 相点名再补。
- 引擎消费接线（票 13/14，C 相）。

## Answer

2026-10-08 交付（分支 feat/gold-features-b-20261008，三提交
8aab7fe/c868ea8/94b2a5f）：

- **`data/gold.py`**（~900 行）：`build_match_features(store, face, duck_con,
  *, today)` 全量重算 → `gold/goalx/match_features`（fixture 粒度单
  parquet，确定性排序字节级幂等，`_meta` 钉 gold_version + 五输入
  silver digest + 运行面 face_inputs 行数 + maturity_today）。
- **era 分层**：断点 `ERA_TRAJECTORY_SEASON="2023-24"` 唯一落点本模块；
  era1=fdhist PSC/PSH/AvgC（match_fixtures v3 名字身份配对），era2=
  cid177 锚赛前末价 + 多书中位数 1/odds 归一共识；两族配对可得时并存，
  era 字段声明正典。
- **PIT**：逐行 `pit_max_ms`（轨迹收盘+双锚切片所用最大 published_at），
  `_assert_no_lookahead` 严格 < 开球违规即炸；SQL 侧同步严格过滤
  （恰开球行/场内行不进收盘）；同刻并列按 source_order 决胜（与 silver
  一致）。非 PIT 面文档化：market_quote 初/终组=页面定义盘前价，xG/
  比分/半场技统=标签侧。
- **AH/OU 收盘族**：market_quote 盘1 行 → 多书中位数线/水、初→终漂移、
  分歧度（样本标准差）、cid8 亚盘锚行值；双锚轨迹 1h/24h 切片**全时代**
  （AH 锚轨迹 2016 起在档——双轴 review 修正，初版误挂 era2 门）；矩阵
  事件相位计数 early/pre/inplay。
- **卫生接线**：`hygiene.is_admin_excluded`/`psc_mature` 单一取数口；
  排除命中→`admin_excluded=true` 且整族不消费；未成熟→fdhist odds 族
  （含 26 扩展列 ou/ah 族）整族置空、标签侧技统保留。
- **xG 一行一源**：understat 主源（配对胜出）→ 源T stats 补位（季门
  2024/25+ 且显式排除 `_unknown` 孤儿分区——correctness F1）；早季缺口
  诚实留空。
- **接线**：`CorpusStore.gold_path`、corpus.duckdb gold 视图
  `match_features`（skip 分支补 DROP VIEW IF EXISTS——残视图修复）、
  OWNERS 登记 data、CLI `gold-build`。
- **测试** 11 用例：合成树全链（MockTransport 攒 bronze→silver→桥→gold）
  断言 era/版本戳/幂等字节一致/digest/成熟度/排除/源纪律/切片时间口径
  （恰开球排除）/PIT 触发/空树降级/CLI/_unknown 排除/降级清快照。
- **真树首建**（2026-10-08，~/goalx-data；先重跑 elo-build 刷新旧快照再
  gold-build）：48,561 行=universe 全量、elo 全量 48,561；era1 34,440/
  era2 14,121；fd_paired 37,865（fd_immature 1,181 与门②截走数完全一致）；
  xg understat 15,426+srct 3,745；close1x2 9,843；market_ah 48,547/ou
  48,548；pit_rows 48,520（双锚切片全时代）；admin_flagged 8（30 排除行
  中可配对者，其余因同日同比分歧义不配=天然不消费，零误伤面与门①一致）。
  注：首次冒烟的 elo_rows=5,623 系 elo_self 旧快照（universe 扩容前构建），
  elo-build 幂等重跑即全量，非 gold 缺陷。
- **三轴审查**：code-review 双轴（Standards：锚常量单一真相源/
  digest 抽 silver.dataset_digest/列组命名化/CLI 单连接；Spec：AH 切片
  全时代/补 fd_htr+fd_referee 全 26 列/face_inputs 可追溯）+ correctness
  三修（F1 季门 fail-closed/F2 降级捕 IOException/F3 降级清陈旧快照）
  一次批量修复；task test 829 全绿 + type/lint/fmt/contract 绿。
- **留白**（票面 Out of scope 照旧）：OU 锚/OU 切片、Prefect flow 壳、
  引擎消费接线（票 13/14）；ADR-0011 决策 1 "gold/ schema 归模型图"
  与 B 相 "构建器落 data/" 的归属张力——用户已认可修订，PR #133 落地
  （2026-10-08 修订决策 1：schema 归 data/gold.py，占位裁决作废）。
