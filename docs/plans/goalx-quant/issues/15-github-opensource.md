# 15 GitHub 开源生态调研

Type: research
Status: resolved

## Question

（用户 2026-09-12 追加。）GitHub 上有无类似或优秀的开源项目：同类「个人足球彩票量化/预测系统」、可复用的模型/数据/回测工具，结论如何影响我们的依赖选型与自研边界？

## Answer

详见 [research/15-github-opensource.md](../research/15-github-opensource.md)。要点：

1. **无同类系统**：端到端的「football betting model」仓库全部 <10★ 学生级——个人足彩量化系统在开源界空白，自建合理。
2. **库生态很厚**（`gh` 实测 star/活跃度）：**soccerdata（2065★，统一抓 Club Elo/FBref/Understat/WhoScored + 限速缓存，化解 FBref 反爬成本）**、eddwebster/football_analytics（2776★ 教材库）、worldfootballR（602★）、penaltyblog（220★，已采纳）、understat 系（185★+）、awesome-football-analytics（215★ 索引）。
3. **v1 依赖修订**：数据层加 soccerdata 为抓取底座（与 02/13 的结论合并）；建模层 penaltyblog 不变。
4. **自研边界清晰**：官方数据接入、奖池滚存 EV、双线对比、价值/成本预估、中文情报线——全部无现成轮子，即本项目的差异化所在。
5. 借鉴 theopenmodel 的「可验证预测注册」设计进纸面跟踪（预测先存证防事后修改）。
