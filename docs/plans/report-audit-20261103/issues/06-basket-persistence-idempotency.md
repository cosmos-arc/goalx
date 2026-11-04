# 06 选注篮全站持久化 + 提交幂等

Type: task
Status: resolved (PR #220, 2026-11-04)
Blocked by: 02

## Question

选注篮是页面局部 useState，三个页面各挂一份、路由切换即丢；goals 页另有
独立内联篮。批量提交 all-or-nothing：中途失败整体报错并保留全部选择，重试
会重复保存已成功腿。是否做全站共享持久化篮 + 逐腿结果呈现 + 重试不重复？

## 事实（2026-11-03 main 实测）

- `web/src/components/had-basket.tsx:31-37` 六个 useState；
  `fixtures-page.tsx:78` / `market-had-page.tsx:37` /
  `fixture-research-page.tsx:67` 各自挂载；`market-goals-page.tsx:52-57`
  独立内联篮——跨页草稿不存在
- PR #214（03ac389）只改了 `pick` 换选序（同场替换先于上限检查），未动持久化
- `market-goals-page.tsx:144-174` 顺序 for 循环逐腿 `createBet`；onError 只
  设消息（173），已成功的 `created` 数组被丢弃；无幂等键
  （`api/goalx.ts:109-111` payload 无 key）——第一条成功第二条失败后重试
  = 第一条双行
- localStorage 现仅用于主题（`lib/theme.ts`）

## 验收

1. 篮子状态单源（context/store），三页面 + goals 页共用同一份草稿；路由切换
   不丢，刷新可恢复（localStorage 或等价），篮内展示各腿截止时间
2. 批量提交逐腿结果呈现（成功 N / 失败 M + 具体哪条），失败后重试只补发
   失败腿
3. 幂等：后端建注接受客户端幂等键（或等价去重），契约同步；同键重复提交
   不落双行
4. e2e 真实任务（报告四.4 验收）：跨页研究两场 → 构票 → 提交一成一败 →
   重试 → 账面无双行、草稿不丢

## 不变量与人裁决项

- 人裁决：幂等键落库位置与保留期（推荐 bets 表加列、永久保留——对账需要）
- 人裁决：篮子恢复过期策略（推荐无过期，靠截止时间展示自然淘汰）
- 状态方案（context vs store 库）留给实现；注意仓内无状态库依赖，ladder 优先

## Comments

- 2026-11-03 人裁决（内嵌全按推荐）：幂等键 = **bets 表加列、永久保留**
  （对账需要，契约同步）；篮子恢复 = **无过期**，靠截止时间展示自然淘汰。
