# 运行验收记录（report-audit-20261104 票 12）

> "代码已合入"≠"运行已生效"——本页是批三（票 09-12）交付的运行侧证据件，
> 各条带日期。观察期（7-14 天覆盖周末+日切）与统计门槛（30 销售日）分别
> 处理，本页只记运行事实，不作盈利裁决。

## 一、六项验收清单（2026-11-04 读数）

| # | 项 | 读数 | 证据 |
|---|---|---|---|
| 1 | 业务库 schema | **v41**（票 09 intent_fingerprint 落列） | `task db-migrate` 2026-11-04 23:46 `schema at version 41`；`/readyz` `{"status":"ready","schema_version":"41"}` |
| 2 | 研究库回填 run 对账 | **23 run 全在、全 done**（id 1..23，m2-full-3season…m05-resid-on），predictions 529,638 行 | 2026-11-04 23:47 直查 `data/goalx-research.db`（与 boundary-hardening 票 07 搬迁 23 run 对账一致，零丢失） |
| 3 | API/调度启动指纹 | **双面一致 = `879a077fe89…`（批三 HEAD），dirty=false** | `/healthz` git_sha=879a077；`.scratch/prefect-serve.log` 2026-11-04 23:48 `runtime fingerprint: git_sha=879a077… schema_version=41`；晨检第 2 项 ok |
| 4 | 价格时间线首批有效记录 | **表就绪、随首批建注积累**（bets=0，timeline 0 行——诚实记缺，不冒充） | `bet_price_timeline` 2026-11-04 count=0；v39 钩子在栈（PR#223），首注即落发现/决策阶段行 |
| 5 | 新鲜度 shadow 按预期拍点 | **待新栈 daily-wrap 首跑**（旧栈 23:31 wrap 早于 v38 上栈窗口未拍；新栈首拍预计 11-05 23:30） | `freshness_shadow` count=0；晨检项 14 ok（"未拍（daily-wrap 首跑后出现）"） |
| 6 | 11-13 检查点（冻结协议首读） | **未到期**——按 prereg-price-edge 冻结口径产出描述性读数，不作生死裁决 | 预注册 docs/plans/report-audit-20261103/prereg-price-edge.md；读数记录入票 09/12 Comments |

## 二、栈重启换新（2026-11-04 23:45-23:48）

- 旧栈（2026-11-02 16:48 起）+ **四组孤儿 schedules**（11-01 20:38 / 11-02 00:48 / 11-02 08:38 / 11-02 14:26，历史重启残留）按 PID 直杀全清；ditto 栈不动。
- 新栈：API server :8000 + prefect server :4200 + schedules **单树**（23:46 起），19 面注册（较旧栈 +gold-rebuild）。
- 今晚夜班在新栈前已收班：`night: 2026-11-04 completed 请求=50`；remat 增量水位 15:31:39Z。
- 启动日志路径纠偏：serve 进程重挂回 `.scratch/prefect-serve.log`（晨检默认扫描路径——首次起栈写去了 serve-schedules.log 导致指纹项误报"行缺"）。

## 三、晨检读数（2026-11-04 23:48，新栈首读）

14 项：**ok=11，degraded=2，fail=1**。

- ok 面：栈活性（prefect+api）、运行指纹（双面 879a077）、夜班 completed、remat 增量、wrap 最新业务日 11-04、账本、corpus 新鲜 15:30Z、今日欧赔 12 场链 11、bets 观察、pool 当期、freshness_shadow（未拍属预期）。
- degraded：①gold built_at 11-03（按需重建面，非日拍）；②serve_log 200KB 窗含旧栈时代 Traceback×10（历史内容，随新日志积累自愈）。
- fail：jc_waiting 2 场（周二003 已开球残差——已知韩职残差类，前板留观项，非本批引入）。

## 四、空目录恢复演练（2026-11-04，人裁决：批三全合+栈重启后）

演练对象=含批三全部修复的运行态。流程：一致性备份快照（`sqlite3 .backup` × 2 库 + observations/models 冷拷贝 + corpus checkpoint.db + bfdata 证据子集）→ **空目录**恢复 → 三读验证：

| 验证 | 结果 |
|---|---|
| 打开账目 | PASS——恢复库 schema v41，bets/bankroll_events 可查（当前 bets=0、真金流水空，如实） |
| 读一个历史研究 run | PASS——`#23 m05-resid-on done 2026-10-20`，配对 predictions 37,595 行 |
| 重放一条原始证据 | PASS——`bfdata/@gap-20261104.js` 114,286B gzip 解压 + sha256 对账通过 |

脚注：`read_raw` 的 `ext` 须按数据集约定传（bfdata=`.js`，台账不存 ext）——已写入 RUNBOOK §9。

## 五、观察期（运行侧，7-14 天）

- **起：2026-11-04**（批三全合 main=879a077 + 栈换新）；**预定讫：2026-11-18**（覆盖两个完整周末 11-07/08、11-14/15 与 14 个日切）。
- 期内动作：晨检每日 09:05 读数（fail/degraded 项跟踪）；观察期暴露的下一批需求届时新开板，不在本图扩容。
- 统计门槛（30 销售日/严格销售日累计）独立于本观察期，不因观察期结束而提前裁决。

## 六、能力登记表对账

四态+本轮证据更新见 [capability-registry.md](../capability-registry.md) 2026-11-04 行（本批新增行：提交意图指纹幂等、发布边界台账先行；更新行：serve 栈/晨检、纸面闭环观察）。经济性列维持空（红线不动）。

## 七、性能一次性读数（2026-11-05，报告二 §八清单；先测量后决策）

| 面 | 读数（3 次采样） | 判读 |
|---|---|---|
| 今日页 `/fixtures/today` | 0.61s 冷 / **0.20s、0.20s** 热 | 亚秒，无需动 |
| 研究页 `/fixtures/1/research` | **0.10s、0.07s、0.07s** | 快，无需动 |
| **验证页 `/validation/progress`** | **18.77s、18.52s、18.35s** | **唯一超标项**：p50≈18.5s 远超页面预算（票 05 已修事件环阻塞=线程池化，但墙钟仍在）。观察期内若验证页使用体验受阻，候选动作=读侧缓存/异步化——**有读数待裁，不先行动** |
| 业务库写事务探针 | BEGIN IMMEDIATE→COMMIT **0.8ms** | 写锁无竞争压力 |
| remat 峰值 | 引既有记录：播种 2h56m 常驻 ~4.5GB（2026-10-24 安静盒）；增量班秒级 | 维持现状 |
| 前端包体 | 单 chunk **1.66MB**（gzip 前） | 报告二"大包懒加载"维持暂缓清单（map Out of scope）；读数留档，拆不拆等观察期后一并裁 |

结论：除 validation/progress 外全绿；该面已记入观察期跟踪项。

## 八、gold 安静盒全量重建验收（2026-11-08，v1 票 08；栈 sha `23b14e5`）

窗口=周日晨 10:07-10:15（12:30 下一采集拍前干净窗）；CLI `gold-build`（与定拍 08:40 同源 `tasks.gold_rebuild`），工作树=栈同 sha，真实语料（market quotes 198 万 AH/226 万 OU、169 分区）。四要素：

| 要素 | 实测 |
|---|---|
| 耗时 | **514.65s**（8m35s；对照同库当日 08:40 定拍班 340s，差异见下） |
| 峰值内存 | time -l max RSS **3.52GB**（段进程合计采样峰 2.95GB；对照 11-07 手动班 market 4.7GB/gold 2.6GB 同量级） |
| 降级事件 | **degraded=None，零 deferred**（余量三探/锁互斥护栏全程未触发） |
| 最终状态 | **绿 EXIT=0**——gold rows 49,140 与同晨 08:40 定拍班逐项一致（fixture/market_quote/xg/elo 摘要 digest 全同；odds_change_event 因 10:05 采集班合理前移），corpus.duckdb 视图刷新成功 |

环境事件如实记（不外推）：10:14:32 起 ditto 仓库测试班重启（外部负载 ~3GB）与 gold 段（2.9GB，正常画像）叠加 → swapfile5/6 新建（swap used 3.67→5.95GB），gold 段被换页拖长、wall 拉大至 514s；gold 班自身内存有界、未触任何护栏。另 swapfile4=今晨 08:44 定拍班所留（b9a1960 栈，全绿）。

前序证据链：11-07 00:23 护栏路径实证（`deferred_low_headroom 4.9GiB` 零写入让位，3.92s/exit 0）→ 11-07 09:28 手动班全绿（351s，批 C 前）→ 11-08 08:40 定拍班全绿（340s，批 C 后 b9a1960）→ 本班（批 C/D/E+W4 全含 23b14e5）。票面"成功或明确 deferred 后恢复均可验收"——本班=成功路径，四次 OOM/SIGKILL 史路径未复现。边界重申（评审 B）：duck_limits 非进程硬上限，本页只记实测值。

## 九、当前版本运行快照（2026-11-09，v1-release 票 07；栈 sha `a6d3bd8`）

补 §一/§二（11-04 `879a077` 读数）之后的证据缺口：V1 发布后栈随 main 前移，本段记录快照时点（19:41-20:00 CST）的栈身份与运行事实，**跨栈证据按实际执行栈归属标注，不冒充当前版本**。

### (a) 运行身份

- 今日三次换栈：11:35（`969e35f`，schema 44）→ 13:35（`f4599c6` #282，schema 45）→ **19:34（`a6d3bd8` #283+#284，schema 46）**，均按 PID 直杀换栈惯例。
- **快照清障发现**：19:34 复核时，16:02 时代旧 server worker 孤儿（PID 5562，ppid=1）仍与 8000 端口**并听**（SO_REUSEPORT）——healthz 应答可能来自任一进程；已直杀并复核单监听。换栈复核清单补"lsof 双监听"一查（观察项：孤儿不抢调度但抢端口应答权）。
- 单树复核：schedules 33712→33713、server 33716→33717 一棵树；prefect server（goalx+ditto 共用，11-07 起）与 ditto worker 未动（惯例）。
- **双面指纹一致**：healthz = schedules 启动日志（19:34:27）= `git_sha a6d3bd821d3c…`、dirty=false、`config_hash 3d3ac0fd…`、schema 46；今日三次换栈 config_hash 同值=配置零漂移（指纹=生效 Settings 规范 JSON 的 sha256，`run_fingerprint.process_face`，可解释）。
- readyz 真检查：只读连接实测业务库 schema==部署迁移链末版（低/高均 503 拒服务），实测 `ready`/46。
- deployments 32 条注册，无 (flow,name) 重复；活跃面 22（19:34:27 `--only` 清单在册，auto-v2 探针阵 auto-paper-day/evening、srct-remat、gold-rebuild 全含，夜间预排 21:01 首锚/23:30 首班在列）；停采面（margin-published×4/fred-published/daily-repair/daily-ingestion）旧注册保留不重启、deployment_version 留旧戳属预期，无重复调度。

### (b) 采集到消费端

- 当前栈已执行：19:40 odds-anchor-dense（sale_stop_probe candidates=0）+ track2-snapshots（24h/12h 全 0）Completed——**零候选原因在册**：今晚（11-09 CST）无开赛场次；4 场在售（fixture 616-619）开球均在 11-11/11-12 凌晨，超出探针 24h 窗（sale_statuses 观测至 19:00:02 CST 仍 on_sale，非断供）。
- 今日完整链（12:30 窗，**归属 11:35 栈 `969e35f`**，如实标注）：JC 官方页 4 场 43 快照（12:30）→ sale_statuses 观测续至 19:00 → srct-shift raw_new 12 行 → remat mode=incremental（watermark 04:30:23Z，触及以甲/英议南等 10 分区）→ 今日页 API 0 场（真实）。
- 增量物化在册：checkpoint.db `srct_remat_state` watermark `2026-11-09T11:30:00Z`（19:30 前栈末拍）/last_rowid 386298，锁空置；当前栈首拍=今晚 23:30 shift 钩子+明晨 08:10 定班。
- 当前栈自身首条"采集→wrap"链=今晚（21:01 auto-v2 首锚、23:30 首班 wrap 含 PR#282 尾挂扫描器步首跑），读数入 daily-log 11-10 行，不入本段冒充。

### (c) 日终完整性

最近一班完整 wrap=**2026-11-08 23:30:03→23:31:54 CST Completed（ver=`23b14e56`，归属 11-08 09:32 栈）**，111s，五阶段明确（票 02 修复代码上的**首班**=2026-11-07 23:30 班，33934f1 栈，同构五阶段：settled=0/CLV 0/audit 0/m3 paired=50·未证明，逐班延续至今）：

| 阶段 | 读数 |
|---|---|
| settlement | settled=0 / still_open=0（11-08 slate 已清） |
| frozen_reconcile | considered=0；price sweep 账龄截断 WARNING 102 行>14 天 NULL 冻结不再重查（设计行为留痕） |
| CLV | recorded=0 / skipped=[] |
| ledger/audit | audit_findings=0 |
| **M3 边界（票 02 修复后）** | tier_a/tier_b=未证明（样本不足）、paired=65、post_settle_enqueued=3——独立异常边界生效、返回体可见，如实报"未证明" |

funnel：n_on_sale=12 → loss_reasons l5_sale_not_on_sale×12（11-08 slate 收尾）；frozen n_cards=36 全 ineligible。当前栈首班 wrap=今晚 23:30，明晨 09:05 晨检读数。

### (d) 计数器镜像与仪器修复

- schema 46（healthz/readyz/主库三处一致）；**bets=0 / bet_price_timeline=0 / freshness_shadow=0 / bankroll_events=0**——与 §一 同为诚实空读数。
- **快照期间发现并修复（本票 v47）**：freshness_shadow 日拍自 2026-11-04 首拍（10:00:05）起逐班降级 `no column named clock_gap_seconds`——v38 DDL 落库后被 9c5b3ce review 修整**原地补列未升版本**，主库表停缺列形态、CREATE IF NOT EXISTS 不重建；§一 第 5 行"未拍属预期"实为仪器坏（当时读数 0 属实、归因错误）。v1-release 票 03（11-06）晨检口径升级后该项持续判 degraded"测量断线"，观察期未单独开票，本票收口：v47 守卫 ALTER 补列（新库 v38 DDL 已含列，幂等跳过；回归测试复现主库形态→修复→写入恢复）。首拍=修复合栈后首个采集窗；11-13 预注册首读（prereg-price-edge）前置。
