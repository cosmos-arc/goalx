# 02 外部数据源选型（≤$50/月）

Type: research
Status: resolved

## Question

在 ≤$50/月 预算下，赛事/赔率/历史/环境/舆情数据的最佳组合是什么？给出明确选型推荐（付费 + 免费组合），供 07（开通）与 08（管道原型）直接执行。

具体要回答：

1. **赛事与基础足球数据**：API-Football (api-sports)、Sportmonks、football-data.org、SofaScore（非官方）等的覆盖（联赛范围、球队/球员/伤停）、价格档位、免费额度，以及 xG 等高级数据的免费获取途径（如 Understat）。
2. **赔率数据**：The Odds API 的免费/付费档位（credits 计费模型）、覆盖的博彩公司与市场（胜平负、总进球、正确比分）、历史赔率快照能力；OddsJam/OddsPortal 等的可达性；重点评估「用欧洲主流公司（bet365/William Hill/Pinnacle 等）赔率近似竞彩盘口」的可行性。
3. **历史收盘赔率库**（回测的关键输入）：football-data.co.uk 的覆盖范围（联赛、字段、更新节奏）、有没有更好的免费/低价替代。
4. **环境数据**：天气（open-meteo 等，赛场当天降水/风速/温度）、赛程密度/疲劳（可从赛程数据推导）。
5. **社区/新闻/舆情**：懂球帝、直播吧、虎扑的可编程性（RSS/页面），国际源（X API 成本、Reddit r/SoccerBetting、OneFootball、 injury news 源如 fantasy football sites）的可用性与成本。
6. **组合推荐**：给出 2-3 个候选组合（如「全免费」「$20 档」「$50 档」）并推荐其一，注明每项的数据质量风险与降级路径。

产出：选型结论 + 价格表 + 覆盖竞彩所涉联赛的核对（竞彩开售的联赛 ⊆ 数据源覆盖）。

## Answer

详见 [research/02-external-data-sources.md](../research/02-external-data-sources.md)。推荐组合（$19-49/月，预算内）：

1. **基础+伤停：API-Football Pro $19/月**（7,500 请求/天，全赛事全端点：fixtures/lineups/injuries/venue/weather；免费档 100 请求/天可作降级）。Sportmonks 因联赛数限制（€29 仅 5 个联赛）出局；football-data.org 免费档当赛程兜底。
2. **欧赔快照与 CLV：The Odds API 免费档（500 credits/月）起步**，按需升 $30/20K 档。覆盖 100+ bookmakers（Pinnacle/Betfair 等）h2h+totals，**无比分盘**（比分玩法靠自有模型 + 500.com 欧赔兜底）。
3. **回测：football-data.co.uk 免费 CSV**——实测含 8 家开盘价 + **Pinnacle 收盘价（PSCH/PSD/PSCA）** + 大小球 2.5 开/收盘 + 亚盘，即 CLV 代理线黄金基准；缺口是亚洲赛事，竞彩主流联赛可接受。
4. **天气：open-meteo**（非商业免费、无 key，降水/风/温度全量）。
5. **舆情：Reddit r/SoccerBetting RSS（实测 200；.json 已 403）+ 直播吧/虎扑/懂球帝页面（均实测 200）**；X API 约 $100/月超预算不接。
6. 覆盖核对：竞彩开售联赛 ⊆ API-Football 全赛事 ✓；The Odds API 覆盖主要欧洲联赛 ✓，小联赛用 500.com 欧赔兜底。时区坑（UTC/北京/当地）记入票 08 原型验证项。
7. 全免费降级路径成立：API-Football 免费 100/天 + The Odds API 500 credits 只拉 Pinnacle h2h——功能不残废，仅刷新率低。

（注：本票 subagent 两次因模型内容过滤中断，主会话接手完成；定价核实日 2026-09-12。）
