# goalx 全项目两轴评审报告（2026-09-28）

> 基线 HEAD = a35338f。两轴并行子代理评审（Standards / Spec）+ 主线逐条复验
> + 规格文档链同步审计。票板与落地台账见 [map.md](map.md)（PR #94–#106 全合 main）。

## Standards 轴（后端 + web）

**硬违规（2 组，均已修）：**

1. **ADR-0008 跨包裸 SQL + 守护测试盲区**：`betting/ledger_audit.py` 用
   f-string 表名直读 data 域 `draw_result_revisions`；表名藏在字符串元组里，
   `test_sql_ownership` 的字面量提取天生抓不到（→ PR #95：归权走
   data 包函数 + 新增 `test_table_names_stay_literal` 红灯测试封插值形态）。
2. **交付层过厚**：`api/pool.py` 内嵌 ~280 行搏冷策略（冷门变体贪心 + 目标
   金额反推风险档/注数数学）（→ PR #96：下沉 `betting/pool_strategy.py`）。

**判断性 smell（批一/批二全部收敛，PR #97–#100/#104/#106）：** models.py
11 死类、死流包装、共识→Shin 装配双份、`_triple` 双胞胎、fixture-research
第三份选注篮、web 死导出/死 mock/重复家族（toast/localTime/dayNoteOf/
IntelLine/kickoffMs/parlayAdviceInput/earliestKickoff+openBetFixtureIds/
BasketFormFields/StatusToast）、handlers 零类型锚点。

**干净项**：orjson 零复燃；限流全走共享闸门；四条 web gotcha 全部正确落地。

## Spec 轴

**规格文档链同步审计**（评审的可信度前提）：spec-v1.0 停在 9-13（无取代声明）、
glossary 停在 9-20 落后 staking 裁决 → PR #94 补取代指针 + 口径同步 +
pool.py 份额来源更正。据此**重新归因**：

- staking 单注上限「矛盾」→ 文档滞后（代码对新裁决），非代码错
- 设置页/真金纪律 → 有记录缓议（M4 / spec.md:70），不开票
- **唯一真代码缺口**：`review_errors` 门槛硬编码恒不通过（validation.py
  从不读 verdict_counts）→ PR #101 落地裁决
- 口径空白：market_skill 多 model_version 聚合无规定，代码取 max 宽松读法
  → PR #101 落地裁决「只认部署版本」

**两裁决（用户采纳推荐，2026-09-28）：**

- review_errors = 误导率 ≤10%（verdict=misleading 占比）且 done ≥10 条；
  无记录/样本不足恒不通过。counts 由 api 层注入（层级契约：evaluation 不引 llm）。
- market_skill = 只认当前部署 model_version（最新 issued_at）；周重训后样本
  窗口重置属诚实降级，历史好版本不作数。两裁均回写 glossary。

**复验修正（评审自身的纠错）**：撤回「handlers pool-states 契约缺路径」
（v1.json 实有）；收窄「feedOrder 无人用」为字段级 nit；子代理两处计数微修。

## 口径对账（已逐项验证一致）

skill/RPS/ECE/DM、haircut（1−竞彩/公允、n<30→10%、O_sim=fair×(1−h)）、
CLV 锚序（主锚书→交易所 2% 佣金→共识，软书永不作锚，DecisionKey 去重）、
pool EV 65% 返奖率、had_quote 300s、void 腿 1/单关退款。

## 流程教训（三条，均已成为本批的防再发机制）

1. 本地定向 ruff 不含 lint-imports 分层契约——后端改动收尾必须全量
   `task lint`（#98 曾因 odds_math→markets 越层红）。
2. 端点 docstring/契约改动必须 export + codegen 两 diff 同提交
   （#101 漏 codegen 致 main CI 红三连，#105 补修）。
3. merge 前 watch 显式确认无 fail 且 checks 已起跑——`--watch | grep -c pass`
   空转 ≠ 绿（#104 曾带空 CI 合入，靠 main 合并后 CI 兜底发现）。
