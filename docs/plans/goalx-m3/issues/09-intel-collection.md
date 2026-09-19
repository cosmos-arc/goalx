# 09 情报与基本面采集（存证表+采集器）

Status: ready-for-agent
Blocked by: （无；GLM web-search 适配器在 08 合入后同票补）

## 目标

`intel_observations` 存证表（M3 唯一新表，票 04 定案）+ 免费采集组合（票 03 裁决 B）。

- 表：append-only，列含 fixture_id/kind/text/source_url/collected_at/collector/raw_payload/raw_hash——对齐 quote_observations 的 ADR-0001 模式；归 `llm/` 包自持 SQL
- okooo 赛事页伤停栏直爬 ingester（复用现有 ingester+observations 落盘模式；站点清单先做源B，其余站调研后增量）
- football-data.org 免费档：12 项赛事 standings+form（10 次/分限速，key 注册免费）
- 内部推导：fdhist 2627 近况/H2H 推导函数（零外部请求）
- 调度：挂每日 forecast 后 + 彩池同步后增量（新增 deployment，合入 main 后重启 serve）
- 迁移：drop 空脚手架表 `match_intels`（票 03 废弃定案）
- GLM web-search 适配器：留接口+stub，08 合入后实装（土超/日职等无结构化场次兜底）

## 验收

task check；采集幂等（重复跑零重复行）；raw 哈希可验证；无情报=零行不是假行。

## 不变量与人裁决项

- append-only：不改不删；原始响应落 observations 目录存证。
- 诚实边界：采集不到就零行，禁止占位/推测数据。
