# 09 情报与基本面采集（存证表+采集器）

Status: resolved（2026-09-19/20 合入 main，PR #32-38）
Blocked by: （无；GLM web-search 适配器在 08 合入后同票补）

## 目标

`intel_observations` 存证表（M3 唯一新表，票 04 定案）+ 免费采集组合（票 03 裁决 B）。

- 表：append-only，列含 fixture_id/kind/text/source_url/collected_at/collector/raw_payload/raw_hash——对齐 quote_observations 的 ADR-0001 模式；归 `llm/` 包自持 SQL
- okooo 赛事页伤停栏直爬 ingester（复用现有 ingester+observations 落盘模式；站点清单先做源B，其余站调研后增量）
- football-data.org 免费档：12 项赛事 standings+form（10 次/分限速，key 注册免费）
- 内部推导：fdhist 2627 近况/H2H 推导函数（零外部请求）
- 调度：挂每日 forecast 后 + 彩池同步后增量（新增 deployment，合入 main 后重启 serve）
- 迁移：drop 空脚手架表 `match_intels`（票 03 废弃定案）
- GLM web-search 适配器：留接口+stub，08 合入后实装（土超/日职等无结构化场次兜底）

## 验收

task check；采集幂等（重复跑零重复行）；raw 哈希可验证；无情报=零行不是假行。

## 不变量与人裁决项

- append-only：不改不删；原始响应落 observations 目录存证。
- 诚实边界：采集不到就零行，禁止占位/推测数据。

## Comments

- **2026-09-19 实施定案**：✅ intel_observations 迁移 v11（append-only 触发器+UNIQUE 幂等）；✅ 内部推导采集器（fdhist 近况/H2H，经 odds_api 别名桥接 fd 队名）；✅ fdorg 客户端+解析（standings；key 待注册，无 key 跳过）；✅ 调度 intel-collect deployment（10:40/22:40，随彩池同步后）。真库首跑 5 条落库、重跑 0 新增（幂等实证）。
- **源B 伤停直爬暂缓（实测发现）**：okooo 场次详情页是 AJAX 网关壳（Remoting/json.php），qingbao/analysis 子路径全 405、主页无数据块——直爬需逆向 JS 网关，成本超预期。按票 03 双路预案，伤停情报改走 GLM web-search 兜底（适配器随票 08 合入后实装）；若后续逆向成功可回补直爬适配器。
- 真库首跑口径：27 场在售（2 期）→ 7 场有 fixture 桥接、5 条情报（2 场队名无 fd 映射诚实零行）、20 场无桥接（多为期次独有小联赛场次，竞彩无此场次——如实计数）。

## Comments（2026-09-19 深夜补充：伤停情报全路径实测）

- **三站直爬全部受阻（用户点名的虎扑/懂球帝/直播吧）**：主站均 200 可达但无结构化伤停（新闻面开着、数据面关门）；数据子域 `open.dongqiudi.com`/`match.hupu.com`/`bifen.zhibo8.com`/`lives.zhibo8.com` 从本网络 **SSL 层直接拒绝**（httpx/curl 双栈同败）；懂球帝 `/schedule` WAF 403。与源B 同一堵墙——**中文站伤停直爬路线正式否决**。
- **GLM web_search 实测（伤停查询）**：
  - coding 订阅端面：**工具被静默忽略**——`tool_calls=0`，模型自述"无联网"并诚实拒答（回答质量本身很好，但不是情报）。**坑：静默降级**——scout 适配器必须检测 `tool_calls>0`，否则会把无网回答当情报存证。
  - payg 按量端面：返回 `429 余额不足`——**账户按量余额为 0**（订阅额度不覆盖工具调用）。
- **结论**：伤停情报落地前置 = **用户给 bigmodel 充值（票 02 清单本来就建议首充 ¥100）**，web_search 按量计费 ¥0.01-0.05/次，典型月成本几十元内；票 10 的 web-search 适配器设计定案：强制 payg 端面 + tool_calls 断言 + 来源/时点提取进 intel_observations。

## Comments（2026-09-19 补充二：computer use 复核）

用户提出用 computer use 捞——已用四层栈验证全部同败：httpx（403）→ curl（数据子域连接拒绝）→ headless chromium（403 / ERR_CONNECTION_CLOSED）→ **应用内真浏览器 webview（仍 403 Forbidden openresty）**。定论：直播吧/虎扑是**网络层封锁**（本网络到其数据域主机无法握手，浏览器无效）；懂球帝是 **WAF 按 IP/请求特征拦**（真浏览器也过不去）。伤停情报路径维持：①充值后 GLM web-search（可调度可存证，首选）；②重点场次 AI 代采（用户日常设备网络干净可访问，RUNBOOK 已有代采模式先例）。

## Comments（2026-09-19 补充三：澳客 computer use 复核，用户提问驱动）

真浏览器三路验证 qingbao 子页全败：①IAB 直连 405（安全拦截页）；②场次主页可正常渲染（积分/排名/身价都有，但这些内部 fdhist 已有零边际价值），**伤停不在主页**——导航"比赛情报"即指向 qingbao；③主页内 JS 点击同站导航（带 referer+cookie）仍 405。定论：澳客伤停封锁是**服务器端路径级 WAF 规则**，与客户端真实度无关——computer use 路线对澳客同样不可行。伤停路径维持：GLM web-search（充值后）为主 + AI 代采（用户设备网络）为辅。

## Comments（2026-09-19 裁决四：web-search 路线否决）

用户否决 GLM web-search 方案。推论：①**充值前置取消**——scout/analyst 走 coding 订阅端面已够，按量余额仅 web_search 需要；②伤停情报面降级为"有则更好"：双线以现有情报面（fdhist 内部推导 + fdorg）开工，Tier A/B 评测不依赖伤停 kind；③重点场次伤停走 **AI 代采**（小 POST 端点，挂票 14，沿用源A 销量代采模式），证据卡无数据诚实标注。

## Comments（2026-09-19 裁决五：三源评估定案，澳客 formation 为主力）

用户提供两条路径触发重评，结论：
- **主力 = 澳客 formation 页**（`/soccer/match/{id}/formation/`）：curl 直连、服务端渲染（同站 qingbao/analysis 被 WAF 拦而 formation 开放）；**场次 id 零映射**（pool_matches.source_match_id 即澳客 id）；数据最富（主客影响总额 + 伤/停 + 位置【卫门中锋】+ 身价 + 出场/进球）；彩池场次天然全覆盖；一场一请求 ~56/日。
- qiumibao（直播吧数据域 stats/db.qiumibao.com，此前会话打通）：留作可选增量，不实施（YAGNI——formation 覆盖更优且零映射）。
- 500.com odds.500.com：JS 挑战（EO_Bot_Ssid 混淆）——非浏览器不可调度，出局。
- 真库实证（2026-09-19 深夜）：27 场在售 → 7 场可拉 → **6 场落库真实伤停**（1 场个别 405 如实计数）。

## Comments（2026-09-20 补六：新浪 AI 网关评估，用户提供路径）

`lottery.sina.com.cn` AI 分析页（SPA）背后是纯 JSON 网关 `mix.lottery.sina.com.cn/gateway/index/entry`，**curl 直连全通**（需 `__verno__=1` 参数，JSONP 可不带 callback）：
- `footballForecastMatchListPao&date=`：按日场次列表（matchId/队名/联赛/欧赔/红黄牌角球；matchNo 竞彩编号仅竞彩场次有值）——映射走日期+队名（一跳，同 match_fixture_id 模式）
- `footballMatchTeamInjury&matchId=`：**伤停 JSON**（team1/team2、球员/位置/伤因文本/缺席场次 missedMatches/起止时间戳）——与澳客 formation 互补（澳客有身价+影响总额，新浪有伤因+缺席场次+复出时间）
- `footballMatchDetail`：含**天气**；另有积分/交锋/近期战绩/智能预测（"智能" tab = 新浪 AI 预测，可作 LLM 线对照素材）
**结论：新浪 = 高价值辅助源**（伤停互校验 + 天气 + 智能预测对照），实现挂票 10 scout 输入面按需做；伤停主力仍为澳客 formation（PR #34，零映射）。

## Comments（2026-09-20 补七：新浪彩池链路一期一页，用户从首页发现）

用户指出新浪彩票首页挂竞彩/胜负彩/北单在售列表，实测升级新浪评估：
- **胜负彩视图页** `view.lottery.sina.com.cn/lottery_index/sfc/index`（curl 直通，服务端渲染）：一期 14 场的 **sina matchId + 队名 + 近5轮战绩**（如"伯恩茅斯 WDWDD"）全在一页——彩池场次映射成本从"按日+队名"降为**一期一页**；
- 竞彩在售：`cat1=jczqOnSellMatches&gameTypes=spf`；北单：`bjdcOnSellMatches`（同网关）；
- 伤停仍走 `footballMatchTeamInjury&matchId=`。新浪完整彩池链路 = 1 页映射 + N 个 JSON，与澳客 formation（HTML 零映射）双源互补互校验。
- 实现时机不变：挂票 10 scout 输入面按需做（澳客 PR #34 已先覆盖伤停主力）。
