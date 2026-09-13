# 13 补充数据源调研（FBref / 澳客 / Understat 等）

Type: research
Status: resolved

## Question

（用户 2026-09-12 追加。）在票 02 选型之外补充评估：FBref、澳客（okooo）、专门的 xG 数据网站（Understat）以及其他值得纳入的源（FotMob、WhoScored、SofaScore、Transfermarkt 等）——覆盖什么数据、可编程性、费用，以及对 02 推荐组合的修订。

## Answer

详见 [research/13-supplementary-data-sources.md](../research/13-supplementary-data-sources.md)。要点：

1. **澳客 okooo ✅ 实测可达**（jingcai 200/726KB、danchang 200/1.3MB）：国内竞彩赔率+亚盘欧赔+**澳客专家 consensus 数据**（奖池玩法的「公众/专家分布」代理信号，02 缺的一块）+ 历史赛果，免费页面解析。与 500.com 并列为国内双源。
2. **Understat ✅ 可达**（首页/联赛页 200）：专门的 xG 网站（五大联赛+俄超），比赛/球员级 xG 与 xG 时间线；联赛页嵌入 JSON 的解析在票 08 原型验证；免费。定位 v2 建模（xG 路线）输入。
3. **FBref ⚠️ curl 403（WAF）**：StatsBomb 授权的高级统计（xG、射门分解、球员表）+ 覆盖广（含中超）；需真实浏览器会话/严格限速爬取（worldfootballR 社区模式），免费。v2 阶段按需接。
4. **FotMob ⚠️**：站点 200，但非官方 API 已加签名头（x-mas），逆向成本中等——暂缓；**WhoScored / SofaScore ❌ 403 反爬激进**，放弃。
5. **Transfermarkt ✅ 200**：阵容/身价/伤停历史，可选补充（社区数据集成熟）。
6. **对 02 组合的修订**：付费核心不变（API-Football Pro $19 + The Odds API 免费起步）；新增免费层——澳客（专家/公众共识信号）现在就值得进 v1 数据管道，Understat/FBref 归入 v2。
