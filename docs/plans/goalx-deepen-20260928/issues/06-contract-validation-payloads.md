# 06 contract：validation 自由字典载荷建模——闭合契约上唯一的洞

Status: resolved
Blocked by: None（可立即开工，与后端族无交集）

**交付（用户视角）**：验证页的 CLV / 前瞻技能指标直接吃契约类型——后端改键在 CI 红
灯可见，而不是页面上静默消失一行。

## 现状

全仓 contract-first + CI drift gate，唯独**最富的载荷**走了自由字典：

- evaluation/validation.py:94-95：`clv: dict[str, Any]` / `forward: dict[str, Any]`；
  api/validation.py:82,91,105 三端点（forward-skill / m3-protocol / baseline-quality）
  返回裸 dict。
- codegen 侧 schema 退化为 unknown → apps/web/src/pages/validation-page.tsx 手写
  ~300 行重推导：pick/asNumber/asString/asBool（:36-57）、CLV_KNOWN_KEYS /
  FORWARD_KNOWN_KEYS（:203-213）、clvMetricRows / forwardMetricRows（:291-412）、
  bestForwardSkill（:415-437，自注「与服务器同构 (validation.py)」）。
- 无双向漂移检测：后端改键 → 行静默消失；后端加键 → 静默进 UnknownMetrics 堆
  （:511-525）。契约 seam 在别处都成立，恰恰在载荷最富处缺席。

## 方案

这批载荷升为契约内 typed models（pydantic 响应模型细化），走既有 contract-export +
contract-codegen 流水；页面删整层重推导与手维护键集合，直接消费生成类型。

## 实现决策

- 分工沿用既有惯例：evaluation 出结构、api 出契约模型；**键名即契约**，趁机冻结现键集
  （不另起改名，把漂移检测和改名 diff 分开）。
- UnknownMetrics 兜底：类型化后如仍有自由扩展位，显式留一个受控 additional 字段，
  不再是整片 Any。
- 顺手同族小件：api/evidence.py:363,390 的 `dict[str, bool]` 响应模型改显式模型
  （goalx.ts:280,285 的 as 断言随之删）——一次 codegen 带走，不另开票。

## 测试接缝

- 后端：test_api_validation.py 断言响应模型形状（先例就在此文件）。
- 前端：msw fixtures 类型锚点随生成类型收紧（PR #106 的 `satisfies DeepReadonly<T>`
  先例升级为直接约束）；页面测试只断言渲染行，pick 辅助随删不补测。

## 范围外

- 不改任何指标口径/数值；不动 evaluation 内部计算；契约其他端点零 diff。

## 不变量与人裁决项

- 不变量：contract export + codegen 两个 diff 同提交（PR #105 教训）；页面渲染输出
  迁移前后等价（键集冻结保证）。
- 人裁决：键名要不要趁机规整（默认不动）。

## 验收

- [x] clv/forward 及三端点载荷进契约为 typed models，键集冻结（review 对 main 逐一核对：无改名无丢键；唯二加性键 tier_b.dm_p_reported=null / mixed.legs=0 无消费者，声明接受）
- [x] export + codegen 两 diff 同提交（契约 +~800 行），CI drift gate 绿
- [x] validation-page 删重推导层与手维护键集合（~230 行），渲染输出等价（review 修正：regression 行恢复「slope 无值不出行」旧口径）
- [x] evidence 的 `dict[str, bool]` 顺手件带走（RecordedView），goalx.ts 两处 as 断言删除

## Answer

PR #114（合 main 44bcec4）。模型落 evaluation/llm 域包、api 挂 response_model
（票面「api 出契约模型」的字面偏离——层序上 evaluation 不能 import api）。
页面契约类型直取；msw fixture 的 satisfies 锚点自动收紧；后端测试 report
调用点 .model_dump()。

**附带发现（记录不改）**：`_mean_ece` 取 'ece' 恒缺（组指标已拆
ece_h/d/a）→ 恒 None，Tier A 的 ece_no_degradation 检查**长期为 False**——
类型化揭出的历史行为，显式 return None 保尸并注释禁静默复活；重建该检查
（如三键加权）属另票。

**TS 形态记录**：pydantic 带默认值字段在生成 TS 里渲染为非可选 + @default
（生成器行为），Optional 字段才是 `?: | null`——页面拿硬类型、后端留容错。

## Comments
