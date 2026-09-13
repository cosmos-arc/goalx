# 03 预测与奖池建模研究（调研笔记）

- 票：`../issues/03-prediction-and-pool-modeling.md`
- 日期：2026-09-12
- 方法：所有论断尽量追到一手来源（论文 DOI/arXiv、GitHub/PyPI API、官方规则页）；库的活跃度与许可证经 GitHub/PyPI API 于 2026-09-12 核实。

---

## 0. 前提事实核对（重要修正）

票面写「返奖率 ~73% 下任9/14」，经核对官方规则：

- **竞彩足球**（胜平负/让球/比分/半全场/总进球，固定奖金+浮动）：2014-09 起返奖率从 69% 提高到 **73%**。来源：人民网报道 http://caipiao.people.com.cn/n/2014/0910/c373276-25632779.html 、新浪规则页 https://sports.sina.com.cn/l/rule/jczq/ （旧规则 69% = 68% 返奖 + 1% 调节基金）。
- **传统足彩**（胜负彩 14 场、任选 9 场、6 场半全场、4 场进球，全部 parimutuel 乐透型）：规则第十条为「返奖奖金为销售总额的 **65%**（当期 64% + 调节基金 1%）」。来源：新浪胜负彩规则 https://sports.sina.com.cn/l/rule/sfc/ 、任选九规则 https://sports.sina.com.cn/l/rule/r9/ 。

**结论**：任9/14 与 4 场进球的真实 takeout 是 **35%**，不是 27%。这直接收紧了奖池玩法 +EV 的空间（见 §4）。

---

## 1. 进球/比分模型：精度对比与玩法概率推导

### 1.1 核心模型谱系（按证据强度排序）

1. **Dixon-Coles (DC)**：Dixon & Coles (1997), "Modelling Association Football Scores and Inefficiencies in the Football Betting Market", *JRSS-C (Applied Statistics)* 46(2) 265–280, DOI [10.1111/1467-9876.00065](https://rss.onlinelibrary.wiley.com/doi/abs/10.1111/1467-9876.00065)。独立 Poisson × 低比分相关性校正（ρ 修正 0-0/1-0/0-1/1-1）+ **时间衰减加权极大似然**（论文原文用指数衰减权重，半衰期量级 ≈ 1.5 个赛季；社区常用 ξ 对应半周期 1–2 年）。这是 25 年来事实上的行业标准。
2. **双变量 Poisson（BVP）**：Karlis & Ntzoufras (2003), "Analysis of sports data by using bivariate Poisson models", *JRSS-D (The Statistician)* 52(3) 381–393, DOI [10.1111/1467-9884.00366](https://rss.onlinelibrary.wiley.com/doi/abs/10.1111/1467-9884.00366)。λ3（协方差项）显式建模两队进球相关；结论：BVP 改善拟合与**平局预测**（独立 Poisson 系统性低估平局），可再加 diagonal inflation。BVP 与 DC 校正解决同一问题，实证精度同档。
3. **模型族横评（最常被引）**：Ley, Van de Wiele & Van Eetvelde (2019), "Ranking soccer teams on the basis of their current strength: A comparison of maximum likelihood approaches", *Statistical Modelling* 19(1) 55–73, [arXiv:1705.09575](https://arxiv.org/abs/1705.09575)、[DOI 10.1177/1471082X18817650](https://journals.sagepub.com/doi/abs/10.1177/1471082X18817650)。十个强度模型横评：**时间加权的 DC 类模型综合最优**，Elo 型作为单变量方法意外地有竞争力。
4. **ELO 路线**：Hvattum & Arntzen (2010), "Using ELO ratings for match result prediction in association football", *International Journal of Forecasting* 26(3) 460–470, [DOI 10.1016/j.j.ijforecast.2009.10.002 类](https://www.sciencedirect.com/science/article/abs/pii/S0169207009001708)。Elo 差是有用的预测变量，单独做生成模型不够（无比分结构），**做特征/冷启动很好**。Lasek, Szlávik & Bhulai (2013), "The predictive power of ranking systems in association football", *IJAPR* 1(1) 27–46, [DOI 10.1504/IJAPR.2013.052339](https://www.inderscienceonline.com/doi/abs/10.1504/IJAPR.2013.052339)：Elo 类优于 FIFA 排名，但仍逊于博彩赔率。clubelo.com 提供免费 API（CSV，日更），已被多项研究用作数据源。
5. **xG 驱动**：用 expected goals 替代实际进球估计 λ，降低运气噪声（射门质量信息领先于比分）。代表文献：socceraction/VAEP 生态（Decroos et al., 见 §2）。实证共识：xG 特征能小幅改善纯比分模型，但需要事件级数据源。
6. **梯度提升（GBM/XGBoost）**：Hubáček, Šourek & Železný (2019), "Learning to predict soccer results from relational data with gradient boosted trees", *Machine Learning* 108(1) 29–49, [DOI 10.1007/s10994-018-5704-6](https://link.springer.com/article/10.1007/s10994-018-5704-6)。用 pi-rating、近期表现等特征 + XGBoost，在捷克聯赛数据上 RPS ≈ 0.206、accuracy ≈ 52%；同组 companion 论文（"Exploiting sports-betting market using machine learning", *International Journal of Forecasting* 2019）显示加入赔率特征后才能稳定跑赢基线。混合随机森林：Groll et al. (2019) "A hybrid random forest to predict soccer matches in international tournaments"。
7. **深度学习**：Bunker, Yeung & Fujii (2024) 综述 "Machine Learning for Soccer Match Result Prediction", [arXiv:2403.07669](https://arxiv.org/abs/2403.07669)。结论（与 2017 Soccer Prediction Challenge 一致）：**深度模型相对结构化统计模型无稳定优势**，收益主要来自数据/特征而非网络结构；赔率基线极难过。

### 1.2 精度对比的总体结论（文献共识）

- 博彩赔率（尤其收盘价）是极强的基准，**最好的统计模型 ≈ 收盘赔率去晦后的精度，难以稳定超越**：Berrar, Lopes & Dubitzky (2019), "Incorporating domain knowledge in machine learning for soccer outcome prediction", *Machine Learning* 108(1)（特刊 editorial: [DOI 10.1007/s10994-018-5763-8](https://link.springer.com/article/10.1007/s10994-018-5763-8)）。
- 因此正确姿势是「**生成模型（比分结构）+ 判别校正（市场信息）**」的融合，而不是单挑市场。注：票面提到的 "Brighton & Fenton hybrid" 未检索到该作者组合，实际文献线是 **Constantinou & Fenton**：pi-football 贝叶斯网络（Constantinou, Fenton & Neil 2012, *Knowledge-Based Systems* 36:322–339, [DOI 10.1016/j.knosys.2012.07.001](https://www.sciencedirect.com/science/article/abs/pii/S0950705112001967)）、Dolores 部分可观测模型（Constantinou 2019, *Machine Learning*, [DOI 10.1007/s10994-018-5703-7](https://link.springer.com/article/10.1007/s10994-018-5703-7)）、以及直接面向亚盘的 "Asian Handicap football betting with Rating-based Hybrid Bayesian Networks"（Constantinou 2020, arXiv:2001.03342 类）。

### 1.3 从比分矩阵推导各玩法概率（标准做法）

设去相关后的比分矩阵 P(h,a)，h,a = 0..N（N≥10），尾部截断后归一化：

| 玩法 | 推导路径 |
|---|---|
| 胜平负 1X2 | Σ sign(h−a) 聚合三格 |
| 让球（竞彩为整数让球线 L 的三向盘，无 push） | P(主让 L) = P(h−a+L > 0)、P(=0)、P(<0) 对矩阵求和；亚盘（双向+push）只是同一 map 的两向版本，见 Constantinou (2020) |
| 总进球（竞彩为精确总进球 0..7+ 共 8 档） | P(h+a=k) 对反对角线求和，k=7+ 归并尾部 |
| 比分（含 胜其他/平其他/负其他） | 单格 P(h,a)；「其他」= 三向尾部格求和 |
| 半全场 HT/FT | 需要**上半场分布**：v1 用比例拆分 λ_1H = λ·k（k≈0.44–0.46，文献一致结论是进球率随比赛时间上升，下半场更密）；v2 用时间非齐次模型。一手来源：Dixon & Robinson (1998), "A birth process model for association football matches", *The Statistician* 47(3) 523–538 —— 建模进球率的时间过程，正是 HT/FT 与分段时间市场的理论基础 |
| 任9/14、4场进球 | 单场边际从上面取（14 场用 1X2 三值分布；4 场进球用 8 档总进球分布），跨场独立相乘得组合概率（条件独立性是文献默认近似，BVP 的 λ3 不跨场） |

---

## 2. 开源可复用库（核实于 2026-09-12）

| 库 | 语言/许可证 | 活跃度（核实值） | 定位与集成成本 |
|---|---|---|---|
| **penaltyblog** [github.com/martineastwood/penaltyblog](https://github.com/martineastwood/penaltyblog) 、[docs.pena.lt/y](https://docs.pena.lt/y/) | Python ≥3.10，**MIT** | 220★，最近 push **2026-09-10**；PyPI 1.12.1（2026-09-10 发布），近 12 个月约月度发版 | **首选主力**。数据抓取（football-data、FBref 等）、ratings（Elo、Pi、Colley、Massey）、models（Dixon-Coles 全套：拟合+时间衰减+预测）、metrics（RPS、Brier）。零跨语言成本，MIT 与本项目兼容。注：仓库已从 dashee87 迁到 martineastwood 维护 |
| **socceraction** [github.com/ML-KULeuven/socceraction](https://github.com/ML-KULeuven/socceraction) | Python，**MIT** | 812★，push 2026-01-07，38 open issues | v2 的 xG/VAEP 特征工厂：Decroos et al. (2019), "Actions Speak Louder than Goals: Valuing Player Actions in Soccer", KDD 2019（Best Applied Data Science Paper），[arXiv:1802.07127](https://arxiv.org/abs/1802.07127)。需要 Wyscout/Opta/StatsBomb 事件数据，集成成本中高（数据获取是瓶颈，不是代码） |
| **goalmodel** [github.com/opisthokonta/goalmodel](https://github.com/opisthokonta/goalmodel) | **R，GPL-3** | 115★，最后 push **2024-03-30**（~2.5 年未更新），v0.6.4 | R 生态里最完整的进球模型参考实现（BVP、DC 校正、copula）。**GPL-3 + R + 停更 → 不建议集成**，作为算法对照文档用；功能在 penaltyblog 里已有 Python 对应 |
| **kickoff.ai** [kickoff.ai](https://kickoff.ai/) | 网络服务，无开源库 | 论文：Maystre, Kristof, González Ferrer & Grossglauser (2016), "The Player Kernel: Learning Team Strengths Based on Implicit Player Contributions", [arXiv:1609.01176](https://arxiv.org/abs/1609.01176)（EPFL） | 球员阵容 kernel + GP 分类，国家队场次的知识迁移。**无公开 API/代码，不作为依赖**，仅作 related work |
| 辅助数据源 | — | — | **clubelo.com**（免费 HTTP CSV API，Elo 日更）；**football-data.co.uk**（欧洲主要联赛历史开盘/收盘赔率 CSV，含 Pinnacle 收盘与 Betfair——市场基准与 CLV 代理线的事实标准数据源，上述论文几乎全用它）；odds API 类商业服务作实时补充 |

**结论**：v1 只需 penaltyblog + football-data.co.uk + clubelo；v2 增加 socceraction（或直接用 Understat/FBref xG 聚合，成本更低）。

---

## 3. 市场去晦（de-vig）与模型/市场融合

### 3.1 Shin vs 简单归一化

- 简单归一化（basic normalization）：p_i = (1/o_i) / Σ(1/o_j)，隐含 margin 均匀摊到各结果。
- **Shin 方法**：源于 Shin (1992/1993) 内部人交易模型（*Economic Journal* 102:1526–1536; 103:1141–1153），把 margin 解释为对知情交易的补偿，解一个 z（ insider 比例）方程。
- 实证对比：**Štrumbelj (2014), "On determining probability forecasts from betting odds", *International Journal of Forecasting* 30(4) 934–943, [DOI 10.1016/j.ijforecast.2013.09.003](https://www.sciencedirect.com/science/article/abs/pii/S0899390713001180)**：Shin 类方法（含 equilibrium 变体）**优于归一化与 power 方法**，尤其当 margin 不均匀（长赔被抽更重）时。三向盘 margin 通常不大（欧洲盘 2–6%），Shin 与归一化差异在第三位小数，但方向上 Shin 更准——实现只要 ~30 行，**直接用 Shin，默认无脑标配**。

### 3.2 模型/市场融合

- 学术共识：模型单独难以击败收盘价（Berrar et al. 2019；Ley et al. 2019 中 DC 模型也仅接近赔率），**融合是主要增益来源**。
- 实用融合谱系（由轻到重）：
  1. **log-odds 线性融合**（几何平均/log-linear pooling）：融合 p_fused ∝ p_model^w · p_market^(1−w)，w 用验证集调，对数域就是加权平均。最常用、最稳健。
  2. **以市场为先验的收缩**：模型 logit 向市场 logit 收缩（James-Stein 式）。
  3. **赔率作特征**：GBM 中加入收盘价（Hubáček et al. 2019 的 companion 论文显示这是从「不盈利」到「盈利」的转折点）。
  4. pi-football / Dolores 的混合贝叶斯网络线（Constantinou & Fenton 系），适合做规则化知识注入，工程成本高。
- 对本项目：**v1 就做 1（log-linear 融合）**，市场侧用欧洲收盘价 Shin 去晦。

---

## 4. 奖池型（任9/14）建模

### 4.1 parimutuel 机制与公众投注分布

- Parimutuel 下单注 EV = P(win combo) × (pool × (1−takeout) / shares(combo))。**+EV 不需要击败 65% takeout 的「平均」赔率，只需要自己组合的 share 比例低于其真实概率**。所以核心是估计**他人的投注分布**。
- **Favorite-longshot bias（FLB）**：赛马 parimutuel 的奠基文献 Thaler & Ziemba (1988), "Anomalies: Parimutuel Betting Markets: Racetracks and Lotteries", *Journal of Economic Perspectives* 2(2) 161–172；机制综述 Ottaviani & Sørensen (2008), "The Favorite-Longshot Bias: An Overview of the Main Explanations" (in *Handbook of Sports and Lottery Markets*, Elsevier)；Snowberg & Wolfers (2010), "Explaining the Favorite-Longshot Bias", *Economic Journal* 120(542) 23–35。足球固定盘证据：Cain, Law & Peel (2000), *Scottish Journal of Political Economy* 47(1)；**Deschamps & Gergaud (2007), "Efficiency in Betting Markets: Evidence from English Football", *Journal of Prediction Markets* 1(1) 61–73**——热门被低估注、平局被系统性低注。
- 英国足球池的历史与行为证据：Forrest (1999), "The Past and Future of British Football Pools", *Journal of Gambling Studies* 15:161–176；更早 "Observed Betting Tendencies and Suggested Betting Strategies for European Football Pools", *The Statistician* (1983)（经由 Flowerdew, Lancaster PhD 2015, *Methods for the Identification and Optimal Exploitation of Sports Betting* 综述）。
- **理论最优框架**：**Clair & Letscher (2007), "Optimal Strategies for Sports Betting Pools", *Operations Research* 55(6) 1163–1177**——把「估计对手分布 → 最大化份额条件 EV → 组合选择」形式化，是任9 策略模块的直接蓝本。相关：Terrell (1996) parimutuel 效率；Haugh 系（Columbia）parimutuel/DFS 信息劣势模型。

### 4.2 复式覆盖（covering designs）

- 「保证至少 k 场全对」的组合最小化 = **covering codes / covering designs**。三值（胜平负）半径 1 的覆盖码即经典 "football pools problem"：K_3(n,1)，见 OEIS [A004044](https://oeis.org/A004044)、Kéri 的 SZTAKI 界限表 https://old.sztaki.hu/~keri/codes/index.htm 、La Jolla Covering Repository（[ljcr.dmgordon.org](https://ljcr.dmgordon.org/cover/low_tab.html)，(v,k,t) 集合覆盖设计，SageMath 可直接查询）。
- 综述文献：Hämäläinen, Honkala, Litsyn & Östergård (1995), "Football pools — A game for mathematicians", *American Mathematical Monthly* 102:579–；专著 Cohen, Honkala, Litsyn & Lobstein (1997), *Covering Codes*, Elsevier。
- **但**：covering 保证的是「最坏情况不漏」，而奖池 EV 由「份额 vs 概率」驱动；对 65% takeout 的池，纯覆盖矩阵是**防守性工具**（控方差/保底中奖），**EV 最优化仍应走 Clair & Letscher 式的份额加权选择**（贪心 set-cover / 小规模 ILP 都够，任9 全空间 3^9=19683 个组合，ILP 完全可行）。
- 4 场进球复式同理：每场 8 档，组合空间 8^4=4096，穷举无压力。

### 4.3 65% 返奖率下存在 +EV 吗？

证据链（按强度）：

1. **机制上可行**：parimutuel 的 EV 取决于相对份额而非 takeout 本身；只要公众系统性偏离真实分布（FLB、平局低注、热门口味、号码偏好）， unpopular-but-true 组合可为 +EV。英国池有历史证据（§4.1）。
2. **滚存（rollover）是最大的结构性机会**：调节基金与奖池滚存会把当期有效返奖率推高，极端滚存期可超 100%（彩票经济学的标准结论，见 Forrest 系文献）。这应是系统显式监控的信号（滚存金额是公开数据）。
3. **但 65% takeout 比英国池历史上更苛刻**（英国池全盛期 takeout 更低、且当时存在统计套利文献证据），**对 35% 的池水，常态轮次 +EV 空间很窄**；现实预期是：常态轮次 ROI 在 −35% 附近，靠「份额优化 + 滚存轮加注」把长期 ROI 拉向 0 或略正。这应作为诚实的 v1 假设写进产品预期，而不是宣传 +EV。
4. 对**竞彩固定赔率玩法**（73% 返奖 = 27% margin）：文献共识是收盘价基准难过（§3.2），27 点 margin 下靠模型稳定 +EV 几乎不可能；唯一可行路径是**官方盘更新滞后者 vs 欧洲市场移动**的错价（Kaunitz et al. 2017 的「赔率滞后」思路，见 §5）。竞彩的真实定位：**低流动性玩法（比分/半全场/总进球）+ 官方盘反应慢**是相对更有机会的缝隙。

---

## 5. CLV 在竞彩语境的定义

- **标准定义**：CLV = 下注时赔率 / 收盘赔率 − 1（或概率差 p_take − p_close）。收盘线被反复验证为真实概率的最佳单点估计：**Kaunitz, Zhong & Kreinovich (2017), "Beating the bookies with their own numbers — and how the online sports betting market is rigged", [arXiv:1710.02824](https://arxiv.org/abs/1710.02824)**（10 年收盘赔率回测盈利；复现代码 [github.com/Lisandro79/BeatTheBookie](https://github.com/Lisandro79/BeatTheBookie)）；市场效率经典综述 Sauer (1998), "The Economics of Wagering Markets", *Journal of Economic Literature* 36:2021–2064。
- **竞彩语境的问题**：中国竞彩官方盘（1）非 sharp line：由官方定价、变动慢、不以平衡头寸为唯一目标；（2）margin 高达 27 点，直接拿官方收盘线算 CLV 会把 margin 误读为「每次都 −27% CLV」，信号被淹没。**不能直接套用**。
- **建议定义（代理线）**：用同场次同市场的**欧洲收盘价**（Pinnacle closing / Betfair closing，来自 football-data.co.uk 历史数据或实时 odds 聚合）做代理 sharp line：
  1. 对齐市场口径（让球线对亚盘做 map；总进球/比分在欧洲无同构市场时，退化为「仅对可映射玩法测 CLV」：胜平负、让球、O/U 类）；
  2. 代理收盘概率 p*_close = Shin(欧洲收盘价)；
  3. 定义 **CLV_proxy = p*_close − (1/o_竞彩买入价)（概率域）**，或对数域 log(o_take) − log(o_close)；
  4. 验证方式：CLV_proxy 与已结算实际收益做横截面回归，斜率显著为正才说明代理线在本语境有效（这是 Kaunitz 式检验的移植）。
- 报告口径：按玩法、联赛、下注距开赛时间分桶报告 CLV_proxy 分布；纸面期至少 1 个完整赛季。

---

## 6. 评估协议

- **排序意识指标优先**：**RPS（ranked probability score）**为 1X2 主指标——Constantinou & Fenton (2012), "Solving the Problem of Inadequate Scoring Rules for Assessing Probabilistic Football Forecast Models", *JQAS* 8(1)（[PDF](http://constantinou.info/downloads/papers/solvingtheproblem.pdf)）；Wheatcroft (2019), "Evaluating probabilistic forecasts of football matches", [arXiv:1908.08980](https://arxiv.org/abs/1908.08980) 也论证 RPS 优于 accuracy/log-loss。penaltyblog 已内置实现。
- **多分类 Brier / log loss**：比分、总进球、半全场等多类玩法用 multi-class Brier 与 log loss（后者对尾部概率敏感，作第二指标）。
- **校准（calibration）**：reliability diagram + ECE，按概率分桶；1X2 按结果分别校准（draw 桶单独看——最常失准处）。
- **对市场基准的 skill**：skill = 1 − L_model / L_market，L_market 用 **Shin 去晦欧洲收盘价** 的同指标。**这是唯一有意义的通过线**：模型必须先 ≈ 市场（skill ≥ 0）才谈融合与下注（§3.2 文献共识）。
- **统计检验**：RPS 差用配对 Diebold–Mariano 或移动 block bootstrap；按联赛/赛季分层报告；纸面验证期 ≥ 1 个完整赛季（≥1000 场）再下「模型 vs 市场」结论。
- **下注侧指标**：flat-stake ROI + t 统计量、Kelly 模拟的资金曲线、CLV_proxy 分布（§5）、按 margin 分桶的 EV 图。
- **奖池侧指标**：份额-概率比（share/probability ratio）分布、模拟池 EV（用估计的公众分布）、覆盖矩阵的保底中奖率与成本。

---

## 7. 推荐路线（v1 / v2）

### v1（基线，2–4 周可落地）

1. 依赖：`penaltyblog`（MIT，数据抓取 + DC 拟合 + RPS/Brier）+ football-data.co.uk 历史赔率 + clubelo API。
2. 模型：时间衰减 DC（ξ 半周期 ~ 1 年起步调参），10×10 比分矩阵，全部玩法从矩阵推导（§1.3 表）；HT 用 λ·0.45 比例拆分。
3. 市场侧：欧洲收盘价 Shin 去晦为基准；log-linear 融合（w∈[0,1] 验证集调）。
4. 评估：§6 协议全量跑通，报告对市场 skill。
5. 奖池：任9 组合 EV = 自有概率 × 模拟份额；份额模型 v1 用「热门厌恶 + 平局低注」文献先验构造，ILP 选组合；滚存监控。

### v2（进阶）

1. xG 驱动 λ（socceraction 或 Understat/FBref xG 聚合入 Poisson 回归）。
2. GBM 判别层（XGBoost/LightGBM：Elo、pi-rating、xG 差、赛程密度、伤停），输出与 DC 生成层 stacking（Hubáček et al. 2019 路线）。
3. 时间非齐次进球率（Dixon & Robinson 1998）改进 HT/FT 与分段时间市场。
4. 份额模型换成「官方销量/奖级分布」反演 + 公众行为特征（若有官方公开的中奖注数数据可校准）。
5. CLV_proxy 正式化（§5）与 Betfair 收盘对照。

---

## 附：关键引用清单

| 主题 | 引用 |
|---|---|
| DC | Dixon & Coles 1997, JRSS-C 46(2), DOI:10.1111/1467-9876.00065 |
| BVP | Karlis & Ntzoufras 2003, JRSS-D 52(3), DOI:10.1111/1467-9884.00366 |
| 时间过程 | Dixon & Robinson 1998, The Statistician 47(3) 523–538 |
| 模型横评 | Ley et al. 2019, Statistical Modelling, arXiv:1705.09575 |
| Elo | Hvattum & Arntzen 2010, IJF 26(3) 460–470; Lasek et al. 2013, IJAPR 1(1) |
| GBM | Hubáček et al. 2019, Machine Learning 108(1), DOI:10.1007/s10994-018-5704-6 |
| ML 综述 | Bunker et al. 2024, arXiv:2403.07669; Berrar et al. 2019, Machine Learning 108(1) |
| 去晦 | Štrumbelj 2014, IJF 30(4), DOI:10.1016/j.ijforecast.2013.09.003; Shin 1992/1993, Economic Journal |
| 融合/贝叶斯 | Constantinou & Fenton 系（pi-football KBS 2012; ML 2019; AH-RHBN 2020） |
| FLB/池 | Thaler & Ziemba 1988 JEP; Ottaviani & Sørensen 2008; Snowberg & Wolfers 2010 EJ; Deschamps & Gergaud 2007 JPM; Forrest 1999 JGS |
| 池策略 | Clair & Letscher 2007, Operations Research 55(6) 1163–1177 |
| 覆盖码 | Hämäläinen et al. 1995 AMM 102; OEIS A004044; Kéri SZTAKI; LJCR |
| CLV/效率 | Kaunitz et al. 2017 arXiv:1710.02824; Sauer 1998 JEL 36 |
| 评估 | Constantinou & Fenton 2012 JQAS 8(1); Wheatcroft 2019 arXiv:1908.08980 |
| 库 | penaltyblog (MIT, PyPI 1.12.1, 2026-09-10); socceraction (MIT, KDD 2019 / arXiv:1802.07127); goalmodel (R, GPL-3, 停更); kickoff.ai (arXiv:1609.01176, 无库) |
