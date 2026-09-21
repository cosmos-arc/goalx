# 投注策略 / 仓位策略（Staking Plans）调研笔记

> 任务类型：research。日期：2026-09-17。
> 范围：为 goalx（足彩量化投注系统）调研科学的仓位策略，覆盖用户点名的倍投 / 指数进退 / 斐波那契等 progression 系统，以及量化下注的学术标准答案（Kelly 及其修正），并映射到 goalx 的三类玩法（胜平负固定赔率 had/hhad、进球类 ttg/crs、奖池型 ttt14/pick9）。
> 结论先行：**纸面期用 flat；真金期用 fractional Kelly（默认 1/4 Kelly）+ 单注/单日敞口上限；progression 系统（倍投/斐波那契/D'Alembert/Labouchere）数学上不改变期望、只重排破产路径，一律不采用**。落地为"建议仓位"只读功能，不自动改单。详见文末结论。

---

## 1. 主流 Staking Plan 全景

前提事实（适用于所有方案）：**任何下注序列的期望收益 = 各注期望之和（期望的线性性）。仓位方案不能把负 EV 变成正 EV，也不能提高正 EV 的期望值——它只能重排收益分布（均值 vs 方差 vs 破产概率）**。有限资金、无限期对局中，负 EV 局的破产概率恒为 1（gambler's ruin 定理），与用哪种下注序列无关。来源：[Gambler's ruin — Wikipedia](https://en.wikipedia.org/wiki/Gambler%27s_ruin)、[Kelly criterion — Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion)。

### 1.1 Flat（固定注额）

- **规则**：每注固定金额（goalx 现状：默认 ¥100）。
- **数学性质**：期望收益与每注 EV 成正比，最透明。固定注额下，长期收益率（yield/ROI）是无偏的 edge 估计量——这使得 skill 验证、CLV 跟踪、回测的解读最干净。
- **风险**：破产风险最低（相对渐进式方案）；但回撤期固定注额占 shrinking bankroll 的比例被动升高（不自我保护）。
- **适用性**：edge 尚未被证明时（纸面期/验证期）的最佳选择；也是实务界推荐的入门方案（bankroll 拆成 50–100 个单位、每注 1–2 单位）。来源：[Optimal sports betting strategies in practice (arXiv:2107.08827)](https://arxiv.org/abs/2107.08827)、[Pinnacle: Revisiting the Kelly criterion](https://www.pinnacle.com/betting-resources/en/betting-strategy/revisiting-the-kelly-criterion-part-2-fractional-kelly/gbd27z9nljvgflgg)。

### 1.2 Percentage-of-Bankroll（固定百分比）

- **规则**：每注 = 当前 bankroll 的固定百分比（常见 1–3%）。
- **数学性质**：复利几何过程，bankroll 永不精确归零（下注额随资金缩小）；期望对数增长率满足 g = p·log(1+f·d) + q·log(1−f)（f 为比例、d 为净赔率）。只有当 f 恰好等于 Kelly 值时增长率最大；其他比例都是"无意识的 fractional Kelly"。
- **风险**：回撤自动降杠杆（自我保护）；但小比例下恢复慢，大比例（如 5%+）时波动剧烈。对参数误差没有显式防护。
- **适用性**：作为 Kelly 的"穷人版"可用，但有显式 edge 估计时应直接用 fractional Kelly。来源：[Staking in Sports Betting Under Unknown Probabilities (Journal of Gambling Studies / SAGE)](https://journals.sagepub.com/doi/10.1177/1527002520921227)。

### 1.3 Kelly Criterion（凯利公式）

- **规则**：最大化期望对数增长率。二元固定赔率闭式：**f\* = (p·d − q) / d**（d = 净赔率 = decimal odds − 1；q = 1 − p）。等价形式 f\* = edge / odds（edge = p·(d+1) − 1）。
- **数学性质**：
  - 长期增长率 asymptotically 最优：几乎必然超越任何"本质不同"的策略（Kelly 1956；Breiman 1961；Thorp 2006）；
  - 破产概率理论上为 0（比例下注，资金不归零）；
  - 但增长率曲线在 f\* 左侧近似线性、右侧二次衰减——**超注的惩罚远大于少注**（过半 Kelly 反而降低增长率且增大波动）；
  - 前提：p 是**真值**。p 是估计值时，全 Kelly 对高估 edge 的惩罚极不对称。
- **风险**：全 Kelly 波动巨大（常见 35%+ 回撤）；对估计误差敏感。来源：[Kelly, J.L. (1956), A New Interpretation of Information Rate, BSTJ 35:917–926](https://ideas.repec.org/h/wsi/wschap/9789814293501_0003.html)、[Thorp (2006), The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market](https://www.worldscientific.com/doi/pdf/10.1142/9789814293501_fmatter)、[MacLean, Thorp & Ziemba (eds.), The Kelly Capital Growth Investment Criterion, World Scientific 2011](https://books.google.com/books/about/The_Kelly_Capital_Growth_Investment_Crit.html?id=GherKrR0X5cC)、[Kelly criterion — Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion)。

### 1.4 Fractional Kelly（分数凯利：1/2、1/4）

- **规则**：下 f = c·f\*（c = 0.5 或 0.25）。
- **数学性质**：对数增长率保留约 **(2c − c²)**：half Kelly ≈ 75% 增长率、quarter Kelly ≈ 44%；而方差近似按 c² 下降（half Kelly ≈ 25% 方差）。Pinnacle 的模拟：Kelly 减半，"bankroll 亏损 20%" 的概率大致减半；再减半后该风险趋近于零。
- **风险**：牺牲部分增长率换取对**参数估计误差**的鲁棒性——这是实务界把 fractional Kelly 当默认的原因：模型 p 有噪声时，全 Kelly 是在拿真实 bankroll 押注估计精度。来源：[Pinnacle: Fractional Kelly](https://www.pinnacle.com/betting-resources/en/betting-strategy/revisiting-the-kelly-criterion-part-2-fractional-kelly/gbd27z9nljvgflgg)、[Matthew Downey: Why fractional Kelly?](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html)、[How Pros Bet: Half Kelly and Quarter Kelly](https://howprosbet.com/how-to-size-bets-kelly-criterion/)。

### 1.5 Martingale（倍投）

- **规则**：输后翻倍，赢一注收回全部亏损 + 首注利润。
- **数学性质**：期望不变（各注 EV 之和）："高概率小赢 + 低概率巨亏"的分布重排。所需资金按 2ⁿ 增长：从 ¥100 起，10 连输累计投入 ¥102,300；有限 bankroll 下连输 n 次的概率严格为正，无限期下破产概率为 1（含 fair game）。"赢的概率为 1" 的说法依赖无限资金 + 无平台限额的假设，现实中不成立。
- **风险**：**灾难性尾部风险**——平时期小赢，一次长连败清空。UNLV 的轮盘盘分析（Pflaumer 2019）给出了利润分布、标准差与破产概率的显式公式。
- **适用性**：任何场景都不推荐，包括正 EV 局（正 EV 下用 martingale 只会把"稳赢"改成"平时小赢 + 偶发破产"）。来源：[Martingale (betting system) — Wikipedia](https://en.wikipedia.org/wiki/Martingale_(betting_system))、[Gambler's ruin — Wikipedia](https://en.wikipedia.org/wiki/Gambler%27s_ruin)、[MathOverflow: Gambler's ruin following the martingale strategy](https://mathoverflow.net/questions/500255/gambler-s-ruin-following-the-martingale-betting-strategy)、[Pflaumer (2019), A Statistical Analysis of the Roulette Martingale System, UNLV](https://oasis.library.unlv.edu/cgi/viewcontent.cgi?article=1630&context=gaming_institute&)、[Math StackExchange: On Martingale betting system](https://math.stackexchange.com/questions/83904/on-martingale-betting-system)。

### 1.6 Fibonacci（斐波那契进退）

- **规则**：输后沿 1,1,2,3,5,8,13,…（前两注之和）递进，赢后回退两位。
- **数学性质**：注额按黄金比 φ≈1.618 增长，慢于倍投的 2，同样 bankroll 能多撑几步；但偶数赔率下**一赢并不回收全部亏损**（需要连续多赢），"保证回本"的机制比 martingale 更弱。期望同样不变（期望线性性）。
- **风险**：与 martingale 同类的尾部风险，只是到达破产的路径更慢。
- **适用性**：不推荐。来源：[Springer: Betting Systems（系统比较 martingale/Fibonacci/Labouchere/Oscar/d'Alembert/Blundell，含限额下的破产分析）](https://link.springer.com/chapter/10.1007/978-3-540-78783-9_8)、[Progressive Betting Systems Explained (SoccerNews)](https://www.soccernews.com/progressive-betting-systems-explained-martingale-fibonacci-paroli-dalembert-1-3-2-6-labouchere/401499/)。

### 1.7 D'Alembert（达朗贝尔）

- **规则**：输后 +1 单位，赢后 −1 单位（线性进退）。
- **数学性质**：隐含假设胜负"均衡回归"（gambler's fallacy）。独立试验下，深处连败时每注亏损大于每注赢利可回收的部分（累计亏损 n(n+1)/2 随 n 平方增长）；期望不变。
- **风险**：尾部风险比指数类温和，但同样的结论：不改变期望，长期负 EV 局必破产。
- **适用性**：不推荐。来源：[Springer: Betting Systems](https://link.springer.com/chapter/10.1007/978-3-540-78783-9_8)。

### 1.8 Labouchere（取消法）

- **规则**：维护一个数字序列，每注 = 首尾之和，赢则划掉首尾，输则把注额追加到队尾；序列清空即完成一轮目标利润。
- **数学性质**：单轮目标小、但轮长无上界，连败时所需注额加速增长（且追加使序列变长，可能发散）；期望不变。
- **风险**：与 martingale 同级的灾难尾部，只是形态不同。
- **适用性**：不推荐。来源：[Springer: Betting Systems](https://link.springer.com/chapter/10.1007/978-3-540-78783-9_8)、[CasinoKeller: Roulette systems — the mathematical truth](https://casinokeller.com/en/blog/roulette-systems-the-mathematical-truth)。

### 对比表

| 方案 | 注额规则 | 对期望的影响 | 破产风险 | 波动/回撤 | 对正 EV 局 | 对负 EV 局 | goalx 适用性 |
|---|---|---|---|---|---|---|---|
| Flat | 固定金额 | 不改变 | 最低 | 与单位大小成正比 | 稳定盈利，指标无偏 | 稳定亏损，亏损显性 | **纸面期默认** |
| Percentage | 当前资金 × 固定% | 不改变 | 极低（不归零） | 中 | 可用，但增长率非最优 | 慢性失血，难察觉 | 可用（Kelly 的模糊版） |
| Kelly（全） | f\*=(pd−q)/d | 不改变；增长率最优 | 理论 0，实际受估计误差威胁 | 高（35%+ 回撤常见） | **理论最优**，但对 p 噪声极敏感 | 加速亏损 | 仅在 p 高可信时 |
| Fractional Kelly | c·f\*（c=1/4–1/2） | 不改变；保留 44–75% 增长率 | 低 | 中低（方差 ≈ c²） | **实务标准答案** | 仍会亏损（正确行为） | **真金期默认** |
| Martingale | 输后 ×2 | 不改变 | **高（必然性随时间→1）** | 平时极低 + 罕见清零 | 重排为"小赢+尾部破产" | 加速破产 | 禁用 |
| Fibonacci | 输后 +φ 递进 | 不改变 | 高（慢于 martingale） | 同上 | 同上 | 加速破产 | 禁用 |
| D'Alembert | 输后 +1 / 赢后 −1 | 不改变 | 中高 | 中 | 弱于指数类，仍不改变期望 | 慢性破产 | 禁用 |
| Labouchere | 首尾和，输后追加 | 不改变 | 高（轮长无上界） | 平时小赢 + 尾部破产 | 同上 | 加速破产 | 禁用 |

---

## 2. 学术共识

### 2.1 为什么 Kelly 是量化下注的标准答案

- Kelly (1956) 把"有噪声信道的信息率"等价于"按真概率优势下注时的资金对数增长率"，给出 **最大化 E[log W]** 的下注准则；Breiman (1961) 证明该策略几乎必然渐近支配其他策略；Thorp (2006) 系统化到 21 点、体育博彩与股市。MacLean–Thorp–Ziemba (2011) 的文集是该领域权威汇编。
- 对数效用 = 最大化长期复利增长率 = 隐含的风险管理（比例下注永不清零）。
- 实证：在真实赔率数据上比较 flat、比例、Kelly、组合论策略，Kelly 与 MaxSharpe 表现最好，"further support its common use in betting practice"（[arXiv:2107.08827](https://arxiv.org/abs/2107.08827)）。另一篇统计理论工作指出体育下注成功的关键是估计**中位数结果**（即模型概率）而非命中率，从理论上支撑"模型概率 → Kelly"的管线（[Dmochowski 2023](https://pmc.ncbi.nlm.nih.gov/articles/PMC10306238/)）。
- 注意学术界的同样共识的另一面：**Kelly 的最优性以"p 已知"为前提**；这正是 fractional Kelly 存在的理由（见 §3.1）。

### 2.2 为什么 progression 系统在数学上无效

- **期望的线性性**：任何"根据历史调整下一注大小"的规则，产生的序列期望 = Σ E[单注]；单注 EV 为 0（fair）或负（抽水），序列 EV 同号。仓位规则只能重排分布（改偏度：多数情况小赢、少数情况巨亏）。
- **赌徒谬误**：progression 系统的直觉基础是"输多了该赢了"；独立试验下这不是事实。D'Alembert 甚至直接把"均衡回归"写进规则。
- **Gambler's ruin**：有限资金 + 无限期对局，fair game 下触及 0 的概率也是 1（对手"无限富"时），负 EV 局更不必说。martingale 的"稳赢"只在无限资金、无限信用、无限额的数学极限中成立。来源：[Gambler's ruin — Wikipedia](https://en.wikipedia.org/wiki/Gambler%27s_ruin)、[MathOverflow](https://mathoverflow.net/questions/500255/gambler-s-ruin-following-the-martingale-betting-strategy)、[Eventually Almost Everywhere: fair games and the martingale strategy](https://eventuallyalmosteverywhere.wordpress.com/2015/12/11/fair-games-and-the-martingale-strategy-iii/)、[Springer: Betting Systems](https://link.springer.com/chapter/10.1007/978-3-540-78783-9_8)。
- 推论（对 goalx 重要）：**即使模型确有正 EV，progression 也只有坏处**——它把"按比例复利的稳定增长"换成"平时小赢 + 尾部破产"，且会污染 CLV/skill 统计（注额与结果序列相关，yield 不再是 edge 的无偏估计）。

### 2.3 Parimutuel（奖池型）下的 Kelly 修正

固定赔率的 Kelly 输入是 (p, d)。奖池型（彩池分红、无固定赔率）需要三处修正：

1. **派彩即赔率，且含抽水**：有效净赔率 = (1 − takeout) × 彩池结构决定的派彩率。中国体彩传统足彩（14 场/任九）返奖率约 65%（64% 当期奖金 + 1% 调节基金），即约 35% 的结构性抽水；这是任何策略长期收益的天花板。来源：[任选九场游戏规则](https://sports.sina.com.cn/l/rule/r9/)、[14场+任9玩法规则](https://help.jd.com/o/help/question-329.html)、[中国体彩网-传统足彩](https://www.lottery.gov.cn/zc/index.html)。
2. **自己的注额推动赔率（price impact）**：parimutuel 下大额下注压低自己中出时的派彩，Kelly 注额必须以"下注后"的派彩计算—— Isaacs (1953) 最早给出最优 parimutuel 下注模型，Kelly (1956) 给出 log 最优版本；两条线在竞速彩池文献中合流（来源：[Isaacs, Optimal Horse Race Bets](https://www.semanticscholar.org/paper/Optimal-Horse-Race-Bets-Isaacs/799c11ef15de20c5b9f67979da8e7cf2dd133b7b)、[Entropy-Based Strategies for Multi-Bracket Pools, arXiv:2308.14339](https://arxiv.org/html/2308.14339v3)）。goalx 个人资金量级下 price impact 可忽略，但接口上应保留派彩随注额重算的能力。
3. **派彩在投注时未知**：期望派彩需用当前彩池/历史形态估计，且存在**分彩风险**——中奖注数多时单注派彩被摊薄，而"人人都能猜中的结果"（热门组合）恰恰派彩最低。因此 EV 应写成 p(中奖) × E[派彩 | 中奖] − 成本，Kelly 输入里的"d"是随机变量；保守做法是用分位数而非均值。学术线：Hausch–Ziemba–Rubinstein (1981) 的彩池效率/期望值模型（Management Science 27(12)）、[Chance Constrained Optimization for Parimutuel Horse Race Betting](https://www.researchgate.net/publication/274012234_Chance_constrained_optimization_for_parimutuel_horse_race_betting)。

---

## 3. 实务修正

### 3.1 Fractional Kelly 是行业默认，而非保守癖好

- p 由模型估计、含误差。全 Kelly 在"高估 edge"方向的惩罚不对称：超注侧增长率二次衰减 + 波动放大，少注侧只线性损失。模拟显示存在估计噪声时 half Kelly 的风险调整后表现常优于全 Kelly（[Matthew Downey 的模拟](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html)）。
- 惯例取 **1/4 到 1/2 Kelly**：serious bettors 常用 quarter/half 作为对模型误差的缓冲（[Pinnacle](https://www.pinnacle.com/betting-resources/en/betting-strategy/revisiting-the-kelly-criterion-part-2-fractional-kelly/gbd27z9nljvgflgg)、[How Pros Bet](https://howprosbet.com/how-to-size-bets-kelly-criterion/)）。
- goalx 已有 skill 三条件（前瞻 skill ≥ 0 才转真金）——转真金初期模型可信度证据有限，建议从 **1/4 Kelly** 起步，skill 证据累积后再放宽到 1/2。

### 3.2 串关（multi/parlay）下的 Kelly

- **正确做法**：把串关当成**一注组合赌**，用联合概率和联合赔率代入 Kelly：f\* = (p_joint·d_joint − q_joint)/d_joint，其中 p_joint = Πp_i（腿间独立时）、d_joint = Πd_i。来源：[GamblingCalc: Kelly Criterion for Parlays](https://gamblingcalc.com/betting/kelly-criterion-for-parlays/)。
- **常见错误**（来源：[OddsIndex Kelly guide](https://oddsindex.com/guides/kelly-criterion-calculator)、[Quant SE: Kelly for multiple simultaneous correlated bets](https://quant.stackexchange.com/questions/68297/kelly-criterion-for-multiple-simultaneous-correlated-bets)）：
  1. **对每条腿分别算 Kelly 再相加/叠投**——系统性超注；
  2. **忽略腿间相关性**：相关腿（同场、同矩阵推导的市场）联合概率 ≠ 概率乘积，naive 乘积会虚增"edge"，让 Kelly 对实际负 EV 的组合开出大注；
  3. **忽视抽水复利**：串关抽水按腿复利 (1+m)ⁿ − 1，单腿正 EV 的组合串起来常常是负 EV——Kelly 前必须先确认组合 EV > 0；
  4. **对高方差组合赌用全 Kelly**：联合赌的方差远高于单腿，更要用 fractional。
- **同场多市场**（goalx 特有）：ttg 与 crs 概率都由同一比分矩阵推导，互斥/相关的多个 ticket 应作为**组合问题**求解（互斥多结果 Kelly 无简单闭式，需小型凸优化；见 Smoczynski & Tomkins 2010, The Mathematical Scientist 35(1) 的显式解），或退化为"单场总敞口上限 + 按比例分配"。

### 3.3 敞口上限的行业惯例

- 单注不超过 bankroll 的 **1–5%**（常见 1–2%）；bankroll 拆 50–100 个下注单位。即使按 fractional Kelly 算出的注额，也要再过这道硬上限（防模型黑天鹅）。来源：[Pinnacle: staking 与 Kelly 系列](https://www.pinnacle.com/betting-resources/en/betting-strategy/revisiting-the-kelly-criterion-part-2-fractional-kelly/gbd27z9nljvgflgg)、[How Pros Bet](https://howprosbet.com/how-to-size-bets-kelly-criterion/)。
- 衍生惯例：单日/单轮总敞口上限（如 5–10%）、单场（同一事件多市场合计）上限——对 goalx 尤其重要，因为同场 ttg/crs/had 的 edge 来自同一个模型，本质是一份风险。

---

## 4. 对 goalx 三类玩法的适用性映射

| 玩法类别 | 赔率机制 | 概率来源 | 推荐策略 |
|---|---|---|---|
| had / hhad（含串关类） | 固定赔率 | DC 模型 forecast | **fractional Kelly（默认 1/4）**+ 1–5% 硬上限；串关按联合概率整注计算，EV≤0 拒单 |
| ttg / crs（进球类） | 固定赔率 | 比分矩阵边缘化 | 同上；同场多 ticket 视为组合，用"单场总敞口上限 + 比例分配"控制相关性风险 |
| ttt14 / pick9（奖池型） | 彩池分红（无固定赔率，返奖率 ~65%） | 比分矩阵 + 彩池派彩估计 | **彩池修正后的 fractional Kelly**：EV = p×E[派彩|中奖] − 成本，派彩用保守分位数；初期建议仅纸面 + flat，真金后低敞口上限（<1%） |
| 纸面期（skill 未过线） | 任意 | 任意 | **flat ¥100**：skill/CLV 指标无偏、零破产风险、把"模型验证"与"仓位决策"解耦 |

要点：

1. **固定赔率类（had/hhad/ttg/crs）**：Kelly 的输入齐备（模型 p、盘口赔率），标准答案直接适用。竞彩官方返奖率 71–73%（[单场竞猜胜平负规则](https://www.lottery.gov.cn/bzzx/yxgz/20191119/10026481.html)），意味着长期可达成收益率的天花板本身不高——只有当模型概率与盘口隐含概率的偏离（去抽水后）仍然显著时才值得下注，此时 Kelly 把 edge 转化为注额。
2. **进球类的相关性**：crs 的 31 个比分互斥且共享比分矩阵——对多个比分下注应做互斥组合 Kelly（或归一化分配），绝不能逐个独立算 Kelly；ttg 与 crs 同场同源同理。
3. **奖池类（ttt14/pick9）**：这是最接近学术 parimutuel 文献的场景。两个结构性难点：(a) ~35% 抽水使得需要非常大的模型优势才可能正 EV；(b) 分彩风险使"热门组合"天然低派彩，价值集中在少数派预测上。建议：先在纸面期用历史彩池数据校准派彩估计，真金期以**极低单注上限 + fractional Kelly** 运作，并把"预计派彩 vs 实际派彩"记入 ledger 供校准。覆盖率策略（多票覆盖高概率组合）只在覆盖成本 < Σp×派彩时有意义，本质仍是组合 EV 问题。
4. **纸面期一律 flat**：与 goalx 现有 skill 三条件门控吻合——skill 验证期内引入任何变注额机制都会让 yield 不再是 edge 的无偏估计、并增加解释变量。flat 是验证期的"实验对照条件"。

---

## 5. 落地形态建议："建议仓位"功能最小需求

定位：**只读建议（advisory），不自动改单**。嵌入建注流（bet slip），用户可采纳/忽略。

### 5.1 核心 API（`modelling` 或新 `staking` 包，表 SQL 归属该包）

```
suggest_stake(
  bankroll: Decimal,          # 来自 bankroll 账本（真金模式）；纸面模式不用
  p_model: Decimal,           # 模型概率（had/ttg/crs 来自 forecast，串关为联合概率）
  odds: Decimal,              # decimal 赔率（串关为联合赔率；奖池型为估计派彩）
  kelly_fraction: Decimal,    # 默认 0.25，可配 0.5 / 1.0
  cap_pct: Decimal,           # 默认 0.02，硬上限区间 0.01–0.05
  stake_step: Decimal = 2     # 注额取整步长（¥2 的倍数）
) -> StakeSuggestion
```

`StakeSuggestion` 字段：
- `ev_per_unit`、`edge`（= p·odds − 1）——展示用；
- `full_kelly`、`suggested`（= min(kelly_fraction 折算, cap_pct×bankroll)，按 stake_step 向下取整）；
- `capped: bool` + `reason`：`EV_NON_POSITIVE`（建议 ¥0，附"无价值"提示）/ `NEGATIVE_KELLY` / `CAP_APPLIED` / `OK`。

### 5.2 行为规则

1. **EV ≤ 0 → 建议 ¥0** 并显式说明（防止"系统推荐了我再自己加码"的误导）。
2. **纸面模式永远输出 flat 默认注额**（当前 ¥100），不输出 Kelly——与 skill 门控一致。
3. 建议值 = min(1/4 Kelly 注额, cap_pct × bankroll)，向下取整到 ¥2 步长；低于最低投注单位时输出最低单位并标记。
4. 串关：由调用方传入联合概率/联合赔率（腿间独立性假设 + 抽水复利检查在前），本函数只做"整注"Kelly，不做分腿。
5. 同场多市场（ttg+crs）：MVP 用"单场总敞口 ≤ cap_pct"约束 + 各 ticket 按 Kelly 权重归一化分配；凸优化精确解列为后续项。
6. 奖池型：MVP 可只接入 EV 展示（估计派彩为输入），Kelly 建议置灰/降级，等派彩估计模型就绪再启用。

### 5.3 记录与可回溯性

- 每次建注记录 `suggested_stake` vs `actual_stake`（进 bet 表或 ledger 备注），供事后分析：用户是否系统性偏离建议、偏离是否损害 yield——这是未来把 fraction 从 1/4 调向 1/2 的证据来源。
- 建议所用输入（p、odds、bankroll 快照、fraction、cap）随注单留痕，保证可复算。

### 5.4 非目标（明确不做）

- 不自动写入注额（不碰现有默认 ¥100 的行为）；
- 不实现任何 progression 逻辑（martingale/Fibonacci/D'Alembert/Labouchere/Paroli）；
- 纸面期不引入变注额；
- 不做多账户/多场联立 Kelly 优化（后续项）。

---

## 6. 结论（推荐方案与理由）

推荐**"纸面期 flat + 真金期 1/4 fractional Kelly + 1–5% 硬上限"**的组合，作为"建议仓位"只读功能落地。理由：Kelly（对数效用）是量化下注的学术标准答案，其实证与理论地位明确（Kelly 1956；Thorp 2006；arXiv:2107.08827 实证支持），但它的最优性以真实概率已知为前提，而 goalx 的模型概率是估计值，因此实务界与文献的共识是取 1/4–1/2 分数以对称化估计误差风险（超注惩罚二次、少注损失线性）；1/4 起步与 goalx 现有的 skill 三条件门控逻辑同构——真金初期模型证据有限，先保守、随证据累积再放宽。用户点名的倍投/斐波那契等 progression 系统在期望的线性性下不可能改变 EV，只以"平时小赢、尾部清零"的方式重排破产路径，且会污染 CLV/skill 统计，全部排除。三类玩法中，固定赔率类（had/hhad/ttg/crs）直接适用该方案（同场多市场按组合处理相关性）；奖池型（ttt14/pick9）需先做彩池修正（返奖率 ~65% 的抽水 + 分彩风险使派彩成为随机变量），初期仅纸面、真金后用极低上限；纸面期（skill 未过线）一律 flat，保证验证指标无偏。

## 来源汇总

学术/理论：
- Kelly, J.L. (1956). *A New Interpretation of Information Rate*. Bell System Technical Journal 35:917–926. [RePEc 条目](https://ideas.repec.org/h/wsi/wschap/9789814293501_0003.html)
- MacLean, Thorp & Ziemba (eds.) (2011). *The Kelly Capital Growth Investment Criterion*. World Scientific. [Front matter](https://www.worldscientific.com/doi/pdf/10.1142/9789814293501_fmatter) / [Google Books](https://books.google.com/books/about/The_Kelly_Capital_Growth_Investment_Crit.html?id=GherKrR0X5cC)
- Thorp, E.O. (2006). *The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market*.（收录于上书）
- Isaacs, R. (1953). *Optimal Horse Race Bets*.（parimutuel 最优下注开山）[Semantic Scholar](https://www.semanticscholar.org/paper/Optimal-Horse-Race-Bets-Isaacs/799c11ef15de20c5b9f67979da8e7cf2dd133b7b)
- Hausch, Ziemba & Rubinstein (1981). *Efficiency of the Market for Racetrack Betting*. Management Science 27(12).
- Smoczynski & Tomkins (2010). *An Explicit Solution to the Problem of Optimizing the Allocations of a Bettor's Wealth When Wagering on Horse Races*. The Mathematical Scientist 35(1).（互斥多结果 Kelly 显式解）
- *Optimal sports betting strategies in practice: an experimental review*. [arXiv:2107.08827](https://arxiv.org/abs/2107.08827)
- *A statistical theory of optimal decision-making in sports betting* (Dmochowski, 2023). [PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10306238/)
- *Staking in Sports Betting Under Unknown Probabilities*. [SAGE](https://journals.sagepub.com/doi/10.1177/1527002520921227)
- Pflaumer (2019). *A Statistical Analysis of the Roulette Martingale System*. [UNLV](https://oasis.library.unlv.edu/cgi/viewcontent.cgi?article=1630&context=gaming_institute&)
- *Betting Systems*（martingale/Fibonacci/Labouchere/Oscar/d'Alembert 系统比较，含限额下破产分析）. [Springer chapter](https://link.springer.com/chapter/10.1007/978-3-540-78783-9_8)
- *Entropy-Based Strategies for Multi-Bracket Pools*. [arXiv:2308.14339](https://arxiv.org/html/2308.14339v3)
- *Chance Constrained Optimization for Parimutuel Horse Race Betting*. [ResearchGate](https://www.researchgate.net/publication/274012234_Chance_constrained_optimization_for_parimutuel_horse_race_betting)

参考/科普（数学性质一致）：
- [Kelly criterion — Wikipedia](https://en.wikipedia.org/wiki/Kelly_criterion) / [Gambler's ruin — Wikipedia](https://en.wikipedia.org/wiki/Gambler%27s_ruin) / [Martingale (betting system) — Wikipedia](https://en.wikipedia.org/wiki/Martingale_(betting_system))
- [MathOverflow: Gambler's ruin following the martingale betting strategy](https://mathoverflow.net/questions/500255/gambler-s-ruin-following-the-martingale-betting-strategy) / [Math StackExchange: On Martingale betting system](https://math.stackexchange.com/questions/83904/on-martingale-betting-system)
- [Pinnacle: Revisiting the Kelly criterion — fractional Kelly](https://www.pinnacle.com/betting-resources/en/betting-strategy/revisiting-the-kelly-criterion-part-2-fractional-kelly/gbd27z9nljvgflgg)
- [Matthew Downey: Why fractional Kelly? Simulations with uncertainty](https://matthewdowney.github.io/uncertainty-kelly-criterion-optimal-bet-size.html)
- [How Pros Bet: Kelly Bet Sizing — Half Kelly and Quarter Kelly](https://howprosbet.com/how-to-size-bets-kelly-criterion/)
- [GamblingCalc: Kelly Criterion for Parlays](https://gamblingcalc.com/betting/kelly-criterion-for-parlays/) / [OddsIndex: Kelly Criterion Calculator guide](https://oddsindex.com/guides/kelly-criterion-calculator) / [Quant SE: Kelly for multiple simultaneous correlated bets](https://quant.stackexchange.com/questions/68297/kelly-criterion-for-multiple-simultaneous-correlated-bets)

中国体彩玩法规则（返奖率/彩池机制）：
- [中国足球彩票单场竞猜胜平负游戏规则（返奖率 73%，现 71%）](https://www.lottery.gov.cn/bzzx/yxgz/20191119/10026481.html)
- [中国体育彩票任选九场游戏规则（返奖率 65%，浮动奖级）](https://sports.sina.com.cn/l/rule/r9/)
- [中国体彩网 — 传统足彩（14 场胜负）](https://www.lottery.gov.cn/zc/index.html)
- [14 场 + 任 9 玩法规则（彩池计奖）](https://help.jd.com/o/help/question-329.html)
