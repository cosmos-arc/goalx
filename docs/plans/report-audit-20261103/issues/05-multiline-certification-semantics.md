# 05 多盘口认证语义收口

Type: task
Status: resolved (PR #219, 2026-11-04)

## Question

multiline 认证只把 OU 落带作拒绝条件，1X2 偏差仅记录不设闸——主胜概率差
50 个百分点仍可"认证"，产物却以可行界自居。是否诚实改名为参数化端点敏感性
分析 + 对称披露，或加 1X2 残差闸？

## 事实（2026-11-03 main 实测）

- `evaluation/multiline_bounds.py:157-158` 唯一翻转条件 = OU 带检查
  （`abs(model_over - p_over) > eps` → infeasible）
- 164-166 `row_x12` 计算后只 append 进 `x12_resids`；169 返回 `in_band_all`；
  231 `certified` 仅凭此返回值
- 334-340 payload 注记自认 `x12_resid_median/max` 为披露性指标
- 隔离构造：主胜差 0.50 仍 certified（OU 在带即可）

## 验收（推荐路径 = 诚实改名 + 披露，不加求解器）

1. 产物 / 字段文档 / 契约措辞从"认证界"改为"端点敏感性分析"；`certified`
   字段保留但语义注释钉死为"OU 带内"；`x12_resid_median/max` 升为显著披露位
2. 若人裁决要闸：`x12_resid_max` 超阈值（默认值人裁决）置 infeasible 并落
   原因，两种语义都有黄金样例测试（OU 在带 + 1X2 差 50pp 各判对）
3. 严格联合上下界（显式约束 + min/max 求解）**不入本票**——记 map fog，
   与票 09 基准读数后合并裁决

## 不变量与人裁决项

- 人裁决：改名披露 vs 加残差闸（推荐改名 + 披露——严格界是研究线不是修 bug）

## Comments

- 2026-11-03 人裁决（内嵌全按推荐）：**改名 + 披露**（产物措辞改"端点敏感性
  分析"，certified 语义钉死为 OU 带内，x12 残差升显著披露位；不加残差闸，
  严格联合界挂 fog 与票 09 读数后合并裁决）。
