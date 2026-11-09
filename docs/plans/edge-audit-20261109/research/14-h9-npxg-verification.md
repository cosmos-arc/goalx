# 票 14 · H9 npxG 特征读数路径查证（2026-11-09）

结论：**属实**——外部审计主张成立：H9 报告（`docs/plans/edge-program-20261107/reports/02-h9-model-families.md`）所称的「understat npxG 双队滚动速率」特征，实际读取的是**未剥点球的 xG**。H9 判决词（refuted）本身不动；本文件产出范围收窄注记稿供票 08 人裁回写。

## 证据链（代码级，固定 d5aa7b7 工作树 = 032324c 后继）

1. **报告脚本读数列**：`02-h9-model-families.py:55` 从 `match_features` 取 `xg_home, xg_away`（非任何 npxg 列）；`build_npxg_proxy`（:137-152）用它构建滚动速率，变量名/报告措辞称 npxG。
2. **gold 装配取数**：`apps/backend/src/goalx_backend/data/gold.py:642` `_fetch_understat` SQL 为 `SELECT ..., xg_home, xg_away FROM goalx.understat_matches`——读的是 xg 列，**不是同表并存的 npxg_home/npxg_away**；`_assemble_row`（:816-821）主源 understat、缺则回退 srct `xg_observation`。
3. **ingest 口径分离已在库**：`apps/backend/src/goalx_backend/data/ingest/understat.py:69-72` UnderstatMatch 字段注释明确：`xg_home` =「dates 口径（**含点球 xG**）」、`npxg_home` =「teams.history 口径（**去点球**）」；两列并存于 `understat_matches` 表（migrations.py:1028-1031）。即数据层早已区分，读数层拿错列。
4. **match_features 无 npxg 列**：corpus.duckdb `describe match_features` 实查，xG 族仅 `xg_home, xg_away, xg_source`——研究侧即便想读 npxG，现 gold 面也不可得（须改 gold 装配）。
5. **混合源**：H9 宇宙（2019-08…2026-07、avgc+FD OU 在场）12,169 场带 xG 值 = understat 9,984 + **srct 2,185**（后者为源T 单源口径 xG，仓内无去点球主张；改读 npxG 时这些行的语义另行裁）。

## 实测量化（corpus.duckdb 只读 + goalx.db 只读 ATTACH，2026-11-09）

`understat_matches` 已完场且 xg/npxg 双列齐备、H9 时间窗内（2019-08-01…2026-08-01）：

| 指标 | 值 |
|---|---|
| n | 12,459 |
| xg ≠ npxg 的场次（任一侧） | 3,409（**27.4%**） |
| 场均差 xg−npxg（主/客） | +0.137 / +0.101 |
| 场均差（总量） | +0.238 |
| 最大差（总量） | +3.197 |
| npxg 列缺失 | 0（改读对列零覆盖损失） |

点球 xG 并非纯常数偏移（各队获点频率随队/时间漂移），进入 12 场滚动均值后既加噪也改变排序——被测特征与「真 npxG 滚动速率」不是同一个特征。

## 对 H9 读数的影响（判读，不改判决词）

- H9 的 refuted 判决对**实际被测对象**（含点球 xG 滚动速率特征 vs 矩阵 ttg 视图，配对 Brier z=−0.39）依然成立、如实留痕。
- 但「understat npxG（去点球）双队滚动速率无增益」**未被检验过**——报告 §设计 与 §读数 的「npxG」措辞与实读列不符。排除范围应收窄到「penalty-inclusive xG 滚动速率特征」；真 npxG 特征仍属未测空间（与外部审计「只排除了一个较窄候选」的判读一致）。
- 分档校准读数（「npxG 特征没有修任何矩阵校准缺口」）同受此措辞影响：结论主体（三面统计不可分）不动，特征名词需改注。

## 范围收窄注记稿（供票 08 人裁后 append-only 回写账本 H9 行）

> H9 注记（2026-11-09 edge-audit 票 14 查证）：票 02 报告所称「understat npxG 双队滚动速率」特征实读 `match_features.xg_home/xg_away` = understat dates 口径**含点球 xG**（gold.py `_fetch_understat` 取 xg 列而非同表 npxg 列；H9 窗内 27.4% 场次 xg≠npxg，场均总量差 +0.238），且 2024/25 后混入源T 口径 xG（2,185/12,169 场）。refuted 判决维持，但排除范围收窄为「penalty-inclusive xG 滚动速率特征不优于矩阵 ttg 视图」；真 npxG（去点球）滚动特征未测，不在本判决排除范围内。若后续复验：gold 装配改读 npxg 列（窗内零覆盖损失）+ 源T 回退行单独裁口径。

## 附：验证脚本（复现用）

```python
# corpus.duckdb 只读 + 业务库只读 ATTACH（业务库路径按运行面实配）
import duckdb
con = duckdb.connect("/Users/chevy/goalx-data/duckdb/corpus.duckdb", read_only=True)
con.execute("ATTACH '<运行面>/data/goalx.db' AS goalx (READ_ONLY)")
con.sql("""
select count(*) n,
       count(*) filter (where abs(xg_home - npxg_home) > 1e-9
                        or abs(xg_away - npxg_away) > 1e-9) differs
from goalx.understat_matches
where is_result = 1 and xg_home is not null and xg_away is not null
  and cast(datetime_utc as timestamp) >= timestamp '2019-08-01'
  and cast(datetime_utc as timestamp) < timestamp '2026-08-01'
""").fetchall()  # → (12459, 3409)
```
