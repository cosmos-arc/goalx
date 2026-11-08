# 票 01：策略 v2 探针阵列（纸面账户重构 + 每注 CLV 接线）

Status: ready-for-agent（池面敞口已裁 2026-11-08：A=¥20/晚测量帽，随 prereg 修订⑤同车落——票 04 窗口内）

## 问题

纸面账户现行 auto-v1 是 EV 集中注——十年逆向选择实证下它是校准探针
却顶着"正 EV 机会"的语义；无来源标签则四类 EV 源的相对表现永远混在
一起；无每注 CLV 则验证轨缺最高样本效率的仪表。

## 交付

1. bets 增列：`edge_source`（calibration_probe|stale_line|rollover|
   price_band|info_lag）与 `clv`（入场价 vs 源T 收盘锚，结算时回填；
   迁移+schema 版本同车）。
2. 探针组合（auto-v2 候选，随六步门纪律晋升）：校准探针 ¥6/晚
  （claimed-EV 注，测当代模型 EV 实现差）；陈盘/滚存/价格带探针各
   ¥2 随扫描器点亮逐个启用（无信号日记零注留痕）。
3. 池面敞口**人裁决位**：A=缩编 ¥20/晚测量帽（纯测量定位）｜B=维持
   64/128（含体验敞口，用户照玩）。裁决落 prereg 修订⑤同车。
4. 报告面：账户 P&L 按来源分列 + CLV 滚动表（分来源正 CLV 持续=
   六步门第③步证据）。
5. 测试：flow 整跑缝（auto-paper 票 03 同缝）钉标签落账/CLV 口径/
   零信号日留痕/幂等。

## 不变量

- 落注仍走 create 闸门零旁路；CLV 只读派生（收盘锚 as-of）；
- strategy_version=auto-v2 与 auto-v1 分账，历史不改写。

## 验收

- 探针注带标签与 CLV 落账；分来源 P&L/CLV 表可见；A/B 裁决入 prereg。
