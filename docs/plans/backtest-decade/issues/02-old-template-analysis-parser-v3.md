# 02 旧模板 analysis 解析器 v3：数组面救回 2019-2020

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` A 组 User Story 2

## 任务

旧模板 analysis 页（5,841 张 raw，2019-2020）按既有数组数据模型
（h_data/v_data 等）解析入库，近况/交战特征向前多覆盖两季。raw 全在，
重物化零网络。

## 不变量与人裁决项

- 旧模板结构（真树实测钉死，2020-12 场 1915380 + 199/199 抽样验证）：
  数组面与新模板同构——var h_data/a_data/h2_data/a2_data/v_data/
  Vs_hOdds/Vs_eOdds 七 var 全在（等号两侧带空格，v2 正则已容忍）；
  homeScoreStr/guestScoreStr 两 var 缺席（积分榜=HTML 表，本票不解析，
  arrays 键留空=空≠无）；meta hometeam/guestteam var 在、strTime 缺
  （kickoff None，silver 经 fixture_universe join 补）；守卫标记=「var
  h_data」（两代页 64KB 头窗内 100% 命中，门⑤分母同步用）。
- 未来五场与新模板不同构：外层表两 50% 半区（主左客右）各含内层
  TABLE，行=首格日期形态（MM-DD）；v2 单格行切换法不适用，v3 独立解析。
- 版本隔离同票 01：v2 行不动，BRONZE_VERSIONS bump
  srct_analysis_v1→srct_analysis_v3，分发入口接线。
- payload 形状与 v2 同构（meta/arrays/future_fixtures）——silver
  match_analysis 摘要面零改动即扩窗（standings_home/away 老页=0）。
- 代称红线：fixture 队名打码。


## Answer

2026-10-07 交付（分支 feat/srct-v3-parser-20261007）：

- `parse_analysis_page_v3` + `parse_analysis_dispatch`；数组面复用
  `_analysis_array_rows`（等号空格容忍实测在）；BRONZE_VERSIONS
  srct_analysis_v1→v3；TEMPLATE_MARKERS[match_analysis]=(数据分析,
  var h_data)。未来五场独立解析：两 50% 半区切分 + 表头剥除移植
  （correctness 反例：37 页表头行缺 </tr> 并块丢首条赛程，已修）。
- 重物化：48,561 raw 全量 v3 重解析 0 失败；修复后旧模板 5,844 页
  定向重补 0 失败。真树冒烟 305 旧页：h_data 100%、v_data 93.4%、
  未来五场双侧 96%、meta home 100%。
- silver match_analysis 摘要面零改动扩窗（standings 老页=0，空≠无）。
- 测试：脱敏 fixture + 并块回归 + 分发路由 + silver 面全链。
