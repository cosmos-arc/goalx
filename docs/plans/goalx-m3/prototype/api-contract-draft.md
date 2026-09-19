# M3 证据面 API 契约草案（票 06 产物，供票 07 拆票用）

PROTOTYPE 草案——形状讨论用，正式契约走仓库 contract-first 流程。

## 1. 彩池页证据卡（存量渲染）

`GET /api/v1/pool/periods/{period_id}/evidence-summary`

```json
{
  "period_id": 26135,
  "generated_at": "2026-09-19T11:03:00+00:00",
  "matches": [
    {
      "seq": 1,
      "fixture_id": 9021,
      "home": "阿森纳", "away": "切尔西",
      "forecast": {"track": "fused", "h": 0.48, "d": 0.22, "a": 0.30, "issued_at": "..."},
      "intel_count": 3,
      "divergence": {"js": 0.014, "routed": false},
      "state": "scout_done"        // scout_done | analyst_done | no_intel | no_forecast
    },
    {
      "seq": 6,
      "fixture_id": 9032,
      "state": "no_intel",          // 诚实降级：无情报无概率，前端只展示官方份额
      "intel_count": 0
    }
  ]
}
```

## 2. 场次证据链（存量渲染）

`GET /api/v1/fixtures/{fixture_id}/evidence`

```json
{
  "fundamentals": {"form_home": "W3D1L1", "form_away": "W2D2L1", "h2h_short": "主3胜", "source": "fdhist+football-data.org", "as_of": "..."},
  "intels": [
    {"id": 512, "kind": "伤停", "text": "萨卡（腿筋，缺阵）", "source": "okooo 伤停栏", "collected_at": "...", "agent": "scout-flash"},
    {"id": 513, "kind": "伤停", "text": "穆西亚拉（存疑）", "source": "glm web-search", "collected_at": "...", "agent": "scout-flash"}
  ],
  "analyst": {"verdict": "轮换风险上调负概率", "model": "glm-5.3", "issued_at": "...", "routed_by_js": 0.072},
  "tracks": {
    "ml":    {"h": 0.61, "d": 0.21, "a": 0.18, "issued_at": "..."},
    "llm":   {"h": 0.56, "d": 0.22, "a": 0.22, "issued_at": "..."},
    "fused": {"h": 0.59, "d": 0.21, "a": 0.20, "issued_at": "..."}
  },
  "review": {"queued": true, "verdict": "key_contribution"}   // key_contribution | irrelevant | misleading | null
}
```

## 3. 追问 analyst（AG-UI 协议，增量交互）

`POST /api/v1/fixtures/{fixture_id}/ask` — AG-UI run 端点：

- 请求体 = AG-UI `RunAgentInput`（messages + threadId + forwardedProps: {fixture_id}）
- 响应 = `text/event-stream`，AG-UI 1.0 事件（RUN_STARTED → TEXT_MESSAGE_CONTENT… → RUN_FINISHED）
- 后端：`ag-ui-protocol` Python SDK 的 Agent 实现，内部调 GLM（openai-agents），prompt 注入该场已存证情报（只引已存证条目，回答带来源/时点）
- 前端 A：ai-sdk useChat + 自定义 transport（@ag-ui/client）；前端 B：@assistant-ui/react-ag-ui

## 4. 复核队列（存量 + 提交）

- `GET /api/v1/review/queue` — {items: [{fixture_id, route: "pre_match_js" | "post_settle", js, settled_result?}]}
- `POST /api/v1/review/{fixture_id}/verdict` — {classification: "key_contribution" | "irrelevant" | "misleading"} → 只进评测集

## 5. 验证报告三列（票 04/05 消费）

扩展现有验证报告端点：`track=ml|llm|fused` 各列 + 配对样本数 + DM p 值 + Tier A/B 达标状态行。
