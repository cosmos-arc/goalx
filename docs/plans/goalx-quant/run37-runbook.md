# 票 37 运行手册(协议 v1)

前置(已完成,2026-09-13):主库迁移 v6;`GOALX_ODDS_API_SPORT_SCOPE=soccer_epl,soccer_italy_serie_a` 已写入 .env;范围冻结代码待 PR 合入后常驻。

## 调度(已启动,PR #13)

2026-09-13 21:40 起常驻(用户授权):`nohup task serve-schedules >> .scratch/prefect-serve.log 2>&1 &`

- daily-capture 10:00/19:00(竞彩→预测→范围内欧赔)
- eu-odds-closing 每 30 分钟(无窗口零成本)
- daily-wrap 23:30(结算+CLV 对账+只读核查)

注意:Mac 睡眠会暂停进程,漏跑窗口如实记入报告分母;长期无人值守时改 `caffeinate -s task serve-schedules` 或 launchd。检查:`pgrep -f goalx_backend.schedules`、`tail .scratch/prefect-serve.log`、Prefect UI(auto)。

## 手动兜底(调度失效时按此补跑)

```bash
task ingest-jingcai && uv run --no-sync python -m goalx_backend.cli forecast && task ingest-odds
uv run --no-sync python -m goalx_backend.cli closing-snapshot
```

## 纸面闭环(浏览器,`task server` + `task dev`)

1. 今日页:确认范围内场次资格(可投/拒绝原因/源调盘时点);无候选就如实记录,不建策略注。
2. 有候选:单关/2串1建建议(strategy_version 填 `diagnostic-paper-v0`)→ 锁定纸面。
3. 赛果公布:投注页按比赛录入(官方口径),已有结果改动必须填更正原因;可先「影响预览」。
4. 结算批跑 → 复盘(状态/盈亏/缺 closing)。

## 每日收尾

```bash
uv run --no-sync python -m goalx_backend.cli clv-reconcile   # closing 对账
uv run --no-sync python -m goalx_backend.cli audit-ledger     # 只读核查(无依据不改账)
```

把当日数字填进 run37-report.md 的日表;人工介入(结果核验、异常处理)记分钟数。

## 分母口径速查(SQL)

```sql
-- 候选总体/有效纳入(当日业务日)
SELECT match_code, json FROM ... -- 直接用今日页/GET /api/v1/fixtures/today(含 had_quote 判定与原因)
-- 窗口完成率:应跑次数(§3 节奏) vs quote_observations 实到
SELECT source, date(observed_at), COUNT(*) FROM quote_observations GROUP BY 1,2;
-- credits/金额
SELECT date(occurred_at), SUM(units), SUM(amount_cny) FROM cost_ledger GROUP BY 1;
-- closing 覆盖(对账后)
SELECT COUNT(*) FROM clv_records; SELECT COUNT(*) FROM odds_snapshots WHERE purpose='closing';
-- 纸面闭环 0 真钱
SELECT COUNT(*) FROM bankroll_events;  -- 验收期内应为 0(除非真实回录,须单独分组)
```

## 故障与诚实规则

- 漏跑/失败:在报告日表记原因,不补造;中断修复后重新累计合格窗口,旧失败证据保留。
- 无比赛日:不计入 3 个合格销售日。
- 预算日尽:当日欧赔记受阻,等 UTC 日切(北京 08:00)恢复,不抬限额。
- 无信号/覆盖不足/额度不足是三种不同的「不通过」,分开报告。
