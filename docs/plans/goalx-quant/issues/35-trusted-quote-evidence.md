# 补齐报价观测、匹配与可购买状态

Type: impl
Status: resolved
Assignee: ZCode
Blocked by:

交接修订：结算修复已合并，本票为下一实施入口。先读[实施安排](../../trusted-paper-handoff.md)中的最小证据契约；交付可被验证任务直接消费的时间、身份、资格和拒绝原因，不同时改验证统计或页面。共享预算/错误与采集证据可在本票内分切片交付。

## 范围

承接竞彩/欧赔采集、匹配及haircut，为had提供可追溯同期报价，不新增供应商或套餐。

- 区分source_updated_at、observed_at、入库时间、历史实际snapshot时间；旧captured_at按源解释，未知observed_at不能伪造。
- 保存脱敏原始响应或本地压缩文件引用/哈希、解析版本；报价未变也保留观测证据，不重复候选。密钥/认证不落日志。
- 保留国内销售状态、had单固资格、价格/让球语义；停售、开赛、未知资格不进入对应正式候选。
- 内部Fixture ID稳定；时间匹配仅候选，已知主客队/ID交叉核对、歧义拒绝，改期不新造比赛。
- 同义市场、同公司完整三向才可比较；先限had，不付费请求后丢弃totals，不把亚洲盘当竞彩让球。
- 新鲜度、配对时差上限工程初值各5分钟，可配置但非成交保证；源时间未知/超窗只观察不纳正式候选，不改时间或自动放宽来凑通过。输出age/时差/源集合/拒绝原因。
- haircut按同一as-of配对，分列比赛和选择数。离线交叉核验自写Shin，比较归一化/Power及先逐公司去水后聚合，记录固定方法版本，不事后选优。
- 每请求预算预检、成功后按实际返回用量入账；部分失败也记已耗额度。常规与closing共享月额。沿用Prefect，补可复现部署定义和错误日志，不另建调度平台。

## 验收

1. 14:00源更新、14:20首次观测不能用于14:05决策；历史返回snapshot不等于请求时刻，旧未知资格不变有效。
2. 原始响应可重解析、重复观测可审计、解析失败不产生部分可信报价。
3. 同时开球/主客互换/队名冲突/改期/重复外部事件均测试；歧义可查。
4. stale/三向缺失/停售/玩法不支持/未知单固明确拒绝；跨时区和时间窗边界正确。
5. haircut配对时间和唯一场次分母正确；Shin差异及方法敏感性可复验，不称收益证据。
6. 临近月限额、并发/重复任务、中途失败不会漏记或重复用量；无法保证的情况保守拒绝并记录。预算不靠提高日限额绕过。

## 验证与交付

先用模拟/录制HTTP和固定时钟，不消耗付费额度。交付部署定义及启动命令，实采归[运行验收](37-trusted-paper-operational-acceptance.md)。迁移保留旧数据；API变更契约同步、相关回归及task check。

## Answer（2026-09-13）

已实现，分支 `feat/trusted-quote-evidence`；本地 `task check` exit 0（后端 215 测试含覆盖率门禁、web 全套、契约一致性），发布 [PR #6](https://github.com/cosmos-arc/goalx/pull/6)，已由用户合并，合并提交 `720694f`；[PR CI](https://github.com/cosmos-arc/goalx/actions/runs/34740082364) 全绿。

验收对照（全部以固定时钟 + MockTransport + 隔离内存库验证，未消耗真实额度、未部署）：

1. 时间语义：14:00 源更新、14:20 首次观测不可用于 14:05 决策（`test_late_observation_not_used_for_earlier_decision`）；旧行 observed_at 未知 → 判定 unknown 不倒填（`test_legacy_sporttery_rows_stay_unknown`）；odds_api 旧行 captured_at 按源解释为观测时间。
2. 原始证据：`quote_observations` + `data/observations/` gzip 存档，可重解析复核（`test_capture_records_observation_and_reparse`）；重复观测快照去重、观测行留痕；密钥不落证据文件；解析失败不产生部分可信报价（缺三向的 book 整体丢弃）。
3. 匹配：同刻歧义/主客互换/队名冲突/改期/重复外部事件各有测试与拒绝原因（`test_join_*`、`test_reschedule_updates_kickoff_not_new_fixture`）。
4. 判定：stale/三向缺失/停售/未知单固/开赛明确拒绝或 unknown，时区与窗口边界（含恰 300s）有测试；`GET /api/v1/fixtures/{id}/had-quote` 输出 age/时差/源集合/原因。
5. haircut：as-of 配对（欧侧观测 ≤ 调盘时刻且 ≤5min 窗），场次/选择分列，方法版本 shin_mean_v1，四方法敏感性可复现（`test_method_sensitivity_reproducible_and_recorded`）；不称收益证据。
6. 预算：逐请求 IMMEDIATE 预留、失败负数行退回、部分失败保留已耗、临近月限额保守拒绝、日限额不绕月限额；totals 付费市场前置拒绝。

已知边界：sporttery sellStatus/single 字段映射为防御式实现（缺字段按端点语义/未知），票 37 首次真实采集时须复核实际取值。后继票 34 从本 PR 合并后的 main 开始，消费 `quote_evidence.adjudicate_had_quote` 与 `/had-quote` 契约。
