# goalx-review-20260928 — 全项目两轴评审票板

> 来源：2026-09-28 全项目 code-review（Standards/Spec 两轴并行子代理 + 主线逐条复验），
> 基线 HEAD = a35338f。文档同步部分已单独走 PR #94（spec-v1.0 取代指针、
> glossary 注额口径、pool.py 份额来源），不入本票板。

## Decisions so far

- 子代理发现复验后：**撤回 1 条**（handlers.ts mock pool-states「契约缺路径」——
  v1.json:1002 实有该路径，剩「mock 无调用方」并入票 08）；**收窄 1 条**
  （combo-engine feedOrder：rank*ForFeed 函数两页在用，生产零读的只是
  `ComboResult.feedOrder` 字段，降为 nit 并入票 08）。
- Spec 轴重新归因（规格文档链审计后）：staking 上限=代码对新裁决、glossary 旧
  （已在 PR #94 同步）；设置页/真金纪律=有记录缓议，不开票；**review_errors
  硬编码恒不通过为唯一真代码缺口**（票 03）；market_skill 聚合口径待人裁决（票 09）。
- 复验中的计数微修：ssl_context 7/8 非 7/7；market-pool 821 行非 822。

## Frontier

01 backend-sql-ownership（最重）→ 02 pool 策略下沉 → 03 review-errors 接线 →
04 backend 死代码 → 05 backend 去重三件 → 06 web research 篮子收编 →
07 market-pool 拆分 → 08 web 死代码+重复家族批票；09 market_skill 聚合裁决
（ready-for-human，随时可裁）。

## 2026-09-28 开工收官（main = 4e00c43）

- **六票落地全合 main**：01→PR #95、02→PR #96、04→PR #97、05→PR #98、06→PR #99、
  08→PR #100（批一：死代码全清+localTime/dayNoteOf/StatusToast 收敛；中件缓办清单在票内）。
  另 PR #94（文档链同步）。净删 ~800 行，行为零变化（contract 零 diff、762+210 测试、e2e 42 例全绿）。
- **未动**：07（market-pool 拆分，纯可维护性低优先，本批未做）；03/09 仍阻塞在
  人裁（「无系统性错误」判定规则 / market_skill 聚合口径）。
- **CI 教训**：PR #98 曾红——odds_math→markets 越层被 lint-imports 分层契约抓；
  本地定向 ruff 不含 lint-imports，后端改动收尾必须跑全量 `task lint`（已修：consensus_probs 改收 selections 参数）。
- **复核 nit（非阻塞，留待下批）**：had-quote-ui 转发 localTime 是 Middle Man
  （仅 goals-ui 一个外部消费方）；useHadBasket 的 fixtures=[] 缺省无人走缺省路径。
- **测试旁路形态记录**：test_table_names_stay_literal 只拦 `{}` 插值，
  `%s`/字符串拼接/引号内插值不在网内（当前树内零命中；DDL 归 INFRA 豁免已缓解）。

## 2026-09-28 终局：票板 9/9 全清

- 用户两裁（采纳推荐）：③ review_errors = misleading ≤10% 且 done ≥10 条；
  ⑨ market_skill = 只认部署版本（latest_model_version，样本不足诚实降级）→ PR #101。
- ⑦ market-pool 拆分（821→417 行 + pool-slots/pool-generators 两组件）→ PR #102。
- 全批合计：PR #94/#95/#96/#97/#98/#99/#100/#101/#102 九连，全部 base=main CI 绿合入。
- 剩余非票事项：票 08 中件缓办清单（骨架屏/IntelRow/kickoffMs/joint-EV/
  earliest-kickoff/footer/handlers satisfies）；两条复核 nit。下轮触点顺手收。

## 2026-09-28 晚补收（PR #103-105）

- PR #103：GOALX_SRCT_NIGHT_OFF 全程班闸门（重建自被并行会话清理的工作区原稿，
  修正原稿插位缺陷——pop 须在 selected 固化前）。
- PR #104：票 08 批二（中件五处 + nit 两条，净 -35 行）。
- PR #105：修 PR #101 漏跑的 contract-codegen（schema.d.ts 注释漂移曾致 main CI 红三连）。
- **流程教训两条**：①改端点 docstring/契约必须 export+codegen 两 diff 同提交；
  ②merge 前 watch 要显式确认无 fail/未起跑 checks（--watch 空转≠绿，#104 曾带空 CI 合入）。
