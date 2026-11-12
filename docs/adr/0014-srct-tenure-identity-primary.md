# ADR-0014: 任期/队籍身份主空间=源T；TM 降级对账/备用

日期：2026-11-12
状态：已接受（mining-loop 票 10 落地，spec-srct-cutover；修订 ADR-0013 的
供给边界表述）

## 背景

六轴教练/阵型/底蕴面的身份链历史上经 TM（TransferMarkt）：facet 键
club_tm_id、教练键=TM 英文名、队籍面=tm_club_seasons。TM 采集器按**现役
联赛名单**枚举俱乐部（204 队快照），历史升降级队零任期行——票 06 实测
搜索窗 E0+E1 教练双侧映射率 62.2%（人裁门槛 95%），同一缺口压着底蕴面
「顶级连续年限/升降级频谱」对 18 家队静默 NULL。身份链六跳
（universe 中文名 → elo 桥 → hist 英文名 → TM 认领 → 任期 → 教练）每一跳
都在丢队。

票 08/09 已落源T（球探网）任期语料与 silver 两表（`srct_coach_tenures`
654 行 / `srct_club_seasons` 528 行按构造完整）；两源对账：区间吻合率
89.8%、起点差 99.5% ≤7 日、错位 17/19 为开区间语义差（无害——读缝
PIT 双闸消费 date_from 为主）。

## 决策

1. **任期/队籍身份主空间=源T**（用户裁定 B'，spec-srct-cutover）：
   - `formation_family_facet`（v62 换键）：俱乐部键 `club_srct_id`（源T
     team_id）、教练键 `coach_key`（英文名优先、缺则 playerInfo id）。
     **单一键空间，无双键兼容列**（不建身份兼容层）。
   - 身份管道（data/srct_identity.py 唯一属主）：corpus 侧同站中文名折
     （universe/match_detail ↔ matchResult 逐季枚举，同站同季同语）；
     hist 英文名折（三级词法 NameIndex + 受控别名：West Brom/Sheffield
     Weds/Peterboro 实测三例 + 同站名漂移两例）；任期装载复用
     `assign_manager_day` 区间语义（open-end=现任），幽灵教练守卫
     （双 playerInfo id 同名同区间折叠为一键）。
   - `club_pedigree` 频谱/年限因子改读 `srct_club_seasons`（顶级口径=
     league_id 受控词表 `SRCT_TIER1_LEAGUE_IDS`，现含英超 36）；荣誉
     计数仍 HONOURS 受控种子（不切）。
2. **读缝零接口改动**（缝策略人裁）：`coach_family_prior` 签名/PIT 双闸
   （`season<=upto AND last_match_date<on_day`）、embargo、S7 REPLACE
   幂等语义逐字不变——只有底下的身份键空间换血（spec story 13）。
3. **TM 降级对账/备用**：采集/调度/表/派生（coach_changes 等）完整保留
   不删；`club_pedigree.club_tm_id` 列保留 TM 锚（信息列，对账用）。
4. **修订 ADR-0013 供给边界表述**：ADR-0013 的「别名供给不灭」定则约束
   team_aliases 锚域（canonical 队身份）；任期/队籍身份不在此链上——
   源T 侧原生键直接成主空间，别名桥（transfermarkt）不再是任期面的
   供给依赖。

## 后果

- **非 GB 诚实收缩**：源T tenure 语料 GB 双联赛先行，facet 非 GB 联赛行
  （128 家俱乐部）随切换消失（`no_srct_identity` 桶如实计数，coverage
  report totals 显形）；pedigree 非 GB 队年限/频谱 NULL。**批量扩域回流
  零新基建**（spec story 15）：采非 GB 逐季/履历文件 → silver 全量重建 →
  derive 自动扩面；tier 词表按国补行。
- v62 迁移整表弃 TM 键旧行、derive 全量重派（首拍诚实报 preserved=0/
  inserted=N）；迁移前主库快照是回滚锚（运维步）。
- 幽灵教练（tdl coach 变量伪 id 现象）在装载层折叠，教练键空间单一——
  换队保族/重逢旧主跨俱乐部追踪不断裂。
- 教练 role 域退化：源T 无 caretaker 分类，面内 `coach_role` 恒
  manager（列与 CHECK 不变，语义注记在属主模块）。
- 任期 date_to 语义差（源T「事实离任」开区间 vs TM 合同期末）：PIT 读缝
  以 date_from 为主不受影响；未来若消费 date_to 需选边（源T 口径更贴
  比赛面，票 09 对账注记）。
