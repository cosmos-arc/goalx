# Spec: 模型迭代与信息层（model-iterate）

Status: closed 2026-10-14（五票全 resolved：K1 #145/K2 #146/K3 #147/N1 #148/N2 #149；总账见各票 Answer；方向裁决=门待用户）
Created: 2026-10-14
Program: 计划 2 模型主线（backtest-decade 收官后的裁决实施：方向 1+2+3，
方向 4 早锁已否定、方向 5 闭环照跑零成本不动）
Prior specs: `docs/plans/backtest-decade/`（总账+方向裁决）、
`.scratch/goalx-quant/spec-v1.0.md`（双线架构）、ADR-0004（LLM 线）、
ADR-0006（比分矩阵 canonical）、ADR-0011（gold 只读桥）
Data basis: run=5 十年跑（33,883 预测）+ 三 D 报告落树 +
research/22 P0 路线图（校准层→判别层→融合，目标"逼近市场+可校准"）+
research/26（NewsShock P0=结构化事件抽取器+PIT 回放评测集，YAGNI 清单）

## Problem Statement

十年回测钉死：DC 模型无 skill（五面一致负——RPS skill 两时代 ≈−0.04、
EV 实现率 −0.42、早锁 beat 27-38%、归因捕捉反向），但**市场自身有
8 个 |t|≥2 错价桶**（策略空间存在、模型未吃）；且模型校准系统性
高估（had：模型 0.334 vs 隐含 0.246 vs 实际 0.236）。research/22 的
结论在十年数据上完整复验：融合是唯一出路、校准优先于准确率。同时
research/26 钉死信息层定位：LLM 只做结构化事件抽取（伤停/轮换/主帅）
不做概率引擎，卫报语料 214,043 篇已入库（1999 起、带正文与双时间戳）。
用户裁决（2026-10-14）：方向 1 模型侧迭代 + 2 错价桶特征接线 +
3 NewsShock 信息层，继续推进。目标：把"无 skill 的 DC"改造成
"可校准、可融合、诚实汇报的模型栈"，并用信息层事件验证第二信息源
是否存在——全程以 6 分钟十年验证循环为实验台，门=报告+用户点头。

## Solution

分两相推进：

- **K 相（模型栈）**：校准层（K1）→ 市场 log-linear 融合层（K2）→
  错价桶判别层（K3）。逐层接入十年引擎，每层一张十年对照报告：
  诚实回答"这层能不能让 skill≥0 / 让模型权重存活"。
- **N 相（信息层）**：NewsShock P0 抽取器（N1，卫报语料→结构化事件
  silver 表，LLM scout 档零边际成本，实体链接走 mapping 变体组）→
  事件信号 PIT 回放评测（N2，事件 vs 赔率移动 vs 模型残差）。
  先有界试点（英超 2023-24/2024-25），信号成立再谈全量回填（门）。

## User Stories

### K 相（模型栈）

1. 作为模型维护者，我要 DC 输出过校准层（isotonic 或温度缩放，
   h/d/a 分侧，walk-forward 拟合 PIT 安全）：校准参数在训练窗内拟合、
   赛前冻结应用到本周预测，十年重跑对照校准前后的 ECE/EV 实现率——
   直接作用于"模型 0.334 vs 实际 0.236"的失准来源。
2. 作为模型维护者，我要市场 log-linear 融合层：
   p_final ∝ p_calib^α × p_fair^(1−α)，α 由尾随窗内学习（网格），
   十年报告回答"最优 α 是多少、α>0 的模型权重是否存活"——把
   research/22"融合是唯一出路"在十年数据上定量化。
3. 作为策略研究者，我要错价桶判别层（K3）：signal-attribution 的
   8 桶特征（线漂移/相位/临场位移）进第二阶段调整，walk-forward
   验证不过拟合（同窗内拟合、出窗评估），十年对照报告：判别层在
   融合输出之上还有没有增量——有则接引擎，无则诚实留空（门=报告）。

### N 相（信息层）

4. 作为信息层维护者，我要 NewsShock P0 抽取器：卫报 bronze
   （214,043 篇）→ 结构化事件 silver 表（news_event：场次关联、
   事件类型伤停/停赛/主帅更迭/轮换线索、置信度、PIT 断言=发布时间
   早于开球），LLM 走既有 client scout 档（glm-4.7-flash 零边际
   成本）+ cost_ledger 记账；实体链接用 mapping 变体组（ADR-0012
   两步法），无链文章如实计数不硬配。范围=有界试点：英超
   2023-24+2024-25。
5. 作为策略研究者，我要事件信号 PIT 回放评测（N2）：试点事件 ×
   gold 临场切片/收盘移动 × 模型残差——事件是否携带市场未消化信息
   （对照面同 signal-attribution 口径），报告回答"第二信息源存在
   与否"；全量回填与信号进模型=下一裁决门，不自动放行。

## Implementation Decisions

- **归属**：校准/融合/判别层落 `modelling/`（calibration.py/fusion.py/
  residual_layer.py，评分矩阵 canonical 不变，ADR-0006）；引擎接线走
  BacktestParams 变体开关（缺省行为不变：不校准不融合，显式开启）；
  对照报告落 `evaluation/`（复用 D 相三报告为评估台）。抽取器落
  `data/ingest/guardian_silver.py`（silver 变换先例 srct_silver），
  事件表 owner=data；LLM 调用走 `llm/client.py`（档位路由+熔断+
  cost_ledger，不另建客户端）。
- **PIT 纪律**：校准/融合/判别参数一律训练窗内拟合赛前冻结（无前视
  断言随引擎常开）；事件表逐行断言 webPublicationDate < kickoff；
  N2 评测只用事件发布时点之前可知的赔率切片。
- **评估台**：run 级对照=decade-skill/CLV/signal 三报告重出（引擎
  变体 run + 显式 --run-id）；K 相每层一张增量报告（evaluation/
  model_layers.py 落树 reports/model-layers.*）。
- **成本口径**：N1 试点 ≈3-6K 篇 × scout 档零牌价（按牌价折算记账
  参与熔断）；全量回填（214K）不进本程序（门后另裁）。
- **代称红线**：代码/文档/输出一律代称（guardian 语料本体除外，
  silver 与 .env/research 同例）。

## Testing Decisions

- 校准/融合：纯函数单测（单调性/边界/极端 α）+ 引擎合成 gold 全链
  （gold_env 攒树先例）断言 PIT 冻结与十年窗行为；重放确定性变体。
- 判别层：walk-forward 出窗评估的行为断言（同窗拟合不泄漏）。
- 抽取器：旧模板 HTML fixture 同例——真语料样本脱敏改制的 LLM
  响应 fixture（不测 LLM 本身，测解析/断言/幂等）；实体链接正反
  用例（变体命中/无链计数）。
- 先例：tests/unit/test_backtest.py（引擎全链）、test_corpus_gate.py
  （合成树）、test_ingest_srct*.py（版本制解析器）。

## Out of Scope

- 早锁/时点策略（方向 4，已否定：选中侧反向）
- 全量语料回填与 NewsShock→赔率移动预测线（research/26 P1，门后另裁）
- 多 agent 投票/人格 agent/社媒 sentiment/视频理解（research/26 YAGNI）
- 模型栈里的 xG 增强/GBM stacking（先答校准+融合+判别够不够，
  xG 数据已就绪留下一迭代）
- 竞彩下注面/纸面闭环改动（票 37 照跑不动）

## Further Notes

- 顺序与依赖：K1→K2→K3 串行（每层吃上层输出）；N1→N2 串行；
  K 与 N 并行无依赖。全程评估台=run=5 变体重跑（6 分钟/轮）。
- 预期管理（research/22）：K 相最好情形是 skill 逼近 0 且校准达标
  （"逼近市场+可校准"），不是打赢市场；K3 若无增量要诚实留空——
  这本身是答案。N 相若事件无信号，信息层线到此为止也是答案。
- 预算：K 相纯本地零请求；N1 试点 LLM 记账走熔断（月预算内）。
