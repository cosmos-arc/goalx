# 15: FD 断点隔离 + 锚供给盘点

Parent: 票 10（伞）。同分支同 PR（纯结论文档随车）。

**What to build:** FD Pinnacle 断点（2025-07-23 官方警示）两子问题结论——回看：受影响读数清单（涉 Pinnacle 列与 Avg/Max 成分变化的全部账本条目×断点前后×影响面）与成分注记方案；前瞻：全部 Pinnacle 锚供给路径盘点（fdhist/源T/odds_api/propline，含覆盖、断点影响、可用性）与六步门③主锚前向定义建议。已知起点：生产 CLV 锚走源T 收盘回放不经 fdhist，运行面无火警。

**Blocked by:** None (can start immediately)

**Status:** resolved（2026-11-09）

- [x] 受影响读数清单逐条落 `research/`（账本条目×窗口×影响面×处置建议）
- [x] 供给路径盘点表（路径×覆盖×断点影响×可用性）
- [x] 六步门③前向锚定义建议 ≥2 选项含利弊，供票 08 人裁
- [x] FD 警示原文逐字引用入档（重新取一次官方页，含取文日期）

## 不变量与人裁决项

- 只登记不执行：换轨/装载/新源采购全是后续票的事，本票零代码变更（除文档）。
- 生产 CLV 锚（源T 收盘回放）本票不动。
- E-02/E-07 等受影响条目的判决词处置留票 03/08，本票只出清单。

## Answer

全部产出在 `research/15-fd-pinnacle-breakpoint.md`（单文件四章）。要点：

1. **FD 警示逐字入档**（取文 2026-11-09）：断点 2025-07-23，Pinnacle pre-closing/closing 双陈旧 + 已从 Avg/Max 计算剔除。十一联赛全为秋春制 → **断点落在 2425/2526 休赛期，隔离按整季剔除即可**（无季内劈裂）；自有语料指纹=2526 PSC 缺 53%、2627 全缺、AvgC 全量（票 51/54 的「换源季」观察由此归因）。
2. **受影响读数清单**：37 条逐条排查，受影响 6 处——需隔离重算 4（E-02 逐季尾 −2.3% 恰为 2526 全季；E-07 嫌疑收窄为 F7/F8 全窗落账+had 结构面+Pin 松/紧切片（psc−avgc 双列受害），**F1-F6 确认窗 25/26 段不成立**（0 候选→零消耗）且搜索窗点估计全在断点前；E-06 显式含 2526 段（n=360 唯一转负季）；E-22 psc 窗含 2526+2627 avgc 分期）；注记级 2（E-20 配对自控、E-23/24 avgc 副锚 n=14）。**重要阴性发现**：E-17/E-18 被 era 分界（psc_proxy 2016-2023 | trajectory 2023/24+）结构性保护；E-03/05/08/09/11/13/14/16 的 PIT/桶/滞后锚全走 srct:1x2:177 轨迹，与 FD 零交集。E-07 F8 与票 10 缺陷 1（ttg 7+）必须同车重算。
3. **供给盘点**：fdhist（Pinnacle 列前向死、Avg 列可续但成分断层）/ 源T srct（生产现役唯一健康通路，2627 入库待观察；AH/OU 面 Pinnacle 真空是既有缺口）/ odds_api（2026-09-25 判死，仅历史存量）/ propline（现役互备，含 Pinnacle 与否待验）。**生产 CLV 锚「不经 fdhist」以代码链坐实**：`frozen.py:373` → `tasks.py:1277-1291`（`_srct_close_provider`）→ `clv.py:106-129`（`srct_closing_prob`）→ `quote_evidence.py:1361`（odds_change_event srct:1x2:177，published_at≤kickoff）——运行面无火警成立。
4. **六步门③前向锚建议（3 选项供票 08 人裁）**：A=单锚 srct_pinnacle 现状化（零成本但单源脆弱+副锚缺位）；B=双源主锚+断点哨兵（复用 corpus_gate PSC×cid177 对账改 srct×propline，断点可检测）；C=分段口径（历史 ≤2425 维持 fdhist PSC、前向 2526+ 一律 srct_pinnacle+账本断点注记）。建议组合=C 立即生效（成本≈0）+B 开后续票；E-29 副锚「欧共识收盘」前向无供给，随 B 重裁。

零代码变更；分支与集成分支同基 d5aa7b7 无 commit（.scratch 不进 git）。
