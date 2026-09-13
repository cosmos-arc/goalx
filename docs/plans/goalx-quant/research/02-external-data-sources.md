# 研究 02：外部数据源选型（≤$50/月）

> 调研日期 2026-09-12，定价以当日官方页面/检索为准。实测项用 curl 从本机验证（见文末）。
> （注：本票原 subagent 两次因模型内容过滤中断，主会话接手完成。）

## 一、逐项核实结果

### 1. 赛事基础数据

| 服务 | 档位 | 价格 | 配额 | 覆盖与要点 |
|---|---|---|---|---|
| **API-Football (api-sports)** | Free | $0 | 100 请求/天（10/min） | **全端点全赛事**（fixtures/lineups/injuries/odds/predictions/weather） |
| | Pro | **$19/月** | 7,500 请求/天（300/min） | 同上，量充足 |
| | Ultra | $29/月 | 75,000 请求/天 | — |
| **Sportmonks** | Free | €0 | — | 仅丹麦超+苏超两联赛（数据功能全） |
| | Starter | €29/月 | 2,000 calls/实体/小时 | **任选 5 个联赛**（功能全含 odds/predictions/injuries） |
| | Growth | €99/月 | — | 30 联赛（超预算） |
| **football-data.org** | Free | €0 | 10 calls/min | 12 顶级赛事（五大联赛+欧冠/欧锦赛/世界杯/荷葡巴冠），赛程+积分，无 odds |
| | Standard | €49/月 | 60/min | 30 赛事；odds 需另加 €15/月 |

来源：api-football.com/pricing（检索快照）、sportmonks.com/football-api、football-data.org/pricing（页面实测）。

**结论**：基础数据选 **API-Football**——唯一「全赛事 + 伤停 + 阵容 + 天气」且免费档就能跑通、$19 档即宽裕的方案。Sportmonks 联赛数限制（5 个）不适合任9/14 的广泛场次；football-data.org 免费档可当赛程兜底。

### 2. 赔率数据

| 服务 | 档位 | 价格 | 配额 | 要点 |
|---|---|---|---|---|
| **The Odds API** | Free | $0 | **500 credits/月** | 100+ bookmakers（含 Pinnacle/Betfair/1xBet/William Hill）；市场：h2h、totals、spreads；**无比分盘** |
| | 20K | $30/月 | 20,000 credits | 全 bookmakers + 历史快照（回溯至 2020） |
| | 100K | $59/月 | 100,000 | 超预算 |
| 500.com 欧赔 | — | 免费 | 页面 | 每场欧赔（多家）、亚盘；odds.500.com 实测 200 可达 |

来源：the-odds-api.com（页面实测）。

**要点**：credit 按「sport×region×market 一次拉全量赛事」计 1 次，500 免费 credits 对「每天 2-3 次快照 × soccer × eu 市场」勉强够（约 180/月），加 totals 翻倍就紧张 → 免费档起步、需要时升 $30 档。
**比分盘缺失**影响：比分/总进球玩法的市场基准靠自有模型（比分矩阵）+ 500.com 欧赔近似，不构成阻断（票 03 已给路径）。

### 3. 历史收盘赔率（回测关键输入）✅ 实测

**football-data.co.uk**：免费 CSV，无需 key。实测 2024-25 英超（`/mmz4281/2425/E0.csv`）表头包含：全场/半场比分、裁判、射门等统计，**8 家 bookmaker 开盘价**（B365/BW/BF/PS(Pinnacle)/WH/1XB + Max/Avg），**对应的 C 前缀收盘价**（B365CH/PSCH…），**大小球 2.5 开/收盘**（B365>2.5, P>2.5, Avg>2.5…），**亚盘**（AHh, B365AHH/PAHH/AvgAHH…）。覆盖主要欧洲联赛多赛季（英系至低级别）。
→ **Pinnacle 收盘价（PSCH/PSD//PSCA）即 CLV 代理线的黄金基准**，与票 03 的「Shin 去晦」结论衔接。缺口：亚洲赛事（中超等）不含——竞彩开售以欧洲主流联赛为主，可接受；个别缺口用 The Odds API 历史快照补。

### 4. 环境数据 ✅

**open-meteo**：非商业免费、无需 key；降水/风速/温度/湿度全量变量；7-16 天预报 + 92 天历史回看。赛场坐标可从 API-Football venue 拿。足够。
（API-Football 自带 weather 端点，Pro 档可用，可作对照。）

### 5. 社区/新闻/舆情 ✅ 实测可达性

| 源 | 实测 | 接入方式 |
|---|---|---|
| Reddit r/SoccerBetting | `/.rss` 200 ✅（`.json` 已 403） | RSS 解析，免费 |
| 直播吧 zhibo8.com | 200 ✅（页面 521KB） | 页面解析（新闻+伤停聚合快） |
| 虎扑 hupu.com | 200 ✅ | 页面/API 解析 |
| 懂球帝 dongqiudi.com | 200 ✅ | 页面；移动端 api.dongqiudi.com 半公开无文档，原型期试 |
| X/Twitter | 未实测 | Basic 档约 $100/月（历史定价，超预算）→ 不接，舆情用上述源替代 |

伤停/阵容一手源：API-Football injuries + lineups 端点（已含在基础数据订阅内），社区源做交叉与情绪信号。

### 6. xG 数据

**Understat**（免费，五大联赛+俄超+中超曾收录）：页面内嵌 JSON，社区爬虫成熟（penaltyblog 亦有封装）。v2 进阶才需要（票 03 的 v2 路线），先不接。

## 二、选型结论

**推荐组合（约 $19-49/月，预算内）**：

| 层 | 源 | 费用 | 用途 |
|---|---|---|---|
| 官方事实 | sporttery 网关 + 500.com（票 01 已实测） | $0 | 当期赛程/竞彩全玩法赔率/开奖/奖池 |
| 基础+伤停 | **API-Football Pro** | **$19/月** | fixtures/lineups/injuries/venue/天气 |
| 欧赔快照/CLV | **The Odds API Free 起步**（→$30/月 按需） | $0-30/月 | h2h+totals 多 bookmaker 快照；Pinnacle 为代理线 |
| 回测 | football-data.co.uk | $0 | 历史 Pinnacle 收盘 CSV |
| 天气 | open-meteo | $0 | 赛场环境 |
| 舆情 | Reddit RSS + 直播吧/虎扑/懂球帝 | $0 | LLM 情报线原料 |

- **全免费降级路径**：API-Football 降 100 请求/天（缓存+只拉竞彩场次）+ The Odds API 500 credits 只拉 Pinnacle h2h——功能不残废，只是刷新率低。
- **加钱路径**（如需）：The Odds API 20K（$30）→ 总计 $49/月顶格；Sportmonks 不建议（联赛数约束）。
- **覆盖核对**：竞彩常规开售联赛（五大联赛、欧冠欧联、荷葡、日韩、巴甲、偶尔中超/美职联）⊆ API-Football 全赛事 ✓；The Odds API 覆盖主要欧洲联赛+部分亚洲 ✓（个别小联赛无 h2h 快照，用 500.com 欧赔兜底）。
- 时区坑：football-data.co.uk 日期为当地时间、sporttery 为北京时间、API-Football 为 UTC——管道统一存 UTC（票 08 原型验证项）。

## 三、实测留档（2026-09-12）

```bash
# football-data.co.uk 历史 CSV（Pinnacle 收盘在 PSCH/PSD/PSCA 列）
curl -sL 'https://www.football-data.co.uk/mmz4281/2425/E0.csv' | head -1
# Reddit RSS 可用 / .json 403
curl -s -o /dev/null -w '%{http_code}' 'https://www.reddit.com/r/SoccerBetting/.rss'   # 200
# 中文源页面均 200：zhibo8.com / hupu.com / dongqiudi.com
# The Odds API / api-football 定价页当日核实（WebFetch/检索）
```
