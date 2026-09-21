# 十年回测语料：fdhist 扩联赛 + openfootball 直连

Type: impl
Status: resolved
Blocked by: none（语料导入独立可先行；openfootball ingest 若票 44 已落地则复用不重做；对账消费接线若 44 未合则后置）

## 问题

research/17 §五补查翻案："十年竞彩史库"（45274159-stack）实为 fd.co.uk + openfootball 缓存搬运（无赔率列、无竞彩编号、无 LICENSE），"官方口径"日更仅 2026-09-03 起且靠 cpbao+500 双第三方镜像互证——不引入。用户诉求（长周期结构回测，验证彩池 v2"无正EV冷门属结构性"的样本外稳健性）真实，正确载体 = 直连两个一手源：fd.co.uk（~25 联赛周更、含 Pinnacle/AvgC 收盘赔率列）扩 FD_COMPETITIONS；openfootball/football.json（CC0、活跃、五大+荷甲+葡超+英冠等）raw 直下。fdhist 现仅六联赛（五大+N1）六季。

## 范围

1. fdhist 扩联赛：FD_COMPETITIONS 按竞彩混编联赛频率扩充（以 fd.co.uk 实际目录有收盘赔率为准；英冠/葡超/土超/比甲/苏超/美职/巴甲/墨超/日职等为候选，清单待人确认）；每季初 SEASONS 维护惯例不变
2. openfootball 直连 ingest（若票 44 已并入则跳过本项）：raw 静态 JSON，`score` dict/list 两形态兼容；比分回填滞后约一轮属正常
3. 语料交叉验证：openfootball 比分对 fdhist（重叠联赛），差异清单落报表
4. 用途边界：结构回测——公平概率 = fd.co.uk Pinnacle 收盘 Shin；竞彩赔率近端用自家快照、远端诚实标注欧赔代理口径（票 34 同原则）；**不做结算事实源**（海外源仅对账/语料不入结算管道）
5. 产出：十年×扩联赛语料落库 + 回填完整性报表（每联赛每季行数/缺口）；为彩池 v2 结构性结论的样本外复验备好数据面

## 增补（2026-09-20 research/18 裁决）：早期赔率列一并导入

- fdhist 扩联赛的同时增导 **PSH/PSD/PSA**（同书早期市场价，与收盘 PSC 同书同口径）
  → hist_matches 增列（migration 追加列，不改既有行）；开→收漂移、时机策略
  （早锁定 vs 等临场）、Buchdahl"1/3 价值蒸发"复现全部可回测（research/18 §一/§三）
- soft 书早期列（B365H 等）**不做**——老 CSV 缺失多且竞彩无历史价，生态效度弱；
  等方向 A 有利润证据再议（证据触发）
- 结构回测用途边界不变：公平概率仍 = 收盘 PSC Shin

## 不变量与人裁决项

- 扩联赛清单（建议按竞彩混编出现频率排序）待人确认
- 远期结构回测的代理口径标注方式（诚实回测边界）

## Answer

### 实现（2026-09-20，feat/decade-corpus）

- **fdhist 扩联赛**（清单待人追认，增删只动 FD_COMPETITIONS 元组）：新增
  E1 英冠 / P1 葡超 / T1 土超 / B1 比甲 / SC0 苏超——五者 2526 季实测
  有 PSC 收盘列（200）；**美职/巴甲/墨超/日职不在 fd.co.uk 标准季目录
  （404）**——竞彩混编的美洲/日职场次收盘基准仍缺口（结构限制，非清单
  裁决能解决）。SEASONS 扩为 1617..2627 十一年窗（每季初维护惯例不变）。
- **openfootball 直连**：票 44 已落地，本票跳过（票面预设分支）。
- **交叉验证**（ingest/openfootball.cross_check_fdhist + CLI corpus-report
  --cross-check）：openfootball 已回填比分 vs hist_matches，规范化队名
  （fd 缩写↔of 全名经 team_align 规范化）+ 比赛日 ±1 配对，比分严格比较；
  404=not_available（of 无该季文件）。
- **完整性报表**（evaluation/corpus.completeness_report，取数层
  rs_store.hist_season_coverage 归 data 域；league 常量正典在 fdhist，
  evaluation 不上行依赖——import-linter 分层修正与票 45 同型）。
- **用途边界**（票面 4）：语料只做结构回测，不做结算事实源；公平概率
  PSC Shin 主锚 / AvgC Shin 兜底（票 34 分期对照原则沿用）。

### 语料落库与报表实测（2026-09-20 主库）

- 导入 38,460 行（11 联赛 × 11 季，failed_files=0；1617 季起全部可得）。
- **交叉验证 8,178 场比对 8,176 一致（99.98%）**；仅 2 场分歧，均在
  P1 2526 末轮 2026-05-16（Moreirense 0:1 vs fd 0:0；Casa Pia 2:1 vs
  fd 1:1）——fd 侧末轮数据错误或含改判，量级 0.02% 可接受，清单已落
  报表样本（mismatch_limit=5 截断）。not_available 6/88 联赛×季
  （E1/N1/P1 的 1617/1718，openfootball 无早期文件）。
- **收盘基准缺口量化**（fd 换源 2025-07-23 问题的十年窗全景）：
  2425 及以前 PSC+AvgC 完整；2526 PSC 缺 2,011/3,761（53%）但 **AvgC
  全量可得**；2627 当季 PSC 全缺（527/527）、AvgC 全量可得。→ 十年结构
  回测的公平概率 = PSC Shin（1617-2425）+ AvgC Shin（2526 起，分期
  标注），口径与票 34 run_baseline_comparison 的 psc/avgc 分期对照同构。
- 报表工件：.scratch/goalx-quant/research/46-corpus-report.json
  （回填完整性 + 交叉验证全量）。

### 待追认 → 已追认（2026-09-21 用户"同意"）

1. 扩联赛清单 = E1/P1/T1/B1/SC0（增删改 FD_COMPETITIONS 一处元组即生效）。
2. 当季 PSC 全缺口径 = AvgC Shin 分期标注（票 34 同则），彩池 v2 复验票
   开工时按此执行。

### 质量门

`task fmt-check/lint/type/test` 全绿（+4 新测试）；import-linter kept
（分层修正）；contract 零漂移（无 API 变更）；web 未动。
