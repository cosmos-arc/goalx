# Spec: 模型换血与错价桶（model-v2）

Status: closed 2026-10-15（四票全 resolved：G1 #151/G2+G3 #152/D1 #153；总账=两方向终审均钉死否定：α 不存活跨模型线+错价桶双面无信号）。**修正后对照注记 2026-10-27**（report-audit-20261027 票 01）：融合层被发现训练/应用输入空间错位（拟合在融合后历史上、应用于融合前概率），修复落 main；隔离复现（冻结输入）证实 Run21/22/23 修正后 RPS skill 点估计微改善（如 −0.009803→−0.009702）、模拟 ROI 全部更差（−10.748%→−11.975% 等）——**两方向终审否定结论不变**（无收益救援），原始 GBM 路径逐场精确重现不受该缺陷影响。
Created: 2026-10-15
Program: backtest-decade → model-iterate 之后的第三程（用户裁决 2026-10-14：
错价桶+模型换血推进，away 事件信号挂起）
Prior specs: `docs/plans/backtest-decade/`（十年终审）、
`docs/plans/model-iterate/`（K 相 α 不存活/N 相抽取器落地）、
`.scratch/goalx-quant/spec-v1.0.md`（v2：xG+GBM stacking 原计划）、
ADR-0006（比分矩阵 canonical）、ADR-0011（gold 只读桥）
Data basis: run=5..11 全家福 + K3 下注面分离（−12.2% vs 安慰剂 −18.3%）
+ gold xG（30-55% 覆盖，双源 caliber）/Elo（全量）/fdhist 技统（55-80%）
/收盘族特征全部在树

## Problem Statement

model-iterate K 相钉死：DC（只吃比分）不可救药地≈市场——融合 α 不存活、
判别层 skill 留空。但 DC 从未吃过特征：xG（信息市场未必全定价）、
自算 Elo（全量）、fdhist 技统、收盘族漂移/相位特征（signal-attribution
8 桶 |t|≥2）全部闲置。spec-v1.0 v2 的"xG+GBM stacking"计划从未执行。
同时 K3 遗留一个未消化的发现：错价桶特征在预测面无增量但在下注面
与安慰剂分离 6.1pp——概率调整压平了它们，下注过滤可能没有。需要：
①换血——GBM stacking 模型线上引擎，十年终审回答"吃特征的模型能不能
活过融合"；②错价桶——下注过滤实验（不是概率调整），诚实对照。

## Solution

- **G 相（模型换血）**：滞后特征面（G1，xG/技统/Elo 滞后化——标签侧
  信息必须 lag 后才是特征）→ GBM 模型线（G2，lightgbm 多分类，
  周重估 walk-forward，had-only——GBM 无 λ 不产比分矩阵，hhad/ttg
  保持 DC 或市场侧不动）→ 十年终审（G3，skill vs DC vs 市场公平 +
  融合 α 在 GBM 上是否存活——K2 钉死的 α≈0 是否只属于 DC）。
- **D 相（错价桶）**：下注过滤实验（D1，桶特征门控下注资格而非调
  概率——K3 的下注面分离直接检验），安慰剂=同接受率随机门。

## User Stories

### G 相

1. 作为模型维护者，我要滞后特征面：gold 行 → 每队赛前滚动统计
   （xG for/against、射门/射正、净胜球、Elo 差等，窗内均值，只用
   kickoff 早于目标场的比赛——标签侧信息 lag 化后才是 PIT 特征），
   (league, team) 键控、确定性纯函数、缺数据诚实留空（xG 覆盖 30-55%
   不插补）；随引擎消费（GoldMatch 携带或旁路 dict）。
2. 作为模型维护者，我要 GBM 模型线（lightgbm 多分类 h/d/a）：特征=
   滞后特征+收盘族（era 正典 fair 概率本身进特征——市场是最强单一
   特征，GBM 学"残差修正"形态）；按联赛周重估 walk-forward（同 DC
   协议，PIT 断言常开）；引擎变体 `model=gbm`（had-only，hhad/ttg
   跳过计数）；重放确定性（lightgbm deterministic 参数+固定 seed）。
3. 作为量化负责人，我要 GBM 十年终审：model-layers 对照（GBM vs DC
   vs 市场公平：skill/ECE/注面）+ **GBM 融合 α 存活性**（K2 的 α≈0
   在更强模型上是否松动）——回答"特征模型能不能在融合下存活"。

### D 相

4. 作为策略研究者，我要错价桶下注过滤实验：引擎候选按桶特征门控
   （如 drift 方向与选择一致的才下注；K3 的 8 桶特征条件），对照=
   未过滤 run + **同接受率随机门安慰剂**（过滤收益≠门槛削减收益）；
   报告接受率/ROI/每桶增量——过滤有效⇒桶特征含下注面信息（概率面
   压平≠下注面无用）。

## Implementation Decisions

- **新依赖**：lightgbm（G2 PR 引入；MIT、wheel 无系统依赖；spec-v1.0
  v2 预批路线）。deterministic=true+seed 固定保重放确定性。
- **滞后特征归属**：`modelling/lag_features.py`（纯函数：gold 行集→
  sid→特征 dict；不上 gold 树——每次 run 从 gold 行即时算，省一次
  物化层与口径漂移）。
- **GBM 归属**：`modelling/gbm_model.py`；引擎 `BacktestParams.model`
  ("dc"|"gbm")；GBM 路径 had-only（无比分矩阵），hhad/ttg 计数跳过——
  诚实范围声明不冒充全玩法。
- **过滤实验归属**：引擎 `bet_filter`（"off"|"buckets"|"random"）+
  门控谓词（桶特征条件，与 K3 residual 同源特征但作用于候选资格）；
  报告沿 model-layers。
- **评估台**：沿用 model-layers 报告 + decade-skill/CLV 三件套
  （变体 run 显式 --run-id）；GBM 融合复用既有 fusion 层（链序
  calibrate→fuse→residual 不变，GBM 只换模型侧源头）。
- 代称红线照旧；观察性结论不自动放行（GBM 进缺省=终审报告+点头）。

## Testing Decisions

- 滞后特征：纯函数单测（滚动窗边界/跨季/缺数据留空/严格 lag 断言
  ——同场信息不得入特征）；引擎全链（gold_env）PIT 断言。
- GBM：合成特征拟合单测（可分数据能学出/不可分不崩）+ 引擎变体
  全链 + 重放确定性变体（同参数同输出逐字节）。
- 过滤：门控谓词正反用例 + 安慰剂同接受率断言 + 引擎全链计数。

## Out of Scope

- away 事件信号/NewsShock 全量回填（用户挂起）
- 比分矩阵生成 from GBM（hhad/ttg 模型侧仍是 DC/市场侧）
- 模型集成/blender 多模型堆叠（先答单 GBM 够不够）
- 下注面参数调优（过滤阈值用 K3/signal-attribution 既有口径）

## Further Notes

- 顺序：G1→G2→G3 串行；D1 与 G 相并行（吃既有 DC run 或 GBM run）。
- 预期管理：research/22——纯数据 ML≈市场−4%；但我们特征含市场公平
  概率本身（GBM 学残差修正），上限是"市场+修正"，下限是收敛市场。
  G3 的 α 存活性是比 skill 更本质的判据。
- 预算：全本地零请求；lightgbm CPU 十年周重估 ~6k 次拟合，单次
  <100ms（小特征集），预计引擎跑时同量级（分钟级）。
