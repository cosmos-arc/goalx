# 17 haircut 校准十年重估：分时代/分联赛（S17）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 17
Blocked by: 13

## 任务

模拟竞彩价 haircut（现 −10%，票 30 校准基于五大三季）按十年数据重估：

- 配对：竞彩官方价（运行面）× gold era 分层欧赔共识（era1 AvgC 族 /
  era2 多书共识），配对口径沿用票 30 的 as-of 与时间容差纪律
  （ADR-0007：防市场移动混入机构折价）；
- 分层：era×联赛（×玩法若样本够）重估 haircut 分布；
- 对照：单关 overround 1.129 实测基线（research/22-27） sanity check；
- 产出：`haircut_calibrations` 新 scope 行（calibrated 源）+ 报告落
  `reports/`；引擎 run 的默认 haircut 消费路径（`calibrated_haircut`
  回落链）确认接上新校准。

## 不变量与人裁决项

- PSC 成熟度纪律共用（fair 基准与 haircut 校准同一谓词——未成熟行
  不进配对；gold 快照 NULL 语义即门）。
- 旧校准行保留（append-only），新 scope 命名可区分十年重估版。
- 人裁决项：新 haircut 值是否替换默认（−10%）进引擎 run——报告给
  建议，用户点头后生效。

## Out of scope

- 引擎本体（票 13）；护栏参数复验（票 18——haircut 是护栏输入之一，
  参数面复验在 18 统一收口）。

## Answer

2026-10-10 交付（分支 feat/haircut-decade-c17-20261010，PR #138）：

- **代码**：`evaluation/haircut_decade.py`（SP×gold era 收盘配对，scope=
  sp-close:overall/lg:{联赛}，n≥30 纪律同票 30）；uniform 表 SQL 落
  归属包 quote_evidence（jc_sp_rows/jc_sp_overround_rows）；gold_reader
  增按 sid 取收盘；CLI haircut-decade。引擎默认不动（替换=用户点头）。
- **真树结论**：单关 overround 中位 1.1293 vs 基线 1.129（Δ+0.0003
  精确复现——管道可信度锚）；sp-close:overall=0.1057（192 样本/64 场
  era2）；联赛层 0.104-0.113。重估值与现默认 0.10 同量级（+0.6pp），
  无推翻性证据——**建议维持 −10% 默认**，sp-close 行留档备查。
- **时代维度如实受限**：era1 无 JC SP（历史回填链属程序外 Out of
  Scope）；sid 链覆盖 64/183 场（jc 面联赛覆盖本身有限）。
- correctness 五修（TEXT 赔率列 CAST 数值比较是关键——'abc'>1 字典序
  为真会以 NULL overround 炸统计）+ 回归测试；ADR-0008 归属修正
  （evaluation 裸 SQL → quote_evidence 取数口）。

## Comments

- 2026-10-10 用户裁决：**维持 −10% 默认**（sp-close 行留档备查）。
