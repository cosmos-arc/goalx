# 04 注额优化、串关数学与业界系统研究（调研笔记）

- 票：`../issues/04-staking-optimization-and-industry-systems.md`
- 日期：2026-09-12
- 方法：论断追到一手来源（论文/期刊、官方规则页、产品官方文档/博客）；所有 URL 于 2026-09-12 访问核实。竞彩返奖率口径沿用 `03-prediction-modeling.md` §0 的核对结果。文中标注「（推导）」的算例为本人基于引用公式的演算，非来源原文数字。

---

## 1. 注额算法

### 1.1 单注 Kelly：公式与两种等价写法

设十进制赔率 O，真实命中概率 p，单位本金：

- 净赔率 b = O − 1，q = 1 − p。Kelly 原始形式：**f\* = (b·p − q)/b = (p·O − 1)/(O − 1)**
- 用 edge（单位 EV）表示：edge = p·O − 1，则 **f\* = edge/(O − 1)**，即注额 = EV ÷ 净赔率。

来源：Kelly (1956)；Thorp, "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market"（1997 会议论文，后收入 *Handbook of Asset and Liability Management* 2006），转引自 [Wikipedia: Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion)（含 Thorp 1997 页码标注）。

关键性质（Thorp，同上）：

- f\* 最大化对数财富期望 E[log(1 + f·X)]，即长期复利增长率 g 的唯一极大点；
- **超注（f > f\*）比欠注（f < f\*）危害大得多**：g(f) 在 f\* 右侧单调下降且在 f ≥ 2f\* 后为负（增长为负，注定了长期破产方向）。这是分数 Kelly 的理论根基。

算例（推导）：O = 2.10，p = 0.52 → edge = +9.2%，full Kelly = 9.2%/1.10 = **8.4% 银行**。可见一个「中等偏强的 edge」对应的 full Kelly 已经是全仓 8%——full Kelly 在实践中不可直接用，必须打折加上限。

### 1.2 分数 Kelly（1/4、1/2）：理论与实证依据

**二次近似**：增长率在 f\* 附近是凹二次曲线，按 λ∈(0,1] 比例下注时 g(λf\*) ≈ (2λ − λ²)·g(f\*)：

| λ | 保留增长率 | 相对方差 |
|---|---|---|
| 1.0（full） | 100% | 100% |
| 0.5（half） | 75% | 25%（vol 减半） |
| 0.25（quarter） | 43.75% | 6.25%（vol 为 1/4） |

来源：[Wikipedia: Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion)（Relative bet sizes 节，引 MacLean-Thorp-Ziemba）；数字与 [MetricGate Kelly 文档](https://metricgate.com/docs/kelly-criterion/)、[OddsIndex Kelly 计算器指南](https://oddsindex.com/guides/kelly-criterion-calculator)（"Half Kelly gives you 75% of the growth rate while cutting volatility in half"）一致。理论正典：MacLean, Thorp & Ziemba (2010), "Long-term capital growth: the good and bad properties of this and fractional Kelly capital growth criteria"，收录于 MacLean/Thorp/Ziemba 编 *The Kelly Capital Growth Investment Criterion: Theory and Practice*, World Scientific 2011（[书页](https://econpapers.repec.org/RePEc:wsi:wsbook:7598)，[书评](https://www.tandfonline.com/doi/full/10.1080/14697688.2011.619561)：full Kelly 伴随大幅回撤、fractional Kelly 以少量增长换安全）。

**蒙特卡洛实证（Buchdahl, football-data.co.uk）**：Joseph Buchdahl《[The Pitfalls of using Kelly Staking in Sports Betting](https://www.football-data.co.uk/blog/kelly_staking.php)》，1,000 注让分盘（O = 1.909，真实 EV = 2.5%，full Kelly = 2.75%）模拟：

| 注额策略 | 1000 注后亏损概率 | 银行减半概率 |
|---|---|---|
| full Kelly 2.75%（EV 估计正确） | ~34% | ~10% |
| 平注（同 EV） | ~18% | 显著更低 |
| half Kelly 1.375% | ~26%（盈利概率 73%） | ~1% |
| 1/3 Kelly | 更低 | <0.1% |
| 5.5%（EV 高估 2 倍） | ~50% | >8% 概率损失 90%+ 银行 |
| 高估 3 倍 | ~66% 亏损 | 更糟 |

核心结论：**Kelly 的命门是 edge 的估计误差**——edge 高估 2–3 倍，「赢的系统」直接变成输的系统；且比例下注输后回本更慢（结果对数正态分布，右尾长但多数路径更「红」）。没把握就用 half/quarter/eighth Kelly，任何分数下最优结果仍在盈利侧。

**估计误差理论**：Carta & Conversano (2020), "Practical Implementation of the Kelly Criterion", *Front. Appl. Math. Stat.* 6, [DOI 10.3389/fams.2020.577050](https://www.frontiersin.org/journals/applied-mathematics-and-statistics/articles/10.3389/fams.2020.577050/full)：模拟显示 half Kelly 是估计误差下最稳健的选择，triple Kelly 在 40,000 期下 P(最终财富 < 初始) = 100%；并引 Rising & Wyner：**分数 Kelly 投资者 ≡ 把收益估计向保守收缩后的 full Kelly 投资者**（分数 Kelly = 收缩估计，有正式理论解释）。

### 1.3 同时多注 Kelly（含相关性）

**正确目标函数**：N 笔同时未结算注，注额向量 f = (f₁..f_N)，联合结果 X = (X₁..X_N)，最大化

  **E[ log(1 + Σᵢ fᵢ·Xᵢ) ]**

——对 2^N 个联合结果枚举求和；N > 2 时无闭式解，梯度可解析计算，用投影梯度上升数值求解（[Vegapit: Numerically solve Kelly criterion for multiple simultaneous bets](https://vegapit.com/article/numerically_solve_kelly_criterion_multiple_simultaneous_bets/)，含 Rust 实现与在线 Kelly 计算器）。

**已知性质**：

- 同时独立多注的总注额**小于**各注单独 Kelly 之和（每注的存在挤占其余注的「风险预算」）；
- 注数多时，Whitrow (2007) 给出近似：**fᵢ ∝ 该注的 probabilistic edge = pᵢ − 1/(bᵢ+1)**（真实概率减赔率隐含概率），即按「去水后 edge」等比例缩放分配。来源：Whitrow, D.J. (2007), "Algorithms for optimal allocation of bets on many simultaneous events", *Statistics and Computing* 17:217–226（精确引用经 [Vegapit 文](https://vegapit.com/article/numerically_solve_kelly_criterion_multiple_simultaneous_bets/)核实）。
- 连续/矩阵近似（Thorp 标准结果，Carta 2020 式(12)）：**F\* = Σ⁻¹(M − r·1)**，F 为各注比例向量、Σ 为收益协方差矩阵、M 为期望收益向量——与 Markowitz 均值方差同构（另见 [expectedvalue.co.uk](https://expectedvalue.co.uk/blog/kelly-criterion-optimal-bet-sizing/)）。**相关注（Σ 非对角）必须走矩阵/数值路线**：正相关会同时放大总注额的方差，解析单注公式逐个套用会系统性超注。

**业界实践（RebelBetting value betting 产品）**：不做全局优化，用「分数 Kelly + 单注上限 + 未结算注扣减」三件套（[官方 value betting guide](https://www.rebelbetting.com/en-us/valuebetting/value-betting-guide)，核实于 2026-09-12）：

1. Kelly 百分比默认 **30%**（可调）；
2. 注额上限（如 1–3% 银行）；
3. **"Adjust for open bets"**：计算新注额时先用「当前银行 − 未结算注的合计风险金」作为有效银行，避免同一时段多注重复叠加风险。

### 1.4 串关内部强相关的处理

**原则：把整个串关当成「一笔注」做 Kelly，用联合概率，绝不逐腿套单注 Kelly。**

- f\* = (P_joint·O_parlay − 1)/(O_parlay − 1)，其中 O_parlay = Π Oᵢ。
- 腿独立时 P_joint = Π pᵢ；**同场腿强相关时 P_joint ≠ Π pᵢ**，须用联合分布估计。Wizard of Odds《[Same-Game Parlays: The Mathematics of Correlation](https://wizardofodds.com/article/same-game-parlays-the-mathematics-of-correlation/)》：典型同场二值腿 Pearson ρ ≈ 0.28–0.42（−0.4 至 +0.6 区间），但因相关导致**联合概率比独立假设高 30–50%**（例：独立推算 16.0% → copula 计入相关 21.2%）；正规算法是 Gaussian copula（Zᵢ = Φ⁻¹(pᵢ)，多元正态相关矩阵 R 下求 P(Zᵢ > cᵢ)）或历史可比场次经验频率（500 场同让分/总分带内统计）。庄家已按此精确定价并叠抽水：SGP 庄家边际通常 **15–25%**（单注的 3–5 倍）。
- **竞彩特例（规则性保护）**：体彩官方 FAQ 明确「同一场比赛的不同玩法不可串关；足球和篮球不能混合串关」（体彩网，经搜索结果核实；规则全文见[新浪竞彩足球规则页](https://sports.sina.com.cn/l/rule/jczq/)）。因此**竞彩 2串1 的两腿天然跨场、近似独立**，P_joint = p₁·p₂ 的条件独立近似成立——与 03 号研究笔记 §1.3 任9/14 的处理同口径。「串关内强相关」问题在竞彩场景下不存在；若系统将来接海外 SGP 才需要 copula/经验联合分布。

### 1.5 增长最优 vs 回撤约束（variance-adjusted / drawdown-constrained Kelly）

**学术正解**：Busseti, Ryu & Boyd (2016), "Risk-Constrained Kelly Gambling", *The Journal of Investing* 25(3):118–134（[作者页](https://stanford.edu/~boyd/papers/kelly.html)、[arXiv:1603.06183](https://arxiv.org/abs/1603.06183)）：在 Kelly 目标上加**回撤概率的 chance constraint**——约束 P(财富跌破峰值×α) ≤ β——作者推导回撤概率上界代换后得到**凸优化问题**，蒙特卡洛验证上界紧；约束的效果等价于把 full Kelly 压到某个分数（牺牲部分增长率换取回撤概率受控）。CVXR 教程有可直接照抄的实现（[cvxr.rbind.io Kelly gambling](https://cvxr.rbind.io/examples/finance/kelly-strategy/)）。

**实用等价做法（推荐给个人系统）**：分数 Kelly 本身就是回撤控制——按 Buchdahl 模拟，half Kelly 把「银行减半概率」从 10% 压到 1%，1/3 Kelly 压到 <0.1%（§1.2 表）。个人系统的正确姿势不是解凸优化，而是：

1. 选 λ（如 0.25）；
2. 用自己的历史注单/回测做蒙特卡洛，得到该 λ 下的回撤分布；
3. 若 P(DD > d_max) > 可容忍值，下调 λ 或收紧单注上限。

### 1.6 Bankroll 更新口径

- Kelly 按定义用**下注时刻的当前银行**；实现上用「有效银行 = 当前银行 − Σ 未结算注的风险金」（RebelBetting "adjust for open bets"，§1.3 来源同）。
- 结算后更新真实银行；已下注的注额不因银行变动回改。
- 平注口径：unit = 银行的固定百分比（0.5–1%），**按周或按月重算一次**而非逐注重算（降低噪声、便于复盘对账），这是 RebelBetting 社区与 flat staking 讨论的通行做法（[RebelBetting 社区：Is flat staking good for value betting?](https://community.rebelbetting.com/t/is-flat-staking-good-for-value-betting/6692)：单注保持在银行的 1–2% 以内；[Bankroll management 指南](https://www.rebelbetting.com/blog/how-to-best-manage-your-betting-bankroll)）。

### 1.7 Flat vs Kelly 实证对比（汇总）

| 来源 | 结论 |
|---|---|
| Buchdahl（football-data 博客，§1.2） | 同 EV 下 Kelly 亏概率 34% vs 平注 18%；edge 高估时 Kelly 灾难化；没把握用分数 Kelly |
| RebelBetting《[Kelly vs flat staking](https://www.rebelbetting.com/blog/kelly-staking-strategy-vs-flat-staking-which-is-the-best)》 | 理论上 Kelly 更优（按 edge 调节注额），但波动大；flat 结果更稳、更易坚持 |
| RebelBetting 社区（[flat-staking-or-kelly](https://community.rebelbetting.com/t/flat-staking-or-kelly-flat-better-yield/8086)） | 部分用户实测 flat 的 yield 反而更高（Kelly 把更多钱押在高估 edge 的大注上） |
| Carta & Conversano 2020 | 估计误差下 half Kelly 最稳；over-betting 致命 |
| Reddit r/algobetting [回测贴](https://www.reddit.com/r/algobetting/comments/1rp5654/i_backtested_martingale_vs_flat_vs_kelly_025/) | 1,506 注回测：0.25 Kelly 与 flat 的增长差距远小于回撤差距 |

**综合结论**：在 edge 估计不可靠（个人模型的常态）+ 竞彩高 takeout 环境下，**「1/4 Kelly + 1% 硬上限」是增长/回撤/容错三者的实用最优**；同时把 flat 1% 作为并行对照策略记录在案（同一注单上模拟两种口径），用于隔离「注额算法」与「选注能力」两个变量。

---

## 2. 2串1（parlay）数学

### 2.1 EV 与方差

两腿十进制赔率 O₁、O₂，真实概率 p₁、p₂，独立：

- 串关赔率 **O_p = O₁·O₂**；联合概率 **P = p₁·p₂**；
- **EV_p = P·O_p − 1 = (1+e₁)(1+e₂) − 1**，其中 eᵢ = pᵢ·Oᵢ − 1（单位 EV）——**串关 EV 是各腿「1+EV」的乘积**（[OddsShopper: Parlay Betting Explained](https://www.oddsshopper.com/articles/betting-101/parlay-betting-explained)：edge 几何级数复合）；
- 单位本金方差：**σ² = P·(O_p−1)² + (1−P)·1 − EV_p²**（推导，二项支付展开）。

**算例（推导，竞彩典型数字）**：腿1 O=2.10, p=0.52（edge +9.2%，σ=1.05）；腿2 O=1.90, p=0.50（edge −5.0%）。2串1：O_p=3.99，P=0.26，EV_p = 1.092×0.95−1 = **+3.7%**，σ_p = **1.75**。EV/σ（增长效率代理）：单腿 0.088 → 串关 0.021，**效率掉到约 1/4**——这就是「两腿 EV 一正一负被迫串关」的量化代价：EV 近似相加衰减，方差近平方放大。Kelly 自动体现这一点：full Kelly = 3.7%/2.99 = 1.25%，1/4 Kelly 仅 **0.31% 银行**。

### 2.2 相关腿

- P_joint = Π pᵢ 仅在独立时成立；正相关（同场腿）P_joint 可比乘积高 30–50%（Wizard of Odds，§1.4）——**把独立公式用到相关腿上会系统性低估命中率、错杀正 EV 串关**，反之高估负相关。业界标准做法：Gaussian copula 或历史可比场次频率估计联合概率（同 §1.4 来源）。
- 竞彩规则禁止同场串关（§1.4），本系统 v1 无需实现 copula；「相关性矩阵」需求降级为：**不同 2串1 之间共享同一腿**的情形（多笔同时未结算注的 Kelly 风险预算问题，走 §1.3 的 open-bets 扣减即可）。

### 2.3 竞彩强制 2串1 的量化分析

**规则事实**（[新浪竞彩足球规则页](https://sports.sina.com.cn/l/rule/jczq/)；体彩官方 FAQ；知乎《[为什么竞彩不敢全面放开单关](https://zhuanlan.zhihu.com/p/19...)》）：

- 竞彩以过关为基本玩法，**2串1 是最低过关单位**，单关仅对部分指定场次开放（2019 年起逐步扩大单关覆盖，单场固定奖返奖率提升到 73%，雪球/媒体口径）；
- 过关奖金 = 各场赔率连乘；比赛无效则退还该场、组合降级；
- 官方奖金池口径 = 销售额的 **73%**（2014-09 从 69% 上调；发行费+公益金合计 27%——见 03 号研究笔记 §0 核对：人民网 2014-09 报道 + 新浪规则页）。**注意口径**：73% 是全玩法综合实付率；知乎理想化模型按单场固定奖 ~90% 返奖率推：**单关 90% → 2串1 81% → 3串1 72.9%**，返奖率按腿数几何递减（每多串一场多抽一次水）。真实单场抽水随场次浮动，公开分析多在 7%–15% 区间（二手估计，未见官方逐场披露）。

**「选两场独立正 EV 腿」**：若两腿各自 edge > 0，则 (1+e₁)(1+e₂) > 1，串关自动正 EV——EV 维度没有额外惩罚，惩罚全在**方差/命中率和增长效率**上（§2.1 算例：两腿 +9.2%/+9.2% → EV +18.8%，但 σ 从 1.05 涨到 ~2.2，命中率 27%）。Kelly 视角下这完全可下，只是注额按 1.25% 级别自动减码。

**「接受负 EV 腿的代价」**：设强腿 e₁、被迫配对腿 e₂ < 0：

- 串关 EV = (1+e₁)(1+e₂) − 1 > 0 ⟺ **e₂ > −e₁/(1+e₁)**（推导）。例：e₁=+9.2% 时容忍 e₂ > −8.4%；e₂=−5% 仍剩 +3.7% EV，e₂=−10% 则串关 −0.8% 应弃注。
- **弃注 EV=0 永远优于负 EV 注**——强制串关≠强制下注；正确策略是：`Π(1+eᵢ) − 1 ≥ τ` 才下（建议 τ = 1–2%，覆盖估计误差），否则把该轮预算留空；
- 配对策略上，**e₂ 取「可选第二腿中 EV 最高者」**（等价最大化乘积），而不是分散选「稳」的腿（低 odds 腿的 e₂ 往往被竞彩高水吃掉更多）；
- 结构性优先级：**有单关资格的正 EV 场次 > 正 EV 2串1 > 不下**（单关返奖率高一个 m 的量级：每串一场，有效 takeout 从 m 变 2m−m²，约翻倍）。

---

## 3. 业界系统功能解剖

### 3.1 OddsJam（oddsjam.com）

- **+EV Finder**：跨 40+ 书商实时扫赔率，与「去水公平赔率」（fair line，由 sharp 书商如 Pinnacle 的市场价去 vig 得出）对比，列出现价高于公平价的注，按 EV% 排序，含最小 edge、市场类型、联赛、开赛时间等过滤（官方页：[oddsjam.com/betting-tools/positive-ev](https://oddsjam.com/betting-tools/positive-ev)）。
- **Bet Tracker + CLV**：自动记录盈亏并**自动按收盘线（以「世界上最锐的书商」即 Pinnacle 收盘为基准）计算每注 CLV**；官方教程（[getting-started-guide/7](https://oddsjam.com/getting-started-guide/7)）；官方教育页称 CLV 是「决定长期盈利与否的最重要因素」（[betting-education/importance-of-closing-line-value](https://oddsjam.com/betting-education/importance-of-closing-line-value)）。
- 其余：低 hold（低抽水）扫描、line movement、middles/parity 工具。
- 社区基准：EV 玩家自检标准是「**真实 beat CLV 比例 ≥ 70%**」（r/EVbetting 讨论，二手）。

### 3.2 RebelBetting（rebelbetting.com）

value betting 闭环（[官方 value betting guide](https://www.rebelbetting.com/en-us/valuebetting/value-betting-guide)，2026-09-12 核实）：

1. **发现**：扫描书商赔率 vs sharp 书商赔率折算的公平价，列出 +EV 候选；
2. **决策**：推荐注额 = 分数 Kelly（**默认 30%**）+ 单注上限 + adjust for open bets（§1.3）；预期长期 **yield > 3%**（官方口径），另有 paper trading（虚拟金）模式供零风险验证；
3. **记录**：Bet Tracker 自动判定输赢、更新银行曲线；
4. **复盘**：按市场/联赛维度统计；特别标记 **"no longer value"**（下注后赔率下跌=市场确认你对了，属正向信号）；
5. **验证**：官方方法论认为「收盘线是最锐的预测」，CLV 一致性是核心自检指标。

官方对回撤的量化提示见其 drawdown/风险文章；社区通行「单注 ≤ 银行 1–2%」。

### 3.3 Betaminic（betaminator 品牌，betaminic.com）

- 定位：**历史赔率回测 + 策略市场**。140,000+ 场历史足球数据上按 odds 区间、统计指标、市场类型筛选，回测规则策略（[Betamin Builder](https://www.betaminic.com/betamin-builder/)；数据规模经 [BF Bot Manager 集成文档](https://www.bfbotmanager.com/en/help/knowledge_base/article/betaminic-integration)核实）；
- 功能闭环：构建策略 → 按 ROI/样本量等统计排序浏览公开策略 → 订阅接收 email picks；免费档可测不能存，付费 €99/月解锁保存与推送（YouTube 官方课程口径）；
- **教训（要抄的警告，不是要抄的功能）**：对历史数据挖出的策略普遍面临**数据挖掘偏差/过拟合**——WinnerOdds 的[策略设计文章](https://winnerodds.com/advanced-betting-strategy/)强调须 blind backtest（样本外验证）+ p 值检验；Betaminic 官方指南也要求大样本与多市场稳健性。个人系统做策略验证时必须内置样本外分割。

### 3.4 Bet tracker 类（Pikkit / Smart Bet Tracker / Betstamp 等）

- **Pikkit**（[pikkit.com/bet-tracker](https://pikkit.com/bet-tracker)、[BookSync](https://pikkit.com/booksync)）：**BookSync 自动同步 30+ 书商账户的全部注单**（消灭手工录入这个最大流失点），自动算 ROI/CLV/命中率，**CLV 按运动、玩法、书商分维度下钻**（[8 things to look for in a bet tracker](https://pikkit.com/blog/8-things-to-look-for-in-a-bet-tracker-for-sports)）；官方建议：**300+ 注起看 ROI（500 更佳）**；**200+ 注内以 CLV 为主**（beat 收盘线 60–65% 即提示持续找到价值，[CLV tracking 指南](https://pikkit.com/blog/how-to-track-closing-line-value-clv-in-sports-betting)）；
- 同类：SlipSync/Betstamp（截图识别）、OddsJam Bet Tracker、Bet Hero（免费 CLV 计算器）——功能同质：自动录入、ROI、CLV、分维度报表。

### 3.5 开源项目

- [bene-art/bet-tracker](https://github.com/bene-art/bet-tracker)：定位即「下注 → 记收盘线 → 结算 → 回答『我的模型校准了吗？我在 beat 收盘线吗？』」——与个人系统问题定义几乎一致，可参考其数据模型；
- [WFord26/BetTrack](https://github.com/WFord26/BetTrack)：双端投注记录+分析；
- [GitHub topic: sports-odds](https://github.com/topics/sports-odds)：含 no-vig/EV 计算、line movement、CLV 分析的 Jupyter notebook 集合，适合抄公式实现；
- 数据标准：**football-data.co.uk 历史开盘/收盘赔率 CSV**（含 Pinnacle 收盘与 Betfair）——学界与业界做 CLV/市场效率研究的事实标准数据源（03 号研究笔记 §2 同此结论）。

### 3.6 功能闭环对照表

| 闭环环节 | OddsJam | RebelBetting | Betaminic | Pikkit 类 | 个人系统应抄 |
|---|---|---|---|---|---|
| 发现 | +EV finder（fair line 对比+过滤） | 同左 | — | line shopping | EV 扫描 + 最小 edge/联赛/市场过滤 |
| 决策 | — | 分数 Kelly 30% + 上限 + open-bets 扣减 | — | — | 1/4 Kelly + 1% cap |
| 记录 | Bet Tracker | Bet Tracker 自动判定 | — | **自动同步/识别录入** + 收盘线字段 | 结构化注单（含收盘价）|
| 复盘 | CLV 逐注 | 分市场统计 + no-longer-value 标记 | ROI 排序 | CLV/ROI 按维度下钻 | yield/CLV/命中率 vs 期望 |
| 验证 | CLV > 70% 自检 | paper trading 虚拟金 | 回测+策略市场（过拟合风险） | 300/200 注阈值 | t 检验 + 蒙特卡洛 + 样本外 |

---

## 4. 记录与复盘指标

### 4.1 最小指标集（定义 + 公式 + 基准）

| 指标 | 定义 | 基准/解读 |
|---|---|---|
| Turnover | Σ 注额 | 分母口径 |
| Profit | Σ (结算返还 − 注额) | |
| **Yield** | Profit / Turnover | 投注语境的「ROI」即此；严肃玩家 2–5% 已优秀（RebelBetting 官方口径 yield > 3% 可长期存活；Reddit 长期讨论 5–6% 属少数） |
| 击中率 vs **期望击中率** | 实际 Σ命中 / n 对比 Σ p̂ᵢ（p̂ 取收盘线去水概率） | 期望击中率来自市场而非自己模型，避免自证 |
| **CLV%（均值）** | mean(O_taken / O_close_novig − 1) | 正值 = 一致性 beat 市场；这是**领先指标**（几十注就有信号）vs 盈利滞后指标（上千注） |
| **CLV beat rate** | beat 收盘线的注数占比 | 55–65% 好（Pikkit：200+ 注 60–65% 即提示持续找到价值）；70%+ 强（r/EVbetting/OddsJam 社区口径） |
| 平均赔率 / 注额离散 | mean(O)、σ(stake/plan) | 控制口径漂移；实际注额 vs 计划注额偏差是纪律指标 |
| **最大回撤 MDD** | max(peak − trough) | 必须与「同参数蒙特卡洛的期望 MDD」对比才可解读 |
| 单注 σ | 历史每注收益率标准差（典型 ~1.0–1.15） | 显著性计算输入 |

口径注意（[Unabated: Getting precise about CLV](https://unabated.com/post/getting-precise-about-closing-line-value)）：CLV 计算必须**用去水后的公平收盘价**，否则会被 ~4.5% vig 系统性误导。

### 4.2 统计显著性与样本量

**t 检验**（每注利润均值≠0 的单边 t）：t = yield·√n / σ_unit。要求 t ≥ 1.645（单边 5%）反解：

  **n ≈ (1.645·σ / yield)²**（推导，σ 为单注利润标准差）

| yield | σ=1.05（均价 ~2.0）所需 n |
|---|---|
| 5% | ~1,190 |
| 3% | ~3,305 |
| 2% | ~7,440 |

与业界基准一致：SportsInsights 经典结论「胜率声称 >57% 需 **~2,000 场**才能显著」（[sportsinsights 统计显著性](https://www.sportsinsights.com/sports-investing-statistical-significance/)）；Pinnacle 教育频道强调小 edge 需要数千注（[sample size needed to evaluate betting trends](https://www.pinnacle.com/betting-resources/en/educational/sample-size-needed-to-evaluate-betting-trends)）；实践共识 **300–500 注 ROI 才方向性可信、1,000–2,000+ 才能下「是能力」结论**（[Pikkit](https://pikkit.com/blog/how-to-track-sports-betting-roi)、[BeHero](https://betherosports.com/blog/roi-sports-betting)）。

**更快的方法（推荐主打）**：CLV。OddsShopper《[CLV vs variance](https://www.oddsshopper.com/articles/betting-101/clv-vs-variance)》：小样本下 P&L「全在说谎」，CLV 才分离运气与能力；Pikkit 口径 200+ 注的 beat rate 即有判读意义。验证手段组合：逐注 CLV 均值 t 检验 + 命中数对「期望命中数」的二项检验 + 银行曲线蒙特卡洛（Buchdahl《Monte Carlo or Bust》方法论的公开化）。

**回撤的期望值**（Pinnacle《[What are drawdowns and how do you manage them?](https://www.pinnacle.com/betting-resources/en/educational/what-are-drawdowns-and-how-do-you-manage-them/5wr2bjk223g5zfsc)》）：53% 命中率（≈6% yield）玩家 1,000 注的期望最大回撤约 **22 个单位**（即 1% 平注下 ~22% 银行回撤是**正常**）——熔断线必须设在自己的模拟期望 MDD 之上，否则会频繁误触发。

---

## 5. 纪律机制（严肃个人玩家实践）

| 机制 | 常见实践 | 来源 | 备注 |
|---|---|---|---|
| 单日止损 | 日亏 ≥ 银行 3–5%（或 N 注连败）当日停 | [PnL Ledger: daily loss limits & weekly max drawdown rules](https://www.pnlledger.com/daily-loss-limits-weekly-max-drawdown-rules/)（"hard stop"） | 作用是防 tilt/追损，**不改变 EV**——正 EV 系统多下本不多亏，但情绪化下的注会偏离系统 |
| 周回撤上限 | 周内回撤 ≥ 7–10% 降注或停 | 同上 | 与凯利口径衔接：周 DD 超过模拟分位数即异常 |
| 冷却期 | 分级冷却（触发 → 15/30/60 分钟；连败/触线 → 24h；情绪强触发 → 数天） | [SignalShield: trading cooldown rules](https://www.signalshieldhq.com/learn/trading-cooldown-rules)（交易侧模板，逻辑同构） | 博彩侧建议：触发止损后 **24h 起步**，强制断开 app/通知 |
| **回撤熔断** | DD ≥ 15% → 注额减半；DD ≥ 20–25% → 全面暂停 1–2 周做复盘 | 实践综合（PnL Ledger、Betaminic drawdown 文章、[bet2invest MDD 指南](https://bet2invest.com/blog/Maximum-Drawdown:-The-Ultimate-Guide-to-Measuring-Risk-in-Sports-Betting)） | 阈值必须 > 自己策略的模拟期望 MDD（§4.2：6% yield 策略 22 单位 DD 属正常），否则误触发 |
| 止盈线 | **不设**为通行实践 | 综合上述来源 | 盈利不改变逐注 EV，止盈只是心理舒适；止盈导致的「收手」机会成本 = 放弃的正 EV 注 |
| 限额不对称生效 | 调低限额即时生效、调高需等待冷却 | [OddsIndex: responsible gambling tools](https://oddsindex.com/guides/responsible-gambling-tools)（RG 工具设计） | 值得抄到个人系统：任何「放宽」参数的操作延迟生效 |
| 反 progression | 禁 martingale/倍投 | [danny.bet: flat vs progressive](https://danny.bet/flat-betting-vs-progressive-staking/)；所有严肃来源一致 | 负 EV 下倍投必毁；正 EV 下也无增益 |
| 银行隔离 | 专用银行账户/额度，与生活资金分开 | RebelBetting bankroll 指南（"fuel" 比喻） | bankroll 定义清晰的先决条件 |

---

## 6. 结论（决策就绪）

1. **注额算法：1/4 Kelly + 硬上限 1% + 有效银行扣减未结算风险**。f\* = (p·O − 1)/(O − 1)，下注额 = min(0.25·f\*·bankroll_effective, 1%·bankroll_effective, 单注绝对上限)；bankroll_effective = bankroll − Σ 未结算注风险金。串关按「一笔注」用联合概率入公式。flat 0.5–1% 作为并行对照口径记录。依据：§1.2 估计误差文献 + §1.3 RebelBetting 实践 + §1.7 对比表。
2. **串关策略**：只在 Π(1+eᵢ) − 1 ≥ 1–2%（τ）时下 2串1；第二腿取可选集中 EV 最高者；e₂ > −e₁/(1+e₁) 是数学下限；有单关资格的正 EV 场次优先于串关；3串+ 不做（返奖率几何递减）。依据 §2。
3. **系统功能**：抄「发现（EV 扫描+过滤）→ 决策（分数 Kelly+上限+open-bets）→ 记录（含收盘线的结构化注单）→ 复盘（yield/CLV 分维度下钻）→ 验证（t 检验+蒙特卡洛+样本外）」五段闭环；自动录入与收盘线采集是 bet tracker 类产品的核心价值；不抄策略市场/跟单/SGP。依据 §3。
4. **最小指标集**：turnover、profit、yield、击中率 vs 期望击中率（收盘线基准）、CLV 均值 + beat rate、平均赔率、MDD vs 模拟期望 MDD、单注 σ。显著性：n ≈ (1.645σ/yield)²（5% yield→~1,200 注；3%→~3,300 注）；CLV 200+ 注即可判读。依据 §4。
5. **纪律**：单注硬上限 1%；日止损 3–5% 银行；周回撤 7–10%；DD≥15% 减半注额、DD≥20–25% 熔断停 1–2 周（阈值须高于模拟期望 MDD）；止损后冷却 ≥24h；不设止盈；禁 progression；放宽参数的操作延迟生效。依据 §5。

## 引用总表（一手来源）

- Kelly (1956), "A New Interpretation of Information Rate", *Bell System Technical Journal* 28(4)（经 Wikipedia 转引）
- Thorp, E.O. (1997/2006), "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market"（经 [Wikipedia: Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion) 转引，含页码）
- MacLean, Thorp & Ziemba (2010/2011), *The Kelly Capital Growth Investment Criterion*, World Scientific（[RePEc](https://econpapers.repec.org/RePEc:wsi:wsbook:7598)、[书评](https://www.tandfonline.com/doi/full/10.1080/14697688.2011.619561)）
- Carta, A. & Conversano, C. (2020), *Front. Appl. Math. Stat.* 6:577050, [DOI](https://doi.org/10.3389/fams.2020.577050)
- Busseti, Ryu & Boyd (2016), "Risk-Constrained Kelly Gambling", *J. Investing* 25(3), [stanford.edu/~boyd/papers/kelly.html](https://stanford.edu/~boyd/papers/kelly.html)、[arXiv:1603.06183](https://arxiv.org/abs/1603.06183)
- Whitrow, D.J. (2007), *Statistics and Computing* 17:217–226（精确引用经 [Vegapit](https://vegapit.com/article/numerically_solve_kelly_criterion_multiple_simultaneous_bets/) 核实）
- Buchdahl, J., [The Pitfalls of using Kelly Staking in Sports Betting](https://www.football-data.co.uk/blog/kelly_staking.php), football-data.co.uk
- Wizard of Odds, [Same-Game Parlays: The Mathematics of Correlation](https://wizardofodds.com/article/same-game-parlays-the-mathematics-of-correlation/)
- 新浪竞技彩，[中国体育彩票竞彩足球游戏规则](https://sports.sina.com.cn/l/rule/jczq/)；体彩网官方 FAQ（过关/单关/同场不可串）；知乎，[为什么竞彩不敢全面放开"单关"](https://zhuanlan.zhihu.com/p/19...)（URL 截断，经搜索摘要核实）
- OddsJam：[+EV](https://oddsjam.com/betting-tools/positive-ev)、[Bet Tracker](https://oddsjam.com/bet-tracker)、[Getting Started L7](https://oddsjam.com/getting-started-guide/7)、[CLV 教育页](https://oddsjam.com/betting-education/importance-of-closing-line-value)
- RebelBetting：[Value Betting Guide](https://www.rebelbetting.com/en-us/valuebetting/value-betting-guide)、[Kelly vs flat](https://www.rebelbetting.com/blog/kelly-staking-strategy-vs-flat-staking-which-is-the-best)、[Bankroll Management](https://www.rebelbetting.com/blog/how-to-best-manage-your-betting-bankroll)、社区帖 [6692](https://community.rebelbetting.com/t/is-flat-staking-good-for-value-betting/6692)/[8086](https://community.rebelbetting.com/t/flat-staking-or-kelly-flat-better-yield/8086)
- Betaminic：[Betamin Builder](https://www.betaminic.com/betamin-builder/)、[BF Bot Manager 集成文档](https://www.bfbotmanager.com/en/help/knowledge_base/article/betaminic-integration)、[drawdown 文章](https://www.betaminic.com/betting-guide/the-importance-of-drawdown-in-sports-betting/)；WinnerOdds [策略设计](https://winnerodds.com/advanced-betting-strategy/)
- Pikkit：[Bet Tracker](https://pikkit.com/bet-tracker)、[BookSync](https://pikkit.com/booksync)、[ROI 指南](https://pikkit.com/blog/how-to-track-sports-betting-roi)、[CLV 指南](https://pikkit.com/blog/how-to-track-closing-line-value-clv-in-sports-betting)、[8 things](https://pikkit.com/blog/8-things-to-look-for-in-a-bet-tracker-for-sports)
- Pinnacle Betting Resources：[Kelly](https://www.pinnacle.com/betting-resources/en/educational/the-kelly-criterion-what-bettors-need-to-know/xqc24kxa968utw84)、[Drawdowns](https://www.pinnacle.com/betting-resources/en/educational/what-are-drawdowns-and-how-do-you-manage-them/5wr2bjk223g5zfsc)、[Sample size](https://www.pinnacle.com/betting-resources/en/educational/sample-size-needed-to-evaluate-betting-trends)（页面 451 时以搜索摘要核实）
- SportsInsights，[Statistical Significance in Sports Investing](https://www.sportsinsights.com/sports-investing-statistical-significance/)；Unabated，[Getting precise about CLV](https://unabated.com/post/getting-precise-about-closing-line-value)；OddsShopper，[CLV vs variance](https://www.oddsshopper.com/articles/betting-101/clv-vs-variance)、[Parlay math](https://www.oddsshopper.com/articles/betting-101/parlay-betting-explained)
- 纪律：[PnL Ledger](https://www.pnlledger.com/daily-loss-limits-weekly-max-drawdown-rules/)、[SignalShield](https://www.signalshieldhq.com/learn/trading-cooldown-rules)、[OddsIndex RG](https://oddsindex.com/guides/responsible-gambling-tools)、[bet2invest](https://bet2invest.com/blog/Maximum-Drawdown:-The-Ultimate-Guide-to-Measuring-Risk-in-Sports-Betting)、[danny.bet](https://danny.bet/flat-betting-vs-progressive-staking/)
- 开源：[bene-art/bet-tracker](https://github.com/bene-art/bet-tracker)、[WFord26/BetTrack](https://github.com/WFord26/BetTrack)、[topic: sports-odds](https://github.com/topics/sports-odds)
