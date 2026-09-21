# 12 LEAP 三项层融合 + ADR-0009

Status: resolved（2026-09-19/20 合入 main，PR #32-38）
Blocked by: 10

## 目标

- LEAP log-pool 融合在**三项层**（票 03 定案）：ML Forecast 三项投影 × LLM 三项 → `forecasts(track='fused')`，payload 引用双源 forecast id
- 融合权重起点 0.5/0.5，参数进配置
- ADR-0009：LLM 线三项概率与融合层级；ADR 0006 比分矩阵 canonical 限定 ML 线（三项=矩阵投影层）

## 验收

task check；三项归一性单测；无 LLM forecast 场次不产 fused（宁缺毋假）；ADR 走仓库评审。

## 不变量与人裁决项

- fused 独立工件，绝不覆写 ml/llm 行（票 04 冻结）。
- 融合线不建注：M3 建注决策只读 ML 轨道（票 04 冻结）。
