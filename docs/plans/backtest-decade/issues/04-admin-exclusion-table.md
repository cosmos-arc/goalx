# 04 行政判赛排除表：训练集与对账集统一剔除"比分真、比赛假"场次

Status: resolved
Spec: `.scratch/backtest-decade/spec.md` A 组 User Story 4

## 任务

运行面显式表 `admin_match_exclusions`（league×队×日期窗×缘由，续添只
INSERT），fdhist 对账集（判门①）与 gold 训练行（B 相）两侧统一引用；
这些场行政判 3-0 从未真踢，训练与对账都剔除。可续添。

## 不变量与人裁决项

- 种子=四刀盘点钉死三处土超（真库 hist_matches 实测拼写/日期窗）：
  Hatayspor 与 Gaziantep 2023-02-06→2023-06-30（地震退赛赛季尾，窗内
  全行政 3-0 且 PSC 空——从未开赛）；Ad. Demirspor 2025-02-09 单场
  （中途离场弃赛判 3-0，PSC 在场——正是"比分真、比赛假"污染型）。
  邻近真 3-0 场（两队窗前窗后）不得误伤——表按 队+日期窗 而非比分驱动。
- 谓词语义：联赛精确 + 队任一侧 + 日期窗含端点；缺表 fail-closed
  （抛错，不静默当零排除）。
- 表归 data 域（ADR-0008）：迁移 v24 建表+种子，单一取数口
  `data/hygiene.py`（gold/C 相引擎接线都从这里引，不散写 SQL）。
- 引擎训练集/gold 行接线=票 14（C 相"引擎入口统一生效"），本票只落
  表+谓词+判门①对账集。
- 队名口径：表存 fdhist 侧拼写；语料侧（中文名）经既有名字身份映射，
  由 B 相接线时引用（如需 corpus 名行，续添 INSERT 即可）。

## Answer

2026-10-08 交付（分支 feat/hygiene-a4-a5-20261008）：

- 迁移 v24：`admin_match_exclusions(id, competition, team, date_start,
  date_end, reason, created_at, UNIQUE(competition,team,date_start,date_end))`
  + 三行种子（INSERT OR IGNORE 幂等）+ append-only 触发器
  （no_update/no_delete，同 draw_sync_runs 惯例——"续添只 INSERT"由
  约定升机械兜底；窗口写错走人工落库手术）。
- `data/hygiene.py`：`AdminExclusion` / `admin_exclusions(conn)`（行数
  全量、ORDER BY 稳定序）/ `is_admin_excluded(competition, home, away,
  match_date, exclusions)` 纯谓词；同模块承载 A5 PSC 成熟度谓词。
- 判门①接线：`build_phase1_gate_report` 在对账集入口统一剔除（运行面
  readonly 连接取排除行），剔除数进门①报告 `admin_excluded` 键+MD 单列；
  `_cmd_srct_gate` CLI 先 migrate 确保表在（缺表即炸）。
- 测试：谓词正反例（双侧命中/窗含端点/窗前窗后/他联赛不连坐）+ 迁移
  种子就位 + 缺表 fail-closed + append-only 触发器拦改删 + 合成树全链
  （缺口行入表 → admin_excluded=1、gaps 0、rate 1.0、verdict 翻 pass、
  分母同步缩）。
- 真树首次生效（详票 03）：admin_excluded=30，与真库行政假赛行数逐行
  一致（地震窗 29 + 阿达纳单场），零真赛误伤。
- correctness-review 加固：取数口校验窗口日期（零填充 YYYY-MM-DD+真实
  存在，坏窗即抛——字典序区间比较的前提，append-only 下纠错昂贵）；
  CLI srct-gate 对库文件缺失 fail-loud（不静默新建空库出"全
  insufficient"假报告）；被剔除配对的语料侧 fixture 落"语料多出"时 MD
  加注记（非真冗余）。
