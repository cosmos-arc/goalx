# 票 08 探针报告：E0 2023-24 轨迹收盘缺口——真因勘误与补齐可行性

日期：2026-10-16（票面排期"一夜探针先行"；实际探针=纯本地分析，零网络）

## 结论（推翻票面根因假说）

票面假说"detail 页 2020 换模板旧页未采（v3 模板候选 2.57 万张）"**不成立**：

- bronze 六端点对 E0 2023-24 **全覆盖**（detail/analysis/stats/asian_odds/
  over_down/odds_1x2d 均 380/380，detail 108,124 行总量含全部该季场次）；
- "v3 模板扫描/一夜夜班"路径不需要——缺口与 detail 页模板无关。

**真因**：odds_1x2d `gameDetail` 旧代行格式（7 字段，无年份尾缀）被
`srct_odds._parse_detail_row` 按 8 字段当代口径解析 → published=None →
`keep_value_changes` 全部按 bad_time 丢弃 → 1x2 变价事件零落
odds_change_event silver → gold `_trajectory_close` 无从派生 close1x2
→ 引擎轨迹 era（2023-24+）该季整季跳过。

铁证链（每步实测）：
1. E0 2023-24 silver `odds_change_event`：每场只有 ah/ou 事件
   （如 sid 2398985：ah 1905 行+ou 1479 行+**1x2 0 行**）；英超分区
   380 场全缺 1x2；
2. 同 sid bronze odds_1x2d：175 书商 × 轨迹点全在，gameid↔cid 映射
   175/175 全配；
3. 单场复现 `_emit_1x2_events`：source_rows_1x2=6810 读入、unmapped=0、
   kept=0——行被时间不可解丢弃；
4. 行格式对比：2023-24 行 `9.2|5.4|1.36|08-12 02:48|0.98|0.96|0.97`
   （7 字段）vs 2024-25 行 `1.65|4|5.5|08-17 02:57|...|2024`（8 字段
   带年份尾缀）；`_DETAIL_FIELDS=8` 校准注释"实测 516,027 行零异形"
   的样本只覆盖当代形态。

## 全库量化（全量流扫 odds_1x2d 1.76GB）

- 旧 7 字段行 **150,169,539**（**75.8%**）/ 新 8 字段 47,903,417；
- 受影响 sid（含旧格式行）：38,681 / 有轨迹 sid 48,524（79.7%）；
- 影响面≠E0 2023-24 一季：英冠/欧罗巴/土超等全部联赛 × 2016→2023-24
  全部赛季（英冠每季 ~552 场全旧格式；2024-25 起新格式）。
  gold close1x2 缺失与 era 分层（psc_proxy 2016→2023 不需要轨迹收盘）
  相互作用，引擎面受伤的正是轨迹 era 的 2023-24。

## 年份推断可行性（分层抽样 5,040 sid / 旧格式行 1940 万）

规则：候选年=开球年±1，取 ≤开球 的最近候选（PIT 安全：宁可 None
不落开球后时刻）。

- 年份可解 **99.8%**；落在开球前 35 天窗内 99.4%；
- 锚书（cid177）在场 5027/5040；推断后可得开球前末价（=收盘可派生）
  **5025/5040（99.7%）**。

## 补齐路径（照探针定节拍）

**零网络零夜班**——bronze 全量在手：

1. 解析器支持 7 字段行+年份推断（`_pre_kickoff_year_ms`，本分支落地，
   ODDS_SILVER_VERSION v2→v3）；
2. `srct-odds` 重物化 odds_change_event（本地长跑，流式 291M 源行）；
3. `gold-build` 重建特征面（close1x2 派生自动恢复）；
4. `srct-gate` 判门复跑受影响面（fail-closed 不放宽）。

原始输出：`probe-e0-close-raw.log`；探针脚本：`probe_e0_close.py`。


## 修复执行与验收（2026-10-16 晨）

- 解析器修复：`_pre_kickoff_year_ms`（开球年±1 取 ≤开球最近候选，
  PIT 安全）+`_parse_detail_row` 7 字段行支持；ODDS_SILVER_VERSION v3。
- 重物化链真树执行（本地，exit 0）：srct-odds（events_1x2
  48.9M→**180.9M**）→ gold-build（close1x2_rows **14,115**）→
  srct-gate 四门复跑全 pass 不放宽：门① 匹配率 0.9952 / 门② PSC×cid177
  中位偏差 0.0023 / 门④ unexplained_gap=0 / 门⑤ 键覆盖 0.9959。
- **目标验收：E0 2023-24 close1x2 0/380 → 380/380 全恢复**；2015-16→
  2022-23 无轨迹收盘=psc_proxy era 设计使然（该 era 走 PSC 收盘，
  非缺口）——引擎轨迹 era 自此数据面完整。
- era 检查面：主锚 2025-07-23 滞后警示落 signal-attribution 判读注意。
