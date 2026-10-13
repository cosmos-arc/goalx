# 03 重物化判门复查：v3 落地后真树五门全量复查

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` A 组（Further Notes：A1-A3 完成后需一次
重物化+判门复查，模板外桶应清零/锐减）

## 任务

解析器 v3（票 01/02，PR#129）落地并全量重物化后，真树重跑 srct-gate 五门：
门⑤ template_mismatch 应清零；门①②④在扩窗 silver 上维持绿；A4/A5 卫生
接线（同分支）首次真树生效验证。

## 不变量与人裁决项

- 判门=报告+用户点头：overall 只如实给 provisional/pending_user。
- 重物化事实（PR#129 收尾，日志 .scratch/remat-final-1007b.log）：elo →
  srct-silver/odds/market → jc-silver 全序列 rc=0（2026-10-08 05:24
  ALL DONE）；本票复查在其后、含 A4/A5 接线代码的树上一跑定音
  （.scratch/gate-a3-20261008.json，报告已落语料树 reports/phase1-gate.*）。

## Answer

2026-10-08 交付（分支 feat/hygiene-a4-a5-20261008）：

- **门⑤（核心验收）全量 pass**：template_mismatch_total=0（PR#129 前
  旧模板外桶 19,878+5,844 → 0）；七数据集键覆盖最差 0.99998
  （odds_1x2d），match_detail/match_analysis 48,561 模板页覆盖均 1.0
  ——v3 两解析器真树全量救回钉死，伪 200/不可读均 0。
- **门① pass**：rate 0.9952（阈值 0.99），matched 37,866（名字身份轮
  37,838）；**admin_excluded=30**——A4 排除表真树首次生效，真库核对
  30 行全为行政假赛（地震窗 29 + 阿达纳 2025-02-09 单场），零真赛误伤；
  gaps 45 / ambiguous 137 / 口径外 484。
- **门② pass**：median 0（v2 裁决口径）；pairs 37,866、usable 5,414、
  **immature_skipped=1,181**（A5 纪律真树生效——近 6 个月未成熟面截走）。
- **门④ pass**：unexplained_gap=0。
- overall=provisional（Phase1 进度 0.9997——判门窗随日期自然增长，
  近期日夜班未收口，非回归）。
- **留账如实（不裁决）**：门② per_season 2025-26 成熟残余 median
  1.3%（5,211 比较）——A5 截走未成熟 1,181 行后仍在，证明 2025/26
  偏差主因不是 PSC 未成熟（该季全在 PSC 换源后，更像换源口径差 vs
  cid177 锚）；既有留账（门② 2025/26 待复查）继续挂着，C/D 相分时代
  报告量化。spec"成熟度纪律落地后自然消解"的预言只兑现一半：未成熟
  尾巴截掉，1.3% 主体仍在。
