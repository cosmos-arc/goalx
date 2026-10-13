# 01 旧模板 detail 解析器 v3：技统/事件/阵容/阵型救回 2016-2020

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` A 组 User Story 1（顺带 Story 3 的版本隔离纪律）

## 任务

旧模板 detail 页（19,878 张 raw，2016-01→2020 换模板前）解析出技统/事件/
阵容/阵型进 bronze（silver match_detail 摘要面同构受益），让 2016-2019 四季
的技统与阵容特征从零变全覆盖。raw 全在语料树，重物化零网络。

## 不变量与人裁决项

- 旧模板结构（真树实测钉死，2017-11 场 1418016 + 2016 分层抽样）：
  页题/表头标记=「详细事件」；技统 th 两代变体（2016「技术统计」/
  2017+「本场技术统计」），行=bar|主值|名|客值|bar 五格；事件表
  首行=半场比分行（colspan≠5 天然跳过），行=主侧|主图标|时刻|客图标|客侧，
  2016 图标无 title——事件语义取自页内图例（bf_img/N.png→标签）；
  阵容=teamNames（队名+阵型）+ plays 容器（class="home[^"]*"，playBox>
  play>锚文本「号 名」融合、pid 在 player URL）+ backupPlay 替补块；
  无 xG/裁判/教练（真无，空≠无，定则 4）。
- 版本隔离（Story 3 纪律）：v2 函数与既有 v1 bronze 行不动；新增
  `parse_detail_page_v3` + 分发入口，BRONZE_VERSIONS bump
  srct_detail_v1→srct_detail_v3，重物化走既有 append_bronze 回补通道。
- payload 形状与 v2 同构（meta/tech/has_xg/events/lineup）——silver
  match_detail 摘要面零改动即扩窗。
- 门⑤同步：TEMPLATE_MARKERS[match_detail] 多标记化（现场分析|详细事件），
  旧模板页进解析分母、出 template_mismatch 桶。
- 代称红线：fixture 队名/球员打码（主队甲/主首A 式）。

## Answer

2026-10-07 交付（分支 feat/srct-v3-parser-20261007）：

- `parse_detail_page_v3` + `parse_detail_dispatch`（新→v2 旧→v3，先查新标记
  防「详细事件」串复用干扰）；BRONZE_VERSIONS srct_detail_v1→v3；
  TEMPLATE_MARKERS[match_detail]=(现场分析, 详细事件)，门⑤分母两代全纳入。
- 重物化：48,561 raw 全量 v3 重解析 0 失败；correctness-review 四反例
  （加时分钟≥100' 210 行/无 span 首发块 sid 1549846/2016 仅场地行 2,063 页
  连坐/事件图标只取首图例）修复后旧模板 19,878 页定向重补 0 失败，
  四反例页逐页复验治愈（11+11 首发/venue 独立捕获/118' 加时串在列）。
- silver match_detail 摘要面零改动扩窗；真树冒烟 1,012 旧页：
  tech 100% 有行、events 99.8%、首发 78.6%、替补 46%、has_xg=0（真无）。
- 测试：脱敏 fixture（2017 版+2016 变体）+ 四反例回归 + 分发路由 +
  版本钉死 + silver 面全链（test_ingest_srct_market 末场走 v3 路径）。
