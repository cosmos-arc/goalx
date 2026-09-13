# 07 数据源开通任务

Type: task
Status: resolved
Blocked by: 02

## Question

按 02 的选型结论，开通/申请所需的账号与 API key（The Odds API、football-data.co.uk 下载、天气 API、选定付费源的低档订阅等），验证每个 key 可用（一次真实调用成功），并把 key 的存放位置（如 `.env`，注意不要提交）记录下来。

人工部分由用户完成（注册/付费），agent 辅助验证与记录。完成标准：清单上每个源都有可用凭据 + 一次成功的样本调用输出。

## Answer（2026-09-13 完成）

凭据与配置：

- **凭据位置**：仓库根 `.env`（已 gitignore，`.env.template` 为模板并注明各源注册地址与免费额度）。真实 key 不入票、不入 git。
- **API-Football**：用户注册免费档（100 请求/天，全端点，有效期至 2027-09）。`/status` 实测通过（账户/配额正常）。**Pro $19/月暂不升级**——免费档够原型期，08 原型跑出限流痛点再升。
- **The Odds API**：用户注册免费档（500 credits/月）。`/v4/sports` 列表 + **真实赔率拉取实测通过**：`soccer_epl` h2h（eu 区）返回 15 场（如 Tottenham vs Everton，Tipico 等多家 decimal 价），消耗 1 credit 后余 499。
- **无 key 源复验**：open-meteo 逐小时天气真实调用通过（温度/降水/风速）；football-data.co.uk CSV、sporttery 网关（须 Referer 头）、500.com、澳客沿用票 01/02/13 的实测结论。

后续票依赖的事实：08 原型可直接开工（全部源就绪）；LLM 线的 DEEPSEEK/ZHIPU key 留 09 裁决后启用（模板已留注释位）。
