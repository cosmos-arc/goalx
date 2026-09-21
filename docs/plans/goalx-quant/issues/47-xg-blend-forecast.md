# xG blend 接线 Forecast 生成

Type: impl
Status: resolved
Blocked by: none（票 45 实证对比已出数；2026-09-21 用户裁决 blend 终选）

## 问题

票 45 实证（7,284 场配对）：blend_goal_xg（goal-DC × xG-DC had 线性池
50/50）是唯一全桶一致改善的变体（overall -0.0012 RPS）。用户裁决（2026-09-21
"同意"）：blend 定为 xG 融合终选，接线进 Forecast 生成；票 46 扩联赛清单与
当季 AvgC 分期口径同批追认。

## Answer

### 实现（2026-09-21，feat/xg-blend-forecast）

- **混合实现（modelling/forecast.py）**：矩阵层算术混合（两网格
  cell-by-cell 50/50）——had 池**精确等于**实证口径的线性池，且保持
  canonical 矩阵表示（crs/ttg/hafu 全玩法推导一致，ADR 0006 不破）；
  λ 字段 = 几何均值（Poisson 对数池语义，hafu 半场拆分近似用）；rho =
  加权混合（信息位，网格已烘焙两侧 rho 效应）。
- **xG 侧供给**：`_fit_league_xg` 每次生成时 as-of 当日现拟合（scipy
  ~0.5s/联赛，无工件落盘）；训练取数 `rs_store.understat_training_rows`
  （datetime < 生成时刻——防前视：训练行全部已完场且早于决策时点）；
  队名解析 = fixture 英别名/canonical → understat title 位置索引
  （NameIndex，规范化三级匹配）。
- **回退语义**：understat 无覆盖（荷甲/Tier2）、拟合不收敛、队名解析
  不到 → 纯 goal-DC 落库（payload.xg_blend=None），model_version 前缀
  `dc-` / `dc-xgblend-{goal工件}+{xg指纹}` 区分可追溯。
- **CI 口径（诚实标注）**：goal bootstrap 样本与 xG 点估计逐样本池化
  （ci_method="goal_bootstrap_pooled_with_xg_point"）——保持 goal 侧参数
  不确定度，xG 侧当点处理（低估其不确定度，payload 注明不冒充）。
- **票 34 冻结边界**：前瞻评分协议不动（仍读赛前最新 Forecast×市场
  基准）；模型迭代经 model_version 分组可见，旧 Forecast 不覆盖。
- ForecastStats 增 xg_blended/xg_unresolved 计数；forecast_daily 返回
  字段同步。

### 测试（4 新增）

- 混合数学：网格混合/had 线性池精确性/λ 几何均值/rho 混合/溯源字段；
- 无 xg 侧 payload 与票 27 形态一致；
- 生成链路：英超 blend 落库 + 荷甲纯 goal-DC 兜底；
- understat 有数据但队名解析不到 → 兜底 + xg_unresolved 计数。

### 质量门

`task check` 全绿（后端 487 + web + e2e）；contract 零漂移（Forecast
payload 自由 dict，无 API 变更）；import-linter kept。
