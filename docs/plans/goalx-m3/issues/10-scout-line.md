# 10 scout 线（三项预测落 forecasts）

Status: resolved（2026-09-19/20 合入 main，PR #32-38）
Blocked by: 08, 09

## 目标

scout agent 全场次跑（Tier1+当期彩池范围，约 30-50 场/日）。

- scout = GLM-5.3-Flash；输入=已存证情报 + 内部推导基本面 + fdorg 结构化
- 输出胜/平/负三项（票 03：不出比分矩阵）落既有 `forecasts(track='llm')`（票 04 复用定案），payload 带 intel_observation ids 引用 + 模型版本
- 调度：每日 forecast 之后链式触发（新增 deployment）

## 验收

task check；幂等（UNIQUE fixture+content_hash 自然去重）；首日真实运行有样本入库。

## 不变量与人裁决项

- 只引已存证情报（无情报场次允许降级：仅基本面输入，payload 标注 intel_count=0）。
- 绝不覆写 track='ml' 行。
