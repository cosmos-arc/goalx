"""
语料域纯静态常量：CorpusScope 中文联赛 ↔ fdhist 联赛码 + gold era 断点。

零仓库内依赖的叶子模块。定义唯一落点在此；``corpus_gate``（门①映射
单一真相源）与 ``gold``（era 常量唯一落点）再导出保持各自公共面不变。
下层消费面（evaluation 等，importlinter 分层在 data.ingest 之下）经本
模块引——corpus_gate/gold 传递依赖 data.ingest，对下层不可达。
"""

from __future__ import annotations

# CorpusScope 中文联赛 ↔ fdhist 联赛码（ADR-0010：15 项中 11 项重叠，
# 门①场次对账的重叠联赛全集）
LEAGUE_TO_FD: dict[str, str] = {
    "英超": "E0",
    "西甲": "SP1",
    "德甲": "D1",
    "意甲": "I1",
    "法甲": "F1",
    "英冠": "E1",
    "荷甲": "N1",
    "葡超": "P1",
    "土超": "T1",
    "比甲": "B1",
    "苏超": "SC0",
}
FD_TO_LEAGUE = {code: name for name, code in LEAGUE_TO_FD.items()}

# gold 时代分层断点（backtest-decade spec 实现决策：集中一处，下游只读
# era 字段）。断点 = 1x2 轨迹起点季（实测 2023-24 起步 115 场/2024-25
# 全季 4,491 场）；含该季起 = trajectory 时代。
ERA_TRAJECTORY_SEASON = "2023-24"
ERA_PSC_PROXY = "psc_proxy"
ERA_TRAJECTORY = "trajectory"
