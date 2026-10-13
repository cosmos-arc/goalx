# 15 hhad/ttg 解锁判定报告：真实 AH/OU 收盘线 vs 1X2 反推（S15）

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` C 相 User Story 15
Blocked by: （无——gold AH/OU 收盘族 + 1x2 轨迹收盘已就绪，票 06）

## 任务

had-only 的来历：M2 实测 1X2→比分矩阵反推（goal_expectancy）总进球
维度系统性欠分散（ttg 桶 4/5/6 隐含低于实际 15-40%），反推价制造假
edge。现在 gold 有真实 AH/OU 收盘族（11 家模板书商十年）与双锚轨迹，
可以做**数据对照报告**回答"真实线派生的 hhad/ttg 是否可信"：

- 真实 AH/OU 收盘线（gold `ah_*`/`ou_*` 族 + cid8 锚行值）+ 市场隐含
  1X2（era 分层收盘）→ 派生 hhad/ttg 市场侧分布；
- 对照旧 1X2 反推价（`market_implied_matrix` 路径）的假阳性根源量化：
  十年逐场 ttg 桶隐含 vs 实际分布、hhad 隐含 vs 实际命中率；
- 报告落语料树 `reports/`（MD+JSON，判门同惯例），era/联赛分层。

**门=报告+用户点头**：数据对照过关后由用户裁决是否解除 had-only；
解除前引擎下注面不动（票 13 已钉死 had-only 不自动放宽）。解锁实施
（若裁决通过）另开小票接引擎 `_candidates_for_match`。

## 不变量与人裁决项

- 先报告后启用；本票不改动引擎下注面。
- 真实线语义按 gold 列定义（盘1/初终组/水位），不二次猜测。
- 代称红线：报告/代码/输出一律 cid+分层角色代称。
- 人裁决项：解锁与否、解锁后先开哪个玩法（hhad 或 ttg 或同时）。

## Out of scope

- 引擎 bet-face 改造（裁决后另票）；OU 1h/24h 切片（票 06 留白，
  如报告需要先补 gold 列）。

## Answer

2026-10-10 交付（分支 feat/unlock-report-c15-20261010，PR #136）：

- **代码**：`evaluation/unlock_report.py`（市场侧=真实线水位双向去水 vs
  反推侧=era 正典 1X2 反推矩阵同线概率，分 era×league Brier/MAE 对照 +
  ttg 桶复检；报告落 reports/ 双视图）；era 公允链抽 `era_fair_probs`
  复用（单一落点）；gold_reader 增报告面取数（11 联赛口径+sid 去重）；
  CLI `unlock-report`。ScoreMatrix 新增 `cover_prob`（半步长线赢盘概率，
  hhad 仅整数线的限制已 docstring 明示）。
- **correctness 五修**：hhad 模型侧 hhad() int() 截断 bug（半线让球质量
  错记平局格）/OU 整数线 push 面/联赛范围（杯赛挪超 17.5% 剔除）/OU
  线≥7 拒收/sid 去重——各配回归测试。
- **真树结论**（40,049 场）：OU 面反推 Brier 0.2587 vs 真实线 0.2500
  （两 era 一致实质差）；AH 面 0.2502 vs 0.2498（反推仅略逊）；ttg 桶
  3-5 反推隐含低于实际 2.0~3.0pp（v1 欠分散十年复现）。
- **判读建议（留用户裁决）**：ttg 解锁证据最强（真实 OU 线校准最优 +
  反推欠分散同现）；hhad 次之（反推可用但真实线更优）。裁决通过后另
  开小票接引擎 `_candidates_for_match`。had-only 维持不动。

## Comments

- 2026-10-10 用户裁决：**解锁 ttg + hhad（按报告建议）**——ttg 证据最强，
  hhad 一并解；实施票 23（真实线市场侧接线引擎，market-λ 联合反推通道）。
