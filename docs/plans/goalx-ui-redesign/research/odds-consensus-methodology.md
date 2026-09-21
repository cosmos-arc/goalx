# 赔率共识方法论（Odds Consensus Methodology）调研笔记

> 任务类型：research。日期：2026-09-17。
> 范围：为 goalx（竞彩量化投注系统，欧赔源为 The Odds API 多 book 三向价，共识 = 多 book 均价 + Shin 去水，CLV 基准拟用 Pinnacle/Betfair，投注对象为竞彩官方固定赔率胜平负为主）调研四个主题：业界共识方法论、主要博彩公司赔率特点、联赛覆盖差异、亚盘是否必要。
> 结论先行：**共识方法建议从"odds 空间简单均价 → 再 Shin"升级为"每 book 各自去水 → 概率/log-odds 空间中位数或去极值均值（sharp 书加权）"，但实证显示这属于低成本改良而非急迫重写；CLV 基准应明确分层——Pinnacle closing 为主锚、Betfair（back 价扣佣或 lay 价换算）为辅、全 book 共识仅作 fallback；书单不必大动，但要按 sharp/soft 分层并剔除高 margin 噪声书（如 1xBet 只参与共识不做基准）；亚盘现阶段不必要（The Odds API 足球只有 h2h + 少量 additional markets，真亚盘需换数据源），最小落地形态是先用 API 已有的 `draw_no_bet`（等价亚盘平手盘）和 `btts` 做交叉验证信号。** 详见文末建议。

---

## 1. 业界赔率共识方法论

### 1.1 sharp / soft bookmaker 的区分

- **定义**：sharp book（Pinnacle 为代表）接受职业玩家、高限额、低 margin（足球大赛 2% 左右），赔率由 sharp 资金直接塑形；soft book（Bet365、William Hill 等娱乐型）低限额、快封 winning 账户、margin 高 2–3 倍（5%–8%），定价策略是"慢跟随 sharp + 面向散户的偏差定价"。sharp 与 soft 之间的价差正是 value betting 的利润来源。来源：[OddsHub: Sharp vs Soft Bookmakers](https://www.oddshub.io/blog/sharp-vs-soft-bookmakers)、[Outlier: How Sportsbooks Set Odds](https://help.outlier.bet/en/articles/9922960-how-sportsbooks-set-odds-soft-vs-sharp-books)、[Bet2Invest: Sharp or Recreational Bookmaker](https://bet2invest.com/blog/Sharp-or-Recreational-Bookmaker)、[Shark Betting: Sharp Books Explained](https://www.sharkbetting.com/blog/sharp-books-explained)。
- **注意"sharp"的真实含义**：Pinnacle 被认为最 sharp 的主要原因是它**接受赢家**（限额高 → sharp 资金愿意进场 → 价格被持续校正），并不保证它在每个 niche 市场都是最准。社区讨论中也存在合理质疑：某些小市场里其他专业书（或交易所）可能更准。来源：[r/algobetting: Still not convinced Pinnacle is truly "sharper"](https://www.reddit.com/r/algobetting/comments/1i4drzy/still_not_convinced_pinnacle_is_truly_sharper/)。
- **对共识构造的含义**：soft 书不是噪声源而是一个"滞后 + 带散户偏差的 Pinnacle 影子"——它增加横截面厚度，但简单均价会把这些偏差等权混入。业界的两种做法：(a) 共识只用 sharp 子集（Pinnacle ± 1–2 本）作 fair 概率代理；(b) 全书均价但去极值/中位数，用于抗单书错误。两种都有人用，取决于书的数量与市场层级。来源：[r/algobetting: Average of sharpest books as fair odds proxy?](https://www.reddit.com/r/algobetting/comments/1c0fxba/average_of_sharpest_books_as_fair_odds_proxy/)、[r/algobetting: How do odds comparison sites' "average odds" work?](https://www.reddit.com/r/algobetting/comments/1k7nvtx/how_do_odds_comparison_sites_average_odds_work/)。

### 1.2 共识构造方法对比

| 方法 | 做法 | 优点 | 缺点/适用性 |
|---|---|---|---|
| 简单均价（odds 空间） | 各书 decimal odds 算术平均 | 实现最简单 | 高 margin soft 书等权混入；odds 空间平均在长赔端被 soft 书的低赔偏差拉偏 |
| 去极值均价（trimmed mean） | 去掉最高/最低价后平均 | 抗单书错价/缓存陈旧价 | book 数少时（<5）无法去极值 |
| 中位数 | 每个选项取各书价格中位数 | 对离群书最稳健，小样本友好 | 丢弃幅度信息；book 数 3–4 时退化 |
| 先去水再平均（概率空间） | 每 book 各自 de-vig 得概率向量 → 概率平均（等价 log-odds 空间平均的一种近似） | 把 margin 差异在书级消掉，共识反映的是"各书认为的公平概率"；文献标准做法 | 计算稍多；去水方法的选择会传导进共识 |
| log-odds 空间平均 | 在 log(p) 空间平均后归一 | 概率论的几何平均，长赔端不易被拉偏 | 与概率空间平均差异通常很小 |

- 文献基准做法：UCD 的 Karl Whelan（体育博彩课程 Lecture 4 "Extracting Signals from Odds"）明确把"**对多 book 各自去水后的概率取平均**"作为真实概率的标准估计，并介绍 log-odds 空间处理；Lecture 3 系统讲解 overround 与 FLB 为何使"直接归一化"有偏。来源：[Whelan: Sports Betting course](https://www.karlwhelan.com/sportsbetting/)、[Lecture 3 PDF](https://www.karlwhelan.com/sportsbetting/Lecture3.pdf)、[Lecture 4 PDF](https://www.karlwhelan.com/sportsbetting/Lecture4.pdf)。
- 一个重要的实证校准点（pena.lt/y，EPL 2024/25 Bet365 closing 价，RPS 评估）：**Shin 去水并不总是赢——在低 margin 市场上，基础归一化（multiplicative）的 RPS 甚至略好于 Shin（0.20829 vs 0.20855）**；方法间差异是二阶小量，一致性（用什么方法就一贯地用）比选哪个方法更重要。来源：[pena.lt/y: From Biased Odds to Fair Probabilities](http://pena.lt/y/2025/09/14/from-biased-odds-to-fair-probabilities/)。
- 结论：多书场景下"先去水再平均"是文献推荐；但 goalx 现状（先均价再 Shin）与它的差距属于小量级，升级应走 A/B 对照（回测 RPS/校准）而不是盲改。

### 1.3 去水（de-vig）方法对比

四种主流方法（对三向市场，raw implied πᵢ = 1/oᵢ，overround B = Σπᵢ > 1）：

| 方法 | 机制 | 对 FLB 的处理 | 适用性 |
|---|---|---|---|
| Multiplicative（归一化） | πᵢ/B 等比例压缩 | 无——margin 均摊，隐含假设偏差均匀 | margin 低时（sharp 书）足够好；实证 RPS 常常不输高级方法 |
| Additive | πᵢ − (B−1)/n 等额减 | 反向——把等额 margin 从各方扣掉，扭曲大 | 文献普遍认为最差，只做对照 |
| Power | 找 k 使 Σπᵢᵏ = 1 | 部分——幂压缩对低概率端压缩更强，介于 multiplicative 与 Shin 之间 | 单参数、无经济解释，但实测稳健 |
| Shin | 解 insider 比例 z：把 overround 解释为 book 对内幕交易的防御，长冷门端被压更多 | 显式建模 FLB | 学术标准（Shin 1993, Economic Journal）；预测准确性有实证支持；高 margin soft 书上明显优于归一化 |

- 各方法机理与实操：[Betherosports: Devigging Methods Explained](https://betherosports.com/blog/devigging-methods-explained)（明确指出 power 位于 multiplicative 与 Shin 之间）、[Pinnacle Odds Dropper: How to De-vig Pinnacle's Odds (4 Methods)](https://www.pinnacleoddsdropper.com/guides/how-to-devig-pinnacle-s-odds-for-betting-on-soft-books)、[DRatings: A Summary of Different No-Vig Methods](https://www.dratings.com/a-summary-of-different-no-vig-methods/)。
- Shin 的出处与实证：Shin, H.S. (1993) 用赔率估计市场内幕交易比例 z，核心预测是** book 为防御内幕交易而系统性地压低长赔端赔率（制造 FLB）**；Vaughan Williams & Paton 的实证支持 Shin 概率比简单归一化更接近真实胜率；Štrumbelj (2014, *International Journal of Forecasting*) 确认 Shin 导出的概率预测优于基础归一化。来源：[Vaughan Williams & Paton (ResearchGate)](https://www.researchgate.net/publication/4988900_The_Favourite-Longshot_Bias_Bookmaker_Margins_and_Insider_Trading_in_a_Variety_of_Betting_Markets)、[Štrumbelj 2014 (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533)、[Whelan 2024 UCD WP: On Estimates of Insider Trading in Sports Betting](https://www.ucd.ie/economics/t4media/WP2024_19.pdf)、[Favourite-longshot bias — Wikipedia](https://en.wikipedia.org/wiki/Favourite-longshot_bias)。
- **对 goalx 的映射**：goalx 现在共识向量上用 Shin、power 做敏感性对照（`odds_math.py`），这与文献一致；要点是**去水方法应按书分层**——soft 书（高 margin、FLB 强）用 Shin 有意义，Pinnacle/Betfair 级低 margin 价格上 multiplicative 与 Shin 差异极小（见上文 pena.lt/y 实证）。

### 1.4 Closing line value 作为"黄金标准"的依据与边界

- **依据**：closing line 是开赛前市场聚合全部信息后的最有效价格（Pinnacle closing 的无偏性有专门研究：football-data.co.uk 的 Pinnacle 效率分析、以及学术检验发现 Pinnacle closing 是真实概率的无偏估计）。因此"持续打败 closing line"被普遍视为长期盈利的最强预测指标——比小样本胜率可靠，因为 CLV 度量的是决策质量而非运气。来源：[Football-Data: Pinnacle closing line efficiency](https://www.football-data.co.uk/blog/pinnacle_efficiency.php)、[Helsinki NHL study (Pinnacle closing 为无偏估计)](https://helda.helsinki.fi/bitstreams/55fbf03d-988d-47a1-9ee1-c7de937a2c06/download)、[PinnacleOddsDropper: Closing Line Value](https://www.pinnacleoddsdropper.com/blog/closing-line-value)、[Trademate: Closing line — the most important metric](https://tradematesports.medium.com/closing-line-the-most-important-metric-in-sports-trading-58e56cdb4458)。
- **边界与批评**（对"黄金标准"的修正）：
  - Whelan《The Truth about Closing Line Value》：CLV **不充分**——大多数正 CLV 的注是亏钱的（margin 的存在使平均正 CLV 仍可能整体亏损）；CLV 是必要条件的证据强于充分条件。来源：[Karl Whelan: The Truth about Closing Line Value](https://www.karlwhelan.com/sportsbetting/the-truth-about-closing-line-value/)。
  - Captain Jack Andrews（Unabated）：CLV "is not so much gospel as it is a guidepost"——必须指明**用哪家书的 closing、哪个市场、如何测量**。来源：[Unabated: Getting Precise About Closing Line Value](https://unabated.com/post/getting-precise-about-closing-line-value)。
  - 小样本（~100 注）下 CLV 与利润都可能被方差支配；低流动性市场的 closing line 本身不够有效，CLV 意义减弱。来源：[OddsShopper: CLV vs Variance](https://www.oddsshopper.com/articles/betting-101/clv-vs-variance)、[VSIN: The Importance of CLV](https://vsin.com/how-to-bet/the-importance-of-closing-line-value/)。
- **实操含义**：CLV 基准必须固定口径（固定书、固定去水法、固定观测窗），否则指标漂移。goalx 现有票 34 口径（closing 快照须实际观测于开赛前 90 分钟窗内、迟到快照排除）与这一要求一致。

### 1.5 Odds movement / steam moves 的用法

- **定义**：steam move = 多 book 几乎同时的同向快速移线，通常由 sharp/syndicate 资金触发；reverse line movement（RLM）= 线逆公众下注比例方向移动，是 sharp 信号的强形态。来源：[OddsShopper: Line Movement Explained](https://www.oddsshopper.com/articles/betting-101/line-movement-explained)、[OddsIndex: Reverse Line Movement](https://oddsindex.com/guides/reverse-line-movement-guide)、[HeatPicks: Line Movement, Steam Moves & Sharp-Action Alerts](https://www.heatpicks.com/features/line-movement)。
- **用法与陷阱**：价值在"移动发生前"拿到价；移动本身不是保证（"No move is a promise"）。对量化系统的可用形态：(a) 把"距 kickoff 的报价路径"存下来（goalx 已有快照表），用"决策时点价 vs closing"的差（即 CLV 的事前代理）而非裸移动方向做特征；(b) 共识构造时对陈旧快照按 `max_age_seconds` 剔除（goalx `books_complete_asof` 已实现），避免把"昨天的价"当共识。
- 竞彩场景的特殊性：竞彩官方价自身也是"soft 定价者"（缓慢跟随国际市场、有固定返奖率），国际市场的 steam 往往先于竞彩调价——这正是 goalx 能吃到价差的结构性来源，但同刻也意味着**用竞彩自身价格构造共识没有意义**，共识必须来自国际源（现状正确）。

---

## 2. 主要博彩公司赔率特点

### 2.1 逐家画像

| 公司 | 类型 | margin（足球 1X2 大赛） | 特点 | 适合角色 |
|---|---|---|---|---|
| **Pinnacle** | sharp 定价者 | ~2%（AH 临场可 <2%） | 接受赢家、限额最高、价格被 sharp 资金持续校正；closing line 是行业事实基准 | **CLV 主锚**；共识 sharp 层 |
| **Betfair Exchange** | 交易所 | 名义 0（back/lay 价差极窄） | P2P 真实成交价，无 baked-in margin，赢利收 2–5% 佣金；流动性低于 Pinnacle 限额但价格最"真" | **CLV 辅锚**（需扣佣/取 mid）；fair 概率对照 |
| **Bet365** | soft（最大娱乐书） | 5%–8% | 覆盖面最广（含亚洲联赛）、开价早、但限额/封号快；散户偏好直接进定价（FLB 强） | 共识厚度源，不做基准 |
| **William Hill** | soft（英国零售系） | 5%–8% | 定价保守、跟随慢；对冷门压价重 | 共识厚度源 |
| **Betway** | soft | 5%–8% | 营销驱动娱乐书，定价跟随型 | 同上；**注意：未在 The Odds API 已确认书单中见到 Betway**，接入前需用 `/v4/sports` 实测确认 |
| **Unibet** | soft（Kindred 系） | 5%–7% | 北欧娱乐书，定价跟随型 | 共识厚度源（API key `unibet_eu`） |

- Pinnacle 角色：[Betherosports: How to Use Pinnacle](https://betherosports.com/blog/how-to-use-pinnacle)（margin 低至 2–3%，价格最接近真实概率）、[HowProsBet: What Makes Pinnacle Different](https://howprosbet.com/what-makes-pinnacle-different/)（临场 AH overround <2%）、[Shark Betting](https://www.sharkbetting.com/blog/sharp-books-explained)。
- Betfair 角色：[Smarkets: How to calculate betting margins](https://help.smarkets.com/hc/en-gb/articles/214180145-How-to-calculate-betting-margins)、[OddsShopper: What is a betting exchange](https://www.oddsshopper.com/articles/prediction-markets/what-is-a-betting-exchange)、[SportBex: Betfair Exchange vs Traditional Bookmaker Odds](https://sportbex.com/blog/betfair-exchange-vs-traditional-bookmaker-odds/)、佣金模型 [PracticalWebTools Betfair guide](https://practicalwebtools.com/blog/betting-exchange-strategy-betfair-guide-2026)；back-lay 换算 mid 的做法见 [Traderline: no-vig workflow](https://traderline.com/education/betfair-odds-comparison-no-vig-workflow)。
- soft 书偏差方向（favorite-longshot bias）：散户平均**高估长赔、低估热门**，book 因此对长冷门压更多 margin——长赔注的期望回报（−20%–30%）显著差于热门注（−2%–5%）。UK 实证（含 bet365/William Hill 等九书同挂 100/1 的案例）见 [Journal of Prediction Markets (UBPLJ)](https://www.ubplj.org/index.php/jpm/article/download/473/510/1496)；理论与综述见 [Wikipedia: Favourite-longshot bias](https://en.wikipedia.org/wiki/Favourite-longshot_bias)、[Snowberg & Wolfers 综述 (ResearchGate)](https://www.researchgate.net/publication/228884358_The_Favorite-Longshot_Bias_An_Overview_of_the_Main_Explanations)、[Whelan UCD WP22-23](https://www.ucd.ie/economics/t4media/WP22_23.pdf)、margin 分摊不均的实务解释见 [Champion Bets](https://www.championbets.com.au/betting-academy-article/favourite-longshot-bias) 与 [Pinnacle Betting Resources](https://www.pinnacle.com/betting-resources/en/betting-strategy/what-is-the-favourite-longshot-bias/vun2u32r85ppf4yp)。**含义：soft 书的三向价里，平局/客胜冷门端最不可信；共识里 soft 书长赔端贡献应被去水方法（Shin）或权重压低。**

### 2.2 The Odds API 实际覆盖

- **区域与书**：请求按 region 拉书（`us`/`us2`/`uk`/`eu`/`au` 等，可逗号分隔；**每 10 本书计 1 个 region 配额**）。已确认相关 key：`pinnacle`（EU 区）、`betfair_ex_uk`（UK 区，交易所，h2h 自动带 lay 价）、`bet365`/`bet365uk`（UK）、`williamhill`（UK）、`unibet_eu`（EU）、以及 `marathonbet`、`sbobet`、`onexbet`(1xBet)、`betclic` 等。来源：[The Odds API: Bookmaker APIs](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html)、[Odds API Documentation V4](https://the-odds-api.com/liveapi/guides/v4/)、[Scribd 镜像书单](https://www.scribd.com/document/1005567625/The-Odds-API-Com-Sports-odds-Data-Bookmaker-Apis-HTML-Us-Bookmakers)。
- **注意**：1xBet（`onexbet`）是 CIS 系高 margin soft 书——参与共识无妨，绝不能当 fair 基准；Betway 未在确认书单中。
- **市场**：featured markets = `h2h`（足球为三向）、`spreads`、`totals`、`outrights`、`h2h_lay`（交易所 lay 价）；**spreads/totals 目前主要限美国 sport 与美系书**，足球的盘口/大小球覆盖有限（详见第 4 节）。数据延迟：pre-match 最高约 40 秒、in-play 约 2 秒；additional markets 约 1 分钟一更。来源：[The Odds API: Betting Markets](https://the-odds-api.com/sports-odds-data/betting-markets.html)、[The Odds API: Bets API](https://the-odds-api.com/sports-odds-data/bets-api.html)。
- **对共识的角色结论**：Pinnacle 是共识里唯一必须保的 sharp 锚（缺失时该场共识应降置信）；Betfair 交易所价可作为第二 sharp 层（扣佣后）；Bet365/WH/Unibet 等提供横截面厚度。

---

## 3. 联赛覆盖：五大联赛 vs 亚洲联赛

### 3.1 覆盖事实

- **The Odds API 层面**：竞彩在售重点亚洲联赛全部有 sport key——J League `soccer_japan_j_league`、K League 1 `soccer_korea_kleague1`、中超 `soccer_china_superleague`、澳超 `soccer_australia_aleague`；J-League 历史赔率可回溯至 2020-06。来源：[The Odds API: Sports APIs（官方 sport key 列表）](https://the-odds-api.com/sports-odds-data/sports-apis.html)、[Historical Odds Data](https://the-odds-api.com/historical-odds-data/)、[Odds API V4 docs](https://the-odds-api.com/liveapi/guides/v4/)。
- **Pinnacle 层面**：四个联赛都在 Pinnacle 覆盖内（有专门页面与赛事预测内容），但限额与关注度低于欧洲顶级联赛。来源：[Pinnacle K League 1](https://www.pinnacle.com/en/soccer/korea-republic-k-league-1/matchups/)、[Pinnacle J-League](https://www.pinnacle.bet/en/soccer/japan-j-league/matchups/)、[Pinnacle: CSL/K1/J1 分析](https://www.pinnacle.com/betting-resources/en/soccer/china-super-league-k-league-1-and-j-league-1-predictions/7um2p7xy4462v75d)。

### 3.2 定价质量与流动性差异

- **限额分层**：Pinnacle 顶级市场 handicaps 可到 ~$30k，小联赛 niche 市场实测低至 €45–75；限额随开赛临近上升，且限额本身就是联赛流动性的代理指标（Bet2Invest 直接用 Pinnacle max-bet 做联赛流动性过滤器）。来源：[Pinnacle: Why Pinnacle offers higher betting limits](https://www.pinnacle.com/betting-resources/en/educational/why-pinnacle-offers-higher-betting-limits-than-other-sportsbooks)、[GhanaSoccernet: Pinnacle betting limits](https://ghanasoccernet.com/uk/wiki/pinnacle-betting-limits/)、[r/algobetting: Pinnacle limits](https://www.reddit.com/r/algobetting/comments/1orjw3t/pinnacle_limits/)、[Arbusers forum](https://arbusers.com/pinnacle-limiting-winners-t10780/)、[Bet2Invest league liquidity filter](https://strategies.bet2invest.com/en-us/filters/league-liquidity)、[HowProsBet: liquidity explained](https://howprosbet.com/sports-betting-liquidity-explained/)。
- **margin 分层**：小联赛 1X2 overround 典型 7%–12%（vs 五大联赛 sharp 书 2%–3%），市场越"不可预测"平均 margin 越高（有研究测到平均 10.8%）。来源：[ResearchGate: The betting market over time — overround in European football](https://www.researchgate.net/publication/329242933_The_betting_market_over_time_overround_and_surebets_in_European_football)、社区汇总见 [sbo.net CSL](https://www.sbo.net/football/chinese-super-league/) 与 [OddsPortal A-League](https://www.oddsportal.com/football/australia/a-league/)。
- **对 CLV 的含义**：亚洲联赛的 Pinnacle closing 仍比 soft 书有效，但其本身有效性与流动性弱于五大联赛——CLV 阈值与解读应按联赛分层，不能拿五大联赛的 CLV 分布套中超。

### 3.3 小联赛样本下共识的注意事项

1. **共识分母会缩水**：报价完整的 book 数在亚洲联赛明显少于英超（软书挂盘不全或晚挂）。goalx `books_complete_asof` 已剔除三向不全的书——正确；需要补的是**每联赛记录共识分母分布，分母 < 某阈值（如 4）时对共识概率打低置信标**，回测与 haircut 分桶处理。
2. **FLB 在小联赛更强**（margin 更高 → 去水方法的差异被放大）：soft 书多的共识里 Shin/加权的作用比在五大联赛更大。
3. **单书依赖风险**：若 Pinnacle 在某些场次缺席，"Pinnacle closing" 退化为"全 book 共识 closing"，两种口径不可混报（呼应 Unabated "指明用哪家书的 closing"）。
4. **历史深度**：The Odds API 历史数据从 2020 起有（J-League），回测样本足够，但早期 book 覆盖更薄，回测要按当时实际可得的书集构造共识（避免前视/幸存者偏差）。

---

## 4. 亚盘（Asian Handicap）是否必要

### 4.1 机制

- 亚盘 = 让球线（含 0/±0.25/±0.5/±0.75/±1 …）+ 双边水位（odds），**把三向市场变两向**：整数线（0, 1, 2）打平退还（push）；半线（0.5, 1.5）必分胜负；四分之一线（0.25, 0.75）= 注金对半拆到相邻整/半线，产生半赢/半输/半退四种结果。无平局选项、退还机制使方差低于 1X2。来源：[Handicap-Bet: Quarter Asian Handicap Explained](https://handicap-bet.com/articles/quarter-asian-handicap-explained-025/)、[Asian-Handicap-Bet: Mastering quarter goals](https://asian-handicap-bet.com/asian-handicap-quarter-goals/)、[OddsShopper: Soccer Spread Betting & Asian Handicap](https://www.oddsshopper.com/articles/betting-101/soccer-spread-betting-asian-handicap)。

### 4.2 亚盘价与 1X2 的换算关系

- 同一分布的两种投影：由 1X2 得 (p₁, pₓ, p₂)，等价亚盘的公平水位约为 **AH_home = (1 − pₓ)/p₁、AH_away = (1 − pₓ)/p₂**（平局概率按两侧原概率比例分摊），线选在使双边水位接近 1.90–2.00 处；反之，亚盘 0 线（DNB）价格 + 平局概率可反推 1X2。实务工具：[TotalCorner: 1X2 ↔ Asian Handicap calculator](https://www.totalcorner.com/page/fulltime-asian-handicap-calculator)、公式推导见 [Salonen: How to Calculate Asian Handicap Odds](https://www.scribd.com/document/507288271/Salonen-How-to-Calculate-Asian-Handicap-Odds)、机制对比（两向、更低 margin）见 [oddsconv.com handicap](https://oddsconv.com/handicap/)。
- **关键点**：这不是无损换算——1X2 只约束三个事件概率，亚盘线族约束的是**分差分布**（需要净胜球分布模型，如 Poisson/Dixon-Coles 才能跨线族一致）。所以"用亚盘交叉验证 1X2"本质是引入了对分差分布的额外约束/信息。

### 4.3 亚洲市场流动性为何集中在亚盘、其收盘是否更 sharp

- 亚洲职业资金（含 syndicate）的主战场是亚盘与大小球，而非 1X2；最深流动性集中在少数亚洲书（SBOBET、SingBet、MaxBet/前 IBCBet 等，多经 broker 接入），亚盘双边 margin 可低至 <1%，比 Pinnacle 的 ~2% 更紧。来源：[AsianBookies24](https://asianbookies24.com/)、[Brokerstorm: Asian bookies and Asian Handicap](https://brokerstorm.com/2020/09/08/asian-bookies-and-asian-handicap/)、[AsianOdds bookmaker reviews](https://asianodds.com/en/bookmaker-reviews)、[Shark Betting: Sharp Books Explained](https://www.sharkbetting.com/blog/sharp-books-explained)、[HowProsBet: What Makes Pinnacle Different](https://howprosbet.com/what-makes-pinnacle-different/)。
- 传统"澳门盘/CIS 资金主导"的说法在现代市场已弱化：当前 hierarchy 是 Pinnacle + 大亚洲书（SBO/SingBet/MaxBet）为最 sharp 层，CIS 系（BetWinner/1win/Megapari 等）是高 margin soft 层。来源：[SBO.net Macau](https://www.sbo.net/country/macau/)、[Betzillion Asia bookmakers](https://betzillion.net/bookmakers/asia/)。
- **是否更 sharp**：亚盘临场价（尤其低 margin 亚洲书的 mainstream 线）至少与 Pinnacle 1X2 同级有效，且因为两向 + 低 margin，去水误差更小。但**sharp 度的增益集中在流动性好的联赛/场次**；小联赛亚盘的限额与有效性与 1X2 一样缩水。

### 4.4 对"投竞彩胜平负/进球"系统能增加什么信号

1. **1X2 概率交叉验证**：亚盘 0/±0.25 线价 + 大小球可反推三向概率，与欧赔共识互相校验——两源分岐大的场次是模型不确定性高的信号（可进 haircut/置信层）。
2. **让球线作为强弱信息**：让球线是市场对两队实力差的连续度量，比 1X2 三档更细——对竞彩让球胜平负（hhad）玩法尤其直接：**竞彩让球盘的"让球数"几乎总是整数（通常 ±1），而市场亚盘主流线在 ±0.5/±0.75/±1.25 附近时，竞彩整数让球盘的平局/胜负结构会被放大——亚盘线位置是 hhad 定价的强先验**。
3. **大小球盘口对 ttg/crs 的参考**：大小球主线与水位直接给出总进球分布的分位数约束，是 ttg（总进球玩法）与 crs（波胆）模型最直接的市场锚——比从 1X2 间接推断总进球分布可靠得多。
4. **steam 源更真实**：亚洲市场的让球/大小球盘最先移动（信息最先到达最大流动性池），跟踪亚盘移动比跟踪欧洲 soft 书 1X2 更接近信息源。

### 4.5 The Odds API 是否提供亚盘与大小球

- **featured markets** 有 `spreads` 与 `totals`，但官方明确"**spreads/totals 主要限美国 sport 与美系书**"；足球可靠可用的只有 `h2h`（三向）。来源：[The Odds API: Betting Markets](https://the-odds-api.com/sports-odds-data/betting-markets.html)。
- **additional markets**（`/events/{eventId}/odds` 端点，覆盖扩张中）里足球可用的有：`btts`（两队是否都进球）、`draw_no_bet`（平局退款，**等价亚盘 0 线**）、`double_chance`、`correct_score`、`halftime_fulltime` 等；另有 corners/cards 系列与 `team_totals`/`alternate_spreads`/`alternate_totals`（标注"limited to US sports and selected bookmakers"）。**没有真正的四分之一盘亚盘市场**。来源：同上官方页面。
- 即：**想拿真亚盘（含水位、四分之一线、亚洲书），The Odds API 给不了，必须另接数据源**（OddsPortal 类聚合、broker API 或专营商）。

### 4.6 结论：必要 / 不必要 / 什么阶段引入

- **现阶段（had 为主 + 纸面验证期）：不必要**。理由：(a) 投注对象是竞彩 had 固定赔率，定价基准是欧赔三向共识，现有 h2h 共识 + Shin 已覆盖；(b) The Odds API 无真亚盘，引入需新增数据源与映射工程，边际成本高；(c) 亚盘信息的大头（强弱、总进球约束）在当前 EV 管线里暂无对应玩法消费。
- **中期（ttg/crs/hhad 玩法上线、或发现亚洲联赛 1X2 共识系统性偏差时）：值得引入**，形态见第 5 节。

---

## 5. 对 goalx 的建议

> 现状锚点（`apps/backend/src/goalx_backend/odds_math.py`、`data/quote_evidence.py`、`evaluation/clv.py`）：共识 = 各书 decimal 价算术平均（odds 空间）→ 对均价向量 Shin 去水；`power_implied` 做敏感性对照；CLV 侧 closing 取"多 book 完整三向共识 Shin"（`odds_api_closing`），未见 Pinnacle/Betfair 单独分层。

### 5.1 共识方法：建议升级，但以 A/B 对照落地（中优先级）

1. **改为"每 book 各自去水 → 概率空间聚合"**：每本书先各自 Shin（soft 书）/归一化（低 margin sharp 书差异小）得概率向量，再在概率或 log-odds 空间取**中位数（书少时）或去极值均值（书 ≥5 时）**。这是文献标准做法（[Whelan Lecture 4](https://www.karlwhelan.com/sportsbetting/Lecture4.pdf)、[pena.lt/y](http://pena.lt/y/2025/09/14/from-biased-odds-to-fair-probabilities/)），消除"高 margin soft 书把均价向量整体带偏"的问题。
2. **保留旧口径做对照**：实证表明方法间差异是二阶小量（pena.lt/y 的 RPS 对比），所以**不要直接替换**——两套口径并行跑回测，用 RPS/校准/最终 EV 稳定性决定切换，避免"感觉更高级"的重写。
3. **共识分母护栏**：每联赛记录"报价完整书数"分布，分母 <4 的场次共识概率打低置信标（进入 haircut 与展示层）；亚洲联赛尤其需要。
4. **steam/移动信息暂不做独立特征**：现有"决策时点价 vs closing"的 CLV 代理已隐含移动信息；等有足够快照密度后再评估显式移动特征。

### 5.2 书单：不必大动，但要分层使用（高优先级，成本低）

1. **CLV 基准分层（明确口径）**：closing 概率改为三级——主锚 `pinnacle`（单独 Shin/归一化）；辅锚 `betfair_ex_uk`（back 价扣 2–5% 佣金，或用 h2h_lay 构造 mid）；fallback 全 book 共识（现状口径），并在 CLV 记录里**标注用的是哪一级**（呼应 Unabated "指明哪家书的 closing"）。Pinnacle 缺席的场次才允许 fallback，且不与主锚混在同一分布里报告。
2. **共识成员**：保持全 book（厚度），但加两条规则——`onexbet`（1xBet）等高 margin CIS 书**只参与共识、永不参与基准**；soft 书长赔端的贡献已被 Shin 压制，无需手工权重起步。
3. **Betway 不在 The Odds API 确认书单**，规划中勿假设可用；书单以 `/v4/sports` 与 [官方书单页](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html) 实测为准。
4. **配额注意**：The Odds API 每 10 本书计 1 个 region 配额，扩书前先算配额成本（[V4 docs](https://the-odds-api.com/liveapi/guides/v4/)）。

### 5.3 亚盘：现阶段不引入，分两步走（低优先级，先做零成本评估）

1. **阶段 0（现在，零成本）**：评估 The Odds API additional markets 对竞彩在售联赛的实际覆盖率——重点 `draw_no_bet`（等价亚盘 0 线，强弱信息）与 `btts`（ttg 参考）。方法是抽样几个联赛的 events/{id}/odds 调用统计可得性。若覆盖可用，即可获得"亚盘信息的最小形态"而无须新数据源。
2. **阶段 1（ttg/hhad 玩法上线时）**：引入 `totals`/大小球类市场（若 API 足球覆盖仍差，则接一个专项源）做 ttg/crs 的市场锚；引入 DNB/亚盘 0 线价做 hhad 的让球线先验——这两个映射（大小球→ttg、DNB→hhad）是"亚盘信息"里投入产出比最高的两块。
3. **阶段 2（仅在证明必要时）**：接真亚盘源（含水位与四分之一线、亚洲书），用于 1X2 共识交叉验证与分差分布约束。触发条件：回测发现亚洲联赛 1X2 共识有系统性方向偏差，或 CLV 显示 closing 参照本身可疑。
4. **明确不做**：现阶段不为亚盘换主数据源、不引入四分之一线结算逻辑——投注对象（竞彩固定赔率）不消费它。

### 5.4 一句话风险提示

共识方法的升级属于改良而非纠错（实证差异小）；**真正影响指标可信度的是 CLV 基准口径分层（5.2.1）与共识分母护栏（5.3/3.3）**——这两个低成本项应优先于任何去水算法层面的精细化。

---

## 来源总索引

**方法论**：[OddsHub: Sharp vs Soft](https://www.oddshub.io/blog/sharp-vs-soft-bookmakers) · [Outlier: Soft vs Sharp](https://help.outlier.bet/en/articles/9922960-how-sportsbooks-set-odds-soft-vs-sharp-books) · [Bet2Invest: Sharp or Recreational](https://bet2invest.com/blog/Sharp-or-Recreational-Bookmaker) · [Betherosports: Devigging Methods](https://betherosports.com/blog/devigging-methods-explained) · [Pinnacle Odds Dropper: De-vig Guide](https://www.pinnacleoddsdropper.com/guides/how-to-devig-pinnacle-s-odds-for-betting-on-soft-books) · [DRatings: No-Vig Methods](https://www.dratings.com/a-summary-of-different-no-vig-methods/) · [pena.lt/y: From Biased Odds to Fair Probabilities](http://pena.lt/y/2025/09/14/from-biased-odds-to-fair-probabilities/) · [Whelan Lecture 3](https://www.karlwhelan.com/sportsbetting/Lecture3.pdf) / [Lecture 4](https://www.karlwhelan.com/sportsbetting/Lecture4.pdf) / [课程页](https://www.karlwhelan.com/sportsbetting/) · [Štrumbelj 2014 (IJF)](https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533) · [Vaughan Williams & Paton](https://www.researchgate.net/publication/4988900_The_Favourite-Longshot_Bias_Bookmaker_Margins_and_Insider_Trading_in_a_Variety_of_Betting_Markets) · [Whelan 2024: Shin's method](https://www.ucd.ie/economics/t4media/WP2024_19.pdf) · [r/algobetting: sharpest books as fair proxy](https://www.reddit.com/r/algobetting/comments/1c0fxba/average_of_sharpest_books_as_fair_odds_proxy/) · [r/algobetting: average odds](https://www.reddit.com/r/algobetting/comments/1k7nvtx/how_do_odds_comparison_sites_average_odds_work/)

**CLV 与移动**：[Football-Data: Pinnacle efficiency](https://www.football-data.co.uk/blog/pinnacle_efficiency.php) · [Helsinki NHL study](https://helda.helsinki.fi/bitstreams/55fbf03d-988d-47a1-9ee1-c7de937a2c06/download) · [Whelan: Truth about CLV](https://www.karlwhelan.com/sportsbetting/the-truth-about-closing-line-value/) · [Unabated: Getting Precise About CLV](https://unabated.com/post/getting-precise-about-closing-line-value) · [OddsShopper: CLV vs Variance](https://www.oddsshopper.com/articles/betting-101/clv-vs-variance) · [VSIN: Importance of CLV](https://vsin.com/how-to-bet/the-importance-of-closing-line-value/) · [Trademate: closing line](https://tradematesports.medium.com/closing-line-the-most-important-metric-in-sports-trading-58e56cdb4458) · [OddsShopper: Line Movement](https://www.oddsshopper.com/articles/betting-101/line-movement-explained) · [OddsIndex: RLM](https://oddsindex.com/guides/reverse-line-movement-guide) · [HeatPicks: steam moves](https://www.heatpicks.com/features/line-movement)

**博彩公司**：[Betherosports: How to Use Pinnacle](https://betherosports.com/blog/how-to-use-pinnacle) · [HowProsBet: What Makes Pinnacle Different](https://howprosbet.com/what-makes-pinnacle-different/) · [Shark Betting: Sharp Books](https://www.sharkbetting.com/blog/sharp-books-explained) · [r/algobetting: Is Pinnacle truly sharper](https://www.reddit.com/r/algobetting/comments/1i4drzy/still_not_convinced_pinnacle_is_truly_sharper/) · [Smarkets: margins](https://help.smarkets.com/hc/en-gb/articles/214180145-How-to-calculate-betting-margins) · [OddsShopper: betting exchange](https://www.oddsshopper.com/articles/prediction-markets/what-is-a-betting-exchange) · [SportBex: exchange vs bookmaker](https://sportbex.com/blog/betfair-exchange-vs-traditional-bookmaker-odds/) · [Traderline: no-vig workflow](https://traderline.com/education/betfair-odds-comparison-no-vig-workflow) · [Wikipedia: Favourite-longshot bias](https://en.wikipedia.org/wiki/Favourite-longshot_bias) · [UBPLJ 实证](https://www.ubplj.org/index.php/jpm/article/download/473/510/1496) · [Whelan WP22-23](https://www.ucd.ie/economics/t4media/WP22_23.pdf) · [Champion Bets: FLB](https://www.championbets.com.au/betting-academy-article/favourite-longshot-bias) · [Pinnacle: FLB](https://www.pinnacle.com/betting-resources/en/betting-strategy/what-is-the-favourite-longshot-bias/vun2u32r85ppf4yp)

**The Odds API**：[官网](https://the-odds-api.com/) · [Betting Markets](https://the-odds-api.com/sports-odds-data/betting-markets.html) · [Bets API](https://the-odds-api.com/sports-odds-data/bets-api.html) · [Bookmaker APIs](https://the-odds-api.com/sports-odds-data/bookmaker-apis.html) · [Sports APIs（sport keys）](https://the-odds-api.com/sports-odds-data/sports-apis.html) · [V4 文档](https://the-odds-api.com/liveapi/guides/v4/) · [Historical Odds](https://the-odds-api.com/historical-odds-data/)

**联赛覆盖**：[Pinnacle K League 1](https://www.pinnacle.com/en/soccer/korea-republic-k-league-1/matchups/) · [Pinnacle J-League](https://www.pinnacle.bet/en/soccer/japan-j-league/matchups/) · [Pinnacle: CSL/K1/J1](https://www.pinnacle.com/betting-resources/en/soccer/china-super-league-k-league-1-and-j-league-1-predictions/7um2p7xy4462v75d) · [Pinnacle: higher limits](https://www.pinnacle.com/betting-resources/en/educational/why-pinnacle-offers-higher-betting-limits-than-other-sportsbooks) · [GhanaSoccernet: Pinnacle limits](https://ghanasoccernet.com/uk/wiki/pinnacle-betting-limits/) · [Bet2Invest: league liquidity filter](https://strategies.bet2invest.com/en-us/filters/league-liquidity) · [HowProsBet: liquidity](https://howprosbet.com/sports-betting-liquidity-explained/) · [ResearchGate: overround over time](https://www.researchgate.net/publication/329242933_The_betting_market_over_time_overround_and_surebets_in_European_football) · [r/algobetting: Pinnacle limits](https://www.reddit.com/r/algobetting/comments/1orjw3t/pinnacle_limits/) · [Arbusers](https://arbusers.com/pinnacle-limiting-winners-t10780/)

**亚盘**：[Handicap-Bet: quarter AH](https://handicap-bet.com/articles/quarter-asian-handicap-explained-025/) · [Asian-Handicap-Bet: quarter goals](https://asian-handicap-bet.com/asian-handicap-quarter-goals/) · [OddsShopper: AH](https://www.oddsshopper.com/articles/betting-101/soccer-spread-betting-asian-handicap) · [TotalCorner: 1X2↔AH 换算器](https://www.totalcorner.com/page/fulltime-asian-handicap-calculator) · [Salonen: Calculate AH Odds](https://www.scribd.com/document/507288271/Salonen-How-to-Calculate-Asian-Handicap-Odds) · [oddsconv: handicap](https://oddsconv.com/handicap/) · [AsianBookies24](https://asianbookies24.com/) · [Brokerstorm: Asian bookies](https://brokerstorm.com/2020/09/08/asian-bookies-and-asian-handicap/) · [AsianOdds reviews](https://asianodds.com/en/bookmaker-reviews) · [SBO.net Macau](https://www.sbo.net/country/macau/) · [Betzillion: Asia](https://betzillion.net/bookmakers/asia/)
