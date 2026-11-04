"""市场参考常量：选项网格与联赛分层（settlement 与 migrations 共用）。"""

from __future__ import annotations

# 竞彩比分（crs/goals4）精确比分集合（28 个，取自官方网关实际盘口）——
# 不在集合内的高比分落入「其他」档。
CRS_AWAY_RANGE: tuple[tuple[int, int], ...] = (
    (0, 5),
    (1, 5),
    (2, 5),
    (3, 3),
    (4, 2),
    (5, 2),
)
CRS_EXACT_SCORES: tuple[tuple[int, int], ...] = tuple(
    (home, away) for home, max_away in CRS_AWAY_RANGE for away in range(max_away + 1)
)

# 胜平负三向选项编码（全仓唯一出处；各模块从这里导入）
SELECTIONS: tuple[str, ...] = ("h", "d", "a")

# 进球类玩法选项网格（票 wb-04，与 sporttery 解析/矩阵视图同键）：
# ttg 八档 0..7+（"7" 归并尾部）；crs 28 精确比分 + 胜/平/负"其他"三档。
# 顺序即官方展示顺序（视图与页面渲染共用；矩阵 crs() 的键序与此一致）。
TTG_SELECTIONS: tuple[str, ...] = tuple(str(n) for n in range(8))
CRS_OTHER_SELECTIONS: tuple[str, ...] = ("h_other", "d_other", "a_other")
CRS_SELECTIONS: tuple[str, ...] = (
    tuple(f"{home}:{away}" for home, away in CRS_EXACT_SCORES) + CRS_OTHER_SELECTIONS
)

# 进球类玩法（MarketGroup 进球类；半全场暂不呈现，spec 工作台 v2）
GOALS_MARKETS: tuple[str, ...] = ("ttg", "crs")


# goals4（四场进球）档位码闭集（票 10 单源）：每场主/客各 0/1/2/3+ 档，
# 3+ 聚尾。betting 构票校验与 settlement 档位判定共用——两处各自硬写会漂。
GOALS4_CODES = frozenset(f"{side}{band}" for side in ("h", "a") for band in range(4))

# 任9/14场 官方池码闭集（票 08 单源，与 GOALS4_CODES 同住所）：3=胜
# 1=平 0=负——betting 池票载荷守卫消费，两处硬写会漂。
POOL_HAD_CODES = frozenset({"3", "1", "0"})
