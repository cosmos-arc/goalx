# 22 十年总账报告与方向裁决门（D 相收官）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` S22
Blocked by: 19, 20, 21

## 任务

- **总账报告** `reports/decade-closeout.{md,json}`：A（数据救回/卫生
  谓词）→ B（gold 特征面 48,561 行）→ C（引擎 v2 十年窗 + hhad/ttg
  解锁 + market-λ 转正 + haircut/护栏复验）→ D（技能/CLV/归因三报告）
  串成总账，回答 spec Problem Statement 三问：模型到底有没有 skill /
  edge 在哪个时代·联赛·玩法 / 下一迭代方向。
- **方向裁决材料**：候选方向清单，每条 = 证据引用（19/20/21 落树报告
  的具体数字）+ 成本粗估 + 依赖前提；交用户裁决，不自动放行。
- **spec 收口**：spec.md Status 更新；票板 Answer 链接校验（01-23+19-22
  全 resolved）。
- **耐久资产转正**：`.scratch/backtest-decade/` 的 spec/报告按仓库惯例
  拷入 `docs/plans/backtest-decade/`（保相对布局，跨链可解析）并提交。

## 不变量与人裁决项

- 门 = 报告 + 用户点头：方向裁决在用户明确回复前不实施（spec 全程门
  纪律的最后一次兑现）；
- 总账数字一律引用 `reports/` 落树文件（可追认），不凭记忆转述；
- 转正提交走 PR（机械 docs diff，免 correctness-review）；
- 人裁决项：下一迭代方向本身（本票只备料）。

## Out of scope

- 方向裁决的实施票（裁决后另开）；web 报告页改版。

## Answer

2026-10-14 交付（docs PR #144）：

- **总账报告** `reports/decade-closeout.{md,json}`（真树落盘）：A/B/C/D
  四相结论串账，三问答案（①无 skill 五面一致；②无正收益桶但市场
  自身 8 错价桶；③方向裁决材料五候选带证据/成本/依赖表）；
- **方向裁决材料**：候选 1 模型侧迭代（推荐主向）/ 2 错价桶特征接线
  （依赖 1）/ 3 NewsShock P0（语料就绪）/ 4 早锁（已否定，beat
  27-38%）/ 5 纸面闭环照跑；判读注意：错价桶 1-2pp < haircut
  11.3% 只能作特征或零 haircut 参照侧，方向 2 必须搭方向 1。
- **spec 收口**：Status: closed 2026-10-14；票板 01-06/13-18/19-23
  全 resolved（22 本票收尾）。
- **耐久资产转正**：spec/issues 17 票 + 8 份报告 MD 拷入
  `docs/plans/backtest-decade/`（保相对布局），docs 机械 diff 免
  correctness-review（AGENTS 惯例）。
- 门=报告+用户点头：方向裁决在用户明确回复前不实施（本票只备料）。
