# 03 M3 验收器语义修复

Type: task
Status: resolved (PR #217, 2026-11-03)

## Question

M3 验收器两处语义缺陷：Tier A 的 ECE 检查恒不可通过（`_mean_ece` 桩），Tier B
只查 DM 显著性不查方向——融合显著**更差**也判"达标候选"。是否按"ECE 三键加权
真算 + 方向/幅度核验"修复？（本票即 `m3_report.py:202-212` docstring 预留的
"另票，勿静默复活"。）

## 事实（2026-11-03 main 实测）

- `llm/m3_report.py:202-212` `_mean_ece` 恒 `None`：类型化揭出后组指标自
  `ece` 拆为 `ece_h/d/a`，取 `'ece'` 恒缺 → 恒 None（docstring 记录在案，
  修复属另票）；195-198 `_ece_clean` 双 None 短路；157 `ece_no_degradation`
  恒 False → 160 `all()` 恒 False，"达标（融合线保留）"不可达
- `m3_report.py:170-184` `_tier_b` 四条件（span_weeks≥30 / pairs≥500 /
  dm_p<0.05 / 复核净）无方向项：`fused_win_rate`、`mean_rps_delta`、
  `dm_stat` 符号从未参与；`evaluation/metrics.py:150` DM 检验显式双侧
- 隔离构造：600 场 / 32 周 / 融合平均 RPS 更差 / p=0.001 → 仍"达标候选
  （须用户显式裁决切换）"——显著更差与显著更好同判

## 验收

1. ECE 真算：`ece_h/d/a` 按 pair 对数加权聚合（或人裁决口径），Tier A 判决
   可达；桩的 docstring 注记更新为已修
2. Tier B 增方向核验：融合更优（`mean_rps_delta` 符号 + `dm_stat` 符号一致）
   为必要条件；显著更差必须判未达标并写明方向
3. 判词三态：达标候选 / 未达标 / 方向反向（显著更差），契约同步
4. 黄金样例：显著更好 → 达标候选；显著更差 → 方向反向；不显著 → 未达标

## 不变量与人裁决项

- 人裁决：ECE 聚合口径（推荐对数加权；docstring 自荐同向）
- "达标候选须用户显式裁决切换"的措辞保留（不自动放行真钱，现状红线）

## Comments

- 2026-11-03 人裁决（逐项过账）：ECE 聚合 = **按 pair 对数加权**（整体期望
  校准误差口径，docstring 自荐同向）；"达标候选（须用户显式裁决切换）"措辞
  **保留**（验收标识 ≠ 自动放行红线不动）。
