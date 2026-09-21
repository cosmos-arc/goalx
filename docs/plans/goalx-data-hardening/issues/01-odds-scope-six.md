# 欧赔范围扩六联赛

Type: impl
Status: resolved
Blocked by: none

## 问题

协议 v1 冻结欧赔范围为英超+意甲，但 DC 模型覆盖六联赛（E0/SP1/I1/D1/F1/N1）——其余四联赛场次有模型概率、无欧赔共识对照，EV/CLV 诊断对它们是盲的。

## 范围

1. The Odds API sport key 名单核实（/sports 不计 credit）
2. `.env` GOALX_ODDS_API_SPORT_SCOPE 2→6 keys
3. run-protocol-v1.md 记 v1.1 修订（日期+理由：覆盖扩展非结果驱动）
4. RUNBOOK 数据源表更新范围说明
5. 实跑一次 ingest-odds 验证 credits=6、六联赛 join

## 不做

- 不改 credit 护栏值（40/日 480/月 保持，实耗远低于此）
