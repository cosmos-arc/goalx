# 教训库（lessons）——体系研究记忆 v1

定位：**为什么死 / 什么条件下别再试**。与假设注册表（可证伪预测状态）
互引不互代。seed=2026-11-10 自 [edge-ledger](../edge-ledger.md) §3 全部
29 条迁移定稿（vision-build 票 05 人裁：票面「26 条」按 §3 实际 29 条
全迁），L-NN ↔ §3#N 一一对应；账本 §3 原文保留为种子源，逐条尾注
「已迁移→」回指。

## 写入纪律（受控文档）

- **append-only**：条目不删、正文不改写。只有人可以改
  `status` / `confidence` / `review_due` 三个字段——这是文档纪律，
  由 git PR 人审定稿制承担（agent 写权限=空集），不是代码强制。
- **状态生命周期**：`active → review_due → archived`。置信翻转/
  归档=人改；脚本（晨检项 22）只报不改；12 月未复现→建议降置信；
  市场结构变化信号可整类作废（人裁执行）。
- **新条目**：文件名=`L-NN.md`（零填充两位起，id 与文件名一致）；
  frontmatter 字段齐一；正文写规矩/死因与证据/边界（如有）/出处。
- **evidence 只引外部证据**（防递归自食）：`edge-ledger §3#N`（账本
  方法学报）、`edge-ledger E-NN`（账本 §2 主账条目）、
  `report:<路径>`（报告路径，盘上存在或账本原文可查）——禁纯
  synthesized。机器核对：`evaluation/lessons.unresolved_evidence_refs`。

## frontmatter schema

| 字段 | 说明 |
|---|---|
| `id` | `L-\d{2,}`，零填充稳定排序 |
| `status` | active / review_due / archived |
| `confidence` | 高 / 中 / 低（人改） |
| `created` | 定稿日（seed=#1-#20 为 2026-11-09 建账日，#21-#29 为 2026-11-10 审计/B 批回写日） |
| `review_due` | 默认 `created`+12 月；「不设」= `null`（正文须说明理由，先例=L-29） |
| `window` | 观测窗（账本原文有则记；方法学规矩类=null） |
| `cause` | 一句话死因 |
| `triggers` | 触发面（场景/联赛/时间窗）≥1 条 |
| `evidence` | 外部证据回指 ≥1 条（格式见上） |

seed 的 `confidence` 统一「高」：§3 定位「每条=一次真实代价换来的
规矩」，L-29 为账本明言置信。

## 读口（`apps/backend/src/goalx_backend/evaluation/lessons.py`）

1. **周批注入**（票 03 消费）：`weekly_injection` + `render_injection`
   ——active 条目 → 提案 prompt 的防重复死法面。
2. **EM/扫描器排除清单**（autoresearch 扫描器消费）：
   `scanner_exclusions` ——active 条目 triggers 逐条摊平的降权排除面。
3. **月读视图**（票 04 消费）：`monthly_review_view` ——新入库（上个
   自然月 created）+ 复审到期（review_due ≤ 今日 且未归档）。
4. **晨检**：`morning_check.check_lessons_review_due`（项 22，到期=
   黄不红，只报不改）。
