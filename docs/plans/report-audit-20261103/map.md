# report-audit-20261103：外部报告审计 → 优化完善计划板

开板 2026-11-03。源报告（基线 fe2b57d，2026-11-02 16:47，含 PR #206）已归档
`report.md`；建板前对全部代码级断言在当前 main（d0c4e7e，含当日合入的
PR #207-214）做了逐条核查——**报告三.2 已被 PR #210 修掉，其余核实仍成立**。

## Destination

报告 P0 两包（票据事实统一 / 验证器语义）全修复合 main；P1 的"纸面决策链可
完成"（持久化篮+幂等 / 首页无候选归因 / 分时钟）与"真实价格前瞻实验预注册挂跑"
落地。终态：每条候选可回答——为何研究、当时能否买到、成什么票、实际发生了
什么、这类优势能否重复。

## Notes

- 本图带执行（沿用既往板惯例）：task 票由会话认领直落 PR；grilling 票需用户
  在场。交付走三轴 review（implement 内嵌 code-review + correctness-review）。
- 批次：批一 = 票 01-05（P0 正确性），批二 = 票 06-10（P1 决策链 + goals4
  裁决）。
- **2026-11-03 人裁决全部过账**（01-10 内嵌项与 grilling 项，详见各票
  Comments）；票 09 判读规则自该日冻结。全票无阻塞，批一 01-05 可领。
- **批一执行进度**：票 01-05 已合 main（PR #215/#216/#217/#218/#219，
  2026-11-03/04，三轴全过）：01 任9 组合单源（9 选子集/预览端点/v36
  史行隔离）；02 票型显式化（SlipForm 词条/slipFormPlan 单源/单双关
  显式选择）；03 M3 验收器（ECE 三键加权真算+不足组不计权/Tier B 方向
  核验三态）；04 情报 as_of 时点边界（store 谓词+三链路传 moment+
  徽章提交时刻盖戳 fail-closed+写入规范化；correctness 三修 W1/I1/I2）；
  05 多盘口语义收口（认证界→端点敏感性分析，certified 钉死 OU 带内，
  x12 升顶层披露位口径=certified 行；correctness F1/F2）。**批一收官**，
  批二 06-08 可领。
- **批二执行进度**：票 06 已合 main（PR #220，2026-11-04，三轴全过）：
  选注篮全站持久化+提交幂等——v37 bets.client_request_id 部分唯一
  索引+重放先于资格校验短路+竞态回带；前端 basket-store 单源
  （localStorage）+腿稳定 request_id+goals 逐腿提交+飞行中冻结+开球
  倒计时；e2e 跨页构票一成一败重试同键闭环；correctness 六发现
  （F1 冻结必修/F3 计数/F4 形状守卫/F6 竞态钉死/F2 latent 注释/F5 记档）。
  票 07 已合 main（PR #221，2026-11-04，三轴全过）：首页六桶归因
  /today/attribution（判定映射单一出处+下钻+corpus_reader 降级标记+
  晨检同源对账+窗口求和；correctness F1/F2 必修、F3/F4/F5/F7 修）。
  票 08 已合 main（PR #222，2026-11-04，三轴全过）：新鲜度五时钟+
  300s 并行影子——v38 freshness_shadow 双结论日拍挂采集流后拍点
  （correctness F1：23:30 wrap 拍恒退化）+拒过去日回放（F2）+缺表
  晨检降级（F3）+读数归期（F4）+corpus 时差注记（F5）；红线核实=
  stale_source 判定逐字节不动。**task 票 04-08 全收官**；余 09
  （grilling 预注册读数，11-13 首读）与 10（goals4 前端入口 grilling）。
- **票 10 已合 main**（PR #224，2026-11-04，三轴全过）：goals4 四场进球
  开面——score_matrix goals4_bands/joint 单源（联合=矩阵格聚合，bands 由
  联合边缘和导出=结构不变量，rho≠0 反例钉死连乘错误）+ 详情端点
  goals4_joint + 构票贯通（每场|主档|×|客档| 积/4 场固定/闭集码校验，
  预览不完备=0）+ settlement 按侧判档修正（W4）+ /markets/pool4 页
  （8 档双侧选择/联合展示/金额EV联合口径；生成器 422 维持；paper 红线）。
  correctness E1 真 bug：共享 client 测试按 CWD 相对路径曾把 6 条测试票
  写进真实 data/goalx.db（已逐行核实清除，测试重建临时库）。
- **全板收官**（2026-11-04）：票 01-10 全 resolved 合 main（PR
  #215-#217 / #218-#222 / #223 / #224），三轴全过。板工件转正
  docs/plans/report-audit-20261103/。下一自然动作 = 11-13 双周检查点
  首读（票 09 price-edge-audit 与延迟生存率 delay-audit 共用读数日，
  首读描述性不判生死）。
- **票 09 已合 main**（PR #223，2026-11-04，三轴全过）：预注册双落
  （.scratch + docs/plans/report-audit-20261103/prereg-price-edge.md）+
  v39 bet_price_timeline 四实时阶段钩子（发现/决策/确认/票面；收盘
  读时 PIT 派生）+ evaluation/price_edge 检查点读数 + CLI price-edge-audit
  （fixture 簇 bootstrap B=2000 种子固定；三向判读仅正式窗注入；票 08
  影子对表）。correctness 六修（F1 重复腿拒收/F2 判读门控/F3 服务器
  时钟/F4 kickoff 归一/F5 UTC/F6 CLI 干净退出）；自查抓分母 NULL 三值
  逻辑 bug（IS NOT 惯用法+钉死测试）。**11-13 首读**：price-edge-audit
  --date-from <合入日> --date-to 11-13（首读描述性不判生死）。
- 契约变更走 contract-export + codegen 同提交；PR 一律 base=main。

### 建板时已核销（不再开票，2026-11-03 main 实测）

- **报告三.2 纸面锁价绑定 + 记录三分类** → PR #210（3ad6d5b）已落地：
  `resolve_paper_price` 绑 `had_quote_binding` 快照、漂移 409 / 确认重绑、
  `manual_research` 三分类隔离出成绩面与结算分母；live 路径按实际条款记账
  不绑价（设计如此）。报告提的"三种记录身份"即此实现。
- **`pool_period_id: null`** → 已改传 `detail.pool_period_id`
  （market-pool-page.tsx:161，后端 404 护栏在位）。
- **报告称 300s 规则系 PR #190 用户裁决——归因有误**：仓内只有"初值可配、
  不自动放宽"的模块 docstring 与 EU 段 60min 观测龄人裁决（PR #189 票 02）。
  300s 规则现状 = 保持；本图票 08 只做并行测量，不改规则。
- **goals4 后端档位边际面**已由 pool-mapping-goals4 板（全 resolved）+ PR #206
  落地；缺的是前端入口与票级联合语义（→ 票 10）。
- 报告七的历史负结果数字与仓内归档一致（decade-closeout / PR #171 / #181），
  无需重验；其"重分层表述"落 fog（能力登记表注记）。

### 红线

- 池票 live 禁用（boundary-hardening 票 07 维持）。
- 书商名零出现（一律 cid + 分层代称）。

## Decisions so far

（空——开板会话不开票决议）

## Not yet specified

- 共识构造改进（克隆去重 / 逐书去水 / 按历史质量赋权）——等票 09 基准冻结
  与首读后再裁是否必要，避免无读数先动口径。
- "不投"决策记录 schema——等票 07 归因分桶跑出真实分布再定型。
- LLM 三能力拆分验收（事实抽取 / 研究辅助 / 概率增量对照实验）——等票 04
  时点边界落地后设计；逐句主张→证据 ID 的结构化引用同挂此处。
- 多盘口联合定价严格界（显式约束 + min/max 求解，Hegarty/Whelan 方向）——
  等票 05 语义收口 + 票 09 基准读数。
- xG 简单模型预注册复制（Wilkens 2026：跨季存活 + 换真实竞彩价）——独立
  研究线，P2。
- 彩池公众份额数据底座（官方销量 / 中奖注数 / 奖池历史 → 份额情景模型）——
  路线 D 前置，P2。
- 旧前瞻负结果的链路归因分层注记进能力登记表（"候选为零"部分是测量链路
  问题）——文档小件，随批二收尾。

## Out of scope

- 300s 规则放宽或收紧——人裁决已钉观察不动；仅票 08 产读数，翻案另开图。
- 池票 / 任九 live 真金路径。
- 海外式跨庄套利 / 优惠转换功能（报告五：不可迁移为体彩可执行机会）。
- 移动端视觉重设计（报告四自认无实测证据；任务连续性优先）。
