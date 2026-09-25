# Goalx（足彩量化投注与预测系统）

面向中国大陆足球彩票（竞彩固定赔率 + 传统足彩奖池型）的个人量化投注系统：双线预测（ML + LLM 情报）、价值与成本预估、注额优化、纸面/真金记录与复盘。本文件是领域术语表，代码与文档统一以此为准。

## Language

### 赛程

**Competition**:
一项赛事（联赛或杯赛），带投入分层标签（Tier 1 主动投入 / Tier 2 被动覆盖 / 排除），大型杯赛期间可临时升级。
_Avoid_: League（与"联赛"混淆，杯赛也是 Competition）

**Fixture**:
一场已排期的比赛，以 UTC 记时；概念上携带杯赛阶段（小组末轮、淘汰赛等）——它是大模型复核的触发依据。
_Avoid_: Match, Game

**MatchCode**:
官方销售编号（竞彩"周二 301"、胜负彩"第 N 期第 M 场"），附属于 Fixture，用于与官方数据对账。
_Avoid_: Slot, 场次号

### 市场

**Market**:
一个可投注的玩法市场，以官方 poolCode 标识（had 胜平负 / hhad 让球 / crs 比分 / ttg 总进球 / hafu 半全场 / ttt14 十四场 / pick9 任选九 / goals4 四场进球 / htft6 六场半全场）。
_Avoid_: PlayType, 玩法（口语可用，实体一律 Market）

**MarketGroup**:
按研究方法与投注策略划分的玩法类别：胜平负类（had/hhad，固定赔率、模型概率×市场 EV 对照、串关主场）、进球类（ttg/crs，固定赔率、概率由比分矩阵推导、单关为主）、奖池型（ttt14/pick9，彩池分红、无固定赔率、覆盖策略）。半全场（hafu 等）暂不归类不呈现。
_Avoid_: 玩法入口（口语）， BetType

**Selection**:
Market 内的一个具体选项（如"比分 2:1""总进球 3"）。
_Avoid_: Outcome, Pick

**OddsSnapshot**:
某时点、某来源、对某 Selection 的一次报价，只增不改（append-only）。
_Avoid_: Odds（泛指时用）， Price

**ClosingLine**:
以 sharp 源（Pinnacle/Betfair）收盘价构成的代理基准线，用于 CLV 与去晦基准。
_Avoid_: Final odds, 收盘价（口语）

**ImpliedProbability**:
在明确去水假设下由报价估计的市场隐含概率，不等同真实发生概率。

### 奖池

**PoolPeriod**:
一个奖池型玩法的销售期（如胜负彩第 26029 期）。
_Avoid_: 期号（单独使用）

**PoolState**:
一个 PoolPeriod 的资金状态：销售额、滚存转入、奖级分配与中奖注数（后者为事后公布）。
_Avoid_: Jackpot

**PublicShare**:
公众注分布。估计值（来自澳客人气等）与公布值（官方事后公布）是两个来源，分开存储。
_Avoid_: 人气（单独使用）

### 预测

**Forecast**:
一次概率输出（比分矩阵或概率向量），携带轨道（ML / LLM / 融合）、模型版本与时点；持久化并做内容哈希存证。
_Avoid_: Prediction（口语）

**MatchIntel**:
某场比赛的原始情报素材（伤停、轮换风险、动机、舆情），携带来源与时点；未经存证提炼。
_Avoid_: News

**IntelObservation**:
已存证的情报条目：append-only，携带来源 URL、采集时点、采集器标识与 raw 内容哈希——LLM 线的证据层工件。
_Avoid_: evidence（泛指）, 数据

**EvidenceSummary**:
LLM 对 IntelObservation 的结构化提炼与展示产物（证据卡内容），advisory 层，不改写任何概率工件。
_Avoid_: 摘要（口语）

**Divergence**:
ML 轨道与 LLM/市场基准之间的分歧度量，决定人工复核路由。
_Avoid_: Diff

**EVAssessment**:
对一注的价值评估：EV、置信区间及明确口径的边际成本；系统固定费用另作期间成本，不默认为每注分摊。
_Avoid_: Value（泛）

### 投注

**Bet**:
一个选项组合及其金额的记录，可以是未锁定建议、正式锁定纸面或实际购买。仅实际购买的真金记录影响 Bankroll；纸面锁定不代表发生了购买。
_Avoid_: Order, Wager

**DecisionKey**:
一注的冻结决策身份：mode + 选项组合与锁定赔率 + 锁定时点（不含金额）。同身份的重试/拆分金额注在验证分母与 CLV 报表中只计一次；全仓唯一公式在 betting 包。
_Avoid_: 去重键（口语）

**BetLeg**:
串关投注中的一腿：引用一个 Selection 及下注时锁定的 OddsSnapshot。
_Avoid_: Pick

**BetSlip**:
一张纸面锁定记录或实际投注票，可汇集多个 Bet；票据数量不等于 Bet 数量。对任9复式可包含多个 Combination。
_Avoid_: Ticket（与 MatchCode 混淆）

**Combination**:
复式票中的一个具体组合（如任9 的 9 场各一选）；注数为各场选项数的乘积。
_Avoid_: 注（单独使用）

**CostLedger**:
系统运行成本（数据订阅、LLM 调用）的记账，供成本预估与盈亏平衡计算。
_Avoid_: Expense

### 事实与结算

**DrawResult**:
经来源核验的官方开奖结果，含无效场次标记；开奖与结算的唯一事实源，更正必须可追溯。
_Avoid_: Result（泛指）

**Settlement**:
按官方规则对固定奖金 Bet 或奖池 BetSlip 的兑付计算；单关或全无效返本金，串关无效腿按1、剩余腿继续计奖。
_Avoid_: Payout（单独使用）

**Bankroll**:
专门用于本系统的真实资金池，投注支出和兑付仅来自实际购买的真金记录，另可记录真实出入金；变动以 BankrollEvent 记录。
_Avoid_: Balance, 余额

### 数据资产

**AssetState**:
一项数据资产相对其消费需求的四种状态判定：**未持有**（库与 CorpusStore 均无）、**已持有未入库**（原始文件在手、未进任何表）、**已入库未启用**（表内有、消费端未用）、**覆盖不足**（在用但深度/赛事/粒度不达标）。缺口矩阵与排期统一以此四态描述，"已持仓未启用"并入第三态。
_Avoid_: 缺数据（口语）、缺口（单独使用）

### 语料

**TrajectoryCorpus**:
以逐庄赔率"开→收"时序为主体、按 CorpusScope 圈定的多赛季历史数据集（2017/18 赛季起，源T 回填 + 增量；2020 前仅轨迹+亚盘深度）；服务漂移/时序研究与建模扩展，不作结算事实源。
_Avoid_: 时序数据（泛指）、历史库

**CorpusScope**:
轨迹语料的 15 项赛事清单（五大+英冠+荷甲+葡超+土超+比甲+苏超+瑞超+挪超+欧冠+欧联），按竞彩出场频率×市场流动性圈定；采集范围与建模范围（TIER1_COMPETITIONS）解耦——采进来不等于进模型。
_Avoid_: 联赛白名单、大流动性清单（口语可用，实体一律 CorpusScope）

**CorpusStore**:
repo 外独立数据资产树（默认 ~/goalx-data/），承载 TrajectoryCorpus 的 raw 压缩件、bronze 解析层、silver canonical 层与研究查询库；与运行面仅经只读桥互通，采集故障与运行面互不波及。
_Avoid_: 数据目录（泛指）、数据仓库（口语可用，实体一律 CorpusStore）

**本地报价研究候选**:
相对于某项 Competition 具有本地投注市场关联、值得检验其报价相对跨市场共识是否含增量信息的 `(Competition, bookmaker_id)` 组合；候选身份不代表已证实领先、预测增益或数据覆盖达标。
_Avoid_: 本地核心书商、领先书商

**最后可见记录价**:
在指定源页时间之前，该书商该玩法的源页轨迹中最后一条完整报价；它不表示该时点实际可见或可下注。
_Avoid_: 实时可买价、真实收盘价
