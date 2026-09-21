# 数据源重评：Understat / 赛果源 / 费用（2026-09-20）

用户提问驱动：Understat 为何不用？caiguo 是最优且必须吗？费用现状与未来要求？业界系统与免费替代？
两路 sub-agent 实查 + 本机 curl 复核。结论先读，细节在后。

## 一、Understat：原"不进主力"结论部分过时，值得以最小形态引入

票 01 原判依据：① 覆盖仅五大+俄超，与彩池混编联赛正交；② 赛后更新无赛前价值；
③ RC4 内嵌解析麻烦。2026-09-20 实查：

- **②③已不成立**：联赛页 RC4 内嵌已撤，改纯 JSON AJAX `GET /getLeagueData/{league}/{season}`
  （首页取 PHPSESSID + Referer/XHR 头，~530KB 明文）；无 Cloudflare，Apache 直 200。
  赛中 5-10 分钟级更新 xG；**累计 xG/xGA 有赛前价值**（DC 攻防强度降噪）。
- **①仍成立**：仅 EPL/西甲/德甲/意甲/法甲/俄超 2014/15-2026/27——只服务 Tier1 五大。
- 学术支撑：Mead 2023（PLOS ONE）xG 显著优于进球指标；xG 增强 DC 优于纯进球 DC
  （statsandsnakeoil / opisthokonta）；价值集中早赛季小样本收缩。
- 附带白得：每场自带 `forecast{w,d,l}` 胜平负概率 = 免费对标基准模型。
- 风险：robots.txt 全站 Disallow（个人研究低频用）；无 ToS。

**最小引入形态**（若裁决通过）：每日 ≤10 请求拉五大 getLeagueData，落 xG 特征表；
早赛季用累计 npxG/npxGA 收缩校准 DC 攻防参数（或 xG 版/进球版概率加权）；
forecast 存对标基准。不碰逐射门/球员级。俄超按需。

**裁决（2026-09-20 用户定案）：开票引入**。价值认定比原案更宽：xG 是公开域
最强进球预测器（Mead 2023），DC 是进球分布模型——攻防参数全季受益（早赛季
边际最大、随赛季递减不清零）；ttg/crs/hafu/大小球等进球类玩法全部下游受益。
两条红线：特征表挂 fixtures 走确定性键；赛前可得性按定则 2 截断
（getLeagueData 赛后滚动更新，特征只能用决策时点前的累计）。

## 二、赛果源：源D 仍是最优可用，官方 webapi 今日实测被 WAF 拦（非 drop-in）

- 理论最优 = 体彩官方 `webapi.sporttery.cn/gateway/jc/football/getMatchResultV1.qry`
  （官方口径：无效场次/腰斩/改期判定，竞彩受注场次 100% 覆盖含亚洲赛事）。
  开源封装（Johnserf-Seed/SportteryAPI）称家宽可直连。
- **本机实测（2026-09-20）**：该端点 403"禁止访问"，与参数/头无关——同域的
  getMatchCalculatorV1（竞彩快照，已在用）同头正常。判定：端点级 WAF，
  非全域封锁。lottery.gov.cn 开奖公告页 200 可达（HTML，未深探）。
- 海外免费 API 全数不适合做事实源：sofascore 403 死、flashscore 重反爬、
  ESPN 韩K 覆盖损坏、football-data.org 免费档无亚洲、api-football 免费档
  season gate + 100/天。都只配做对账。
- 业界：开源生态全用 football-data.co.uk CSV，无"按中国彩票场次结算"先例
  ——官方口径需求是本场景独有，没有现成轮子。

**判定：源D 维持结算唯一事实源**（现状即当前最优解）；官方 webapi 列为
候选升级（需解 WAF：页面预热会话/浏览器代采，成本未证）；海外源仅对账不入管道。
**（§五 更新 2026-09-20 晚：uniform 族官方赛果端点同头直通，"需解 WAF"
前提已破——升级路径从"成本未证"变为"已实测可行"。）**

## 三、费用：现状边际≈0，预算约束内无新增必须项

真实台账（2026-09，cost_ledger）：
- llm_call 44 次 ¥0.14（影子价：Coding Plan 订阅额度内边际成本≈0；scout 36 次/pool 8 次）
- odds_api_credit 165 credits（免费档 500/月内，¥0）
- 爬虫源（澳客/新浪/源B/源D/fdhist）全 ¥0
- 真实现金支出 = GLM Coding Plan 订阅固定费（已付）

未来费用要求（≤$50/月既定约束内排队，全部"证据触发"不预付）：
- API-Football Pro $19/月 —— 仅当票 05 证明伤停时效有价值
- Grok X Search ≈$5.6/月 —— 同上，伤停盲区兜底低频口径
- Understat / open-meteo / 官方接口 —— ¥0
- 结论：无新增必须项；任何付费升级都有明确的触发条件与预算余量

## 四、开源数据集/项目盘点（2026-09-20 用户线索，GitHub API + raw 实测）

线索：openfootball / soccerdata / StatsBomb Open Data / awesome 地图 / Kaggle 历史库。逐项核实：

| 项目 | 实测（2026-09-20） | 内容 | 定位 |
|---|---|---|---|
| **openfootball/football.json** | ✅ 活跃（09-19 仍推送），CC0 | 静态 JSON：赛程 + FT/HT 比分，raw 直下无 WAF 无 key。2026-27 覆盖五大+荷甲+葡超+英冠 | **对账首选**：§二"海外源仅对账"的最好对账位（见下） |
| probberechts/soccerdata | ✅ 活跃（09-17 推送），自定义许可 | 统一封装 clubelo/espn/fbref/fd.co.uk/sofascore/sofifa/understat/whoscored → DataFrame | **不引入**：可用 reader 要么已自建（fdhist=match_history）、要么同样撞 WAF（fbref/whoscored/sofascore——包装器绕不开站点封锁）、要么已变简单（understat 纯 JSON，§一） |
| hudl/open-data（StatsBomb Open Data，已迁 hudl org） | ✅ 活跃（09-07 推送），非商用许可 | 事件级数据+xG，**仅选摘赛事**（世界杯/欧洲杯/特定赛季），非联赛系统覆盖 | **研究语料**：§一 DC+xG 方法论验证的免费素材，不入管道 |
| awesome-football / withqwerty/open-football | ✅ 存在（08-24 / 06-05 推送） | 免费数据集/爬虫工具地图 | 参考索引，留档 |
| European Soccer Database 等 Kaggle 历史库 | 未实测（需登录） | SQLite/CSV，数据止于 ~2016/17 | **弃**：fdhist 5 季收盘 CSV 更新鲜且已在管道 |

**football.json 质量实测**：2026-27 已踢场次比分回填 87-91%（约一轮滞后）；2025-26
完整赛季 3045/3048（法甲/荷甲各缺 1）。坑：`score` 字段 dict `{ft,ht}` 与 list
两种形态混用，解析需兼容。

**判定**（全部印证 §二：海外源仅对账/研究，不进结算管道——无中国彩票口径）：
- openfootball/football.json = 赛程/赛果对账源：sporttery 场次（match_codes）
  对账赛程、fdhist 对账比分；CC0 静态文件成本≈0，要用时直接取 raw，无需开票。
- 英冠（en.2）覆盖是附带增益——竞彩混编场次含英冠时 Understat 覆盖不到（§一之①），
  这里能对上。
- 其余：soccerdata 不引入（依赖换不来新数据）；StatsBomb 服务 §一 裁决后的
  方法论验证；Kaggle 历史库跳过。

## 五、竞彩官方 API 生态：uniform 族端点本机实测直通（2026-09-20 用户线索核实）

线索：官方未文档化 gateway（getMatchListV1 / getUniformMatchResultV1 等）+ 四个开源
封装。GitHub API + 本机 curl 逐项核实：

| 项 | 实测（2026-09-20） | 要点 |
|---|---|---|
| `uniform/football/getMatchListV1.qry?clientCode=3001` | ✅ 200（66KB） | 官方在售场次（实测含亚冠/澳超），赛程对账位 |
| `uniform/football/getUniformMatchResultV1.qry`（日期区间+分页 pageSize=30） | ✅ 200（当日 32 场/2 页） | **官方赛果+结算状态**，载荷见下 |
| Johnserf-Seed/SportteryAPI | ✅ 活跃（07-24 推送） | §二 已录；CF Worker+MCP，推导隐含概率/返还率/凯利 |
| excalibur-sa/football-lottery | ✅（03-13 推送） | Flask+小程序全玩法赔率/历史/导出；**uniform 端点参数出处** |
| 45274159-stack/sports-lottery-football-analysis | ✅ 活跃（**09-20 当日推送**） | 十年竞彩史审计库：40,842 场/14 赛事（2016/17 起），官方编号+固定奖金轨迹+伤停扩展；**数据集真在库内**（data/processed 61 文件 9.4MB CSV） |
| Jinbigbig/jinbet、qqyg000/lottery-football | ✅ 存在活跃（09-20/09-19） | 网易体育抓取 / Java 分析，生态样例留档 |

**赛果载荷实测**（2026-09-19 法甲 巴黎FC 2:1 斯特拉斯堡）：`matchNumStr="周六019"`
（竞彩场次号 → match_codes 一跳映射）、`sectionsNo1/999` 半/全场比分、
`poolStatus="Payout"`（**官方派彩状态**）、`winFlag="H"` 胜平负判定、h/d/a 赔率、
`goalLine` 让球线、`bettingSingle` 单固、分页字段齐全。

**对 §二 的修正**：§二 实测 403 的是 **jc 族** `getMatchResultV1`；**uniform 族
同域同浏览器头直通**——所谓"端点级 WAF"实为 jc 族赛果端点被拦，官方 webapi
本身开放。地域注记（用户线索，未验）：gateway 或有 IP 地域限制（海外机房需
代理）；本机家宽直通，serve 同机部署无碍。

**判定**：
- 官方赛果源升级路径**已验证可行**：getUniformMatchResultV1 提供源D（500 系）
  没有的官方口径——poolStatus 派彩状态、matchResultStatus（腰斩/无效枚举
  语义需一次映射深探）、竞彩场次号原生对齐。是否切换/并行 → 待裁决。
- 45274159-stack 十年竞彩史 = 彩池回测语料候选（**当晚补查翻案、撤销——见下方
  §五补查**）；
- 其余仓库按生态样例留档；500.com/澳客/网易 已在组合中（源B/源D/研究02）。

**§五补查（2026-09-20 晚，逐目录实测 raw CSV/JSON，推翻"十年竞彩史"定位）**：
- "十年 40,842 场"拆开看：top5 目录 `source=football-datasets`（= fd.co.uk
  搬运，**无赔率列**——fdhist 直连更新鲜且带 PSC/AvgC 收盘）；8 个扩展联赛
  （挪超/法乙/荷乙/德乙/葡超/美职/沙特/荷甲，无 J1/澳超/韩K）`source=
  footballcsv/cache.footballdata`（= openfootball 缓存搬运，CC0 本就计划直连）。
  十年部分无竞彩编号、无固定奖金。
- "官方口径"部分：`results/` 仅 2026-09-03 起日更；核验 = cpbao + 500 两个
  **第三方镜像互证**（非官方 webapi，弱于本机已直通的 uniform 族）；HT 比分
  常缺（实测鹿岛-浦和行 half_*=null）。
- "固定奖金完整变化轨迹"= 采集功能存在，但**从现在开始攒，无历史存量**；
  仓库无 LICENSE。
- **改判**：维持生态样例留档、**不入管道**（原"语料候选"撤销）。十年长周期
  回测语料的正确来源 = fdhist 扩 FD_COMPETITIONS + openfootball 直连
  （**提级为待开票项，见待裁决 3**）；十年竞彩固定奖金免费渠道不存在，
  唯一解 = 自源A 快照逐日 append-only 积累——daily capture 赔率快照不可断档，
  断一天永久少一天。可借鉴：其逐行 source_url+revision+observed_at 出处纪律
  （与定则 2/3 同构）。

## 六、赔率/统计源线索补充（2026-09-20 核实；多数已有判定，仅列增量）

用户线索两批（商业API + 可爬取/免费源）。已有判定不变的不展开，只记增量：

| 线索 | 实测（2026-09-20） | 判定 |
|---|---|---|
| Football-Data.co.uk（~25 联赛周更） | 在用（源3） | 无动作；与线索描述一致 |
| FBref 表格导出 | research/13 已测：WAF 403 + Opta 下架风险 | 不变 |
| Understat | §一已重评（改纯 JSON 直连） | 待裁决 1 |
| **Club Elo**（免费 CSV API 无 key） | ❌ **本机不可达**：api + 主站均连接层失败（HTTP 000），当日两次不同 scheme 复测同果 | Elo 对 DC 先验有价值（soccerdata 有 clubelo reader），但网络路径未通——引入前先解决可达性（复测/代理），暂挂 |
| **Odds Portal**（历史多 book 赔率） | ✅ 页面 200（787KB）；社区爬虫 gingeleski/odds-portal-scraper 活跃（07-10 推送，129★） | **挂候选不动作**：定位 = The Odds API 历史快照 $30 档的免费替代；但 fdhist 8 家收盘已够 CLV 基准，爬取工程成本现无对应需求 |
| S1M0N38/soccerapi（888sport/Bet365/Unibet） | 179★ 但 **2022-12 停更**（~4 年），书商改版大概率失效 | 弃（线索自注"可能失效"属实） |
| EasySoccerData（49★，05-14 推送）/ "Ryzellx/football-live-api"（精确名未搜到） | 存在性核实如左 | Sofascore/FotMob 系封装——§四同判定：包装器绕不开端点封锁（research/13：Sofascore 403 / FotMob 需逆向签名），不引入 |
| OpenFoot API / TheStatsAPI | GitHub 检索无可信公开服务（仅 0★ 或无关仓库） | 疑为讹传，不追 |
| API-Football / TheOddsAPI / Sportmonks / football-data.org | research/02 选型已定 | 不变：API-Football 挂伤停触发、TheOddsAPI 在用（165/500 credits）、Sportmonks 弃（5 联赛限制）、football-data.org 免费档无亚洲 |

**净结论**：本批无新增必做项——两个真新面孔（Club Elo、Odds Portal）一个不可达、
一个无当前需求，均挂候选；其余全部落入既有判定（02/13/§一/§四）。

## 七、点名三源复核（2026-09-20：Sportmonks / soccerdata / SportteryAPI）

用户点名再看三源。三路实查（官方定价页 + GitHub API/源码逐文件 + 同库复测），
**全部维持原判，无新增裁决项**：

| 源 | 实测（2026-09-20） | 判定 |
|---|---|---|
| **Sportmonks** | 定价结构未变（Free 丹超+苏超固定/Starter €29 任5/Growth €99 30联，09-12 research/02 记录仍准；pricing 页已迁 /football-api/plans-pricing/）。**唯一实质增量 = xG add-on（€19+/mo）覆盖 Understat 盲区**：J1(968)/澳超(1356)/荷甲/葡超/欧冠在 56 联赛 xG 名单，**K 联不在**；仅 2024/25 起两季、数据商不透明。伤停为核心功能但被联赛档位捆绑 | **维持弃**（02 判定不变）：5 联赛装不下竞彩混编 ~12 联；Extra leagues €4/个补到 12 联 = €57/mo 已超 ≤$50 预算且未含 xG。触发重评：预算放宽 ~€100 或推出按需低价联赛包 |
| **probberechts/soccerdata** | v1.9.1（07-24）后无真实代码活动（§四 "09-17 推送"实为 renovate 依赖 bump 分支）。2026 无新增 reader、净减 2（FotMob **应站方要求移除**、FiveThirtyEight 站死）。三条原理由仍成立且恶化：FBref/WhoScored 全面退到 seleniumbase 真浏览器 + **仅 GUI 可用的 CAPTCHA 求解**（无人值守管道不可用，headless 直接放弃）；sofascore reader 的 tls-requests 指纹伪装**今日同库复测仍 403**；understat 双方同向确认纯 JSON | **维持不引入**（§四 判定不变）。带走两条免费情报：① fd.co.uk 2024-25 起 CSV 编码 UTF-8-SIG——已核对 fdhist 处理正确；② 若未来救 sofascore，别投 TLS 指纹伪装路线（已被打穿） |
| **Johnserf-Seed/SportteryAPI** | 2026-06-19 一天成型（main 仅 4 commit 同日），07-24 pushed_at 为 dependabot 分支。全仓 grep 实证：**仅封装 calculator 端点，参数与 goalx 逐字同**（poolCode 五池+channel=c，iPhone UA+m. Referer）；对 jc 族 getMatchResultV1（我们 403 的）零接触，uniform 族两端点零知识。推导层 = 教科书比例归一去水+凯利指数（evaluation/baseline 已有等价物） | **维持生态样例留档**（§五 判定不变），无端点增量——反向成立：我们知道得比它多。留档补两条情报：① webapi 对 **datacenter IP（含 Cloudflare）回 EdgeOne WAF 567**，换头无效需中国可达出口中继——serve 未来搬离本机家宽时的选址先行情报；② `src/parlay.ts` 含官方 M串N 对照表（2串1…8串247、单注2元、封顶500万、had/hhad≤8关 crs/ttg/hafu≤6关木桶）——将来做过关票的核对参考 |

**净结论**：三源复核零翻案；§待裁决 1/2 不变。soccerdata 的 FotMob"应官方要求
移除"再添一条爬虫源寿命/合规风险佐证（对应 research/02 事实源策略：官方口径优先）。

## 八、落地工程约束（2026-09-20 用户定则，五条）+ goalx 现状对照

原则（用户原话要点）：落地最重要的不是爬虫数量，而是这几个工程约束。
逐条对照 goalx 现状（2026-09-20 查 migrations.py / models.py）：

1. **比赛映射不靠名称模糊匹配单打**。确定性键优先：来源 ID、赛事、赛季、
   双方球队 ID、开球时间、主客方向、中立场；模糊名称匹配只做候选生成，
   冲突进人工确认队列；禁止 Agent 自信合并同名青年队/女足/预备队/延期赛事。
   *现状：地基已对*——`match_codes` UNIQUE(kind, business_date, code) +
   source_match_id（竞彩场次号一跳映射）；`team_aliases` UNIQUE(source, alias)
   受控别名表；`fixtures.join_method` CHECK('time_window','manual')——人工
   确认已是枚举值。*落点*：§五 uniform 族 matchNumStr 与 openfootball 对账
   接入时，新源 join 走"确定性键→候选→冲突入 manual 队列"路径，禁 LLM 合并。
2. **区分三个时间**：event_time（事发）/ published_at（来源发布）/
   observed_at（本系统看到）。回测要证明决策信息在决策时确实可得——
   "今天下载的历史伤停名单"≠当年赛前认知；可信时间戳的历史快照可重建过去
   但保留依据；不能仅凭比赛日期早就认定关联字段可赛前用。
   *现状：两时间已硬*——models.py 明注 observed_at"必填、不伪造（本机收
   到响应时间）"+ published_at + source_updated_at 语义注记；诚实回测
   （M2 had-only 结论）已是同一原则。*落点*：伤停源（票05 触发）引入时
   情报行必须带三时间，event_time 缺失即标注，禁止以 observed_at 冒充。
3. **原始数据追加保存，修订不覆盖**：保留原始响应、来源版本、采集时间、
   标准化结果；赛程调整/伤停改判/赔率变化追加新记录；保存范围受数据许可
   约束（尤其新闻全文、社交帖子、商业赔率）。
   *现状：数据库层已强制*——`draw_result_revisions`/`settlement_revisions`/
   `intel_observations` 均带 BEFORE UPDATE/DELETE RAISE ABORT 触发器
   （append-only 不可绕）；intel_observations UNIQUE(fixture_id, collector,
   raw_hash) 幂等 + raw_payload 原文留存；odds 快照天然追加。*落点*：引入
   新闻/社交全文源时 raw_payload 收敛为摘要+出处（许可约束），商业赔率
   （The Odds API 条款）快照存数值不存原始分发物。
4. **"没有数据"≠"没有伤停"**：接口空列表可能是真无伤停/联赛不覆盖/
   更新未完成/请求失败——必须区分，否则稳定产出错误结论。
   *现状：最大缺口*——sync_runs 记录运行成败，但无"覆盖范围"语义
   （没有 league-coverage 维表）；伤停源本身未引入（票05 触发式）。
   *落点*：伤停表设计时每行带 coverage 状态（covered/absent/unknown），
   absent 断言仅当源声明覆盖该联赛且采集成功。
5. **Agent 提取解释，程序匹配计算**：Agent 读公告/抽取事件/定位证据/解释
   差异；程序做时间过滤/实体匹配/赔率标准化/玩法结算/缺失值/质检。
   不让 LLM 自由生成"看起来合理"的伤停/概率/历史赔率补库。
   *现状：已是现行架构*——sina 情报提取在 llm 域、映射走 fixtures 仓储
   （commit 2838016）；review_items 结论三分类只进评测集"不改预测工件"
   （票 05 冻结）就是本条的实例。*落点*：作为 llm 域代码评审的常设条款。

**净结论**：五条约束中 2/3/5 已有硬地基（触发器级 append-only、"observed_at
不伪造"、Agent 产物隔离），1 有雏形待新源接入时执行，4 是唯一实质缺口、
挂票 05（伤停源触发条件）一并设计。

## 待用户裁决 → 已全部定案开票（2026-09-20）

1. **Understat xG 特征票 → [issues/45](../issues/45-understat-xg-features.md)**
   （§一 裁决注：特征层通用化、全季收益、两条红线不变）。
2. **官方赛果源 → [issues/44](../issues/44-official-results-reconciliation.md)**
   （2026-09-20 用户定案"按建议"；当晚再裁决"直接切换"免观察期，已执行：
   uniform 升事实源/源D 降审计源，详见票 Answer 追加节）。
3. **fdhist 扩联赛 + openfootball 直连 →
   [issues/46](../issues/46-decade-backtest-corpus.md)**（十年语料票，
   替代十年库引入）。
