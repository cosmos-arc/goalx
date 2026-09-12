# 纸面与真金统一为单一 Bet 实体（mode: paper | live）

存在两种记录需求：纸面跟踪（验证期）与真金回录（实盘）。决定用同一个 Bet 实体加 mode 字段，而非两个实体：票 12 要求回测、纸面、实盘走同一套 Settlement 与复盘统计代码，分离实体会造成口径漂移。Bankroll 与 CostLedger 只受 live 模式影响；CLV、yield、RPS 等指标对两种模式统一计算、分组呈现。
