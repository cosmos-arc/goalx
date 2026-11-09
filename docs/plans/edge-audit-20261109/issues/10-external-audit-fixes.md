# 10 外部审计缺陷定修与受影响面重算

Type: task
Status: resolved

## Question

外部审计（`research/external-audit-032324c.md`，固定 032324c）主张的缺陷，经 2026-11-09 主会话源码核验后的定修与重算。核验结论前缀：✅=已核验坐实；❓=待查证。

已坐实（修复+定点重算）：
1. ✅ **ttg「7+」按 `t == k` 结算**（`docs/plans/autoresearch-20261107/reports/09-em1-facet-a.py:283-285`，ks=range(3,8) 含 7 档，总进球 8/9 漏回款）→ 修 `t >= 7`（仅 7+ 档），重算 F8 over 篮 ev_real（影响方向：向下偏置，F8 verdict z=−1.27 远离门，预计不翻转但要出修正数）。
2. ✅ **OU 梯锚 arg_max 无书维度**（`_lib.py:392-417` SQL group by sid,line，拿到的是最后更新那家的水位，docstring 却称「各线最晚赛前报价后跨书均值」）→ 改 per-book last→跨书均值，重算 F8/ladder 依赖读数；噪声锚可能掩盖小效应，重算前 F8 读数不得再引用。
3. ✅ **BH 先筛后校**（`09-em1-facet-a.py:203-214`、`10-em1-facet-bc.py:299-312`：val_pv 只收过 promo_gate 者）→ 未来轮次家族=当轮全候选（含全阴），E-32 协议措辞同步钉死；历史零幸存者不受影响（假阳性风险在未来）。
4. ✅ **账户三处**（`apps/backend/src/goalx_backend/betting/account.py`）：`_after_tax:185-203` 逐注计税绕开 tax.py 期次归集（同期同游戏应合并，2×6000→2400 税）；`sample_sufficient:231` 分母=全部 paper 注（应=已结算、合格、独立暴露）；`EdgeSourceFace:246-248` CLV SE 按行独立（同场多锚相关，应按场聚类）→ 三修。
5. ✅ **auto-v2 预筛 HAD-only**（`auto_strategy.py:307-309` latest_sale_status_asof 硬编码 "had"）+ ❓下游 `bets.py:435` 玩法级资格链 → 补 per-market 在售/单关资格。
6. ✅ **FD Pinnacle 断点**（football-data.co.uk/data 页官方警示：Since 23/07/2025 Pinnacle public API unreliable，pre-closing/closing 双陈旧，**且已从 Avg/Max 列剔除**）→ 两个子问题：①口径断点——AvgC 在 2025-07-23 前后成分不同（含/不含 Pinnacle），E-02「逐季收窄 −5.8%→−2.3%」尾部与 E-07 确认窗（24/25–25/26 的 25/26 段）读数须带成分注记并隔离重算；②前向供给——现行 CLV 主锚（Pinnacle 收盘）的供给路径盘点：哪些走 fdhist（post-2025-07 污染）、哪些走 srct/odds_api（可用），auto-v2 探针 CLV 回填与 E-30 σ_CLV 实测的锚来源必须换轨或双源。警示原文逐字引用随修复落账。
7. ❓ **H9 npxG=未剥点球 xG** 主张（`docs/plans/edge-program-20261107/reports/02-h9-model-families.md` 实现）→ 验特征读数路径；属实则 H9 排除范围注记收窄（报告自己也只主张「窄候选被排除」）。

完成标准：上列修复落 PR（**2026-11-09 人裁：同车一 PR、三轴审查全走**）+ 受影响读数定点重算出新旧差对照 + E-32/account 语义钉死 + FD 断点两子问题给出供给轨结论（已知起点：生产 CLV 锚=srct_closing_prob 不经 fdhist，运行面无火警——盘点收窄为研究侧读数+六步门③主锚前向定义）。判读红线：修复≠翻案，重算结果按账本判决词如实落。

## Answer

**PR #286 已合 main（6f88657，CI 全绿，task check 全量门绿）**。子票 12/13/14/15 同车收口（各自 Answer 见票文件）。

- 缺陷 1-6 全修：ttg 7+ 按 ≥7 / OU 每书末价→跨书均值 / BH 全家族 / 账户三修（税期归集+独立暴露分母+CLV 按场聚类）/ 玩法资格链单源（+sporttery v5 补 hhad/hafu 市场行）/ FD 断点两子问题结论（票 15 文档：隔离=整季剔除，受影响 6 处清单；前向唯一健康 Pinnacle 锚=源T srct，运行面无火警坐实；六步门③三选项供票 08）。
- 缺陷 7（H9 npxG）查证=属实：判决词维持，排除范围收窄注记稿在 research/14 供票 08。
- F8 解冻以新值为基准：三窗 0 候选/0 晋升/0 幸存不变，E-07 判决词不变（修复≠翻案落地）。新旧差对照 research/12（逐格闭合）。
- 顺延（有依据）：E-02 尾季/E-06 等断点驱动隔离重算只出清单（票 15），判决处置留票 03/08；E-32 措辞只出稿。
- 三轴全走：Standards 零硬违反（5 judgement call 修）；Spec 核对全过；Correctness 2 info 修（partial 不入分母 F1 / CLV exact-set 前提注释 F2）。附带：票 13 引入的 ADR-0008 SQL 越界归位；smoke 场次页空窗日第三态补测（main 同红的时钟窗缺口）。
- 换栈：夜班（23:30-06:58）后 07:15 一次性重启 serve 栈生效。
