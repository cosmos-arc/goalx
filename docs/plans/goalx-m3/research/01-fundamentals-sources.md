# 研究 01：基本面与情报源调研对比（M3 票 01）

> 调研日期 2026-09-19。API-Football 实测 8 次请求（本机 curl + `.env` 真实 key，配额纪律：个位数）。
> 前置复用：旧图研究 02（`.scratch/goalx-quant/research/02-external-data-sources.md`，2026-09-12）、研究 13（`13-supplementary-data-sources.md`，2026-09-12）。

## 一、结论速览

1. **实测推翻票面核心假设**：API-Football 免费档被 **season gate 锁死在 2022–2024**——当季（2026）injuries / standings / fixtures 全部被拒（错误原文见下）。免费档 100 次/天**只能做历史回填，无法服务当季彩池情报**。
2. **联赛覆盖不设限**：彩池混编联赛 league id（荷甲 88 / 葡超 94 / 英冠 40 / 土超 203 / 欧冠 2 / 巴甲 71 / 日职 98）全部有效，付费档全联赛可用；免费档在 2022–2024 窗口内也已实测正常返回（荷甲 injuries 1884 条、葡超 standings 全量含 form）。
3. **参数级 free/paid 边界**：`last` 参数免费档被拒（实测）；推断 `next` / `live` 同属受限实时参数（未实测，标注）。
4. **Understat**：仅六大联赛（五大 + 俄超），无任何彩池混编联赛；不进主力，留作 Tier1 五大联赛 xG 补充（v2 方向，沿用旧图）。
5. **open-meteo**：非商业免费 10k 次/天、无 key，进主力情报面（环境特征，优先级低于伤停）。
6. **中文舆情**：zhibo8 / 懂球帝 / 虎扑今日实测 200 可达，可行但**不进主力管道**——scout 线用 GLM web-search（票 02 定）替代。

## 二、API-Football 实测记录（2026-09-19，共 8 请求）

响应头实测配额：`x-ratelimit-requests-limit: 100`（天）、`x-ratelimit-limit: 10`（分钟）——与官方定价页一致。

| # | 请求 | 结果 |
|---|---|---|
| 1 | `GET /injuries?league=88&season=2026` | ❌ `{'plan': 'Free plans do not have access to this season, try from 2022 to 2024.'}` |
| 2 | `GET /standings?league=40&season=2026` | ❌ 同上 season gate |
| 3 | `GET /fixtures?league=88&season=2026` | ❌ 同上 season gate（**fixtures 端点同样锁**） |
| 4 | `GET /fixtures?league=88&season=2024&last=2` | ❌ `{'plan': 'Free plans do not have access to the Last parameter.'}` |
| 5 | `GET /fixtures?league=203&season=2024&last=2` | ❌ 同上（土超 league id 203 本身被接受，错误仅涉参数权限） |
| 6 | `GET /lineups?fixture=`（脚本取 id 失败导致空参） | ❌ 无效请求（我的瑕疵，消耗 1 次；lineups 形状未实测成，配额纪律不再补测） |
| 7 | `GET /injuries?league=88&season=2024` | ✅ **1884 条**。形状：`player{id,name,photo}` + `type:"Missing Fixture"` + `reason:"Injury"` + `team{...}` + `fixture{id,date:UTC}` + `league{...}`——伤停与停赛（reason 区分 Injury/Suspended）逐场逐人覆盖 |
| 8 | `GET /standings?league=94&season=2024` | ✅ 葡超全量积分榜：`rank/team/points/goalsDiff/group` + **`form`（近况 W/D/L 串）** + home/away 分解——近况信号直接可用 |

要点：

- **season gate 是 plan 级而非端点级**：injuries / standings / fixtures 三端点对 2026 全拒，错误文案一致。
- 免费档历史窗口（2022–2024）内标准参数（`league+season`、`date`）可用，且一次请求可拉全季（1884 条不分页）。
- lineups 文档上按 `fixture` 必填查询，官宣阵容通常开赛前 20–60 分钟可得（文档口径，未实测）。

## 三、API-Football 定价与免费/付费边界

| 档位 | 价格 | 配额 | 备注 |
|---|---|---|---|
| Free | $0 | **100 次/天**（10 次/分） | **season gate：仅 2022–2024**；`last`（推断含 `next`/`live`）实时参数不可用 |
| Pro | **$19/月** | 7,500 次/天（300/分） | 全端点全赛季，配额宽裕 |
| Ultra | $29/月 | 75,000 次/天 | — |
| Mega | $39/月 | 150,000 次/天 | — |

- 来源：官方站快照（2026-03 更新口径）[api-football.com](https://www.api-football.com)、[api.market](https://api.market)、[highlightly.net](https://highlightly.net)；本日实测响应头。
- season gate 有第三方旁证：GitHub 项目 README 记录同错误并建议"换 2022 或升级"；LinkedIn 开发者帖记录"2026 被静默封锁"后弃用改选 football-data.org（[来源](https://github.com/Xoshbin/asyar-worldcup-extension)）。
- 免费 vs 付费字段边界小结：**不是字段级限制，而是"当季访问权 + 实时参数"两级门槛**。免费档在旧赛季窗口内字段完整（injuries 含 reason/type、standings 含 form 等均实测可得）。

### 彩池混编联赛覆盖核对

| 联赛 | league id | 免费档（2022–24 窗口） | 付费档（当季） |
|---|---|---|---|
| 荷甲 Eredivisie | 88 | ✅ 实测（injuries 1884 条） | ✅ |
| 葡超 Primeira Liga | 94 | ✅ 实测（standings 全量） | ✅ |
| 英冠 Championship | 40 | ✅（实测 2026 仅报 season 错，id 有效） | ✅ |
| 土超 Süper Lig | 203 | ✅（实测 id 被接受，仅参数权限错） | ✅ |
| 欧冠 UCL | 2 | 未单独实测（标准 id，覆盖口径同上） | ✅ |
| 巴甲 Serie A | 71 | 未单独实测（同上） | ✅ |
| 日职 J1 League | 98 | 未单独实测（同上） | ✅ |

结论：**覆盖问题的答案不是"是否含这些联赛"，而是"是否买当季访问权"**——全联赛无差别，season gate 一刀切。基本面情报面跟彩池全联赛走（情报超圈 ≠ 欧赔超圈，欧赔范围仍按 run-protocol v1.1 六联赛），成本在 Pro 档内一并消化。

## 四、Understat xG

- **覆盖**：仅 EPL / La Liga / Bundesliga / Serie A / Ligue 1 / RFPL 六联赛（首页实测枚举）；**无**荷甲/葡超/土超/英冠/巴甲/日职/欧冠。
- **时效**：EPL 页 season selector 已有 **2026/2027** 当季，数据在线更新（联赛页实测）。
- **获取方式**：无官方 API；页面内嵌 JSON（`datesData` / `teamsData` / `playersData`，本票未再解析验证）；官网现提供 **CSV/JSON/XLSX 下载**（新发现，比爬页面友好）；社区包 `understat`（Python async）/ `understatapi` 成熟。
- **判定**：**不进主力**——覆盖与彩池混编联赛正交，只对五大联赛有效。保留旧图定位：Tier1（五大+欧战）analyst 线的 xG 补充，v2 再接。配合研究 13 的 FBref/Opta 下架风险注记，Understat 反而是免费 xG 里最稳的一个，但只稳在五大。

## 五、open-meteo 天气

- 免费档（Open-Access）：**无 key、600 次/分、5k 次/时、10k 次/天、300k 次/月**；仅限非商业用途，商用需订阅（Standard 1M/月起，价格需询价）。来源：[open-meteo pricing](https://open-meteo.com/en/pricing)、[docs](https://open-meteo.com/en/docs)。
- 预报 7–16 天（GFS 16 天 / ECMWF 15 天）；`past_days` 0–92 回看 + Historical Forecast API，可回测（时点质量可控）。
- **判定：进主力情报面**。零配额压力、无 key、可回测验证；优先级低于伤停/阵容（足球天气弹性小），作总进球/平局先验修正特征。注记：goalx 若未来产生真金收入，需转商用订阅。

## 六、中文舆情/门户（可行性结论）

- 今日实测（浏览器 UA）：zhibo8 200（491KB）、dongqiudi 200（354KB）、hupu 200（590KB）。旧图（2026-09-12）同 200；懂球帝移动 API 半公开无文档。
- **结论：可行、不进主力管道**。理由：页面结构变动维护成本高、反爬风险中等（500.com 有封禁前科）、信息噪声大。scout 线（票 02）用 GLM web-search 可覆盖大部分需求；需要页面素材时按次抓取（zhibo8 伤停聚合最快），不做常驻爬虫。X/Twitter $100/月超预算，维持放弃。

## 七、已集成国内源复用（零新增成本）

- **源B okooo**：专家共识/人气投注比例（任9/14 公众注分布直接代理）+ 竞彩全玩法——已集成，作情报输入直接复用，含伤停聚合页可作 API-Football 伤停的对照源。
- **源D 赛果**：已集成，开奖事实层不动。

## 八、对比表（字段 / 覆盖 / 配额 / 成本 / 时点质量）

| 源 | 字段 | 联赛覆盖 | 配额 | 成本 | 时点质量 |
|---|---|---|---|---|---|
| API-Football Free | 伤停/停赛、阵容、积分+form、赛程赛果（**仅 2022–24**） | 全联赛 | 100/天（10/分） | $0 | 历史数据，无当季 |
| API-Football Pro | 同上全端点全赛季 + 天气/odds/predictions | 全联赛（彩池混编全覆盖） | 7,500/天 | **$19/月** | injuries 逐场滚动；lineups 开赛前 20–60min；standings 赛后即时；可存证快照 |
| Understat | xG/xGA/PPDA/xPTS，射门图 | 仅五大+俄超 | 无配额（页面/下载） | $0 | 赛后数小时，无赛前价值 |
| open-meteo | 温/风/降水/湿度，逐小时 | 全球（场馆坐标） | 10k/天免费 | $0（非商业） | 7–16 天预报临近收敛；92 天回看可回测 |
| 源B okooo（已集成） | 专家共识/人气分布/竞彩赔率/伤停聚合 | 竞彩开售范围 | 自有管道 | $0 | 随竞彩开售节奏，赛前持续更新 |
| 源D 赛果（已集成） | 开奖事实 | 竞彩范围 | 自有管道 | $0 | 开奖后即时 |
| 中文门户（zhibo8 等） | 新闻/伤停聚合/舆情素材 | 广 | 按次 | $0 | 实时，噪声大 |

## 九、推荐主力组合与配额分配（建议，用户裁决）

**推荐组合**：

| 层 | 源 | 成本 | 角色 |
|---|---|---|---|
| 当季基本面主力 | **API-Football Pro** | **$19/月** | 伤停/阵容/积分/近况/赛程，全彩池联赛；快照入 quote_observations 式存证 |
| 环境层 | open-meteo | $0 | 场馆天气特征 |
| 国内层（复用） | 源B okooo + 源D | $0 | 专家共识/公众注分布 + 开奖事实 |
| 舆情层 | GLM web-search（票 02）+ 按次页面抓取 | 并入 GLM 预算 | scout 素材，无常驻爬虫 |
| 不进主力 | Understat（留 Tier1 v2）、FBref/WhoScored/SofaScore（放弃）、X | — | — |

预算核算：$19（API-Football Pro）+ GLM（票 02 估算）≤ $50/月 约束，需票 02 合并核算确认；若 GLM 逼近上限，API-Football 免费档降级路径=只做历史回填 + 当季情报改用源B 页面伤停（工程成本转爬虫维护，不推荐但可行）。

**配额分配**：

- **Pro 档（7,500/天）**：非比赛日 ≈5 次/天（`fixtures?date=` 1 次拉当日全联赛赛程+比分 + 天气/重试）；彩池轮比赛日 ≈40 次/轮（injuries 按联赛 ~10 + lineups 按场 14·开赛前 60min + standings 周更 ~10 + fixtures 按日期 3–5）。余量 >95%，可支持逐场 statistics 拉取与重试。
- **免费档（100/天，仅历史回填情景）**：每联赛-赛季基础三件套（fixtures + injuries + standings）≈3 请求 → 每天回填 ~30 个联赛-赛季；2022–2024 三季 × 彩池 7+ 联赛约 1–2 周完成回测库。当季情报不指望免费档。

## 十、缺口清单

1. **当季数据免费不可得**（最大缺口，实测定案）：免费档 season gate 2022–2024；当季情报必须 Pro $19/月或改用源B 页面。
2. **彩池小联赛 xG 缺位**：Understat 不覆盖荷甲/葡超/土超/英冠/巴甲/日职/欧冠；免费源无解（FBref 403、WhoScored/SofaScore 放弃），xG 特征只对五大联赛可得。
3. **实时参数免费档受限**：`last` 实测被拒；`next`/`live` 未实测，按文档同属付费（升级后无此问题）。
4. **射门级/尖值统计成本**：`fixtures/statistics` 按 fixture 计请求，免费档不可负担，Pro 档无压力（属方案内非缺口，升级即解）。
5. **中文舆情无稳定结构化 API**：反爬与结构变动风险，只能按次抓取或走 GLM web-search。
6. **open-meteo 商用边界**：真金收入产生后需转商用订阅（当前非商业使用无风险）。

## 来源

- 本日实测：8 次 API-Football 请求（curl，错误原文与响应头已录于上表）；zhibo8/dongqiudi/hupu 可达性。
- [api-football.com](https://www.api-football.com)（定价快照 2026-03：Free 100/天、Pro $19 7,500/天、Ultra $29、Mega $39）
- [Xoshbin/asyar-worldcup-extension](https://github.com/Xoshbin/asyar-worldcup-extension)（season gate 错误第三方旁证）
- [understat.com](https://understat.com/)（联赛枚举、2026/27 在线、CSV/JSON/XLSX 下载）
- [open-meteo pricing](https://open-meteo.com/en/pricing) / [docs](https://open-meteo.com/en/docs)（免费配额、非商业条款、past_days）
- 旧图研究：`.scratch/goalx-quant/research/02-external-data-sources.md`、`13-supplementary-data-sources.md`（okooo 集成、FBref/WhoScored/SofaScore 判定、门户 200 实测）
