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
