# 官方赛果并行对账（uniform 族）与 coverage 维表

Type: impl
Status: resolved
Blocked by: none（matchResultStatus 枚举深探是本票第一步，实证不过则回报改道，勿硬上）

## 问题

ADR-0001 定"官方开奖是系统唯一事实源"，但现状事实源是源D（live.500.com 彩果页，票 42）——研究 01 时代"官方网关 403"认知下的被迫妥协。research/17 §五 已实测翻案：403 的只是 jc 族 `getMatchResultV1`，**uniform 族 `getUniformMatchResultV1.qry` 同域同头直通**（日期区间+分页 pageSize=30），载荷带源D 没有的官方口径：`poolStatus`（派彩状态）、`matchResultStatus`（腰斩/无效枚举，语义待深探）、`matchNumStr`（竞彩场次号，与 match_codes 一跳映射）、半/全场比分、winFlag。源D 无法机器判定无效/腰斩/改期（status≠4 一律待人工），官方端点是结算边界场景自动化的唯一正解。

## 范围

1. **matchResultStatus 枚举映射深探**（先行）：采集样本覆盖各状态值，与 01 号研究核实的官方规则（无效场次/腰斩/改期判定）对齐成映射表；语义不明 fail-closed 待人工
2. `data/ingest/` uniform 族采集模块：网络层复用 sporttery.py 薄 GET 模式 + 分页遍历；三时间按票 35 口径（observed_at=本机响应时间，不伪造）
3. **并行对账阶段**（本票交付态，不改结算事实源）：官方与源D 双拉——一致 → 无动作；不一致或单侧缺失 → 待人工清单（复用 draw_sync_runs 的 append-only 元信息+清单模式）；对账一致率/差异清单可查
4. **coverage 维表**（定则 4 落地，research/17 §八）：每源每业务日"覆盖了什么"；absent 断言仅当源声明覆盖该联赛且采集成功；空≠无
5. openfootball/football.json 对账接入（CC0 raw 直下）：赛程对账（对源A 场次）+ 比分对账（对官方/源D）；`score` 字段 dict `{ft,ht}` 与 list 两形态兼容（research/17 §四 实测坑）
6. 新源 join 一律走定则 1：确定性键（matchNumStr / 业务日+时间窗）→ 候选 → 冲突入 manual 队列，禁 LLM 合并

## 不变量与人裁决项

- **切换时点**：何时把官方升为结算事实源（源D 降对账）——建议设并行对账一致率门槛+观察窗口，门槛与窗口须用户裁决；切换本身可另开小票
- coverage 维表粒度（联赛级 vs 场次级）与呈现位置
- 对账差异处置流程（纯人工清单 vs 引入信源优先级）
- IP 地域注记：本机家宽直通；serve 搬机房需中国可达出口（EdgeOne WAF 567 情报，research/17 §七）

## Answer（2026-09-20）

已实现，分支 `feat/official-results`（基于 origin/main acaa006），两个 commit
（30dfdc5 实现 + e96bae5 评审修正），未 push（按派单惯例，PR 待用户指示）：

### 第一步：matchResultStatus 枚举深探（当日完成）

七周实测（2026-08-06..09-20，725 场全分布），枚举空间定型、无其他形态：

| matchResultStatus | poolStatus | sectionsNo999 | 语义 |
|---|---|---|---|
| '2' | Payout | "H:A" + winFlag | 完场已派彩（终态） |
| '2' | 空 | "H:A"，无 winFlag | 完场未派彩——**可持久存在**，poolStatus 不是"已结算"指示器 |
| '2' | Refund | "无效场次" | 官方判无效（退款）→ void |
| '0' | 空 | "取消" | 官方取消 → void |
| '1' | Close | 空 | 未完场 |

未知状态值 fail-closed 进待人工。**join = `match_codes.source_match_id = str(matchId)`
一跳确定性键**（源A calculator 已写同一官方 ID，实测主库 match_codes 尾部即 2041615 样式）。

### 实现要点

- **migrations v14**（data 域，OWNERS 登记）：`uniform_result_observations`
  （append-only + UNIQUE(match_id, observed_at) 同跑幂等；poolStatus 迁移多跑多行留痕）、
  `draw_reconciliation_runs`（append-only 运行日志，一致率分母=compared）、
  `source_coverage`（现态维表 UPSERT——四态 covered/fetched_empty/fetch_failed/not_covered）。
- **ingest/uniform**：薄 GET+分页；解析纯函数（fixture=实测 138 场载荷，五种形态齐）；
  winFlag 与比分不自洽/未知状态/终态非数字比分 → fail-closed 待人工；
  **不 import 任何赛果**（ADR 0001 澄清条款，并行对账阶段事实源不动）；
  observed_at=批次起始时刻（与源D 票 42 同口径，published_at 不伪造）。
- **data/reconcile**：比较纯函数——全场严比、半场两侧齐全才比（源页面半场可缺）、
  void 双向冲突、终态缺事实 → 待人工清单（不自动冲正）。
- **ingest/openfootball**：赛季文件 raw 直下（一次一联赛，404=not_covered 不炸整跑）；
  score dict/list 两形态；join=开球日±1+双方 team_aliases 规范化解析（ingest 层可
  import modelling，分层方向合法）；多候选进人工、**禁 LLM 合并**；联赛映射 11 个
  （五大+英冠+德乙/法乙/荷甲/荷乙/葡超，2026-27 实测有文件的竞彩联赛）。
- **源D 挂 coverage**：每业务日页面级一行（页面无联赛细分）。
- **接线**：`official-reconcile` task/flow/CLI（清单逐条 warning 打印）+ 调度
  08:30（跟源D 08:00 补扫落事实之后半小时）。无 API/契约变更（零漂移实测）。

### 隔离库真实冒烟（主库零改动）

- uniform：84 场观测、16 终态比对 **14 一致 0 不一致**、2 缺果——当日待出一场
  （周日001 0:0）+ **周三014 莱万特官方无效场次**（research/17 §五 那场：源D
  status≠4 结构性抓不到的 void，官方口径第一天就抓到了，进待人工）。
- openfootball：21/21 全一致；38 unmatched（未映射联赛+回填滞后，信息位不报警）。
- coverage 即刻判别力：en.1=40/en.2=83/de.1=27/fr.1=36/pt.1=53 covered，
  **nl.2/de.2/fr.2=not_covered**（2026-27 确无文件——"空≠无"四态区分成立）。

### 评审与修正

code-review 两轴（Standards/Spec 并行 sub-agent）：Standards 0 硬违规（ADR-0008/0001
全过）；Spec 抓到三真问题已修（e96bae5）——注释声称的 matchDate ±1 放宽未落地
（晚场漏采）、run 行 unmatched 恒 0、404 与暂时失败混淆。留档未修（判定可接受）：
`data.reconcile` 未列 importlinter layers（与 data.pool 同先例，未列=不受约束）；
stats_dict 双份（caiguo 先例）；openfootball 赛程对账以 unmatched 信息位呈现
（回填滞后 ~一轮，差异做人工队列是噪音）。

### 追加：直接切换已执行（2026-09-20 晚，用户裁决"目前没有上线使用，没关系"）

用户免掉观察期，切换当晚落地（commit 84c4f0f）：

- **ingest/uniform 升事实源**：终态观测（比分或官方 void）→ import_draw_results；
  库内不同结果仍不冲正（ADR 0001 澄清条款不变）；draw_sync_runs 元信息由
  uniform 写（UI 同步面板通道不变，/draw-sync 端点重指 uniform，契约文案
  同 commit 更新）。
- **ingest/caiguo 降审计源**：audit_draw_results（解析→匹配→对账不落库）；
  窗口 = recent_business_dates（近 7 天已开赛场次，含已落果日——与"仅待出"
  的候选推导不同语义）；未完场行静默跳过。
- **调度**：draw-results-sync（*/30 晚间+08:00 补扫）自动变为官方源（同 flow
  换任务体）；official-reconcile 08:30 = 源D 审计 + openfootball 对账。
- **migrations v15**：source_coverage 四态（not_covered/fetch_failed 分离）；
  v14 已在主库应用，表重建换 CHECK。
- **切换冒烟（隔离库）**：官方导入 1/unchanged 6/对账 17/17 一致；
  **源D 审计 105/106——抓到真实分歧：周四003（09-17 欧罗巴 克里特 vs
  霍芬海姆）库内 manual 1:1 vs 官方 2:0 + 源D 2:0 双源一致 = 人工录入
  错误**，按守则走人工更正通道（待用户在 UI/CLI 更正，审计层已留痕）。
- **运维注记**：serve 的 Prefect 子进程每次 run 重新 import 工作树代码——
  切换随分支代码在主库自动生效（PR 待指示）。
- **清理轮（commit 7315619）**：v15 折叠回 v14（分支未合入，四态 CHECK 直接
  落 v14 终态；主库 schema_migrations v15 记账行删除，纯簿记）；
  candidate_business_dates 迁 uniform（事实源自带窗口推导，与审计模块解耦）；
  对账清单构造统一 reconcile.add_manual；test_api 死 fixture、"并行对账阶段"
  过时措辞、web mock/断言 500.com 残留全清。切换+清理合计五 commit
  （30dfdc5→7315619）。

### 待追认（pending-user-confirm）

1. ~~切换时点门槛~~ **已裁决（2026-09-20 晚）：免观察期直接切换，已执行**（见上节）。
   后续观察点转为日常审计：源D/openfootball mismatch 率与待人工清单消化。
2. **coverage 粒度已按源粒度落地**：uniform=日×联赛、源D=业务日页面级、
   openfootball=赛季×联赛；呈现暂无 UI（DB 可查 + CLI）。要场次级或 UI 呈现再开票。
3. 对账差异处置维持纯人工清单（信源优先级自动裁决未做——等首个真实分歧案例再定）。

### 质量门

`task fmt-check/lint/type/test` 全绿（463 passed，coverage 93.81% ≥90）；
import-linter 1 kept；schema-doc 生成块已同步（docs/db-and-domains.md 三新表
章节+注解）；contract-export 零漂移。web 未动（本票无 UI 契约）。
