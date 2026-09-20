# ADR-0009: LLM 线三项概率与融合层级

日期：2026-09-20
状态：已接受（票 12）

## 背景

ADR-0006 定比分矩阵为 canonical 概率表示。M3 引入 LLM 线（scout/analyst，
票 10/11）后，需要决定 LLM 轨与融合轨的概率表示层级：LLM 直接产出 25 格
比分分布，还是只出胜/平/负三项；LEAP 融合（票 03 定案 log-pool）在什么
层级进行。

## 决策

1. **LLM 线只产出三项概率**（H/D/A）。LLM 直接出比分分布噪声大、token
   成本高，且当前全部下游消费者（彩池 EV、验证 RPS/ECE、融合、复核路由）
   都在三项层。
2. **ADR-0006 的 canonical 地位限定 ML 轨**：ML 轨 payload 保持比分矩阵，
   三项是矩阵的投影（`ScoreMatrix.had()`）；llm/fused 轨 payload 直接
   携带三项。读取侧（`llm.gate.latest_triple`）对 ml 轨经矩阵推边际，
   对 llm 轨直读——表示差异不泄漏到消费层。
3. **融合（LEAP log-pool）在三项层进行**：`p_fused ∝ p_ml^w × p_llm^(1-w)`
   后归一，w（ML 权重）缺省 0.5，可配置。产物是独立工件
   `forecasts(track='fused')`，payload 引用双源 forecast id；绝不覆写
   ml/llm 行；融合线不参与建注（票 04 冻结，证明前真钱资格只认 ML 轨）。

## 后果

- llm/fused 轨**不提供** crs/ttg 等矩阵推导玩法——这些玩法继续仅由 ML
  轨的矩阵支撑（推导链不动）。
- 每轨三项可同层对照（验证报告三列，票 13），JS 散度路由（票 11）与
  融合共用同一读取口径。
- 若未来 LLM 线需要比分级输出（如比分玩法证据），须新 ADR 推翻本决策。
