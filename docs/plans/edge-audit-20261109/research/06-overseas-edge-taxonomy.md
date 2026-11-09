# 06 海外 edge 存活谱系与翻译矩阵（research 笔记）

票：`issues/06-overseas-edge-taxonomy.md`｜日期：2026-11-09｜方法：网络检索（WebSearch/WebFetch，2020s 证据优先，经典文献作量级锚）。搜索边界：见 §5 缺口注记；无法核到原文的结论一律降级标注。

JC 执行面约束（翻译筛）：仅赛前、无对冲/交易所/lay、单一运营商（无书间套利）、had 抽水约 13%（overround 1.1293）、传统足彩 14 场池 takeout 35%+滚存、>1 万奖金 20% 税。全部量级一律过抽水/税后口径（账本 §1 扣水纪律）。

---

## 1. 谱系表：海外有证据存活的 edge 类别

| # | 类别 | 海外证据强度 | 代表证据与海外量级（gross 口径） | JC 可翻译性 | 数据前提 | 过抽水后预期量级 |
|---|---|---|---|---|---|---|
| a1 | 池面 FLB（热门低估） | **强**（理论+多市场实证） | O-S 均衡：知情者后下注→终局份额低估热门（Ottaviani & Sørensen 2003/2010）；韩国 Sports Toto 足球池实证热门欠买/冷门超买（Chung & Hwang 2010，34 池 86 场 EPL）；马会经典 FLB（Snowberg & Wolfers 2010：随机下注 −23%，极端冷门期望 −60%，热门近打平） | **低**（结构真实，但 JC takeout 35% 吞噬） | 真终局资金份额（S5+票 68） | 偏离 ~5pp ⇒ share/fair≈1.05–1.10 ≪ 1.5385 门槛 ⇒ **净 −31%~−33%**；十年实测 max 1.4916（E-22） |
| a2 | 滚存 EV | **中**（lotto 强、足球池弱） | lotto 数学：滚存可把单注 EV 推正（Thaler 1988 综述；Huerta 2025：$5 票需 $1.1B 头奖打平；税后口径需 $2.6B+，Ganel 估算）；racetrack parimutuel「惊人地有效」、place/show 正 EV 在 1986 修正后基本消失（Asch, Malkiel & Quandt 1986） | **低** | 十年官方彩果 prize_tiers（已回填 1,624 期） | 我方 E-25 十年 1,623 期税后 +0.0942±0.0691 z=+1.36 未过门；一等奖贡献 ~10⁻⁶×注入量级 ⇒ **单独不可用** |
| a3 | 马会 syndicate/exotic（quaddie 等） | **中**（survey+轶事） | 澳洲 quaddie 价值取决于池规模与构成（racing 媒体 2022，轶事级）；HKJC 混池 takeout ~15.05%+职业 syndicate 密集，被公认接近强式有效（行业+学位论文）；Ziemba 2020/2023 survey：syndicate、交易所返佣（rebate 战争）与基本面信息是活下来的三要素；Benter 系统为传奇案例 | **无** | — | syndicate 经济的前提=低 takeout+返佣+高池深；JC 35% takeout 无返佣 ⇒ **结构性出局** |
| a4 | 份额 vs 公允背离可交易性 | **弱→中**（阈值论证） | O-S 理论背离存在；韩国池实证背离存在但小；马会大池被职业资金抹平 | **低** | 真终局份额 | 需背离 >53.8%（fair/share>1.5385）；海外足球池未见此量级背离的报告；我方十年 max 49.2% ⇒ **未观察到可交易背离** |
| b | 慢运营商吃价（stale line） | **强**（学术+行业数据） | 软书 5–8% margin、Pinnacle 动后软书陈价数分钟（行业共识）；书商 vs 交易所无风险套利出现在 19.2% 场次（Franck, Verbeek & Nüesch 2013，机制=书商调价慢）；实盘 value betting 收益：Trademate 公开样本 €1,501,172 利润、ROI 4.62% ≈ 4.6% 收盘 edge；RebelBetting 12 个月样本约 3.6% yield/注 | **无→条件性**（JC 不满足存活条件，见 §2-b） | 秒级鲜锚（Pinnacle 实时）+秒–分钟级执行 | E-09 直接对照：JC 面 −51.2%（z=−3.12）⇒ **净深度为负**；例外条件见翻译矩阵 |
| c1 | 换帅效应（new-manager bounce） | **强**（方向=均值回归） | 学术共识：bounce≈均值回归而非真效应（Balduck & ter Weel 2008：赛季中换帅无显著质量改善；Sousa 等：新帅效应 ~5 场内消失；CIES/UEFA 2025、The Athletic 2026 同向） | **中**（作为叙事定价偏差的测量轴） | TM 教练履历+中文名映射（§5.1 已列） | 效应量小（零点几分/场）⇒ gross 需 >13% 才过 had 门 ⇒ **测面可行、下注面预期不过门** |
| c2 | 裁判偏置（主场哨/红黄牌） | **强**（COVID 自然实验） | 无观众→客场判罚惩罚消失（Wunderlich et al. 2021，~190 引）；无观众即无主场优势（Nevill et al. 2022/2024）；补时偏置随观众消失（Kocsoy et al. 2025）；主哨效应 ~0.1 球级先验 | **中**（测量轴；E0 裁判 100% 在库、成本近零） | fd_referee 已在库（§5.2） | 0.1 球级 ⇒ ~1–3pp 概率位移 ⇒ **gross <13%，下注面单独不可用** |
| c3 | 赛程拥挤 | **弱** | 系统综述：身体输出效应含混、技术表现基本不变（Julian et al. 2020）；节奏再分配（Doncaster et al. 2023） | 低 | 无新增 | 预期无独立信号 |
| c4 | 收官轮战意/dead rubber | **弱/轶事** | 无同行评审定价研究；2026 世界杯行业文宣称「极端错价」（affiliate 级，轶事）；学术仅建模 tiebreaker 规则使 dead rubber 频率 +5pp（2026） | 中（可测面） | 赛程+出线状态机 | 未知；按轶事处理，**不作为先验** |
| c5 | 天气 | **弱**（足球） | EPL 分析：雨 +0.06 球、风 20–30km/h −0.03 球；另有宣称豪雨 −0.6 球的矛盾结果（affiliate 级）；强效应证据集中在 NFL 风 | 低 | 天气数据 | 分数球级 ⇒ <13%，**不可用** |
| d | CLV 作为决策/评估指标 | **强**（学术+行业闭环验证） | Štrumbelj 2014（收盘隐含=最有效概率）；其 2015 模拟：持续 beat 收盘线者长期盈利概率高；行业：Pinnacle 收盘线为最锐公开线（Pitcan 2026）；Trademate 4.62% 实测 ROI ≈ 4.6% 收盘 edge（CLV≈EV 的闭环证据） | **已采纳**（E-29/E-30，本条为外部背书） | 外部收盘锚（已在库） | 不产 edge，产**判定准绳**；σ_CLV 实测仍是待办（§4.1-8） |
| e | LLM/AI 预测与投注 | **中**（vs 零售市场）/ **弱**（vs 锐线） | ML 系统综述（arXiv:2410.21484，219 篇 2010–2024）：Stübinger 2019 集成 1.58%/场（47,856 场）、Stübinger & Knoll 2018 5.42%（8,082 场）、Wilkens 2021 无显著优势；LLM：Hou et al. 2024 LLM≈传统 ML（F1≈0.55）；WorldCupArena 微粒度分化；2025–26 Kalshi LLM 代理可盈利但机制=「错时亏得更少」利用行为偏差（OpenReview），非预测更准；LLM-superforecaster 持平预测 ~2026 底 | **低**（对 JC 13% 抽水面） | LLM 抽取管线（已建） | 对锐收盘无胜证 ⇒ 与 E-18 默认冲突面；唯一可辩护用法=软因素信息抽取（已试：E-12/E-13 refuted） |
| f | 平局偏好跨市场 | **中**（方向异质是结论） | UK 固定赔率：draw **欠买**（Pope & Peel 1989，Economica 56:323–341）；UK pools：休闲玩家**欠选** score draw（经典 pools 结构+打分奖励 score draw）；韩国池：经典 FLB（冷门超买）、平局向未单列；我方 E-24：JC 池平局**高买 +6.7pp**（≈8.1SE） | **已本地钉死**（方向反转） | S5 真终局份额复核 | 文献方向与 JC 相反 ⇒ **文献先验禁止进口**（账本 §3-13 已立规）；E-24 维持 |
| g | 池面组合/覆盖（dutching/组合 Kelly） | **中**（方法学） | Whelan 2023/2025：互斥多向联合 Kelly 比逐向独立 Kelly 更激进、需选最优子集 S₀；Real Kelly 算法/实现 | **高（作为 sizing 方法学）** | 任意未来幸存者的下注面 | 不产 edge；直接约束=池类 Kelly=0 帽（E-37 修订②）+尾部派彩感知目标（E-26） |
| h | 赛前价格形成与 late move | **中** | 交易所对进球近乎瞬时定价（Croxson & Reade 2014，in-play 基准）；赛前赔率存在**时间序列动量**（Vandenbruaene/De Ceuster/Annaert 2022/2023：17,380 场、30 书商小时级——移动延续而非反转）；1X2 市场显著 FLB 而 AH 市场无偏（Hegarty & Whelan 2023，252,690 注/84,230 场 2011–2022） | **低** | JC 自身轨迹+收笔时钟 | 动量在锐书空间为真 ⇒ 利用需在「未走完的书」下单= E-09 家族（JC 面已 refuted）；JC 收笔−开球中位 −2.7h 物理封死 late 窗（E-14） |

**谱系小结**：海外 2020s 仍存活的赚钱面集中在①软书吃价（结构=书商自愿做跟随者补贴 recreational flow，上限 ~2–5% yield，且被限额制度对冲）②低 takeout 大池 syndicate（HK/澳，JC 无此结构）③零售预测市场对 LLM 的行为偏差（JC 无此对手盘）。足球池面上，「结构存在但过不了 35% takeout」是全部池面条目的共同形态——与我方 E-22 阈值论证完全同构。

---

## 2. 翻译矩阵：edge → JC 对应物 → 前提 → 量级 → 冲突检查

| edge | JC 对应物 | 数据前提 | 预期量级（净） | 与已关线冲突检查 |
|---|---|---|---|---|
| a1 池面热门低估 | 14 场池反人群构票（热门重仓+降平局覆盖） | S5 真终局资金份额+票 68 官方十年注数 | 偏离不足：净 −31%~-33%（share/fair 1.05–1.10 < 1.5385） | **与 E-22 冲突（表面）**：E-22 钉死市场侧背离不存在。不冲突的例外路径恰是 E-22 自己留的口——「搏冷只在自家模型概率显著高于公众份额时才有意义」+§4.1-5 真终局份额缺失。即：E-22 关的是**历史快照份额**面；S5 真终局+官方注数落地后按注册门重判=E-23 活线的合法重开（与 E-23 directional +9.0pp z=1.32 一致，尚不过门）。**不推翻 E-22，只走其重开条件** |
| a2 滚存期 EV | 滚存期结构化票（反人群热门） | 已有 1,624 期 prize_tiers；缺当期份额 | 单独净 EV≈0（z=1.36 未过门）；一等奖 EV 贡献 ~10⁻⁶×注入 | 与 E-25 不冲突（E-25 自己指出组合出路）；与 E-23 组合=账本 §5.3 已登记的「滚存期反人群热门票」。**唯一理论组合面，维持待验** |
| b 软书吃价 | had 单关吃 JC 陈价（欧盘动 JC 未跟时下 JC「价值」侧） | PIT Pinnacle 锚（已建） | **−51.2%（z=−3.12）已实测**（E-09） | **正面冲突 E-09（已钉死）**。例外论证：海外存活条件=（i）慢书是**机械跟随者**（feed 滞后，Sportradar/BetGenius 类供给）而非自主定价者——JC 场均调价 4.2 次 vs 锐书 11 次、对球队新闻反应率 22.6%≈对照基率 18.6% 且方向一致率 50.0%=随机（E-12/H6），说明 JC 是**独立慢定价者**，其「不跟」本身携带信息（逆向选择），不是可吃的滞后；（ii）margin ≤8%——JC 13%，同一信号余量直接翻负；（iii）秒级执行窗——JC 收笔−开球中位 −2.7h， lineup 窗（−60~75min）在多数场次已收笔（E-14）；（iv）锚外独立更快信息源——E-13 已被 H6 杀前提。**四条件 JC 全不满足：无例外，维持关闭** |
| c1 换帅叙事差 | had 单关/hhad：换帅后 1–3 场 gap 面（人群是否高估新帅方） | TM 教练履历+身份映射+既有 gap 管线 | gross 预期 1–3pp < 13% ⇒ **下注面预期不可用；测面可行** | 与 E-11 facet C 无冲突（C 族未做换帅条件化）；与 E-18 冲突面=若宣称模型侧改善——限定为**市场反应差测量**（人群侧），不宣称模型胜市场即不触 E-18 反驳面 |
| c2 裁判主哨 | 概率面条件特征（不单独下注） | fd_referee 已在库 | 1–3pp 概率位移 < 13% ⇒ 仅测面 | 与 E-03 教训兼容（桶效应 1–2pp 数学上过不了 JC 下注面）；按 §5.2 已有排序登记为测量轴 |
| c4 dead rubber | had 单关 gap 面（收官轮战意不对称场次） | 赛程+出线状态机（可自建） | 海外无定价研究 ⇒ 未知；先验按轶事 | 无已关线直接冲突（未搜索过）；但量级先验受 E-03/E-11 约束（≤2pp 级桶效应过不了 13%） |
| d CLV 准绳 | 六步门③/S3/探针 CLV 双条件（已建） | 外部收盘锚（已建）；缺 σ_CLV 实测 | 不适用（非 edge） | 与 E-29/E-30 一致并获外部背书（Trademate 4.62%≈4.6%、Štrumbelj 模拟、Pitcan 2026）；**无冲突，反增信** |
| e LLM 预测 | 概率面/信息抽取（已试） | LLM 管线（已建） | 对锐线无胜证；Kalshi 类行为偏差面 JC 无对应物（JC 对手盘=13% 抽水而非可交易的零售错误报价） | **与 E-18 冲突（若作概率面）**：五面一致是默认反驳面；例外只能是「先过 CLV 探针门证明 beat Pinnacle close」——目前文献无任何 LLM 做到。与 E-12/E-13 冲突（信息抽取面已 refuted-by-prerequisite）：除非锚外信息源出现。**结论：LLM 面无新可翻译物，维持现状** |
| f 平局偏好 | 14 场池构票**降低**平局覆盖；S5 平局警戒带 +6.7±1.7pp | S5 真终局份额复核 | 文献（Pope & Peel draw 欠买）方向与 JC 相反——按 E-24 本地实测执行：平局覆盖=负 EV 放大器 | **正面冲突风险已由 E-24 处理**：海外固定赔率 draw 欠买 + UK pools score-draw 欠选 + JC 池 draw 高买 = 跨市场方向异质。翻译规则：**凡人群偏好类先验必须本地重验后使用**（§3-13），当前 JC 面取 E-24 方向 |
| g 组合 Kelly/dutching | 任意未来幸存者的 sizing 层；池面构票的双选/三选组合 | 幸存者出现（尚无） | 不产 edge；规则=互斥联合优化（Whelan）+池类 Kelly=0 帽+尾部派彩感知（E-26） | 无冲突；对 E-26 是理论补强（联合优化比逐向 Kelly 激进⇒更须受帽约束） |
| h 赛前动量/late move | had 单关顺移动量（B2 已测：−4.1%/−17.4%） | 已测 | JC 面**无信号**（E-10） | 与 E-03/E-10 一致；海外动量存在于锐书空间（30 书商小时级），JC 面不可交易的原因同 b 条（ JC=独立慢定价者+收笔封窗）。**无冲突** |

**翻译矩阵总判**：8 类中 JC 可直接翻译的只有 d（已建）与 g（方法学）；a1/a2 走已登记的 E-23/E-25 活线重开条件；b/h 在 JC 面被 E-09/E-03/E-10/E-14 四线合围，且海外存活条件（机械跟随者、低 margin、秒窗、锚外源）JC 逐一不满足——**E-09 的 −51.2% 不是异常，是该 taxonomy 预测内的结果**；c 族全部受「效应 <13% 抽水」封顶，只能做测量轴；e/f 无新可翻译物。

---

## 3. 候选方向清单（供 §5 登记人裁；每条可证伪）

1. **滚存期反人群热门票（E-23×E-25 组合，账本 §5.3 已列，此处给可证伪表述）**：滚存期真终局热门份额/fair 中位数比非滚存期低 ≥3pp，且分布上 5% 分位越过 1/0.65=1.5385 的期数占比 >0——判据需 S5 真终局份额 ≥100 期滚存样本；若越门期数=0 则池面组合面彻底关闭（与 E-22 合并终审）。
2. **平局覆盖权重函数**：构票目标函数中平局覆盖率设为份额−公允差的函数（E-24 实测 +6.7pp 为参数），可证伪面=若 S5 真终局份额显示高买消失（回到文献方向），本函数作废——本质是给 E-24 加漂移监测，非新 edge。
3. **换帅后 1–3 场市场反应差**：换帅方 JC gap（vs 公允锚）系统偏移 ≥2pp 且方向=人群高估新帅方（n≥200 场、三窗纪律）——只测不押（13% 抽水封顶已写在先验里）；数据=TM 履历+身份三级匹配（§5.1 成本清单已估）。
4. **裁判主哨校准直测**：E0/E1/SC0 主哨身份对主队犯规/黄牌/补时的条件偏差 ≥0.05 球级当量（对照=同队非该哨场次）——纯测量票，产出进特征库不进下注面（§5.2 已有框架，此处补判据）。
5. **dead rubber 战意 gap 探测**：已出线/已淘汰方 vs 有战意方的 JC gap 面系统差 ≥2pp（收官轮+杯赛小组末轮，n≥200）——海外无定价研究，先验标轶事级；探索性注册，预期死。
6. **σ_CLV 实测重跑 E-30**（§4.1-8 已挂账）：β 批次探针基建后实测 σ_CLV，重算 CLV 门功效——外部证据（Trademate 闭环）支持优先级；非新 edge。
7. **组合 Kelly sizing 规则**（方法学注册）：凡幸存者出现，单场互斥多向按 Whelan 联合优化+全局帽；池面按 E-26 尾部感知目标——现在登记防届时拍脑袋。

**不建议进入 §5 的**（本票排除项）：软书吃价一切变体（E-09 四条件合围）、LLM 概率模型对市场（E-18 默认反驳+无文献胜证）、天气/拥挤（效应量级不足）、quaddie/syndicate 结构（takeout 结构性不可比）。

---

## 4. 来源清单

**a 池面**
- Asch, Malkiel & Quandt (1986), J. Business 59(2):157–160（racetrack 效率+修正）
- Thaler (1988), JEP 2(2)（pools/lotto 综述，滚存 EV 量级锚）: https://pubs.aeaweb.org/doi/pdfplus/10.1257/jep.2.2.161
- Snowberg & Wolfers (2010)（FLB 量级：随机 −23%、极冷门 −60%）: https://www.nber.org/system/files/working_papers/w15923/w15923.pdf
- Ottaviani & Sørensen (2003 CEPR DP4092; 2010 AEJ:Micro)（知情后注→FLB）: https://cepr.org/publications/dp4092/DP4092.pdf
- Chung & Hwang (2010), Southern Economic J. 76(4):884–900（韩国 parimutuel 足球池 vs 书商：池内 FLB）: https://onlinelibrary.wiley.com/doi/abs/10.4284/sej.2010.76.4.884
- Ziemba (2020 LSE DP/2023 Annual Review of Financial Economics)（syndicate/返佣/行为偏差 survey）: https://ideas.repec.org/p/ehl/lserod/118873.html
- HKJC 混池 takeout ~15.05%（行业报道）；quaddie 价值随池构成（racing 媒体 2022，轶事级）
- Huerta (2025)/Ganel 估算（lotto 滚存打平线 $1.1B/$2.6B；轶事-科普级）

**b 慢书吃价**
- Franck, Verbeek & Nüesch (2013), Economica 80:300–325（书商-交易所套利 19.2% 场次）: https://onlinelibrary.wiley.com/doi/abs/10.1111/ecca.12009
- Trademate 公开样本（ROI 4.62%≈closing edge 4.6%）、RebelBetting（~3.6% yield/注）——厂商数据，中签级
- 软书 feed 滞后/限额实践（r/algobetting、Pinnacle 行业文，轶事级）
- Vandenbruaene, De Ceuster & Annaert (2022/2023)（30 书商小时级赛前动量：延续非反转）

**c 软因素**
- Balduck & ter Weel (2008)（换帅=均值回归）: https://wps-feb.ugent.be/Papers/wp_08_535.pdf
- Sousa 等（新帅效应 ~5 场消失）、Radzimiński et al. (2022)、CIES/UEFA (2025)、The Athletic (2026)
- Wunderlich et al. (2021)、Leitner et al. (2021)、Nevill et al. (2022/2024)、Kocsoy et al. (2025)（COVID 无观众自然实验：裁判偏置随观众消失）
- Julian et al. (2020) 系统综述、Doncaster et al. (2023)（拥挤效应弱）
- dead rubber：2026 世界杯行业文（轶事级）+ tiebreaker 建模（2026）；天气：EPL 分析（+0.06 球级，弱）

**d CLV**
- Štrumbelj (2014), Int. J. Forecasting 30(4):934–943 + 2015 模拟研究: https://www.semanticscholar.org/paper/8074edd44af6a4a64ab1fffee2642893d693720a
- Pitcan (2026)（Pinnacle 收盘=最锐公开线）；Pinnacle/Buchdahl 行业证据

**e LLM/AI**
- ML 系统综述 arXiv:2410.21484（2024，PRISMA 219 篇；Stübinger 系 1.58%/5.42%，Wilkens 2021 反例）: https://arxiv.org/abs/2410.21484
- Hou et al. (2024)（LLM≈传统 ML）、WorldCupArena benchmark
- Kalshi LLM 代理（2026，「盈利=错时亏更少」机制，OpenReview）；LLM-superforecaster 持平预测 ~2026 底（预测区间宽）

**f 平局偏好**
- Pope & Peel (1989), Economica 56:323–341（UK 固定赔率 draw 欠买）
- UK pools score-draw 欠选（经典 pools 文献+打分结构；二手综述级）
- Chung & Hwang (2010)（韩国池 FLB，平局向未单列）
- 对照我方 E-24（JC 池 draw 高买 +6.7pp≈8.1SE）

**g 组合/Kelly**
- Whelan (2023/2025)（互斥多向 Kelly 联合优化）: https://www.karlwhelan.com/Papers/EconomicaFinal.pdf 站内多向 Kelly 文
- Real Kelly 算法（Wikipedia Kelly criterion 多互斥结果节+实现）

**h 价格形成**
- Croxson & Reade (2014), Economic J. 124(575):62–91（交易所信息近瞬时）: https://academic.oup.com/ej/article-abstract/124/575/62/5076978
- Vandenbruaene 等 (2022/2023)（赛前动量）；Hegarty & Whelan (2023)（1X2 有 FLB、AH 无偏；252,690 注/84,230 场）: https://cepr.org/voxeu/columns/forecasting-soccer-matches-betting-odds-tale-two-markets

---

## 5. 缺口与诚实注记

- **Chung & Hwang (2010) 平局向**：摘要级信息只给 FLB 方向，平局份额未单列——引用时不得外推为「韩国池平局欠买」。
- **Quandt 1986**：可核到的是 racetrack 效率修正（Asch-Malkiel-Quandt）；票面所引「滚存 EV」在可核文献里属 lotto/Thaler 1988 支线，足球池滚存 EV 的直接海外实证**未找到**——我方 E-25 本身即是该问题最完整实证（如实标注）。
- **dead rubber 定价**：全部为轶事级（2026 世界杯营销文）；无同行评审定价研究。
- **中国池 draw 高买的海外平行案例**：未检索到公开英文文献直接研究 JC/中国足球彩票池份额结构——E-24 的反转在文献层面**孤立**，这正是 S5 真终局份额复核的价值所在。
- **厂商数据**（Trademate/RebelBetting ROI）：自报样本，选择偏差不可排除，用作量级锚（2–5%）而非精确估计。
- **LLM 文献时效**：2025–2026 结果多为预印本/会议，量级结论按「中签级」处理。

*研究子代理产出，2026-11-09。红线遵守：未改 map.md、未改账本、未改代码。*
