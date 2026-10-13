# 13 引擎 v2 扩窗十年：gold 消费接线 + 11 联赛 walk-forward（S13）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 13
Blocked by: （无——B 相票 06 已 resolved）

## 任务

既有回测引擎（`evaluation/backtest.py`，ADR-0007 五大三季版）改造为十年
引擎：**数据面从运行面 hist_matches 切到 gold `match_features`（corpus
duckdb 只读视图，ADR-0011 只读桥纪律）**，窗口扩到 2016-08→数据尾
（2026-10），11 重叠联赛（corpus_gate.LEAGUE_TO_FD 全量码表），每联赛
每周时间衰减 DC 重估保留，`assert_no_lookahead` 常开，run_id 幂等
（每次调用新 run，run 内确定可重放）。

核心接线裁决（票面定"什么是对的"，实现自便）：

- **公允基准时代分层**（gold era 字段唯一真相源，断点常量在 data/gold.py）：
  - era1（psc_proxy）：PSC→AvgC 兜底（Shin 去水，现状语义保留）；
  - era2（trajectory）：cid177 锚收盘价（Shin，与 PSC 同为尖货收盘——
    方法学跨时代连续）→ 多书中位数共识归一（traj_cons）兜底；
  - `fair_source=psc/avgc` 语义保留为**源选择器**（票 34 对照 run 用，
    跨 era 作用于配对行）；`auto`=时代感知正典链。fair_source 落库值
    扩展 `traj_anchor`/`traj_cons`。
- **卫生在引擎入口统一生效**：gold 行 `admin_excluded=false` 单一过滤
  （训练面+预测面同一份取数，S4 "训练集与对账集都剔除"由构造满足）；
  PSC 成熟度由 gold 快照承载（未成熟=odds 列 NULL，门=gold 构建一处），
  引擎不二次判——运行时再判只可能是空集上的 no-op，删掉防"两套口径"。
- **键位迁移**：`backtest_predictions` 键从 hist_match_id（INTEGER FK）
  改 match_key=sid（TEXT）；旧 M2 五联赛 run 行**保留**迁移（hist id 转
  text，era/ftr 置空），审计不丢。ftr 随预测行落库，指标计算不再回查
  hist_matches。
- **训练行**来自 gold 同一行集（源T 名字空间自洽：DC 工件队名=预测行
  队名，杜绝跨源映射）；结算继续走 `settle_fixed_bet`（票 12 口径统一）。
- **run 溯源**：run params 入档 gold_version、行数、窗口、fair 链——
  重放可解释（gold 侧 digest 已钉 _meta）。
- **had-only 不动**：hhad/ttg 解锁是票 15 的门（先报告→用户裁决），
  不因 AH/OU 数据可用而自动放宽。

## 不变量与人裁决项

- 防前视：训练截止=周最早比赛日前一天；`assert_no_lookahead` 常开；
  gold 本身 PIT 断言（票 06）在构建侧把关特征。
- 代称红线：代码/测试/文档一律 cid+分层角色代称。
- 性能天花板：全量≈11 联赛×~540 周 DC 重估（周池随窗增长），分钟级/
  小时级可接受（CLI 一次性跑，不进常驻）；不加缓存投机优化。
- 人裁决项：era2 锚优先于共识的 fair 链（2026-10-10 随本票开工裁定，
  依据=与 era1 PSC 同为尖货收盘、方法学连续）；旧 run 行保留策略；
  era 对照量化（PSC vs 轨迹末价差异）留给 D 相报告，不在引擎逐场调和。

## Out of scope

- hhad/ttg 真实线定价/解锁（票 15）；market-λ（票 16）。
- haircut 十年重估（票 17）与护栏复验（票 18）——本票跑通引擎即可。
- 实盘/纸面闭环任何改动；live 周训练面卫生（票 14——backtest 侧入口
  已在本票统一，14 收 live 面 `tasks.weekly_train` 及剩余散点）。

## Answer

2026-10-10 交付（分支 feat/engine-decade-c13-20261010，三提交
d5d3cdc/07d1d88/4d14cc3，PR #134）：

- **引擎 v2**（`evaluation/backtest.py` 重构）：窗口 start/end 参数化
  （缺省 2016-08-01→数据尾），11 重叠联赛缺省，每联赛每周时间衰减 DC
  重估（训练池指针推进，窗口前行=首季热身池），assert_no_lookahead
  常开，run_id 幂等（同输入重放逐字节一致——真树 E0 半季修复前后两
  run 逐位相等实证）。
- **era 分层公允链**：era1 PSC→AvgC（Shin）/ era2 cid177 锚（Shin）→
  traj_cons 兜底；fair_source=psc/avgc 源选择器语义保留（baseline
  对照随引擎走 gold 配对行）；fair_source 落库扩 traj_anchor/traj_cons。
- **卫生单一入口**：取数一处 `admin_excluded=false`（训练/预测同份行
  集）；成熟度由 gold 快照承载（未成熟=NULL=无基准），引擎不二次判。
- **分层执法接线**：联赛映射/era 断点常量抽零依赖叶子 `data/leagues.py`
  （corpus_gate/gold 再导出，公共面不变——二者传递依赖 data.ingest 对
  evaluation 层不可达）；match_features SELECT 落归属包叶子
  `data/gold_reader.py`（纯字面量 SQL；f-string SQL 会让表归属静态审
  失明——code-review Standards 轴硬伤，挪回归属包后执法真实可见）。
- **迁移 v25**：backtest_predictions 换键 match_key(sid TEXT)+era/ftr，
  旧 M2 run 行含 id 保留；ftr 随预测行落库，metrics 不再回查 hist。
- **run 溯源**：params 入档 gold_view/gold_rows/gold_duplicate_sids/
  gold_version/built_at/maturity_today/input_digests；_meta 后读+版本
  漂移警告（TOCTOU 溯源裂缝暴露）。
- **三轴**：code-review 双轴（Standards：SQL 归属/CLI 脚手架/gold_env
  contextmanager；Spec：gold_rows/重放测试/fair_source 校验）+
  correctness 七修（NaN 收盘价否定式放行会炸 Shin/坏窗冒充视图缺失/
  重复 sid 同场串关身份裂缝/整周 start 门丢窗口内场次/未建桥裸栈/
  meta TOCTOU/v25 保 id）各配回归测试。
- **验证**：task test 838 全绿 + type/lint/fmt 绿 + schema-doc 刷新；
  真树 E0 半季冒烟 ×2（修复前后逐位一致：183 预测全 era1/psc、
  gold_rows=377、duplicates=0）。
- **留白**：全量十年跑（~5900 次 DC 拟合，小时级）待 D 相评估前执行；
  票 14（live 周训练面卫生）/15/16/17/18 依赖或并行就绪。
