# 08 数据管道原型（throwaway prototype）

Type: prototype
Status: resolved
Blocked by: 07

## Question

用最便宜的方式把「选定数据源 → 本地存储 → Web 上可看」跑通一遍，产出一次性的原型脚本（不是生产代码），让用户对数据形状、质量、更新节奏有直观感受，作为架构设计的反应物。

范围（尽量小）：

- 拉取一轮真实竞彩当期赛程 + 官方赔率快照（国内源）+ 对应欧洲公司赔率（国外源），存入本地 SQLite。
- 拉取一至两期历史开奖 + 历史收盘赔率样本（回测输入的可行性验证）。
- 一个最简 Web 页面或导出的 Markdown 报告，展示：当日竞彩场次对照表（竞彩赔率 vs 欧洲隐含概率）。
- 记录踩坑：反爬、限流、字段缺口、时区（比赛时间北京时间 vs 源 UTC）。

产出：原型脚本目录 + 「数据形状与坑」备忘（链接到票），回答「这套数据够不够支撑双线预测与回测」。

## Answer（2026-09-13 原型跑通，问题得到肯定回答）

产物（throwaway，gitignored）：[prototype/pipeline_prototype.py](../prototype/pipeline_prototype.py)（单文件 stdlib+curl，一条命令跑通）+ [prototype/report.md](../prototype/report.md)（对照表）+ `scratch_prototype.db`。一次运行：竞彩 43 场 → 欧赔 223 事件（16 sport keys，16 credits）→ join 25 场 → 历史 CSV 760 行入库。

**结论：数据足够支撑双线预测与回测的最小闭环。**

1. **竞彩 vs 欧洲共识 EV 系统性 -5%~-17%**——正是 27% takeout 的数学体现（票 14 互证）；陈盘捕捉（方向 A）的"错价>27% 才有正 EV"意味着触发会稀少，需 Shin 去晦 + 干净 join 后持续扫描。
2. **竞彩快照自带调盘时点**（`updateDate/updateTime` 字段）——方向 A 的数据基础成立，可监控官方调盘滞后。
3. **Join 策略定案**：联赛映射（竞彩联赛名→sport key）+ ±20 分钟开球窗口可消歧 ~90%；同联赛同时开球仍有歧义（本轮 2 场被正确标记）→ **生产版需球队级 ID 映射表**（建议以 API-Football fixture/team id 为 canonical，映射 sporttery matchId，可从 500.com 对照页冷启动）。
4. **Tier 2 缺口实证**：14/43 场无欧赔源（葡超/日职/瑞超/芬超/挪超不在 Odds API eu 区）——票 17 的张力落地：这些场次 CLV 代理线缺失，按 02 结论用 500.com 欧赔兜底或降级为纯模型基准。
5. **回测输入成立**：E0 两赛季 760 行，Pinnacle 收盘列 78% 非空、市场均值收盘（AvgC 系）100% 可补。
6. **踩坑记录**：① 本机 python3.9 OpenSSL 与 api.the-odds-api.com 握手失败（curl 正常）→ 原型统一走 curl；生产用 uv 管理的新 Python 应无此问题，但 HTTP 客户端要带重试。② Odds API sport key 命名会变（法甲是 `france_ligue_one` 非 `ligue_1`）→ 动态发现 + 前缀匹配，勿硬编码。③ h2h outcome 的 name 是队名/"Draw"，非 home/away。④ sporttery businessDate（销售日）≠ matchDate（比赛日），跨天场次勿混用。⑤ 任9/14 奖池历史页（500.com zucai）确认可达但解析未做——非阻断，实现期补（票 13 已留）。

对下游：12（回测协议）的代理线方案验证成立；09 的数据层设计可按此形状展开；球队映射表进实现期任务清单。
