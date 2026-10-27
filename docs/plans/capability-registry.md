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

## 登记表（2026-10-28 快照；✓=该态达成有证据，—=未达成/不适用）

| 能力 | 代码 | 实验 | 运行 | 经济 | 证据 |
|---|---|---|---|---|---|
| 源T 夜班采集（srct-night，恢复+补欠） | ✓ | ✓ | ✓ | — | PR#77/#178（[06087a9](https://github.com/cosmos-arc/goalx/commit/06087a9)）；补欠 fixture 89→5623；夜班台账 `srct_night_summaries`（10-28 夜 completed/39 请求/pending 0） |
| 增量物化 srct-remat（rowid 水位+余量护栏） | ✓ | ✓ | ✓ | — | PR#177（[1c4af03](https://github.com/cosmos-arc/goalx/commit/1c4af03)）/PR#180（[7be6603](https://github.com/cosmos-arc/goalx/commit/7be6603)）；安静盒播种 2h56m；`srct_remat_state` 行在、10-27 08:15 incremental degraded=[] |
| 同步报价账本（price_deviation_ledger） | ✓ | — | ✓ | — | PR#172（[1e54016](https://github.com/cosmos-arc/goalx/commit/1e54016)）；主库 555 行（10-20 起 23:30 sweep 日增）；A 线积累中，读数阶段未到 |
| 漏斗面（funnel + 轨二资格分层） | ✓ | — | ✓ | — | PR#181（[a05b1a8](https://github.com/cosmos-arc/goalx/commit/a05b1a8)）qualification 面；10-27 wrap 21 卡落库、10-27 起写分层字段 |
| 冻结决策 write-once + 参考身份（v29/v31） | ✓ | — | ✓ | — | PR#167（[e8e8eaa](https://github.com/cosmos-arc/goalx/commit/e8e8eaa)）/PR#181 v31 anchor_ref+close_ref；159 行、10-27 起漂移按身份分层 |
| 税后视图（归集计税只读面） | ✓ | ✓ | ✓ | — | PR#174（[a251de2](https://github.com/cosmos-arc/goalx/commit/a251de2)）；2026-10-28 口径核验 396 期次编号零错配/零跨期次组（本票 06）；面随 wrap 日落，奖金面至今全零未触及免征线 |
| 回测工具链（backtest + 十年报告 + 运行指纹） | ✓ | ✓ | — | — | 运行指纹随 run 入档（票 06 本批，[633b7b2](https://github.com/cosmos-arc/goalx/commit/633b7b2)）；十年四报告 docs/plans/backtest-decade/（[c092ad4](https://github.com/cosmos-arc/goalx/commit/c092ad4)，Run21-23 可精确重放）；按需工具非日常运行 |
| 新闻提取（NewsShock 抽取器） | ✓ | ✓ | — | — | caliber-loop N 相 137 事件（[a662b36](https://github.com/cosmos-arc/goalx/commit/a662b36)）；线已终审冻结归档（证据不足），不做日常运行 |
| 估值带（multiline 约束可行性，B 线归档工具） | ✓ | ✓ | — | — | measure 票 10（[3396a3c](https://github.com/cosmos-arc/goalx/commit/3396a3c)）+ PR#181 票 04 certified/conflicts 分流；归档为估值带工具非运行面 |
| 密集锚/轨二快照（track2_snapshots） | ✓ | — | **✗** | — | 票 47（PR#57）+ 轨二（PR#169 [2288297](https://github.com/cosmos-arc/goalx/commit/2288297)）；**主库 0 行——代码在册、零候选触发，运行验收不达成是本表第一价值行** |
| 语料树（raw-first + checkpoint + silver/gold） | ✓ | ✓ | ✓ | — | PR#77 体系；12 个 raw 数据集、fixture_universe 48,952 行、_meta 新鲜（10-27T00:10Z） |
| serve 栈 + 晨检（17 面注册 + 09:05 七项） | ✓ | — | ✓ | — | schedules.py；2026-10-28 七项全绿（见下节对账） |

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

## 维护纪律

- 每次运行验收（新能力上线首跑、护栏首次触发等）随手更新对应行 + 证据；
  状态只进不退，降级（如轨二从运行退回）须写明日期与原因。
- 经济性列是诚实红线：任何一格要打 ✓，须带税后口径的受控验证报告链接。
- 表结构变化（加列/改态定义）在文末记一行变更日志。
