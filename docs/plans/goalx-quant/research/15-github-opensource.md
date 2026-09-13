# 研究 15：GitHub 开源生态调研

> 调研日期 2026-09-12，`gh search repos` 实测（star/最近推送为当日快照）。

## 一、核心发现：没有同类系统，但有强力库生态

直接搜「football prediction model / soccer betting model」的端到端项目全部是 <10★ 的学生级仓库（多项式逻辑回归、基础 ML+ELO 之类）——**不存在成熟的「个人足球彩票量化系统」开源等价物**，我们要自建，但底座库很厚。

## 二、按用途分类（star / 语言 / 活跃度）

### 数据获取层（对 02/13 的最大增益）

| 仓库 | ★ | 语言 | 最近 | 用途 |
|---|---|---|---|---|
| **probberechts/soccerdata** | 2065 | Python | 2026-09-11 | **统一抓取 Club Elo / ESPN / FBref / football-data.co.uk / Understat / WhoScored**，自带缓存与限速——直接化解 FBref 反爬工程成本，v1/v2 数据管道的候选底座 |
| JaseZiv/worldfootballR（+ _data 81★） | 602 | R | 2026-08 | FBref/Transfermarkt 等的 R 生态标杆（参考其反爬模式） |
| amosbastian/understat（+understatr 86★, dataset 71★） | 185 | Python | 2026-08 | Understat xG 抓取的成熟实现 |
| martineastwood/penaltyblog | 220 | Python | 2026-09-10 | 票 03 已采纳（Dixon-Coles + 数据抓取） |

### 建模参考

| 仓库 | ★ | 用途 |
|---|---|---|
| **eddwebster/football_analytics** | 2776 | 最全的教程库（notebook 课程：数据工程、xG、Elo、Dixon-Coles）——v1 实现时的参考教材 |
| ML-KULeuven/socceraction | （票 03 已录） | VAEP/xG 价值模型，v2 |
| scibrokes/dixon-coles1996 | 8 | Dixon-Coles 原始复现参考 |
| diegopastor/awesome-football-analytics | 215 | 精选清单（发现新资源的索引） |

### 有趣的独立项目

| 仓库 | ★ | 用途 |
|---|---|---|
| Hicruben/theopenmodel | （新） | Elo+Dixon-Coles+蒙特卡洛，**每日发布可验证预测**（预测注册器概念）——与我们的「纸面跟踪」理念同构，可抄其公开预测存证设计 |
| HintikkaKimmo/surebet | 87 | 套利/价值扫描器（active 2026-08）——EV 扫描的参考实现 |

## 三、结论

1. **v1 直接依赖**：penaltyblog（建模）+ soccerdata（多源抓取底座，含 FBref 限速处理）+ football-data.co.uk CSV——零成本覆盖模型与数据两层。
2. **无需自研的部分**：FBref/Understat 反爬（soccerdata 处理）、Dixon-Coles 拟合（penaltyblog）、Club Elo 评分（soccerdata 拉取）。
3. **需要自研的部分**（也正是本项目的差异化）：竞彩/传统足彩官方数据接入、奖池型（滚存 EV + 覆盖优化）、双线对比、成本/价值预估、中文舆情情报线——开源界完全空白。
4. theopenmodel 的「可验证预测注册」值得纳入纸面跟踪设计（预测先存证、赛后对账，防事后修改）。
