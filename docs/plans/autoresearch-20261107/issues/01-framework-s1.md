# 票 01：框架（注册表+台账）+ S1 市场校准日扫

Status: ready-for-agent

## 交付

1. `edge_findings` 台账表（迁移+schema 版本；append-only，归
   evaluation）：scanner/segment_key/metric_value/n/se/window/
   status(new|promoted|dead)/found_at/notes。
2. 扫描器函数 `evaluation/edge_scan.py`：S1 市场校准（昨日已结算场
   分桶累计，滚动 90 日窗，二项 SE，越门落台账）+ 注册结构（供
   S2-S5 逐个点亮）。
3. 定拍：日扫挂 daily-wrap 后（赛果已结算）或独立 DatasetSpec。
4. CLI：`goalx edge-scan [--scanner s1]`（手动重跑/回填）。
5. 测试：分桶累计正确性/越门判定/幂等（同窗重跑不重复落台账）。

## 验收

- 真树首跑：对昨日已结算场出第一份台账行；十年回放一次（历史验证
  口径 vs 今日手工版一致）。

## Comments

- 数据源：hist/psc 面的已结算场（今日手工扫描用的 backtest_predictions
  是十年回放面；日扫用运行面 fixtures×results×odds as-of）。
