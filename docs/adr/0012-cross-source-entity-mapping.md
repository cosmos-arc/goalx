# ADR-0012: 跨源实体映射——确定性键物化（定则 1 转正）

日期：2026-09-27
状态：已接受（票 77 落地；修订 ADR-0010 §"映射走确定性键（定则 1）"的雏形表述）

## 背景

多源并存（源T 语料、竞彩官方、fdhist/Understat/538 训练域、clubelo）后，场次 join
此前是散装的：CLV 收盘锚在查询期用中文队名+北京日期即时模糊匹配（票 75 两步法）；
jc 台账 kickoff 与运行面 fixtures 不通约；clubelo 英文名映射挂账；Understat/538 各自
一次性 alias join。用户裁决（2026-09-27）："大量交叉源必须有机制互相映射比赛、球队、
可能球员，不然无法准确 join；kickoff 统一走源T"。映射错配会污染对账/CLV/结算全链。

## 决策

1. **物化层单一住所**：`data/mapping.py` + `source_match_links`（v23）——
   fixture_id × source 一行，`method` 记命中路径（primary_key / kickoff_exact /
   manual），`status`=linked/ambiguous，候选与漂移档案进 `meta` JSON。消费端
   （evaluation/ingest）只读该层入口（`srct_sid_for_fixture` 等），不自行重写匹配。
2. **两步确定性键 + 变体组，禁自信合并**：① 主客中文队名+北京日期精确；② 兜底
   kickoff（北京 naive）精确+主队名精确。变体组形态把 canonical 名与
   team_aliases(srct/manual) 别名一并参与（真树点测发现竞彩/源T 系统性命名变体如
   「赫塔费/赫塔菲」，单名形态结构性卡死）——仍是精确串匹配，命中路径记 provenance。
   唯一命中才落链；多候选落 ambiguous 进人工队列（`manual` 覆写）；零命中不落行。
   manual 链永不被自动同步覆写；不再解析的链撤除（镜像真值）。
3. **kickoff canonical=源T**：已链场次（含 manual）的 fixtures.kickoff_utc 向
   fixture_universe 对齐——≤12h 漂移自动校准并留前值档案，超限/撞唯一键只标记进
   人工队列。jc 停售时刻仍 JC 官方独供，不在此列。
4. **别名属主红线**：team_aliases 表归 modelling（ADR-0008）；映射层只产出
   (team_id, 源T队名) 对，由编排层（tasks）交 `team_align` 落库；别名组注入同理
   （data 不直查 modelling 表）。clubelo 英文名经既有英文别名（odds_api/propline）
   三级解析桥接（source='clubelo'），未覆盖进人工队列不硬配。
5. **查询期兜底保留且语义不变**：票 75 的 SQL 两步法降为无链兜底（多命中取 sid
   首行，对账场景可接受）；物化层更严（多候选不硬配）。两引擎并存是接受的成本，
   漂移风险由两侧单测共同钉住。

## 后果

- CLV 收盘锚：先查物化链（sid 直查），无链走兜底——快路只增不减，基线语义不变。
- jc_sp_change_event 的 kickoff 冗余列注入式改走映射（corpus 树保持纯净，覆写由
  编排层组装注入，jc 台账兜底）。
- 球员映射 YAGNI 预留：源T pid 域内自洽，首个跨源消费方（LLM 信息层）启用另票。
- 审计起步门（tier1 未映射 <2%）report-only：门是排期信号不是 CI 红灯。
- 真树首跑（2026-09-27，195 场）：两轮 bootstrap 88→96 链、0 歧义、tier1 未映射
  11.9%→4.76%；余量=CorpusScope 外联赛（结构性）+一字变体（set-alias 人工补线）+
  语料窗口右缘（silver 重物化后自然补链）。
