# 能力登记表（capability registry）

> 挂载点：`docs/plans/capability-registry.md`（report-audit-20261027 票 08，2026-10-28 建）。
> 动机：文档领先/落后真实能力已咬过两次（PR#178 修复合而栈未重启、
> srct-remat 无状态行 deferred）——本表是"仓里有什么、验到哪级"的单一认知面。

## 四态定义（只许逐级达成，每格必有可点开证据）

| 态 | 准入证据 |
|---|---|
| 代码已实现 | 已合 main + 行为有测试钉住（提交/测试路径） |
| 实验已验证 | 在合成 gold 或真数据上产出过预期报告/实验结论，且经审查合入 |
| 运行已验收 | 在日常运行中持续工作，有带日期的运行证据（晨检/日志/库行） |
| 经济性已确认 | 税后正期望在受控验证中成立——**当前无任何能力达成**（十年结论=模型无 skill、账本 would_bet 面在积累、bets=0） |

## 登记表（2026-11-04 快照；✓=该态达成有证据，—=未达成/不适用）

| 能力 | 代码 | 实验 | 运行 | 经济 | 证据 |
|---|---|---|---|---|---|
| 源T 夜班采集（srct-night，恢复+补欠） | ✓ | ✓ | ✓ | — | PR#77/#178（[06087a9](https://github.com/cosmos-arc/goalx/commit/06087a9)）；补欠 fixture 89→5623；夜班台账 `srct_night_summaries`（10-28 夜 completed/39 请求/pending 0） |
| 增量物化 srct-remat（rowid 水位+余量护栏） | ✓ | ✓ | ✓ | — | PR#177（[1c4af03](https://github.com/cosmos-arc/goalx/commit/1c4af03)）/PR#180（[7be6603](https://github.com/cosmos-arc/goalx/commit/7be6603)）；安静盒播种 2h56m；`srct_remat_state` 行在、10-27 08:15 incremental degraded=[] |
| 同步报价账本（price_deviation_ledger） | ✓ | — | ✓ | — | PR#172（[1e54016](https://github.com/cosmos-arc/goalx/commit/1e54016)）；主库 555 行（10-20 起 23:30 sweep 日增）；A 线积累中，读数阶段未到。**注记（report-audit-20261103 收板，2026-11-04）**：旧前瞻负结果中"候选为零"部分系测量链路问题（拍点不在可购窗/候选过滤结构性零触发——延迟生存率首跑 L2=0 仪器面实证），非无信号结论；票 09 价格存活协议重开测量（五价时点+双周检查点，11-13 首读） |
| 漏斗面（funnel + 轨二资格分层） | ✓ | — | ✓ | — | PR#181（[a05b1a8](https://github.com/cosmos-arc/goalx/commit/a05b1a8)）qualification 面；10-27 wrap 21 卡落库、10-27 起写分层字段 |
| 冻结决策 write-once + 参考身份（v29/v31） | ✓ | — | ✓ | — | PR#167（[e8e8eaa](https://github.com/cosmos-arc/goalx/commit/e8e8eaa)）/PR#181 v31 anchor_ref+close_ref；159 行、10-27 起漂移按身份分层 |
| 税后视图（归集计税只读面） | ✓ | ✓ | ✓ | — | PR#174（[a251de2](https://github.com/cosmos-arc/goalx/commit/a251de2)）；2026-10-28 口径核验 396 期次编号零错配/零跨期次组（本票 06）；面随 wrap 日落，奖金面至今全零未触及免征线 |
| 回测工具链（backtest + 十年报告 + 运行指纹） | ✓ | ✓ | — | — | 运行指纹随 run 入档（票 06 本批，[633b7b2](https://github.com/cosmos-arc/goalx/commit/633b7b2)）；十年四报告 docs/plans/backtest-decade/（[c092ad4](https://github.com/cosmos-arc/goalx/commit/c092ad4)，Run21-23 可精确重放）；按需工具非日常运行 |
| 新闻提取（NewsShock 抽取器） | ✓ | ✓ | — | — | caliber-loop N 相 137 事件（[a662b36](https://github.com/cosmos-arc/goalx/commit/a662b36)）；线已终审冻结归档（证据不足），不做日常运行 |
| 估值带（multiline 约束可行性，B 线归档工具） | ✓ | ✓ | — | — | measure 票 10（[3396a3c](https://github.com/cosmos-arc/goalx/commit/3396a3c)）+ PR#181 票 04 certified/conflicts 分流；归档为估值带工具非运行面 |
| 密集锚/轨二快照（track2_snapshots） | ✓ | — | ✓ | — | 票 47（PR#57）+ 轨二（PR#169 [2288297](https://github.com/cosmos-arc/goalx/commit/2288297)）；曾长期主库 0 行（odds_api 候选过滤结构性零触发，本表首条 ✗ 价值行）；2026-11-01 PR#193 候选解绑广义 joined 后首拍即出数——**5 行（3×12h+2×24h，corpus 锚 cid177，全可购）**，运行验收达成 |
| **今日页欧赔共识 + had 判定 EV 面** | ✓ | — | **◐** | — | **2026-10-31 补行（用户使用投诉触发）**：自 9-25 换源裁决结构性断供——读侧死命名空间（`odds_api:%`）+ 当期零映射 + 当日物化缺通道（诊断记忆 `goalx-today-page-eu-feed-dead-20261031`）；10-31 晚 23 场在售 books=0/EV 全空实证。修复板 `.scratch/odds-read-switch/` 票 01-03 |
| **陈盘信号（stale_line）** | ✓ | ✓ | **✗** | — | **同日补行：sharp 锚 `odds_api:pinnacle` 同死源**（票48 落地 9-21，源 9-25 摘除后主锚从未活过）；cid177 切换在修复板票 02。10-31 晚全部在售场 sharp_ref/drift=NULL 实证 |
| **had 纸面下注闭环（join→quote→stake→bet）** | ✓ | — | **✗** | — | **同日补行：`bets` 表 0 行，闭环从未真实发生过**（闸门 fail-closed 全拒 + 输入断供双因）；修复板落地后首注即本行运行验收证据 |
| 语料树（raw-first + checkpoint + silver/gold） | ✓ | ✓ | ✓ | — | PR#77 体系；12 个 raw 数据集、fixture_universe 48,952 行、_meta 新鲜（10-27T00:10Z） |
| **五价时点记录 + 价格存活检查点（票 09）** | ✓ | — | — | — | PR#223（[091c3bc](https://github.com/cosmos-arc/goalx/commit/091c3bc)）v39 bet_price_timeline 四实时阶段钩子 + price-edge-audit CLI；预注册 docs/plans/report-audit-20261103/prereg-price-edge.md 判读规则冻结；运行验收=纸面决策流积累（11-13 首读） |
| **新鲜度五时钟 + 300s 并行影子（票 08）** | ✓ | — | ◐ | — | PR#222（[95ca9ab 前](https://github.com/cosmos-arc/goalx)）v38 freshness_shadow 日拍挂采集流后拍点；规则红线零改动（stale_source 逐字节不动）；运行验收=读数归期（11-13 与票 09 共用） |
| **首页无候选六桶归因（票 07）** | ✓ | — | ✓ | — | PR#221 /today/attribution 单源判定映射+晨检同源对账；随 serve 栈日渲染 |
| **goals4 四场进球开面（票 10）** | ✓ | — | ◐ | — | PR#224（[95ca9ab](https://github.com/cosmos-arc/goalx/commit/95ca9ab)）同场联合=比分矩阵格聚合（一致性测试钉死）+/markets/pool4+构票贯通；运行验收待真实期次首笔纸面票 |
| **幂等提交意图指纹（票 09 批三）** | ✓ | — | ✓ | — | PR#234（[ba9831d](https://github.com/cosmos-arc/goalx/commit/ba9831d)）v41 intent_fingerprint：同键同意图 201 回带/异意图 409（域层预检+UNIQUE 竞态两路）；运行验收=新栈 v41 落列（2026-11-04 migrate+readyz 读数）；前端冲突恢复+e2e 响应丢失→改单→另建钉死 |
| **语料发布边界（台账先行，票 11 批三）** | ✓ | ✓ | ✓ | — | PR#236（[879a077](https://github.com/cosmos-arc/goalx/commit/879a077)）ingest_raw BEGIN IMMEDIATE 先于正式替换（busy 官方字节零触碰/崩溃窗重写收敛两用例）；运行验收=新栈夜班+remat 增量在票 11 代码上首班收班（2026-11-04 夜 completed 请求=50、水位 15:31Z） |
| serve 栈 + 晨检（19 面注册 + 09:05 十六项） | ✓ | — | ✓ | — | schedules.py；2026-11-04 23:46 换新栈（批三 HEAD 879a077，单树；四组孤儿 schedules 同日清毕）注册 19 面（+gold-rebuild）；晨检 14 项首读 ok=11/degraded=2/fail=1（[run-acceptance.md](report-audit-20261104/run-acceptance.md)）；readyz 真检查（库可读+schema 等值）随票 10 落地 |
| **能力一：决策锚基准冻结（market-baseline-freeze）** | ✓ | — | ◐ | — | auto-paper 票 01（PR#261，[4fccd77](https://github.com/cosmos-arc/goalx/commit/4fccd77)）v42 两表+三锚 deployment（12:33/16:33/21:33）；首锚 2026-11-07 21:33 手动补拍 49 行（28 JC+21 EU）实证；运行验收=换栈后定拍锚积累（11-11） |
| **能力二：auto-v1 策略落注 + 账户面** | ✓ | — | ◐ | — | 票 03+04（PR#275/#276）选票引擎（prereg 修订①-④）+auto-paper 双 deployment+锚台账 v43+`/api/v1/paper-account` 只读面+晨检第 15 项；运行验收=换栈后首锚真实落注（11-11） |
| **队名身份机制：srct_en 英文活水 + clubelo 桥解饿** | ✓ | ✓ | ◐ | — | identity 票 01（PR#262，[8a0c115](https://github.com/cosmos-arc/goalx/commit/8a0c115)）raw 重解析零网络供给 source=srct_en（168 条零冲突）+锚源集合追加 srct_en/manual+ADR-0013/CONTEXT 词条；运行验收=换栈后 mapping_sync 常态计数（11-11） |
| **队名覆盖守门（晨检第 16 项 + align-report 阈值）** | ✓ | — | ◐ | — | identity 票 02：最近业务日 forecast_coverage no_mapping>0 即红附队名（容忍度=0）+覆盖账无行/陈账黄+AlignmentReport.status <0.95 degraded；运行验收=换栈后首个晨检窗红绿读数（11-11） |
| **探索轨扫描器阵列（S1/S2/S4/S5 + 发现台账 + 周报）** | ✓ | — | ◐ | — | autoresearch 票 01+04：v45 两表（edge_findings/edge_observations）+`evaluation/edge_scan.py` 注册表+daily-wrap 尾挂步+CLI edge-scan+晨检第 17 项；真树首跑 2026-11-09（首条发现 p10-20:d、S5 历史回放 1,737 期/355 滚存标注、周报首期落 corpus reports/）；票 02 S2 点亮（模型校准分玩法置信 10 箱+纸面注 EV 实现差配对面，n≥200 门，晨检第 17 项扩 s1/s2/s4）；运行验收=wrap 定拍夜扫留痕（11-10 晨检第 17 项首读） |

## V1 功能冻结矩阵（2026-11-06 定版，tag v1.0.0）

功能范围冻结裁决（两轮冻结评审收口，措辞与 USER-GUIDE §2 一致）：

| 入口 | V1 定位 | 边界 |
|---|---|---|
| HAD 今日/研究/单关/串关/纸面锁定结算复盘 | **正式纸面核心** | 唯一进正式分母（验证/CLV/EV 快照）的面 |
| 前瞻验证、价格时间线、价格存活 | **正式测量工具** | 测"我们准不准/买得好不好"，不产投注建议 |
| 非 HAD 纸面（goals 等） | 研究入口（`manual_research`） | 记录无偏，不进正式分母 |
| 彩池（14场/任9/四场进球） | 研究构票 | 奖金未实现——纸面组合保持待结算，live 拒绝 |
| LLM（scout/追问）、融合、M3 | 辅助参考 | 永远参考列；真钱资格只读 ML 轨 |
| live 回录 | 人工记账 | 赛前建建议+赛后人工回录，不开自动真实执行动线 |

解冻规则仅限：价格/赛事身份错误、时间泄漏、重复记录、错误结算、证据无法追溯、恢复失败、关键任务持续无法完成（观察期 2026-11-04→11-18）。M3/LLM 晋级决策独立于本矩阵（用户显式裁决，另行开图）。

## V1 冻结基线（实验记录必填字段）

以本基线为分母的实验（前瞻验证/价格实验/延迟生存率等）记录时必填：

| 字段 | V1 值 |
|---|---|
| 发布 SHA | tag `v1.0.0`（含最终文档树；运行栈指纹以 /healthz git_sha 为准） |
| 数据口径 | 语料 raw-first（源T 主链）→ silver/gold 物化；业务库 schema v41；fixture_universe 48,952 行（2026-11-04 快照） |
| ML 工件身份 | DC 分池模型 + 比分矩阵（`data/models/`，model_version `dc-xgblend-` 前缀含 xG blend 溯源）；回测 run 随 run_params 入档运行指纹 |
| LLM provider/model | GLM（智谱）三档：scout `glm-5.3-flash` / analyst `glm-5.3` / fallback `glm-4.7-flash`（config 默认，熔断三级 60/80/95%） |
| prompt 代码版本 | prompt 在代码内随 git 版本管理（=发布 SHA 对应树，无独立版本号） |
| 融合权重 | 融合 α 周级拟合（fusion.py fit_alpha，run summary alpha_log 入账）；真钱资格只读 ML 轨（ADR-0009） |

## 与 09:05 晨检七项对账（2026-10-28，建表时手跑）

| 晨检项 | 结果 | 对应登记行 | 差异说明 |
|---|---|---|---|
| 栈活性 3 进程 | ✓（73852 wrapper + 98798/98802 server） | serve 栈 | 一致 |
| 昨夜夜班 completed | ✓（10-28 夜 39 请求 pending 0） | 夜班采集 | 一致 |
| remat incremental | ✓（10-27 08:15，degraded=[]） | 增量物化 | 一致 |
| 23:30 daily-wrap | ✓（10-27 收尾正常） | 漏斗/冻结/税后 | 一致 |
| 账本行数日增或持平 | ✓（555 行，10-27 净增 63） | 报价账本 | 一致 |
| 语料 36h 新鲜 | ✓（10-27T00:10Z） | 语料树 | 一致 |
| 失败面零 | ✓（CRASHED/Traceback 0） | serve 栈 | 一致 |

差异行说明：晨检不覆盖回测/新闻提取/估值带/轨二快照四行——前三者是按需
或冻结能力（无日常运行面故无晨检项），轨二快照是**本表独有暴露行**
（晨检七项不查 track2_snapshots 行数；若要纳入，等延迟生存率 Stage 2
门控触发时一并挂）。2026-10-28 无矛盾行。

**2026-10-31 教训补记**：本表 10-28 建表时按"最近批次"自底上行，漏登了
三个消费面（上表 ✗ 三行），导致"能力是否都开发完"的答复建立在不完全
普查上。两堵缺口：①晨检不查今日页欧赔输入（断供五周无日警）——补项在
修复板票 04；②表无依赖追踪，9-25 换源杀 odds_api 输入时未扫消费侧依赖
——纪律见下节。

## 维护纪律

- 每次运行验收（新能力上线首跑、护栏首次触发等）随手更新对应行 + 证据；
  状态只进不退，降级（如轨二从运行退回）须写明日期与原因。
- 经济性列是诚实红线：任何一格要打 ✓，须带税后口径的受控验证报告链接。
- 表结构变化（加列/改态定义）在文末记一行变更日志。
- **行集是普查不是幸存者清单**（2026-10-31 起）：登记对象=全部用户可见/
  日常运行能力（API 面、判定面、闭环），不只最近开发批次；答"能力是否
  齐全"前必须对本表全行核运行态。
- **摘除数据源/拍点等输入时必须扫依赖**（2026-10-31 起）：被摘输入的下游
  能力行当日改运行 ✗ 并注断供日期，不允许"代码在册=能力在册"。

## 变更日志

- 2026-11-08：identity 板票 02/03 补两行（srct_en 英文活水+桥 [PR#262]；覆盖守门晨检第 16 项+align-report 0.95 阈值）；srct_en CONTEXT 词条；auto-paper 票 05 已由本板承接关闭（热修档案保留）；晨检 15→16 项。
- 2026-11-08：auto-paper 票 04 补两行（能力一=决策锚基准冻结[PR#261]；能力二=auto-v1 策略落注+账户面[PR#275/本票]）——运行列 ◐ 待 11-11 换栈后首锚/首班验收；经济列空（红线：盈利唯一主指标在纸面账户层验证）；晨检 14→15 项（+纸面账户观察）。
- 2026-11-06：V1 发布收口（票 06）——新增"V1 功能冻结矩阵"与"V1 冻结基线
  （实验记录必填字段）两节（tag v1.0.0 定版）；同日书商名展示解禁（用户
  裁决：合法数据分析），研究页直名展示随本发布落地（票 13，含 /validation //glossary 截图入册）。
- 2026-10-31：补三行（今日页欧赔共识+had EV 面、陈盘信号、纸面下注闭环，
  均运行 ✗ 断供/零触发在案）；新增维护纪律两条（普查纪律、摘源扫依赖）。
  触发=用户使用投诉（欧赔/EV 全空、闸门全拒）。
- 2026-10-31 晚：odds-read-switch 票 01-03 合 main 后欧赔面行 ✗→◐（待
  首绿）；晨检七项→八项（+今日页欧赔输入面，票04；红线语义=红不许静默
  跳过）。
- 2026-11-01：小账清账——track2 行注记候选面已解绑 odds_api；晨检八项→九项
  （+纸面闭环首注观察，首注>0 翻闭环行）。
- 2026-11-01 晚补：PR#193 合 main + 栈重启后 track2 首拍 5 行落库（corpus
  锚），track2 行 ✗→✓——补行以来第一条由 ✗ 翻 ✓ 的行（修复闭环走通）。
- 2026-11-04：批三（report-audit-20261104 票 09-12）收官对账——新增幂等
  意图指纹/发布边界两行（运行验收=换新栈首读）；serve 栈行更新（19 面、
  晨检 14 项、readyz 真检查、恢复演练记录）；经济性列维持空（红线）。
