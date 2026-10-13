# 20 CLV 十年仪表盘：下注价 vs 收盘的蒸发/增值分解

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` S20
Blocked by: 19

## 任务

- **新报告模块** `evaluation/clv_decade.py` + CLI 子命令：分母 = 票 19
  十年跑的全部腿，按 era × 联赛 × 玩法分层。
- **结构性校验面**：模拟下注价 vs 收盘公平价——构造上应 ≈ −haircut
  （价 = (1/p_mkt)×(1−haircut)），全量实证；偏离=护栏剪切或价链 bug，
  逐类解释（护栏触发样本已有票 18 计数通道）。
- **早锁价值面**（真蒸发/增值问题）：模型选中的 selection，更早市场
  时点隐含概率 vs 收盘隐含概率——市场后来向模型靠拢 = 模型领先
  （正信号），反之 = 跟随噪声：
  - 有轨迹/切片：q1h1x2/q24h1x2 切片 + AH 锚 open→close
    （ah_anchor_drift / open-close line）；
  - era1：PSH（开盘）→ PSC（收盘代理）；
  - 切片缺失率分 era 诚实计数，不插补。
- beat/evap 定义沿用既有 `evaluation/clv.py` 口径，不另造；
  报告落 `reports/` MD+JSON。

## 不变量与人裁决项

- 收盘公平价 = 引擎同源市场概率（不重算出分歧口径）；
- era1 PSC ≠ 轨迹末价的已知局限在报告显著标注；
- 观察性结论：不回写引擎参数（haircut 已裁维持）；
- 生产 CLV 通道（clv.py）不动；
- 人裁决项：无（结论供票 22 方向裁决引用）。

## Out of scope

- 生产/纸面闭环 CLV；odds_snapshots 三级锚（生产链路，本票只吃十年回测腿）。

## Answer

2026-10-14 交付（分支 feat/clv-decade-c20-20261014，PR #142，三提交）：

- **报告模块** `evaluation/clv_decade.py` + CLI `clv-decode-report
  --run-id`：结构校验面 / 早锁价值面（同族早晚价对：era1 PSH→PSC、
  era2 q24h/q1h→close1x2 锚；比例 devig 两端同法；era+联赛双层，
  4 era 面 + 44 联赛面）/ 线漂移描述面（被下注 vs 全预测场次）；
  报告落真树 reports/clv-decade.{json,md}（run=5，52,865 腿，
  had 独立选样 23,816）。
- **十年发现**：
  - 结构校验完美——had 27,760 腿恒等式 odds×fair_p≡1−haircut
    中位 0.88723==期望零偏离，三玩法隐含带外 0（价链无 bug）；
  - **模型系统性选中市场正在抛弃的一侧**：早锁面全线 beat 27-38%
    （psh_open era1 18,067 选样 28.5%、q24h era2 5,654 选样 26.7%），
    clv 中位 −0.010，联赛分层面无一幸免——与票 19 负 skill 互为
    印证：模型不是"晚到"而是方向反着跟（研究 18"模型信号应早锁"
    的前提在此数据上不成立：模型信号本身反向）；
  - 线漂移：被下注场次与全预测场次无差异。
- **口径裁决记档**：hhad/ttg 选择=联合矩阵派生无同族早价（覆盖
  缺口计数）；AH 锚两向带 push 语义与 hhad 三向不同构，早晚锚差只入
  描述面不冒充选择级 CLV；早锁 clv=两端同法 devig 概率位移
  （clv.py 口径的归一化变体，MD 判读注意自认）。
- **连带修复**：metrics.load_backtest_run 共享加载器（decade_skill
  同步迁移）；gold_reader 两个 by-sid 取数定序首行去重（票 17
  fetch_close_rows_by_sids 的 last-wins 旧账一并修）。
- **三轴 review**：双轴四修 + correctness 五修（odds 退化护栏/
  sid 谱系/beat 未圆整/None 桶/had-无-fair 标签），全带回归测试；
  891 tests / type / lint / fmt 全绿。
- 观察性报告不回写引擎参数（haircut 已裁维持）。
