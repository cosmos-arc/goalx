# ADR-0013: 队名身份机制——别名供给不灭与英文域确定性互通

日期：2026-11-07
状态：已接受（identity-alias-20261107 板落地；修订 ADR-0012 §4 别名供给的隐含假设）

## 背景

`team_aliases` 的英文名供给历史上是 odds_api/propline join 的副产品；两源 2026-09
下旬停采后供给机制性死亡。后果：新进竞彩的队（升级队/新队）只有源T 中文名，
`generate_forecasts` 三级匹配全不中 → no_mapping **静默跳过**。2026-11-07 普查坐实
本季 31 支 FD 联赛队（阿森纳/利物浦/法兰克福/费耶诺德等常客）数月零 ML 覆盖，
无任何告警；训练侧（understat/FD join）同构依赖别名，是同一类死法。clubelo 英文桥
（ADR-0012 §4）只认 odds_api/propline 锚——锚死桥死，自饿。

用户裁决（2026-11-07）：彻底解决，建立机制，不允许一次性补丁（当日 31 条 manual
热修仅为恢复运行的一次性行动）。

## 决策

1. **身份主干与名字登记**：`teams` 为身份主干（JC ingest 语义不变——每个新中文
   名串新建行，中文变体去重另议）；凡活源携带队名一律登记进 `team_aliases`
   （per-source provenance：srct/srct_en/manual/clubelo/odds_api/propline）。
   **供给不灭定则**：任何 ingest/join 路径见到新队名，必须经 team_align 登记或进
   未匹配清单，不允许"见过即丢"。
2. **英文活水 = 源T raw 重解析（source='srct_en'）**：源T 原始 odds_1x2d 页自带
   英文队名（`hometeam`/`guestteam` var，与 `_cn` 并存），历史解析器丢弃但 raw
   100% 归档。mapping_sync（srct-shift post-beat */30）对已链场次（ADR-0012 物化
   链）重解析归档原页产出 (team_id, 英文名) 对——零网络、历史回填与增量同路径。
   这是唯一无需英文锚即可引导的英文供给：understat/FD join 自身依赖已有别名
   （鸡生蛋），救不了新队。队级幂等门（已持 srct_en 的队跳过）兜住常态成本。
3. **同名异队不静默**：`record_srct_en_aliases` 落库前显式查现有归属，
   UNIQUE(source,alias) 拦下的后到者进冲突清单交人工裁决（定则 1 不硬配）。
4. **clubelo 桥解饿**：锚源集合由 (odds_api, propline) 追加 srct_en/manual——
   桥从死锚迁到活水，ADR-0012 §4 的隐含假设（英文别名总可得）显式化。
5. **英文域互通走确定性三级 NameIndex**（team_align 既有：精确→规范化→词元
   子集唯一命中，歧义返回 None）——FD 缩写（Ein Frankfurt/M'gladbach/Hull/For
   Sittard）经规范化与缩写表等价展开，禁模糊置信合并（定则 1）。
6. **守门**（identity-alias 票 02）：晨检读最近业务日 forecast_coverage 的
   no_mapping 行即红（容忍度=0），辅以 alignment-report 覆盖率；普查计数
   （english_alias_census：竞彩 universe 队 × 英文可解析比）随 mapping_sync 输出。

## 后果

- 新队进竞彩：首场被链 + raw 页在档 → 别名当日登记（30 分钟级）；未链/无页 →
  晨检红 + set-alias 人工兜底——静默洞在结构上不可再发生。
- 尾巴诚实可见：链接覆盖外的队（约 43% 全 JC 面，多为非建模联赛）不进英文域，
  由普查分母如实呈现；FD 建模域覆盖以晨检为准。
- manual 热修与 srct_en 共存互证（UNIQUE(source,alias) 幂等）；odds_api/propline
  若复活，其别名写入路径不变，天然回归。
- team_aliases 归属与 ADR-0008/0012 红线不变：mapping/ingest 只产对子，落库在
  team_align。

## 已知取舍

- teams 表中文变体重复行（如「弗洛西诺内/弗洛西诺尼」两行）不在本 ADR 范围：
  下游经别名索引归并，上游行数冗余是接受的成本；去重牵动 fixture 自然键，另板。
- clubelo 评级表本身停更（票 74）：仅作名字词典与历史 Elo，不构成本机制的依赖。
