# 09 裁决：market_skill 的 model_version 聚合口径

Status: resolved（PR #101，2026-09-28 裁决后落地）
Type: grilling

## 问题

glossary.md:31/58 只规定「回测 skill 与前瞻 skill 分开看，三条件只认前瞻；前瞻
skill ≥0 且 ≥30 场」，**未规定多 model_version 如何聚合**。

`evaluation/validation.py:255` 现实现取 `max(skill_rps for eligible groups)`
per model_version：任一历史好版本达标即可让 market_skill 通过，**当前部署版本
可能为负仍过门**。

## 选项

- (a) 只认当前部署（latest active）model_version——最严，验证的就是在跑的模型；
- (b) 保持 max——最宽，认可"曾经准过"；
- (c) 达标版本须 ≥30 场且部署版本不劣于 −ε 之类组合线。

## 裁决后动作

裁决结果回写 glossary market_skill 行 + validation.py 按口径实现（若非 b），
并入票 03 同文件可搭车。倾向 (a)（门槛语义=当前模型可信），等用户定。

## Comments
