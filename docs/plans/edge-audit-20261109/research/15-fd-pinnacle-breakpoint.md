# 15 · FD Pinnacle 断点隔离 + 锚供给盘点（研究档）

2026-11-09 · 票 15（伞=票 10 缺陷 6）· 工作树 `goalx-ea15@edge-audit-20261109-15-fd-breakpoint`（零代码变更）。
输入：spec.md「FD 断点处置」、账本 §2 全 37 条、`research/external-audit-032324c.md` §四「有一个必须隔离的数据质量断点」、各源报告（路径随条目注）。
本档只登记不执行：换轨/装载/新源采购全是后续票；生产 CLV 锚不动；判决词处置留票 03/08。

## 一、FD 警示原文逐字引用（重新取自官方页）

取文日期 **2026-11-09**，URL `https://www.football-data.co.uk/data.php`（WebFetch 直接抓取官方页正文）。Pinnacle 相关警示全文逐字：

> ** Since 23/07/2025 Pinnacle's public API for odds delivery has become unreliable meaning their odds are systematically out of date relative to odds for other bookmakers, including both the pre-closing and closing odds. Consequently they should be used with caution when undertaking any betting analyses, and are no longer being included for the calculation of market average and maximum odds.

同页相关背景句（逐字，涉及口径理解）：

> The first set is collected after market opening at times specified on my fixtures page.
> The second set are the closing odds ('C' included the data column headings).

要点拆解（对照票 10 缺陷 6 的三个主张，全部坐实）：

1. **时点**：2025-07-23 起（"Since 23/07/2025"）。
2. **受害面**：Pinnacle 公共 API 的 pre-closing 与 closing **双陈旧**（"systematically out of date … including both the pre-closing and closing odds"）——FD 自家 `PS*`（非 C 列）官方定位本就只是 pre-closing，断点后连这个 pre-closing 也系统性滞后于其他书。
3. **成分变化**：Pinnacle **已从 market average 与 maximum 计算中剔除**（"no longer being included for the calculation of market average and maximum odds"）——`Avg*`/`Max*` 列（含 `Avg>2.5` 族与 `AvgAHH/AvgCAHA` 族）在断点前后成分不同。

## 二、断点边界与自有语料指纹

**赛季级干净分界**：FD 十一联赛（E0/D1/SP1/I1/F1/N1/E1/P1/T1/B1/SC0，`data/ingest/fdhist.py:39-51`）全部为秋季-春季赛制，2025-07-23 落在 2425 季（2025-05 完赛）与 2526 季（2025-08 开赛）之间的休赛期。**断点前=1617…2425 全部季；断点后=2526、2627 全部季；无季内劈裂**——隔离重算按整季剔除即可，无需逐场日期切。

**自有语料指纹**（断点在库内的经验表现，早于本审计已被注意到但未归因）：

- 票 51（`.scratch/goalx-quant/issues/51-pool-v2-out-of-sample.md:17,55-60,84-88`）：**2526 季 PSC 缺 53%、2627 季 Pinnacle 列全缺**，AvgC 全量；psc 证据窗=1920–2526 八个可用季；「两个异常 fx 之一在换源季 2526、fx 正值过半集中该季——尾部是噪声不是窗口」。
- 票 54（`.scratch/goalx-quant/issues/54-drift-replay.md:42-56`）：「分季九/十季为正（+2.8%~+14.7%），**仅 2526 换源季 −2.7% 且样本仅 360**」；「2526 换源季连早价信号都失真（唯一转负季）」。

两条报告的「2526 换源季/尾部噪声」观察现可统一归因为 FD 断点：方向正确的经验怀疑、缺失的因果注记——本档补上。

**受影响列清单**（`fdhist.py:114-134` 装载映射）：

| 列族 | 断点影响 |
|---|---|
| `psc_*`（Pinnacle 收盘 1X2） | 陈旧（系统性滞后）；2526 缺 53%、2627 全缺 |
| `psh_*`（Pinnacle 早价 1X2） | 同上（pre-closing 陈旧） |
| `avgc_*`/`avg_*`（市场均值 1X2 开/收） | 成分变化（剔除 Pinnacle）——**数值仍在但口径断层** |
| `avg_ou_*`/`avgc_ou_*`（OU2.5 均值，票 73） | 成分变化 |
| `ah_line/ahc_line` + `avg_ah_*`/`avgc_ah_*`（亚盘均值，票 73） | 成分变化 |
| 赛果/比分/统计列（FTR/HT/射门等） | 不受影响 |

## 三、受影响读数清单（账本 §2 全 37 条逐条排查）

排查方法：每条判决追到源报告的**锚列**（PSC/PAH 系=FD Pinnacle；AvgC/OU/AH 均值=FD 均值列）与**样本窗**（是否含 2526/2627 段）。已知嫌疑（E-02 尾季、E-07 确认窗 25/26 段）先行核验，其余逐条过。

### A. 受影响（需隔离重算或成分注记，判决词处置留票 03/08）

| # | 条目 | 断点后暴露段 | 影响面 | 处置建议 |
|---|---|---|---|---|
| A1 | **E-02**（H1 侵蚀差，refuted 方向反转）`07-h1-flb-erosion-gap.md` | avgc 主锚 2019-20…2025-26 七季（:20）**含 2526 全季**；JC 侧样本 2019+ 含 2026-08/09（2627 初段，:18 残差注记）；psc 锚同构读数（+0.90pp z=+5.6，:39）含 2526 陈旧行 | ①「逐季收窄 −5.8%→−4.5%→−2.3（2025-26）」（:50-51）的**尾季恰为断点季**——收窄可能部分来自 AvgC 剔除 Pinnacle 后均值钝化，而非「JC 贴价收紧」；②全局侵蚀差 −6.12% 与长赔带 −4.67%±0.12%（n=10,221）的断点后段贡献；③假想 EV −2.5%±6.2%（n=1,042）尾部 | **隔离重算**：2425 截止口径重出逐季+全局+假想 EV；若收窄在剔除 2526/2627 后仍在 →「JC 贴价收紧」解释保留，若消失 → 逐季段降级为断点伪影注记。报告已有 srct 锚同构面（不受断点影响）可作交叉验证 |
| A2 | **E-07**（EM-1 facet A，已搜索未发现）`09-em1-facet-a.md` | 三窗=搜索 2016-17…2021-22 / 验证 2022-23…2023-24 / 确认 2024-25…2025-26（:17-18）。**F1-F6 搜索窗点估计（德甲+74%、法甲+27%、Pin紧×<20% EV −66.4%、西甲 −38.7% 等）全部产生于搜索窗=断点前，不受影响**；但 ①had 结构面 32,889 行（三窗）的 gap 结构读数（长赔带 +1.7~1.8pp、60%+ 热门带 +9.9pp）含确认窗 2526 行；②**Pin松/紧三分位分类**（`09-em1-facet-a.py:115-142`，pin_dev=psc−avgc，**两列双双受断点影响**）应用于全窗行——2526 行的分类语义变化；③**F7 hhad AH 收盘（1,203 场整数线）与 F8 ttg OU 锚（27,844 篮子行）全窗落账**（:62）——avgc_ah/avgc_ou 成分变化段=2526+2627 | **隔离重算**：F7/F8 与 had 结构面按 2425 截止重出；F8 同时吃票 10 缺陷 1（ttg 7+ 结算）——**两缺陷必须同车重算**（先修结算再剔除断点段，避免两次扰动）；Pin松/紧切片的 2526 行分类重验。确认窗 25/26 段的嫌疑在 F1-F6 上**不成立**（0 候选→确认窗零消耗，:62）——嫌疑收窄为 F7/F8+结构面 |
| A3 | **E-06**（收盘有效性十年，锚合法性基础）`54-drift-replay.md` | 窗口 1920–2526（:29）**显式含 2526**；「仅 2526 换源季 −2.7% 且样本仅 360」（:42-43） | 收盘隐含 vs 实际命中的分季证据中 2526 段（n=360/107,262≈0.3%）基于陈旧 PSC——**该段读数无效**；主结论（<0.2pp 偏差）的质量主体在断点前 | **隔离重算**：剔除 2526 重出主表（结论预计不动摇）；「2526 换源季」注记升级为「FD 断点季」；早价前视教训段（EV_early>5% 组实得 +8.84%）确认不含 2526 |
| A4 | **E-22**（冷门十年，结构性出局）`51-pool-v2-out-of-sample.md` | psc 证据窗 1920–2526 八季**含 2526**（PSC 缺 53% 段）；2627 仅入 avgc 分期（:88）；「两个异常 fx 之一在换源季 2526、fx 正值过半集中该季」（:84-85） | 主读数（23,169 冷门候选正 EV=0、max 1.4916、份额≈fair 锁步）的质量主体断点前；2526 段的 psc 缺失行与陈旧行、2627 段的 avgc 成分变化进入分季/分期分布读数 | **隔离重算（轻量）**：剔除 2526/2627 段重出 max ratio 与分季分布（结论预计不动摇）；「换源季」注记升级；REFUTED-级结论（正 EV=0）不依赖尾季 |
| A5 | **E-20**（H9 分族模型，refuted）`02-h9-model-families.md` | 分母 12,849 场=金表 2019-08…2026-07（avgc 1X2 + FD OU2.5 收盘在场，:12,16,19）**含 2526 全季+2627 零星** | 配对设计（两族同锚同窗）自控相对比较——Brier 打平判决稳健；但绝对校准读数（ttg 桶隐含 vs 实际）含断点后锚派生概率 | **注记级**：若票 03 功效审计发现两族差异接近门再触发 2425 截止稳健性复核；否则带成分注记即可 |
| A6 | **E-23/E-24**（H2/H3 份额校准）`03-h2h3-share-calibration.md` | 市场公允主锚=竞彩 sporttery had 快照去水（53/54 场，:21）**不涉 FD**；hist avgc 收盘副锚仅 **n=14**（:67） | REFUTED/directional 判决全部依赖主锚——不受影响；avgc 副锚若含 2526/2627 行则成分变化，但 n=14 仅佐证「三锚一致」 | **注记级**：三锚一致的 avgc 腿带断点注记即可，无重算必要 |

### B. 无涉（锚源或窗口在断点外，逐条留痕）

| # | 条目 | 无涉理由（锚源/窗口证据） |
|---|---|---|
| B1 | E-01（FLB 首扫） | 十年 JC 官方 SP × 赛果——JC 自有源，无 FD 列 |
| B2 | E-03（8 错价桶） | 桶特征=gold 的 `ou_close_line_med/ou_close_over_water_med` 等（`gold.py:204-207`）——**srct 轨迹派生的多书中位**，非 FD 均值列；`_lib.py:389` OU_BOOKS 全为 srct:ou:* |
| B3 | E-04（M2 同源镜像） | 结论已翻案为恒等式伪影（不依赖锚质量）；估值带工具读数（界宽 ≤1.3pp）若复用带 2425 截止注记即可 |
| B4 | E-05（market-λ 转正） | λ 反推用 `ou_close_*`（srct 派生，`market_lambda.py:253-269`）；1X2 侧对照的 psc/avgc（:100-101）含 2526 段但只作误差成本旁证（+0.0006）——注记级 |
| B5 | E-08（H4 调盘滞后） | 欧侧轨迹=`odds_change_event` **srct:1x2:177**（`08-h4-h6-historical.md:21`）——源T，非 FD |
| B6 | E-09/E-10/E-11（B1/B2-4/facet C） | PIT 锚=`_lib.py:26` PIN_1X2=**srct:1x2:177** 轨迹 as-of——源T；facet C 的联赛带/Elo 面同管线 |
| B7 | E-12/E-13（H6/facet D） | JC 调盘面 × LLM 事件；facet D「Pinnacle 已动」=srct 轨迹同款 |
| B8 | E-14（delay-audit） | `data/delay_survival.py` 用 `srct_closing_prob`（grep 坐实）——源T 收盘回放 |
| B9 | E-15（阵容线）/E-16（NewsShock） | JC 变价面；N2 市场位移=gold 临场切片（srct 轨迹，「gold 轨迹收盘 0/380」注记同源） |
| B10 | E-17（早锁否定） | **era 分界恰好保护**：psh_open:psc_proxy 对（n=18,067）=era1 2016–2023（`clv_decade.py:10`、decade spec:96「2016-2023 PSC 代理时代」）**全在断点前**；era2（2023/24+）走 q1h/q24h 轨迹=srct。断点 2025-07-23 落在 era2 内但 era2 锚不是 FD |
| B11 | E-18（模型五面）/E-19（GBM） | RPS skill 分 era（psc_proxy −0.0397 n=25,980=era1 断点前 / trajectory −0.0426 n=7,903=era2 srct，`decade-closeout.md:22,48`）；EV 实现率=JC 可买价；α/skill 的市场公平基准=同 era 分层列 |
| B12 | E-21/E-25/E-26/E-27 | 模型线收尾/池面/JC 投影——无 FD Pinnacle/Avg 引用 |
| B13 | E-28~E-31/E-33~E-37（方法论与运行面） | 见下「运行面核验」与 §五；E-30 σ=1.525 来自 JC had 收盘（JC 源）；S1=JC、S3/S4=srct、S5=池面 |

**清单小结**：37 条中受影响 6 处（A1-A6，其中需隔离重算 4 处：E-02/E-07/E-06/E-22；注记级 2 处：E-20、E-23/24），无涉 31 条（含 E-07 的 F1-F6 搜索窗点估计与 E-17/E-18 被 era 分界结构性保护两处重要阴性发现）。

## 四、生产 CLV 锚核验：「不经 fdhist」坐实（运行面无火警）

代码链（工作树 `goalx-ea15`，与集成分支同基 d5aa7b7）：

1. **决策冻结**：`betting/frozen.py` 模块 docstring（:11-13）——「执行对账：would_bet 记录的锁后漂移（p_sharp_close−p_sharp，**源T 主锚收盘经 srct_closing_prob 同口径**）」。
2. **对账注入缝**：`frozen.py:373`（`reconcile_frozen_decisions` docstring）——「close_provider(home, away, kickoff_utc, selection) → (p_close, 基准, sid)|None——**生产=clv.srct_closing_prob 同口径**（注入缝供测试）」。
3. **生产接线**：`tasks.py:1277-1291`——`_srct_close_provider(anchor)` 返回 `clv_mod.srct_closing_prob(duck_con, home, away, kickoff_utc, selection)`，喂给 `frozen_mod.reconcile_frozen_decisions(conn, provider)`。
4. **锚实现**：`evaluation/clv.py:106-129`——`srct_closing_prob` → `quote_evidence.srct_pinnacle_closing_triplet`（data 包语料桥）→ Shin 去水 → `(prob, "srct_1x2_closing", basis="srct_pinnacle")`。
5. **语料桥**：`data/quote_evidence.py:1361-1387`——读 corpus duckdb `odds_change_event`（源T 百家行 Pinnacle=srct:1x2:177），**资格线=published_at ≤ kickoff**，取最新一笔；库/视图缺席降级 None 不打断对账。
6. **fdhist 零引用**：`betting/` 与 `evaluation/clv.py` 全文无 fdhist/hist_matches 引用（grep 坐实）；fdhist 在 `tasks/flows/cli` 的引用仅为①周刷新赛果重导（`flows.py:79`）②CLI 导入命令③**三对账**（`cli.py:947`「fdhist×源T 场次 / **PSC×cid177** / understat×源T xG」——`data/corpus_gate.py:652` raw_psc 对照）。

结论：**生产 CLV 锚（源T srct:1x2:177 收盘回放）与 FD 断点零交集，运行面无火警**——票 10 缺陷 6 的「已知起点」核验成立。且既有 PSC×cid177 对账（corpus_gate）天然是断点哨兵候选（见 §六 选项 B）。

## 五、Pinnacle 锚供给路径盘点表

| 路径 | 装载点 | Pinnacle 内容 | 覆盖 | 时间语义 | FD 断点影响 | 前向可用性 |
|---|---|---|---|---|---|---|
| **fdhist** | `data/ingest/fdhist.py` → `hist_matches`（sqlite）→ gold `match_features`（psc/psh/avgc + avg_ou/avg_ah，票 73） | 1X2 开/收（PSH/PSC）+ 市场均值 1X2/OU2.5/AH（Avg 族） | 11 联赛 × 1617–2627 季 | 源自报开/收盘快照（**无轨迹**） | **断点核心受害者**：PSC/PSH 陈旧（2526 缺 53%、2627 全缺）；Avg/Max 成分变化 | 断点后 Pinnacle 列**不可作锚**；Avg 列可继续装载但须成分注记；**前向角色=历史锚（≤2425）+ 赛果/统计列** |
| **源T srct** | corpus duckdb `odds_change_event`（srct:1x2:177；研究侧 `_lib.py` PIN_1X2 同款；AH 面走 cid3 最大书、OU 面 srct:ou:* 多书——Pinnacle 无 AH/OU 命名空间覆盖） | 1X2 全轨迹（收盘=as-of kickoff 末笔）；AH/OU 仅**非 Pinnacle** 主账书 | 主锚 ~44K 场；**2627 季部分未入库**（E-02 残差 :18 实录） | published_at（源自报）≤ kickoff 资格线，PIT 完备 | 无（独立管道） | **生产现役**（票 75 接管，basis=srct_pinnacle）；auto-v2 CLV 回填同款；2627 入库完整性待观察；**AH/OU 面 Pinnacle 真空是既有缺口**（非断点造成） |
| **odds_api** | `data/ingest/oddsapi.py`（票 20/35）→ `clv.py` 三级取锚（`PINNACLE_SOURCE="odds_api:pinnacle"`） | Pinnacle 1X2 快照（purpose=closing 窗口内） | 历史 clv_records 存量（close_basis=pinnacle 行） | observed_at 观测线（票 34） | 无 FD 断点影响 | **前向判死**：eu-odds-closing 面 2026-09-25 判死（`clv.py:11-13`「odds_api 三级对新注单必然空手」）；仅历史 basis 分列存量 |
| **propline** | `data/ingest/propline.py`（票 50 免费层，第二欧赔源互备） | 多书欧赔快照（**书单是否含 Pinnacle 待验证**——免费层响应未逐书盘点） | 免费层 1,000 req/日 | observed_at / source_updated_at（票 35 同款纪律，完整三向才可比） | 无 | 现役（互备/交叉验证定位）；**作 Pinnacle 独立副锚的第一候选**（书单验证=后续票） |

盘点结论一句话：**前向唯一健康的 Pinnacle 1X2 锚通路=源T srct（生产现役），fdhist Pinnacle 列已死、odds_api 判死、propline 待验——单源依赖是当前供给结构的核心风险**。

## 六、六步门③主锚前向定义建议（≥2 选项，供票 08 人裁）

六步门③=「CLV 审计（外部锚持续正）」（E-28）；E-29 已裁定主锚=Pinnacle 欧收盘、副锚=欧共识收盘双报。FD 断点击穿两条：fdhist PSC（主锚的历史供给）与 fdhist AvgC（「欧共识收盘」副锚的供给）——前向定义必须换轨。三个选项：

**选项 A：单锚现状化——主锚=srct_pinnacle（源T cid177 收盘回放），副锚缺位如实标注**
- 利：零迁移成本（生产已同口径，§四核验）；PIT 语义完备（published_at 资格线）；E-30 CLV 门快 364–1517 倍的通路立即可用；对账降级路径已内建（无锚诚实 skip）。
- 弊：单源单依赖——FD 事件正是「外部源无预警变质」的活例，源T 断供/改版/2627 入库缺口会让 CLV 门静默失锚（missing_close 只计数不告警语义化）；E-29「防自我强化/评分锚在系统控制之外」原则对**供给层**没有第二眼；「欧共识收盘」副锚无前向供给（fdhist AvgC 成分已变）。

**选项 B：双源主锚+断点哨兵——主锚=srct_pinnacle + 副锚=第二独立 Pinnacle 通路（propline 书单验证后优先，odds_api 复活次之），差异带告警**
- 利：抗单源断供（E-29 原则推广到供给层）；**断点可检测**——FD 断点潜伏数月才被外部审计抓到，而双源同场差异带会在变质当周显形；既有基建可复用：corpus_gate 的 PSC×cid177 对账（`cli.py:947`、`corpus_gate.py:652`）就是哨兵雏形，双源时代改为 srct×propline 同款对账即可；对齐 E-30 σ_CLV 实测对锚稳健性的要求。
- 弊：第二通路装载与书单验证是后续票工作量；PropLine 免费层是否真含 Pinnacle 未验（美式赔率转换/队名归一坑位已在票 50 记录）；两源「closing 时刻」的口径对齐（同为 kickoff 前末笔 vs 各自快照窗）需要一次冻结定义；若免费层不含 Pinnacle 则触发小额付费源评估（<¥2000/源/年档，票 07 域）。

**选项 C：分段口径——历史读数（≤2425 季）维持 fdhist PSC 口径不动，前向（2526 季起）一切 CLV 评价锚=srct_pinnacle，账本带「2025-07-23 断点」成分注记（=本档 §三清单）**
- 利：历史十年质量保留（E-06 收盘有效性主体在断点前成立）；前向供给独立于 FD；与本票回看处置（隔离重算受影响段）自然衔接、无回头重写；era 分界先例（E-17/E-18 的 psc_proxy|trajectory 分层）已示范分段口径的可记账性。
- 弊：跨段比较存在口径断层——「历史 PSC ≈ srct cid177」的等价性需一次性桥接验证（建议用 2019-2025 重叠期两源同场收盘对比做等价/偏移带，一次性票）；六步门③「外部锚持续正」的「持续」跨段时要分段陈述，不能混算一个通过率。

**建议组合（供票 08 参考，非决定）**：C 立即生效为记账口径（成本≈0，账本注记即本档清单）；B 作为供给韧性建设开后续票（哨兵复用 corpus_gate 对账）；A 是 C 的缺省子集。E-29 的「副锚=欧共识收盘双报」前向无 fdhist 供给，副锚定义建议随 B 重裁（第二 Pinnacle 通路或多书共识去 srct 化）。

## 七、不变量遵守声明

本票零代码变更（分支与集成分支同基 d5aa7b7，无 commit）；生产 CLV 锚未动；§三清单只登记读数影响与处置建议，判决词修订（E-02 refuted 是否加边界、E-07 措辞等）留票 03（功效审计）/08（合成人裁）；换轨/装载/新源采购（选项 B/C 的执行件）全部为后续票。

## 八、文件与证据指针

- 官方警示：`https://www.football-data.co.uk/data.php`（取文 2026-11-09，§一逐字）
- 生产锚链：`betting/frozen.py:11-13,373` / `tasks.py:1277-1291` / `evaluation/clv.py:106-129` / `data/quote_evidence.py:1361-1387`
- 装载：`data/ingest/fdhist.py:39-66,114-134,183-219`
- 语料指纹：`.scratch/goalx-quant/issues/51-pool-v2-out-of-sample.md` / `.scratch/goalx-quant/issues/54-drift-replay.md`
- 受影响读数源报告：`.scratch/autoresearch-20261107/reports/07-h1-flb-erosion-gap.md`、`09-em1-facet-a.md`（+ `09-em1-facet-a.py:115-142` Pin 切片）、`08-h4-h6-historical.md`、`10-em1-facet-bc.md`、`11-em1-facet-d.md`、`_lib.py:26,389`、`.scratch/edge-program-20261107/reports/02-h9-model-families.md`、`03-h2h3-share-calibration.md`、`docs/plans/backtest-decade/reports/clv-decade.md`、`decade-closeout.md`、`docs/plans/backtest-decade/spec.md:79,96`
