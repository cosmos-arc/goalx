# 04 LLM 情报 as_of 时点边界

Type: task
Status: resolved (PR #218, 2026-11-04)

## Question

情报读取无 as_of 过滤：`intel_for_fixture` 返回该场全部历史情报，scout 接受
now 参数却仍读全量，前端每条回答后展示的也是全量情报徽章——历史回放因此不
具备严格时点正确性。是否给情报链路贯通 as_of？

## 事实（2026-11-03 main 实测）

- `llm/store.py:67-75` `intel_for_fixture` 无时间谓词（`ORDER BY collected_at`
  全量）；78-92 `intel_summary_by_fixture` 同
- `llm/scout.py:143` `now` 参数仅用于 `issued_at`（191）；155 读全量；181
  `intel_ids` = 全量 id；`ask.py:135` 同型
- `web/src/components/ask-analyst.tsx:55-67` 每条 assistant 消息渲染整个
  `intels` prop（来自 `api/evidence.py:278` 全量），非逐句主张实际引用
- 对照：forecast 侧已有 asof（`latest_forecast_asof`，fusion/gate/review 均
  用），intel 侧缺失——两套证据时点纪律不对称

## 验收

1. `intel_for_fixture` / `intel_summary_by_fixture` 加 `as_of` 参数
   （`collected_at <= as_of`），不传 = 现行为（实时面零改动）
2. scout / ask / fusion 链路按各自 moment 传 as_of；回放路径读不到未来情报
3. 前端徽章按决策时刻 as_of 过滤（面板级；不要求逐句主张级引用）
4. 回归：实时路径行为不变 + 新增"回放不泄漏"黄金样例（情报 collected_at
   晚于 as_of 不入 payload / 不显徽章）

## 不变量与人裁决项

- 人裁决：as_of 语义用 `collected_at` 还是 `published_at`（推荐
  `collected_at`，与库内现实及排序键一致）
- 逐句主张 → 证据 ID 的结构化引用**不入本票**（归 LLM 三能力拆分，见 map
  fog）；本票只做读取边界与展示过滤

## Comments

- 2026-11-03 人裁决（内嵌全按推荐）：as_of 语义 = **`collected_at`**（与库内
  现实及排序键一致）；逐句主张级结构化引用不入本票（归 LLM 三能力拆分 fog）。
