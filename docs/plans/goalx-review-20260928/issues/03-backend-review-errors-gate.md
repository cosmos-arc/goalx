# 03 backend：review_errors 门槛硬编码恒不通过（Spec 轴唯一真代码缺口）

Status: resolved（PR #101，2026-09-28 裁决后落地）

## 现状

glossary.md:59 口径：「review_errors：人工复核无系统性错误，**无复核记录**恒
不通过」——语义是仅"无记录"才恒不通过，有记录应按记录评估。

`apps/backend/src/goalx_backend/evaluation/validation.py:274-282` 硬编码：

```python
ConditionProgress(key="review_errors", achieved=False, current="无复核记录(未评估)", ...)
```

从不读 `llm/review.py:145 verdict_counts`——**即使存在干净的复核记录门槛也
永远过不去**，且有记录后 current 文案就是错的。三条件门因此实际退化成两条件。

## 修复

接 verdict_counts：无记录 → 现状（False + "无复核记录"）；有记录 → 按
"无系统性错误"判定规则给 achieved/current，文案如实反映记录统计。

## 不变量与人裁决项

- **人裁决（阻塞）**：「无系统性错误」的判定规则是什么？候选：(a) 错误类
  verdict 占比 ≤ 阈值（阈值多少）；(b) 全部复核无错才算过；(c) majority
  verdict 无系统性错误类别。裁决前本票不动手。
- 验收：造有/无记录两种 fixture，门槛行为分别符合口径；glossary 行同步补充
  判定规则（若裁决产生新口径）。

## Comments
