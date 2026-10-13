# 19 十年全量跑 + 分时代技能报告（era×联赛×玩法）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` S19
Blocked by: （无——C 组 13-18/23 全 resolved）

## 任务

- **全量十年跑**（C 相遗留的 D 相前置）：CLI `backtest` 十年窗（默认
  DECADE_START→今）11 联赛三玩法（had/hhad/ttg 已解锁，票 23），真树
  后台跑（估 1-2h：~5,900 次联赛×周 DC 重估 + ~34k 次 joint 拟合）；
  日志落 `.scratch/backtest-decade/decade-run.log`；跑前小窗冒烟已有锚
  （E0 半季 run=4：355 注 had 189），跑后活性验证对库不对 ps；
  run label + params 快照记入 Answer。
- **技能报告** `evaluation/decade_skill.py` + CLI 子命令：
  - 维度：era（psc_proxy / trajectory）× 联赛 × 玩法 × 赛季；
  - 技能面（分母 = run 预测集，非下注集）：RPS skill vs 市场公平基准
    （既有 1−RPS_model/RPS_mkt 口径），每轴带 n；
  - 下注面（同 run 的 bets）：yield/ROI、命中率、staked/profit、
    EV 实现率（结算 EV vs 下注时账面 EV）；
  - era 断层对照：两时代 skill 差 + 样本量（粗判，不搞形式检验）；
  - 报告落语料树 `reports/` MD+JSON（与判门/四报告同惯例）。

## 不变量与人裁决项

- 预测集 = 引擎已结算预测（PIT 内）；公平基准 = 引擎同链公平概率
  （era 分层），不另造基准；
- hhad/ttg 的"市场基准"= 联合矩阵派生概率（引擎市场侧同源）；
- 报告必须直接回答主问题："数据质变后模型到底有没有 skill"——
  每轴结论句而非裸表；坏消息照报；
- CLI 必须 repo 根跑（相对 db 路径）；跑若中断：同 label 重跑幂等续跑；
- 人裁决项：结论是否改变下一迭代方向（门=报告+用户点头，汇总进票 22）。

## Out of scope

- CLV/蒸发分解（票 20）、信号归因（票 21）、模型栈扩展、策略参数调优。

## Answer

2026-10-14 交付（分支 feat/decade-skill-report-c19-20261014，PR #141，
三提交 c21933d/6e98515/b19b830）：

- **全量十年跑**：run=5（label=t19-decade-full），33,883 预测 / 49,457 注 /
  staked 1,644,776 / profit −269,769 / ROI −16.40%；三玩法全开，
  haircut=0.1128（票 17 校准值，n=66），窗口 2016-08-01→数据尾；
  skip 计数：no_market_side 24,033 / no_fair_baseline 3,981 /
  pre_start 1,892 / untrained_teams 269 / thin_train_weeks 3；
  6 分钟收班（远快于 1-2h 估）。
- **报告模块** `evaluation/decade_skill.py` + CLI `decade-skill-report
  --run-id`（缺省最新 done，冒烟顶替风险在 help 注明）：纯 sqlite 读面
  （era/ftr 随 v25 预测行落库，不回查 gold）；47 预测面/42 下注面/
  6 EV 层；报告落真树 reports/decade-skill.{json,md}。
- **十年主问题答案（坏消息照报）**：
  - 两时代模型均无 skill——psc_proxy（n=25,980）skill_rps=−0.0397、
    trajectory（n=7,903）−0.0426，DM p≈0.000（探索性）；skill_gap
    −0.0029：数据质变没有带来 skill，瓶颈在模型侧不在数据侧；
  - 三玩法腿级 Brier skill 全负 + 校准系统性高估（had 模型 0.334 vs
    隐含 0.246 vs 实际 0.236；hhad 0.466/0.358/0.344；ttg 0.104/0.073/0.069）；
  - EV 实现率 −0.42（账面 +47.6 万 vs 实现 −19.9 万，反噬）；
  - 下注 ROI 两时代 −16.0%/−17.6%（t=−9.3/−7.5），全轴无正收益桶。
- **口径备注**：hhad/ttg 市场基准=腿价隐含 (1−haircut)/odds，代数上
  即引擎联合矩阵市场侧概率（价由其构造）——票面不变量满足；腿面
  显式标"下注集"（选择有偏）。era1 基准=PSC 代理的口径差在报告
  判读注意里声明。
- **三轴 review**：双轴（联赛/赛季下注面补齐、EV 玩法分层、
  flat_stake_stats 复用、结论句扩轴）+ correctness 四 finding 批修
  （部分未知腿整注 unknown 桶/run 状态进报告防冒充全量/md先写json后写/
  None 渲染占位+ftr 缺失计数），全带回归测试；878 tests / type / lint /
  fmt 全绿。
- 方向裁决材料汇总进票 22（不自动放行）。
