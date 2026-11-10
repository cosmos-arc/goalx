# 20: BH 轮级单家族合并（E-32 修订 C 落地小票）

Type: task
Status: resolved
Blocked by: None（2026-11-10 E-38 收官人裁 G-02=b 开出）

**What to build:** 把 09-em1-facet-a.py（F1-F6 与 F8 两处）+10-em1-facet-bc.py 的 bh_family 调用合并为**一轮 EM 全 facet 单家族**（E-32 修订 C 粒度裁定）。历史各轮候选数 0，合并不改已落账数字，只约束未来轮次。
- [ ] 三处调用合并为轮级单家族（跨脚本共享族成员装载）
- [ ] 合成反例单测：两 facet 脚本候选并族后 m 计数与阈值正确
- [ ] 三轴走 code-review + correctness（行为变更，非纯 docs）

## 不变量与人裁决项
- 家族=该轮进入验证窗全部候选（全阴/样本不足/验证窗无行者均在族内，z 不可计算 p=1.0）；任何先筛减族再校正=违约。
- 09-em1-protocol.md 的 E-32 措辞同步（修订 C 已随收官 PR 落账本，协议文件随本票同步）。

## Answer

**分支/commit**：`edge-audit-20261109-20-bh-round-family` @ `56dd45f`（fix(reports)，base=main b6131d9，merge-base 校验过；报告时 main 无新提交无需 merge）。

**改动**（5 文件，+358/−45）：
- `docs/plans/autoresearch-20261107/reports/_lib.py`：新增轮级族装载段——`EM1_ROUND_BH_GROUPS`（预注册 BH 贡献组 09-A/10-B/10-C；D 按票 11 refuted 关闭无候选路径）、`read_trial_ledger`、`em1_round_family`（家族=台账 window=search 且 stage=candidate 全部条目跨 facet 并集；验证条目缺失/z="nan" → z=nan 即 p=1.0 入族）、`em1_round_missing_groups`（轮完整性 fail-closed 判据）、`em1_round_adjudicate`（两脚本共用裁定 seam：缺组→bh_pass=∅ 不裁，否则全族一次 BH）。
- `09-em1-facet-a.py`：F1-F6 与 F8 两处 `bh_family` 内联调用废除；改为先落搜索+验证账→`em1_round_adjudicate` 装载全轮族一次校正→两段晋升/确认窗从同一 bh_pass 取成员（确认行二次幂等落账）。
- `10-em1-facet-bc.py`：同构改造（B/C 与 09-A 并族）。
- `09-em1-protocol.md`：追加修订 C（人裁措辞稿原样 + 落地段：台账装载/fail-closed/历史 m=1 无翻案）。
- `apps/backend/tests/unit/test_em1_script_semantics.py`：扩 `TestBhRoundSingleFamily` 9 测（两 facet 脚本候选并族 m=20、并集阈值钉死分族反例、强候选并族下仍过、无行者 nan 入族、缺组 fail-closed、read/adjudicate seam 往返）。

**族装载/防硬序**：族成员=共享试验账（09-em1-trial-ledger.csv，两脚本本就同账）的 search-candidate 条目并集；脚本各自先落自己的搜索+验证账再读账装载 → 跑 09 不需要先跑 10。轮不完整（贡献组缺 search 账）时 fail-closed：不裁、候选留 bh_pending，其余脚本落账后重跑即收口（脚本幂等，确认窗只碰实际晋升者）。沙箱全链路验证：完整账下 09/10 均报「并集 m=1（10-C-C3-荷甲 dead，z 不可计算 p=1.0）、晋升 0」且**试验账字节不变**（md5 前后一致）；空账先跑 09 → 正确打出缺 10-B/10-C 未裁、跑 10 后重跑 09 收敛同一 m=1 视图，entry_id 集与原账完全一致。

**验证**：定向 20/20 绿；`task lint`/`task type` 绿。全量 unit 1647 过 + 1 失败（test_bucket_face::test_live_faces_reproduce_report，干净树同样失败=预存数据漂移，与本票无关）。三轴：tdd 红绿循环；code-review 双轴（Standards 发现两脚本 fail-closed 块重复 → 已抽 `em1_round_adjudicate` 共用 seam 消除；Spec 全项覆盖）；correctness 七类对抗探查（边界/空账/状态/原子性/幂等/身份后缀换算/时间）无阻断项，两 info 级备注：裁定用台账 3dp z（单一事实源，刃缘口径差异可复现）与跨轮同账并族只更保守不放松。
