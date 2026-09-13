# 研究 13：补充数据源（FBref / 澳客 okooo / Understat / 其他）

> 调研日期 2026-09-12，可达性均本机 curl 实测（UA: Chrome/126）。

## 实测汇总

| 源 | 实测 | 数据内容 | 接入方式 | 定位 |
|---|---|---|---|---|
| 澳客 okooo.com | ✅ jingcai 200 (726KB)、danchang 200 (1.3MB) | 竞彩赔率（含单固）、亚盘/欧赔对照、**专家/人气投注比例**、胜负彩/任9 数据、历史赛果 | 免费页面解析（量大，注意编码/分页） | **v1：国内第二源**，重点是其专家共识/人气分布数据——任9/14「公众注分布」的直接代理信号 |
| Understat | ✅ 首页/联赛页 200 | 专门的 xG 网站：五大联赛+俄超的比赛/球员 xG、xG 时间线、射门图 | 页面内嵌 JSON（社区有 understatapi 包；联赛页嵌入字段在票 08 验证） | **v2：xG 建模输入**（免费） |
| FBref | ⚠️ curl 403（WAF 拦脚本） | StatsBomb 授权高级统计：xG/xGA、射门分解、球员/球队表、门将数据；联赛覆盖广（**含中超**）、多赛季 | 真实浏览器会话 + 严格限速（worldfootballR 社区成熟模式：低频、带退避） | **v2：高级特征**（免费但工程成本高）。**风险注记（2026-09-12 补）**：The Athletic 2026-01 报道 FBref/Opta 被迫下架部分高级数据——v2 以 soccerdata 多源冗余 + Understat 为主，FBref 可用则用 |
| FotMob | ⚠️ 站点 200；`/api/*` 返回 HTML（已加 x-mas 签名头） | 比赛统计（Opta 系）、阵容、部分联赛 xG | 非官方 API 需逆向签名——成本中等 | 暂缓（有 API-Football 替代） |
| WhoScored | ❌ 403 | Opta 数据 | 反爬激进 | 放弃 |
| SofaScore | ❌ 403 (48b) | 事件流/评分 | 反爬激进 | 放弃 |
| Transfermarkt | ✅ 200 | 阵容、身价、伤停历史、转会 | 页面 + 社区数据集（transfermarkt-datasets） | 可选补充（伤停历史回测特征） |

## 对票 02 组合的修订

```
付费核心（不变）：API-Football Pro $19/月 + The Odds API 免费起步（按需 $30）
国内双源（免费）：sporttery 网关（官方事实）+ 500.com（历史/欧赔）+ 澳客 okooo（新增：专家共识/人气分布）
回测（免费）：football-data.co.uk（Pinnacle 收盘 CSV）
环境（免费）：open-meteo
舆情（免费）：Reddit RSS + 直播吧/虎扑/懂球帝页面
v2 新增（免费）：Understat xG（优先）→ FBref 高级统计（浏览器会话爬取）→ Transfermarkt（可选）
放弃：WhoScored、SofaScore、FotMob（逆向成本）、X API（$100/月超预算）
```

**关键增益**：澳客的**专家 consensus / 人气比例数据**直接补上票 03 指出的任9/14「公众注分布估计」的数据缺口——原方案只能用模型估计分布，现在有实测数据可对照/训练，是本次补充里价值最大的一项。
