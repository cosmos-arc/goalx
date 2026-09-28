# 08 backend：router 残留领域规则下沉——前瞻分类与 pool 概率源策略

Status: resolved（2026-09-28，PR #117 合入 main 6edd547）
Blocked by: None（可立即开工；与 06 同动 api/validation 相邻面但不同文件，可并行）

**交付（用户视角）**：无可见行为变化——但前瞻纳入/排除的判定与彩池概率源选择从此
在领域层可单测、可被验证门复用，router 只做 HTTP 形状。

## 现状

- api/bets.py:193-233 `_review_view`：**前瞻纳入/排除 5 分类推导**（锁定早于开球的全腿
  对比、收盘完备性）是领域规则，住在 delivery 层，自读 fx_store.kickoffs_for_fixtures
  + clv_mod.closing_leg_counts；前端只贴标签（bets-page.tsx:55-62 FORWARD_LABELS）。
  validation 门的同类规则在 evaluation——同一概念两处住所。
- api/pool.py:205-263 `_match_views`：**概率源选择策略**（模型优先→欧赔去水兜底）+
  逐行 EV 组装在 router，三端点共用；api/evidence.py:219-231 第二份同款逐行组装。

importlinter 分层与 SQL 归属测试都管不到「逻辑住哪」——ADR-0008 的 delivery-委派
精神无执法面，纯靠 review 眼睛。

## 方案

两条规则各自下沉所属领域包，router 委派。deletion test 判定：纯搬迁，caller 不变，
复杂度不会在别处重现。

## 实现决策

- 前瞻分类归 evaluation（前瞻验证表在那，规则同源——人裁决确认）；pool 概率源策略
  归属以 docs/db-and-domains.md 权属为准（data/pool 或 betting）。
- 下沉函数签名收数据进、视图出，不带 HTTP 概念（status_code / Request 不入领域）。
- evidence 侧第二份逐行组装收敛到同一领域函数。

## 测试接缝

- 领域 seam 单测：前瞻分类给 (bet legs, kickoffs, closing counts) 断言 5 分类——把
  现 api 测试里的领域断言搬过来，不是新写。
- router 测试只断言委派与响应形状（先例：test_api*.py 家族）。

## 范围外

- api/fixtures.py:257-349 的 research 视图装配不动（是装配不是规则）；api/markets.py
  的 N+1 查询形态不动（性能问题另议）；FORWARD_LABELS 前端文案不动。
- 「delivery 不藏领域规则」的静态执法（importlinter 补规则之类）本票不上——先靠
  搬迁 + review，为一条规则造静态分析不值。

## 不变量与人裁决项

- 不变量：API 响应逐字段不变（contract 零 diff）。
- 人裁决：前瞻分类归 betting 还是 evaluation（建议 evaluation，理由见实现决策）。

## 验收

- [x] 前瞻 5 分类下沉领域包并有领域 seam 单测（断言自 api 测试搬移）
- [x] pool 概率源策略一份，evidence 侧第二份逐行组装收敛
- [x] router 仅剩委派 + HTTP 形状；contract 零 diff
- [x] `task test` + 全量 `task lint` 绿

## Answer

**落地**（PR #117，单提交 9ae128b，+363/−177，7 文件）：

- 前瞻 5 分类 → **evaluation/bet_review.py**（新模块）：`review_views(conn,
  rows)` 逐字等价搬迁 `_review_view`；`BetReviewView` 随迁（类名不变 → 契约
  schema 不变）；api/bets.py 两调用点只剩委派，fx_store/clv_mod import 净删。
- pool 概率源策略 → **data/pool.py**：`match_views(conn, pool_period_id, now)`
  逐字等价搬迁 `_match_views`；`PoolMatchView/PoolSelectionView` 自
  betting/pool_strategy 迁 data/pool——权属依据：pool 表 SQL 全在 data/pool
  （表主人）、data/today.py 是 data 层拥有 pydantic 读模型视图的先例、
  betting/pool_strategy 章程「纯函数无表无 SQL」不允许持 conn 装配；
  pool_strategy 反向引（betting→data 合法向下）。
- evidence 收敛：共享骨架 `data/pool.match_rows_with_fixture_ids`（对阵行+
  fixture_id 桥接）一份，期次详情/搏冷/证据卡三路共用。
- seam 单测 test_bet_review.py：六态全矩阵 + 判定链顺序钉法 + closing 按腿
  数对账。措辞更正：断言是**自 test_api 三条复制扩全**（test_api.py:598/725/
  912 原断言保留为 HTTP 面抽查，未删）。

**人裁决**（票面建议项）：前瞻分类归 evaluation（前瞻验证表同包、规则同源），
按票面建议落地；pool 概率源策略归 data/pool（非 betting），依据见上。

**评审收口**（双轴）：
- Standards P1：live 分支链位未钉（原 live 挂未来场，判定链挪动测试不红）
  → live 注改挂已开赛场（fixture 2）钉住 live>post_kickoff；live 造态改用
  领域 API（BetDraft(mode=LIVE)+record_purchase，bankroll 兜底 0.0 无需铺
  余额）；slip_id 置 NULL 是唯一保留的 SQL 造态（无领域 API 可产，诚实）。
- Standards P2 采纳：共享 conftest db fixture + seed 助手（去本地遮蔽）；
  第二测试死调用改真断言；「搬移」措辞改「复制扩全」；pool_strategy 浮置
  归属注释并入 docstring 一处。
- Spec 六项全符合（含 contract 零 diff 独立复验、_match_views 行为等价逐项
  核对、无循环导入实证）。

**不变量验证**：规则体对照 git show main: 逐行等价；contract-export 零 diff
（schema 名/描述/字段序原样，生成物无 diff）；788 后端测试全绿；task lint
（含 importlinter）/type（strict 0 错）/task check（含 e2e）全绿；PR #117
CI 16 项全绿。

**后续候选（票面外，未做）**：
1. data.pool 补进 importlinter 分层清单——**不是一行的事**：
   model_prob_for_fixture 函数级局部导入 modelling.forecast 是
   data→modelling 上行边（既有环，grimp 可见），入清单即红；需先破环
   （如注入 forecast 读取器）再入清单。
2. llm 域还有四份同款「对阵行+桥接」骨架（llm/collect.py:190、gate.py:335、
   okooo_formation.py:200、scout.py:209），match_rows_with_fixture_ids
   现成可作收敛目标。
3. router 仍住的小型状态推导（api/pool.py _status_for、api/evidence.py
   _match_state、_ev_snapshot_view）——比本票两条规则小一个量级，痛了再议。

## Comments
