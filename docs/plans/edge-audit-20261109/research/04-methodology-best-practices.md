# 04 国际方法学最佳实践对照（research 笔记）

票：`.scratch/edge-audit-20261109/issues/04-methodology-best-practices.md`
日期：2026-11-09。方法：research skill——网络检索 + 逐条追源（arXiv API / GitHub / 出版社页直接核对者标注「已核」；仅经检索摘要未见全文者标注「检索摘要，未逐字核对」）。对照起点=账本 §3 二十条 + §4 待优化清单 + E-28/E-29/E-30/E-32/E-33/E-36 条目原文。

---

## 一、一页四栏总览

| 已覆盖（账本证据） | 缺而便宜 | 缺而重 | 不适用（JC 可购面约束） |
|---|---|---|---|
| 预注册+三窗一次性（E-32, §3.3）≈ 因果前沿上的 expanding walk-forward | S1 扫描器挂 Romano-Wolf stepdown（E-33 注记的未校正债） | σ_CLV 实测与「CLV edge≈EV edge」验证（§4.1#8） | CPCV/K-fold 多路径选型（与三窗只碰一次互斥，烧样本） |
| BH/Holm 多重校正+总尝试数报告（E-28⑥, E-32, §3.4） | DSR 式 max-z 折减报告落地（2026-10-22 已裁 A 线，未实现） | Hansen SPA/White RC 对 flat-k 基准的 data-snooping 检验管线 | 海外 sharp 执行面方法（line shopping、收盘执行优化）——JC 单一慢运营商 |
| 安慰剂/同接受率/flat-k 反事实对照（E-15/E-03/E-02, §3.6/7/10） | 搜索/验证窗边界 embargo（特征时窗跨界泄漏） | 多幸存者组合管理层（相关性/敞口/回撤；等首批幸存者） | 学术 parimutuel 时机/套利策略直接移植——35% 抽水+停售结构（E-25/E-31） |
| PIT/as-of 纪律 + xG 赛后值禁入（§3.2, E-11） | log loss 第三指标进 S2 与判读分层（RPS 批评的回应） | PBO/CSCV 后验读数（若 196 条试验账存有逐切片序列；否则升「重」） | 盘中/in-play 方法——JC 无滚球盘（E-08/E-09 已三角互证吃不到） |
| CLV 外锚主判 + σ 闭式功效（E-29/E-30，领先文献通行做法） | Venn-ABERS 校准区间作 E-28② 分族校准证据补强 | CLV→ROI 映射的本地校准（外锚→本地实现的桥） | 全速 Kelly/增长最优目标——税墙下 Kelly 只作保险丝（E-28⑤/E-31/E-35） |
| PAVA isotonic + RPS/Brier 双指标（E-16 隔离复现管线） | DC/EM 重拟合的时间衰减半衰期敏感性报告 | conformal 弃注层（研究场验证后再议执行面） | LLM 直接预测执行面——H6 前提死后关闭（S6 仅观测） |
| 分数 Kelly 保险丝 + 收缩五级 L0–L4 + PSI 漂移唤醒（E-28⑤, E-36） | — | — | — |

---

## 二、分主题详表（每条：我方现状 → 判定 → 来源）

### A. 回测过拟合与样本外验证

| 主题 | 国际最佳实践基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| PBO/CSCV | 用组合对称交叉验证估计「样本内最优在样本外排名失效」的概率（PBO），作为回测过拟合的点估计 | 未显式计算。替代物=三窗一次性+196 条预注册试验账+BH。**判定：报告层缺而便宜**（用试验账后验重算一个 PBO 读数作元指标，不改选型流程；前提是逐切片时间序列存全，否则升「重」） | Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting"（CSCV/PBO；Western Michigan ScholarWorks 版 https://scholarworks.wmich.edu/math_pubs/42，已核存在）；同团队 "Pseudo-Mathematics and Financial Charlatanism"（Notices of the AMS, 2014） |
| Deflated Sharpe Ratio (DSR) | 对「试了 N 个里挑最大」的选择偏差做折减，报告胜者 Sharpe 超过零假设期望最大值的概率 | 2026-10-22 裁决已把 White RC/SPA/DSR 写进 A 线预注册（账本 §3.4）；实现面现状=BH/Holm 已冻结进 EM-1/E-32，DSR 式折减未见落地实现（S1 未校正注记=旁证）。**判定：缺而便宜**——闭式公式，用试验账的 N 与切片相关性即可出折减后 z | Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for Selection Bias"（2014, SSRN 2460551, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551，已核存在） |
| OOS 方法比较（2024） | 合成受控环境下比较各验证法：非平稳金融数据上 walk-forward 系比 K-fold 系更保守可信 | 我方三窗/44 联赛 walk-forward 属该结论推荐侧。**判定：已覆盖（方法选型面）** | Arian, Norouzi & Seco, "Backtest overfitting in the machine learning era"（Knowledge-Based Systems, 2024；SSRN 4778909；检索摘要，未逐字核对全文） |
| 时间序列验证的不可能三角（2026） | 训练充分性/测试覆盖率/时序因果三者不可兼得；expanding walk-forward 恰在因果帕累托前沿，purged k-fold 以「距离」付费 | 我方 expanding 式三窗=前沿上的选择；据此**CPCV 引入应慎重**（见不适用栏）。**判定：已覆盖（理论佐证我们现状）** | Li, "The Impossible Trinity of Time-Series Validation"（arXiv:2609.29530, 2026-09，已核：纯噪声下 shuffled 5-fold IC=+0.32 vs contiguous +0.004） |

### B. Purged/embargoed CV 与 walk-forward

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| Purging+embargo | 标签时窗重叠时必须 purge；测试段后加 1–3% embargo 防串相关泄漏 | 我方场级标签不重叠（一场一结局），PIT/as-of 纪律在数据面防泄漏（§3.2）。但**特征侧时窗**（近况窗/H2H 窗）跨搜索/验证边界仍可能带信息——EM-1 协议未见边界 embargo 条款。**判定：缺而便宜**（在 E-32 加一句：切分边界丢弃特征窗半宽±若干日的场次） | López de Prado, *Advances in Financial Machine Learning*（Wiley, 2018）ch.7；skfolio `CombinatorialPurgedCV` 与 eslazarev/purged-cross-validation 实现（2024–2025 活跃，检索摘要） |
| CPCV 多路径 | 组合留出多路径，报 Sharpe 分布而非点估计 | 与三窗只碰一次纪律直接冲突（多路径=窗口反复消耗），且 JC 有效搜索窗本就有限（§4.1#4）。**判定：不适用（研究场纪律优先）**——若未来开非 JC 研究分支可再议 | 同上 + RiskLab AI / PapersWithBacktest CPCV 综述（检索摘要） |

### C. 多重检验

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| FDR 族（BH/Holm） | BH 控 FDR、Holm 控 FWER，切片正相关时保守 | **已覆盖**：BH 进 EM-1 晋升门、Holm 进 delay 分层、六步门⑥按注册总尝试数校正（E-28⑥/E-32/§3.4） | Benjamini & Hochberg 1995；Holm 1979（经典，不另列链接） |
| Romano-Wolf stepdown | 重抽样+stepdown 利用检验统计量的相依结构，FWER 控制下比 BH/Holm 更有功效；有成熟实现（rwolf2 Stata / Python 移植） | EM-1 的 74 切片同族相关（联赛带重叠），BH 假设 PRDS 偏保守但不假；**真正的债在 S1**：21 段×逐日无任何校正（E-33 注记自认）。**判定：缺而便宜**（S1 输出挂 RW stepdown，工具成熟） | Romano & Wolf 2005/2016；Clarke, "The Romano-Wolf Multiple Hypothesis Correction"（IZA DP 12845, https://docs.iza.org/dp12845.pdf，已核存在） |
| Data-snooping 检验（White RC / Hansen SPA） | 对「最优策略 vs 指定基准」做 bootstrap 联合检验，专治数据挖掘；SPA 对劣质模型再中心化，功效更高 | 基准天然有=flat-k 反事实（E-02），但没跑过正式 SPA 检验；2026-10-22 已裁进 A 线预注册未落地。**判定：缺而重**（bootstrap 管线工程量中等；学术标准做法） | White, "A Reality Check for Data Snooping"（Econometrica 2000）；Hansen, "A Test for Superior Predictive Ability"（JBES 2005）；Python `arch.bootstrap.SPA` 官方实现（已核其存在） |

### D. 体育预测评估基准（RPS 之争）

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| RPS 作为标准 | RPS 是足球三层有序结局的事实标准（Constantinou & Fenton 2012 确立） | **已覆盖**（RPS/Brier 双指标） | Constantinou & Fenton, "Solving the Problem of Inadequate Scoring Rules for Assessing Football Predictions"（2012, JQS 系） |
| 对 RPS 的批评 | Wheatcroft：距离敏感性在名义三分结局上不必然成立，Brier/对数损失更稳；2025 从业者复现 RPS 反例（给更低概率于真实结局的预报反而得分更好） | 我方双指标但无 log loss。**判定：缺而便宜**——S2 与判读分层加 log loss 第三指标，顺带覆盖尾部概率判别（对池面尾部派彩口径尤其有用） | Wheatcroft, "…the case against the Ranked Probability Score"（JQAS 17(4):273–287, 2021; arXiv:1908.08980）；Eastwood, "Better Metrics for Football Forecasts: Moving Beyond the RPS"（penaltyblog.com, 2025-05，从业者级，检索摘要）；Gneiting & Raftery 2007（proper scoring rules 通论） |
| Ley/Van Eetvelde/Groll 系 | 最大似然球队实力排名系（Statistical Modelling 2019）；Van Eetvelde 2023 提出 transformed RPS；Groll 系 ML+混合预报 | 我方 DC 单矩阵+GBM 融合+market-λ 联合反推在方法谱系内；文献共识=收盘赔率仍是最强单预测器、纯 ML 稳定胜过赔率罕见——与我方全阴读数一致。**判定：已覆盖（谱系内）** | Ley, Van de Wiele & Van Eetvelde, Statistical Modelling 19:55–77（2019）；Van Eetvelde, "Data-based ranking and prediction in football"（Ghent 博士论文, 2023）；Hubáček, Šourek & Železný, "Exploiting sports-betting market using machine learning"（检索摘要） |

### E. 校准

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| Venn-ABERS vs PAVA isotonic | VA=两条 isotonic（PAVA）拟出的概率区间，分布外校准保证（validity）免费送；2026 大规模实证：后验校准器里 VA 平均 log-loss 降幅最大；2025 理论扩展到一般损失+子群体多校准 | 我方 PAVA 点估计（S2/分族校准证据 E-28②）。**判定：缺而便宜**——pip 包（ip200/venn-abers）+既有 walk-forward 折可做校准集；区间宽度还能当「该族校准可信度」读数，正好补 E-28② 的证据面。注意 exchangeability 假设在场级近似成立、跨季漂移时放宽解读 | Vovk & Petej, "Venn-Abers Predictors"（arXiv:1211.0025, 2012/2014，已核）；van der Laan & Alaa, "Generalized Venn and Venn-Abers Calibration…"（arXiv:2502.05676, 2025，已核摘要）；Manokhin & Grønhaug, "Classifier Calibration at Scale"（arXiv:2601.19944, 2026，已核存在）；Petej & Vovk, "Inductive Venn-Abers and related regressors"（arXiv:2605.06646, 2026，已核存在）；实现 https://github.com/ip200/venn-abers（已核 README） |

### F. Conformal prediction

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| Conformal 胜率区间/弃注 | 分布无关覆盖保证；用于体育=胜概率区间与「预测集非单点则弃注」的门 | 未用。对单关 EV 判读不改变数学，但可作**仓位/弃注层**（预测集含多结果→降仓或不注）与研究场不确定度可视化。**判定：缺而便宜（研究场层）/执行面暂不适用**（不改变 EV 主判读，只影响 sizing 与弃注——进未来协议） | Johnstone, "Using Conformal Win Probability to Predict the Winners"（2024，检索摘要）；Kelly et al., conformal+Kelly 足球框架（2023，检索摘要）；自适应 conformal（时间序列 exchangeability 破缺场景，2024-12 arXiv，检索摘要未取到编号——如实标注） |

### G. CLV 作为评估指标

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| CLV 主判读 | 收盘线是赛前最有效价格，持续 beat-the-close 是技能的证据（从业者规范：对 sharp 书的 fair odds 算「真 CLV」即去水）；学术面正式把 CLV 当**晋升门**的文献反而稀薄——通行做法仅作事后描述 | 我方 E-29（Pinnacle 欧收盘主锚+欧共识副锚）+ E-30（σ=1.525 闭式功效→CLV 门唯一准绳）**领先于文献通行实践**（把 CLV 从描述指标升为带功效推导的主门）。**判定：已覆盖（此为我方强项）**；缺口在自身复盘已写明——σ_CLV 未实测、「CLV edge≈EV edge」未验证（§4.1#8），属**缺而重**（需 β 批次探针基建） | Pinnacle, "What is Closing Line Value" + "Using the closing line to test your skill"（https://www.pinnacle.com/betting-resources，从业者规范，已核可达）；Lahtinen, "When do betting odds best represent the actual outcomes?"（Economics Letters 系，检索摘要）；Dmochowski, "A statistical theory of optimal decision-making in sports betting"（2023，检索摘要）；Galekwa et al., "A Systematic Review of Machine Learning in Sports Betting"（arXiv:2410.21484, 2024，已核存在） |

### H. LLM 时代体育预测

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| LLM 预测能力 | 前瞻式（防污染）基准：LLM 可与传統 ML 打平（~0.57 准确率量级）但不稳定胜过赔率基准；带/不带检索、提示策略、预测视野是主要变量 | H6「LLM 信息差」前提死后关闭（S6 仅观测），方向与文献一致。**判定：已覆盖（关闭正确）+ 不适用（执行面）**；保留一项便宜的：把 LLM-benchmark 文献当 S6 观测面的信息差监测源，不产探索票 | LLM-SoccerArena（arXiv:2607.24573, 2026，检索摘要；引用了 Ley 系）；WorldCupArena / WC2026-Agents 系（2026，检索摘要）；GPT-4 vs LightGBM 对比研究（0.57 vs 传统最优，检索摘要，未取得稳定出处——如实标注） |

### I. 回测样本加权/时间衰减

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| 时间衰减权重 | AFML：按 uniqueness/时间衰减半衰数加权训练样本；结构断点数据降权而非等权 | 模型侧 DC 原生带 δ 衰减（Dixon-Coles 1997），但 EM 扫描与十年回测读数无衰减敏感性报告；PSI 漂移唤醒（E-36）已是断点监测。**判定：缺而便宜**——EM/DC 重拟合报告附半衰期（如 2–3 季）敏感性一栏 | López de Prado, AFML ch.4/7（2018）；Dixon & Coles 1997（原生时间衰减）；Winkelmann et al. 2024（模拟证据：FLB 类偏差与市场无效性的连接，检索摘要） |

### J. 池面（parimutuel）与约束面

| 主题 | 基准 | 我方现状与判定 | 来源 |
|---|---|---|---|
| Parimutuel 理论与 FLB | 时机/信息聚合理论（O-S）；竞争市场下风险厌恶可生 FLB；韩国 Toto 型官营池呈**反向**（人群偏热门）——与 E-23「O-S 热门低估」活线同向 | E-26（parimutuel 目标函数反相关教训）+ 尾部派彩感知口径已落修订；官营池反向 FLB 文献给 E-23 重跑提供外部先验。**判定：方法参照已覆盖；执行面学术策略移植不适用**（35% 抽水+停售结构+税墙，E-25/E-31；滚存例外已单列） | Snowberg & Wolfers, QJE 2010；Whelan, Economica 2023（https://www.karlwhelan.com/Papers/EconomicaFinal.pdf，已核可达）；Ottaviani & Sørensen parimutuel 系；Annual Review of Financial Economics 2023 survey（https://www.annualreviews.org/doi/pdf/10.1146/annurev-financial-053122-021925，已核可达）；韩国 Toto vs bookmaker 池对照（检索摘要） |
| Kelly 与参数不确定 | 边际估计有噪时收缩注码（Baker & McHale）；分数 Kelly 是共识解 | **已覆盖**（E-28⑤ 分数 Kelly 保险丝 + E-36 收缩五级）；可选便宜加项= sizing 前 edge 向 0 的贝叶斯收缩注记 | Baker & McHale, "Optimal Betting Under Parameter Uncertainty"（已核 Semantic Scholar 收录）；Chu, "Modified Kelly Criteria"（检索摘要） |

---

## 三、采纳建议（供票 08 汇总人裁）

### 进 E-28（六步门）修订——均为措辞级，不动门结构

1. **② 分族校准证据**：补一句「校准证据=PAVA 点估计 + Venn-ABERS 区间宽度（区间过宽→该族只可探针规模）」。来源：Vovk & Petej 2014；Manokhin & Grønhaug 2026。
2. **⑥ 显著性校正**：把「按注册总尝试数校正」落成可执行式：BH 为主 + RW stepdown（同族相关时）+ DSR 式 max-z 折减报告（N=注册总尝试数）。来源：Romano-Wolf 2005/2016；Bailey & López de Prado 2014。
3. **⑤ Kelly**：注记「edge 估计不确定度→sizing 前收缩」（把分数 Kelly 的理由从经验升为成文规则）。来源：Baker & McHale。

### 进 E-32（EM-1 协议）修订

1. **边界 embargo**：切分边界丢弃特征窗半宽场次（防特征侧时窗跨窗泄漏）。来源：AFML ch.7。
2. **S1 挂 RW stepdown**：清 E-33 自认的未校正债（21 段×逐日）。来源：Clarke 2019 实现。
3. **log loss 第三指标**进 S2 与报告判读分层（RPS 之争的对冲）。来源：Wheatcroft 2021。
4. **时间衰减敏感性**：EM/DC 重拟合附半衰期敏感性栏。来源：AFML ch.7；DC 1997。
5. **PBO 后验读数**（条件采纳）：若 196 条试验账可复原逐切片时间序列，加一个报告层 CSCV-PBO 元指标；数据不全则记为待办不动协议。

### 进未来新协议（幸存者出现后/β 探针基建后）

- **σ_CLV 实测 + CLV→ROI 本地映射校准**（E-30 复盘自认的信用缺口；学术文献支持方向但不给本地参数）。
- **Hansen SPA 检验管线**（最优幸存者 vs flat-k 基准的正式 data-snooping 检验；2026-10-22 已裁 A 线，建议随首个幸存者候选一起落地）。
- **conformal 弃注层**（研究场先验证覆盖保证，再谈执行面）。
- **多幸存者组合管理层**（§5.4 雾；等首批幸存者）。

### 明确不动项（对照后反而加确信）

- 三窗一次性不换成 CPCV/多路径（Li 2026：expanding walk-forward 已在因果前沿；Arian 2024：非平稳环境下 walk-forward 系更保守；多路径烧样本与预注册纪律冲突）。
- 海外 sharp 执行面、盘中方法、全速 Kelly、LLM 执行面：因 JC 可购面/税墙/停售结构不适用（E-25/E-29/E-31/E-08/09、H6）。

---

## 四、来源清单（含可访问性注记）

**已直接核对**（arXiv API / GitHub / 出版页 fetch 确认）：
- Vovk & Petej, Venn-Abers Predictors, arXiv:1211.0025 (2012/2014)
- van der Laan & Alaa, Generalized Venn and Venn-Abers Calibration, arXiv:2502.05676 (2025)
- Manokhin & Grønhaug, Classifier Calibration at Scale, arXiv:2601.19944 (2026)
- Petej & Vovk, Inductive Venn-Abers and related regressors, arXiv:2605.06646 (2026)
- ip200/venn-abers GitHub（README 含 IVAP/CVAP 与应用文献表）
- Li, The Impossible Trinity of Time-Series Validation, arXiv:2609.29530 (2026)
- Bailey & López de Prado, DSR, SSRN 2460551（页面存在）
- Bailey/Borwein/LdP/Zhu PBO-CSCV, Western Michigan ScholarWorks math_pubs/42（页面存在）
- Clarke, rwolf, IZA DP 12845, https://docs.iza.org/dp12845.pdf
- Pinnacle CLV 两篇（pinnacle.com/betting-resources）
- Whelan, Economica 2023 PDF；Annual Reviews 2023 parimutuel survey PDF
- Galekwa et al., ML in sports betting systematic review, arXiv:2410.21484 (2024)

**检索摘要确认存在、未逐字核对全文**（引用时以此级别为准）：
- Arian, Norouzi & Seco, Knowledge-Based Systems 2024（SSRN 4778909）
- Wheatcroft, JQAS 17(4) 2021（arXiv:1908.08980）
- Ley, Van de Wiele & Van Eetvelde, Statistical Modelling 2019；Van Eetvelde 博士论文 2023
- White 2000 Econometrica；Hansen 2005 JBES；Romano & Wolf 2005/2016；`arch.bootstrap.SPA` 实现
- Lahtinen（Economics Letters 系）；Dmochowski 2023；Hubáček/Šourek/Železný
- Johnstone 2024 conformal win probability；Kelly et al. 2023 conformal 框架
- LLM-SoccerArena arXiv:2607.24573（2026）；WorldCupArena 系；GPT-4 vs LightGBM 对比（无稳定出处）
- Snowberg & Wolfers 2010 QJE；Ottaviani & Sørensen 系；韩国 Toto 池对照研究
- Baker & McHale（Optimal Betting Under Parameter Uncertainty）；Chu Modified Kelly
- Dixon & Coles 1997；López de Prado AFML 2018（经典书目）

**未取得/未核实**（如实标注）：「Standardizing Venn-ABERS Predictors」未在 arXiv 检索到（疑不存在或为 SSRN 别名；实际存在的是 Manokhin & Grønhaug 2026 经验研究）；自适应 conformal-by-betting 2024-12 arXiv 编号未取得；GPT-4 0.57 准确率对比研究的稳定出处未取得。

---

## 五、结论摘要

方法栈对照结论：**无 P0 级结构性缺口**——预注册/三窗/多重校正/对照设计/CLV 外锚主判五根柱子都在国际最佳实践的前沿侧（CLV 门甚至领先文献通行做法）；差距集中在**实现债**（S1 未校正、DSR/RW 已裁未落地）与**自我声明的验证缺口**（σ_CLV），全部有便宜的落点。最大的「不采纳」决定（拒绝 CPCV 多路径）有 2024–2026 文献新证据反向支持。
