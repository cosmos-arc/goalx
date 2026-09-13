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
