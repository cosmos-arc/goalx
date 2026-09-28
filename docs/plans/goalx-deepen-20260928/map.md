# goalx-deepen-20260928 — 架构深化票板

> 来源：2026-09-28 /improve-codebase-architecture 全仓走查（120-commit churn 定位热点
> → 后端 / web+api 两路子代理走查 → 主线对尖锐主张逐条源码复核）。基线 HEAD = a5e261f。
> HTML 报告在 OS temp 目录（易逝），候选结论与 file:line 证据录于各票「现状」节。
> 用户裁决（2026-09-28）：七候选全有价值直接建票；同日 /to-tickets 复切——原票 02
> （数据集注册 seam）自含两阶段超一个上下文窗口，按 expand–contract 拆为 02+03，
> 其余六票补 Blocked by 与验收清单后重编号。

## Decisions so far

- 候选→票映射：C2→01 / C1→02+03（expand 首批 / 全量 migrate+contract）/ C3→04 /
  C4→05 / C5→06 / C6→07 / C7→08。
- Blocking edges（只连真门）：02←01（01 是形状证明）；03←02。04-08 无硬门可立即开工，
  但**建议串行 01→02→03→04→05**：03 与 04 都重写 cli/tasks 的 corpus 接线（双改会撞），
  04 与 05 都动 srct_odds。06/07/08 与后端族无交集，随时并行。
- ADR 护栏（各票不得重开）：0005 Prefect 统一编排、0008 领域包 SQL 归属（不复活 store/ 层）、
  0011 CorpusStore 混合底座、0012 跨源物化映射。
- 明确不动清单（走查确认 deliberately fine）：shadcn copy-in（coverage 分母）、
  api/goalx.ts + client.ts（真 seam）、进球篮子独立变体（had-basket.tsx:27 文档化故意）、
  rate_limit.py（已 deep）、data/mapping.py（走查最佳行为模块，ADR-0012 刚落地）。
- 走查记录在案的「暂不动」（痛了再议）：corpus_gate 四职责混杂（866 行，若 05 后仍痛
  再拆）；run_night 13 参接缝参数宽度；config.py 单 Settings 类累积；CorpusStore 逐源
  checkpoint 尾部 + PRAGMA 手迁小灶（02 收 checkpoint 表登记时顺带看一眼）。
- **票 01 resolved（PR #108，cf23baf）**：两端点委派 tasks.*，api 层 ingest
  内部件 import 归零；importlinter 把 tasks 拆出 `api | tasks | server` 带独立
  成层降至 api/server 之下（执法记录落 ADR-0008 执法节）。review 修正：pool
  502 元组补 RuntimeError（zucai_official 源页异常面）；契约仅两端点
  summary/description 文案对齐。已知残余（02/03 收口）：router 写走 task_conn、
  状态读走请求连接，仅「app 注入 Settings≠env」形态分叉，生产同库无虞。
- **票 02 resolved（PR #109，68bc45b）**：datasets.py 注册表（DatasetSpec）
  推导 cli/flow/deployment/RESUME；首批 guardian-sync/pool-snapshot/srct-night
  入册删手写段。口径偏差已声明：checkpoint 表=规格核对非运行时推导（分层
  所限，核对面单向）；srct-night 手工 CLI failed>20 截断（与定拍收敛）。
- **票 03 resolved（PR #110，e7fb17d）**：注册面 3→12（全量单 cron 定拍面，
  含判死面 srcb-collect/eu-odds-closing resume=False）；schedules 手写面 14→5
  （组合面+双 cron draw-results）；flows 删 9 同构壳；cli 删 4 handler；
  RESUME/DEAD 全推导。验收 2 缩窄口径（人追认）：corpus 装配收敛=有 tasks
  对应的定拍命令；手工 one-shot 命令归 04。留手写：official-reconcile cli、
  draw-results 双 cron、组合面。
- **票 04 resolved（PR #111，d9d0281）**：data/ingest/shell.py 收所五族 UA
  （值不归一，lean-audit 裁定）+ sporttery_json_headers（uniform/jc 同串）+
  browser_ssl_context/polite_client（无 re-export，tasks×17/cli×6 改引）；
  uniform.stats_dict 删换 asdict（唯一纯拷贝）。附注：票期间混入的限速 25
  经用户确认为提速令（见票 05 批次）。附带发现：polite_client limits= 配自定
  transport 自始 no-op（池默认 100，有效面 retries=3），改并发留另票。
  金标手法：头字典用 items() 序列断言钉键序（dict== 序不敏感拦不住字节序变化）。
- **票 05 resolved（PR #112，b89e728）+ 相邻 PR #113（b0acb76，用户提速令
  25/min + 2.4s±0.8s 独立落地）**：内核 data/silver.py（corpus 层）——
  keep_value_changes 合一（注入式，两形态同构测试钉死）、write_partition +
  write_dataset_file 两形写舞步、共享工具迁入（provider/version 参数注入
  保层序）、CorpusStore.silver_path()/raw_dir() 唯一落点（gate/duckdb/六
  builder 自拼归零）。CONTEXT.md 增 SilverKernel 词条。留观察：provider/
  dataset/version 三元组 7 处轻度 Data Clump（层序约束，痛了再收）。
  **06-08 的接手须知**：出网头一律引 shell；api 域面 import 非内部件勿误清；
  silver 路径/写件一律走 silver_path/write_*，勿再自拼。

- **票 06 resolved（PR #114，44bcec4）**：clv/forward + 三端点（forward-skill/
  m3-protocol/baseline-quality）+ evidence dict[str,bool] 升契约 typed models，
  键集冻结（对 main 逐一核对）；页面删 ~230 行重推导层，msw satisfies 锚点
  自动收紧；export+codegen 同提交（契约 +~800 行）。**附带发现（未修）**：
  _mean_ece 恒 None（ece 已拆三键）→ Tier A 的 ece_no_degradation 检查长期
  False——重建该检查属另票（若修：三键加权 + 调 m3 阈值测试）。模型落
  evaluation/llm 域包（层序：api 只挂 response_model）。

- **票 07 resolved（PR #115，f5fb248 + 追认 PR #116，998a6dd）**：web
  lib/query-keys.ts 注册表 = queryKey + 失效清单唯一住所（11 常量 + 7 工厂 +
  3 失效辅助只包既有清单）；/fixtures/today 双键合一
  todayFixturesKey(days)（单日页显式 days=1，后端缺省即 1）；消费方 15 文件
  （票面 12 为漏数）+ 漏列 backtest-runs 一并收编；散键字面量全站归零。
  **staleTime 人裁决终裁 = 删（用户 2026-09-28：30s 保鲜过度设计，单用户
  重拍可忽略、赔率新鲜优先）——PR #116 删 TODAY_STALE_MS 回全站默认 0，
  overview 注释去假 invariant；验收 2 口径缩窄人追认（双键合一=同端点单份
  缓存条目）**。评审收口：fixtureEvidenceKey 接 null（键值逐一等价，去
  哨兵）、stakeAdviceKey 对象整进键。**08 接手须知**：web 侧新增查询必须
  先入注册表再消费；失效只走辅助/常量，勿手写键；staleTime 不设值。

- **票 08 resolved（PR #117，6edd547）**：router 残留两规则下沉——前瞻
  5 分类（票 36 规则）→ evaluation/bet_review.review_views（BetReviewView
  随迁）；pool 概率源策略（模型优先→欧赔去水）→ data/pool.match_views
  （PoolMatchView/PoolSelectionView 自 betting/pool_strategy 迁 data/pool，
  权属=表主人+data/today.py 先例，pool_strategy 保持纯函数反向引）；
  evidence 第二份逐行组装收敛到共享骨架 match_rows_with_fixture_ids。
  seam 单测六态矩阵+链序钉法（live 挂已开赛场）；断言自 test_api 三条
  **复制扩全**（原三条保留为 HTTP 面抽查）。contract 零 diff。**后续候选
  （未做）**：data.pool 入 importlinter 清单须先破 modelling 上行环（函数级
  局部导入，非一行事）；llm 域四份同款骨架可收敛；router 小型状态推导
  （_status_for/_match_state）痛了再议。

## Frontier

**01-08 全部 resolved ✅ 票板收官（2026-09-28）**——八票十一个 PR 全合 main：
#108（tasks 委派）/#109-110（数据集注册表 3→12）/#111（采集壳）/#112
（silver 内核）/#114（契约 typed models）/#115-116（query-keys 注册表 +
staleTime 终裁删）/#117（router 规则下沉）+ #113（提速令独立落地）。
本目录即转正快照（docs/plans/goalx-deepen-20260928/，2026-09-28 随票板收官
整体拷贝自 .scratch，相对布局不变——跨链按文件名可解析）。

## CI 教训（沿用 goalx-review-20260928）

- 动契约的票（06）必须 contract-export + contract-codegen 两个 diff 同提交（PR #105 教训）。
- 后端改动收尾跑全量 `task lint`——定向 ruff 不含 lint-imports 分层契约（PR #98 教训）。
- merge 前 watch 显式确认无 fail / 无未起跑 checks（--watch 空转≠绿，PR #104 教训）。
