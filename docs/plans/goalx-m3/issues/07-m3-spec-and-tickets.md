# 07 M3 spec 增补与拆票

Type: grilling
Status: resolved (2026-09-19)
Blocked by: 04, 05, 06

## Question

前序票定案后，收口成可开工交付：

1. **spec 增补**：spec v1.1 的暂缓条款改写（M3 启动记录用户 2026-09-19 裁决），评测协议与隔离方案入 spec；`docs/plans/goalx-quant/` 同步。
2. **拆票**：首批实施票（数据面/管线/融合/评测落库/UI 各几张），沿用一票一文件、不变量与人裁决项格式，ready-for-agent。
3. **验收口径**：每张票的验收与 task check 关系；票 37 运行不受扰的回归点。

抵达本票 = 地图抵达目的地。

## 不变量与人裁决项

- 拆票粒度与优先顺序由用户最终确认。

## Answer（2026-09-19 HITL 会话，三项全按推荐）

1. **拆票**：八张实施票 08-15 已发布（全部 ready-for-agent，含依赖链与不变量）：08 GLM 基座 / 09 情报与基本面采集 / 10 scout / 11 gate+analyst / 12 LEAP+ADR-0009 / 13 评测协议落库 / 14 API+存量 UI / 15 AG-UI 追问。
2. **spec 增补**：§9 已改写入工作副本 `.scratch/goalx-quant/spec.md`（暂缓条款→M3 启动记录+两档冻结阈值+真钱资格只认 ML）。
3. **转正时机**：docs/plans spec 修订 + goalx-m3 地图/研究/原型转正 + ADR-0009 随首个实施 PR（票 08）一起提交。
4. **开工顺序**：08+09 并行起步（互不触碰）；任何新 deployments 合入 main 后重启 serve，票 37 运行不受扰。

**地图抵达目的地**——M3 从此进入实施阶段。
