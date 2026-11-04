# 10 goals4 前端开面 + 同场联合语义

Type: grilling
Status: resolved (PR #224, 2026-11-04)

## Question

goals4（四场进球）后端已备（pool-mapping-goals4 板全 resolved：8 档边际 /
映射 id 直连；PR #206 档位概率），前端零入口。开不开面？开则同场主客两档
联合概率必须由比分矩阵聚合，不能把八个球队边际当独立事件连乘（报告路线 D
红线）。不开则收口半供给状态。

## 事实（2026-11-03 main 实测）

- 前端零入口：`router.tsx` 路由仅 `/markets/had|goals|pool`；
  `market-tabs.tsx:10-12` 仅胜平负 / 进球 / 14 场任九；goals4 字样前端零
  出现（生成物除外）；pool 页不传 marketCode（后端默认 `ttt14`，
  `api/pool.py:163`）——现有 goals 页是竞彩 ttg/crs，非足彩 goals4
- 后端已备：期次详情 selections 按 market_code 分支渲染 8 档
  （h0..h3/a0..a3，ML 矩阵行 / 列和，3+ 聚尾，PIT 同口径）；生成器端点对
  goals4 422 护栏在位（`api/pool.py:246`，"一场两档硬跑产出废票"）
- 联合语义缺口：`modelling/score_matrix.py:226-238` `pool_margin` 给档位
  边际；同场（h 档 × a 档）联合需比分矩阵格聚合，边际连乘口径错误
- 供给：goals4 2026-11-01 修复（param=94,0 独供），26226 期恢复后 live

## 验收（若裁决开面）

1. 池页加 goals4 期次入口（marketCode 贯通前端 → API），进球档选择面
   （8 档 / 场）
2. 同场联合概率 = 比分矩阵格聚合；一致性测试钉死：联合的边缘和 == 现有
   `pool_margin` 边际
3. 金额 / EV 建议用联合口径；跨场独立假设沿用池票现状并披露
4. 生成器端点（搏冷 / 目标票）暂不放行 goals4（一场两档复式爆炸风险），
   422 护栏维持

若裁决不开面：后端补 goals4 在 pool 消费面的明示（避免长期半供给），
本票闭为 wontfix 记录。

## 不变量与人裁决项

- 人裁决：开不开 / 优先级（相对票 06-08）；开则人裁决档位展示口径
- 联合概率实现位置（score_matrix 内聚合函数 vs 调用方）留给实现

## Comments

- 2026-11-03 人裁决（grilling 过账）：**开面，批二末位**（06-08 之后）——完整
  构票 + 同场联合概率（比分矩阵聚合，一致性测试钉死）+ 金额/EV 联合口径；
  生成器端点维持 422 不放行；正确性与通用决策链优先。
