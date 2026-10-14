# 04 N1 NewsShock P0 抽取器：卫报语料→事件 silver（有界试点）

Status: ready-for-agent
Spec: `.scratch/model-iterate/spec.md` S4
Blocked by: （无——与 K 相并行）

## 任务

- **抽取管线** `data/ingest/guardian_silver.py`（silver 变换先例
  srct_silver）：bronze news_article（214,043 篇）→ silver
  `guardian/news_event`（schema：sid/article_sid、league/season、
  fixture 链接（match_key）、事件类型 injured/suspended/manager_change/
  rotation_hint/other、队伍侧（home/away）、置信度、抽取模型与版本、
  _meta 版本戳+输入 digest 钉死，幂等重建）；
- **实体链接**：文章标题+正文 → 联赛/球队 → fixture（mapping 变体组
  两步法，ADR-0012 先例）；无链文章如实计数不硬配；日期窗=文章发布
  日 ±3 天内 fixture 候选；
- **LLM 抽取**：`llm/client.py` scout 档（glm-4.7-flash 零牌价，
  按牌价记账参与熔断），约束输出 schema；PIT 断言逐行
  webPublicationDate < kickoff；
- **有界试点范围**：英超 2023-24+2024-25（约 3-6K 篇候选，先队名
  预筛再 LLM）；全量回填不进本票（门后另裁）。

## 不变量与人裁决项

- 幂等：重跑零重复（article_sid 去重）；无前视：事件行只附发布时点
  早于开球的 fixture；解析器版本制（BRONZE/PARSER 版本 bump 惯例）；
- LLM 输出 schema 违例 fail-loud 计数（不静默丢）；成本账经
  cost_ledger，熔断触发即停（断点续跑）；
- 代称红线：guardian 语料本体除外（silver 同例），代码/输出代称；
- 人裁决项：试点信号成立后是否全量回填（N2 报告后门）。

## Out of scope

- 事件信号验证（N2）；全量回填；P1 赔率移动预测线。


## Answer

2026-10-14 交付（分支 feat/newsshock-pilot-n1-20261014，PR #148，两提交，
合 main dd33d69）：

- **模块** `data/ingest/guardian_silver.py` + CLI `guardian-news-pilot`：
  英文链接（hist_matches 真比分 × match_fixtures v3）/ 时刻窗候选预筛
  （词边界+同文同场去重）/ LLM scout 抽取（strict schema+空响应分账
  +温度重试+[1301] 计数跳过+断点续跑 checkpoint）/ silver 落盘幂等。
- **真树试点（英超 2023-24+2024-25）**：202,182 篇扫描 / 2,951 候选 /
  759/760 链接 / 400 调用 → **67 篇相关 / 124 事件**；空 72/违例 14/
  过滤 1 分账；PIT 零违例。
- **review 战果**：双轴抓到两个真 bug——0-0 假比分喂配对器致链接
  51/760（真比分后 759/760）；PIT 候选/终检口径不一（日粒度终检误杀
  同日稿，40 分钟抽取白烧一次）→ 统一真实开球时刻贯穿。断点续跑/
  空响应分账/limit 显式截断（全量 2,951 留 N2 信号定向后另裁）。
- 人裁决项（试点信号成立是否全量回填）→ N2 报告后备料。
