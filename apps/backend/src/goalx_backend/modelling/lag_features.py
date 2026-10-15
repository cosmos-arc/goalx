"""
滞后特征面（model-v2 票 01，spec S1）。

标签侧信息（xG/技统/比分）lag 化后才是可用的赛前特征——本模块把
gold match_features 行集（kickoff 定序）变成每场**赛前**滚动特征：

- 每队（(league, team) 键控）最近 N 场（缺省 10）滚动：xG for/against
  均值（只对有 xG 的场滚、场数不足留空）、射门/射正 for/against、
  场均净胜球、场均积分（胜 3 平 1）；
- 对照差（home−away）：以上各量 + Elo 差（elo_*_pre 本就赛前值）。

**严格 lag**：某场的特征只用 kickoff **严格早于**该场的同队比赛
（同日也不入——保守 PIT）。纯确定性函数（同输入同输出）；
不落 gold 树（每次 run 从 gold 行即时算，省物化层与口径漂移）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

LAG_WINDOW = 10  # 滚动窗场数
MIN_XG_SAMPLES = 3  # xG 滚动最少样本（不足留空不插补）


@dataclass(frozen=True)
class LagRow:
    """一场 gold 行的滞后特征消费面（构建器输入的最小投影）。"""

    sid: str
    league: str
    kickoff: str  # ISO 时刻或日（字符串比较即可——同源同格式定序）
    home: str
    away: str
    home_goals: int
    away_goals: int
    xg_home: float | None
    xg_away: float | None
    shots_home: int | None
    shots_away: int | None
    sot_home: int | None
    sot_away: int | None
    elo_home_pre: float | None
    elo_away_pre: float | None


def _window(maxlen: int) -> deque[float]:
    """定长滚动窗工厂。"""
    return deque(maxlen=maxlen)


def _team_window(window: int) -> _TeamWindow:
    """按窗宽构造一队滚动窗。"""
    return _TeamWindow(
        xg_for=_window(window),
        xg_against=_window(window),
        shots_for=_window(window),
        shots_against=_window(window),
        sot_for=_window(window),
        sot_against=_window(window),
        goal_diff=_window(window),
        points=_window(window),
    )


@dataclass
class _TeamWindow:
    """一队的滚动累计（定长队列，严格 lag 由构建顺序保证）。"""

    xg_for: deque[float]
    xg_against: deque[float]
    shots_for: deque[float]
    shots_against: deque[float]
    sot_for: deque[float]
    sot_against: deque[float]
    goal_diff: deque[float]
    points: deque[float]


def _mean(values: Sequence[float]) -> float | None:
    """非空均值（空→None）。"""
    return sum(values) / len(values) if values else None


def _mean_min(values: Sequence[float]) -> float | None:
    """XG 滚动均值（样本 <MIN_XG_SAMPLES 留空——票面"场数不足诚实留空"）。"""
    if len(values) < MIN_XG_SAMPLES:
        return None
    return _mean(values)


def _diff(home: float | None, away: float | None) -> float | None:
    """对照差（任一侧缺→None）。"""
    if home is None or away is None:
        return None
    return home - away


def _assert_sorted(rows: list[LagRow]) -> None:
    """定序断言（严格 lag 的前提——乱序输入 fail-loud 不静默泄漏）。"""
    for prev, cur in pairwise(rows):
        if cur.kickoff < prev.kickoff:
            raise ValueError(
                f"输入未按 kickoff 升序（{prev.sid}→{cur.sid}）——严格 lag 被破坏"
            )


def _points(home_goals: int, away_goals: int) -> tuple[float, float]:
    """主客积分（胜 3 平 1 负 0）。"""
    if home_goals > away_goals:
        return 3.0, 0.0
    if home_goals < away_goals:
        return 0.0, 3.0
    return 1.0, 1.0


def _push_pair(
    for_win: deque[float],
    against_win: deque[float],
    value: float | None,
    against: float | None,
) -> None:
    """计数对入窗（缺值场不进窗，不冒充 0）。"""
    if value is not None:
        for_win.append(value)
        against_win.append(0 if against is None else against)


def build_lag_features(
    rows: list[LagRow], *, window: int = LAG_WINDOW
) -> dict[str, dict[str, float | None]]:
    """
    Gold 行集（**kickoff 升序**，调用方保证）→ sid → 滞后特征 dict。

    严格 lag 的实现：按定序逐场处理——先快照当前滚动窗为该场特征，
    再把该场结果入窗。同 kickoff 并发场互不可见（快照先于入窗的
    顺序对同批场全部成立）。
    """
    windows: dict[tuple[str, str], _TeamWindow] = {}
    features: dict[str, dict[str, float | None]] = {}
    if window < 1:
        raise ValueError(f"window 须 ≥1（得到 {window}）")
    _assert_sorted(rows)

    def _snapshot(row: LagRow) -> dict[str, float | None]:
        win_h = _win(row.league, row.home)
        win_a = _win(row.league, row.away)
        return {
            "elo_diff": _diff(row.elo_home_pre, row.elo_away_pre),
            "xg_for_diff": _diff(_mean_min(win_h.xg_for), _mean_min(win_a.xg_for)),
            "xg_against_diff": _diff(
                _mean_min(win_h.xg_against), _mean_min(win_a.xg_against)
            ),
            "shots_for_diff": _diff(_mean(win_h.shots_for), _mean(win_a.shots_for)),
            "shots_against_diff": _diff(
                _mean(win_h.shots_against), _mean(win_a.shots_against)
            ),
            "sot_for_diff": _diff(_mean(win_h.sot_for), _mean(win_a.sot_for)),
            "sot_against_diff": _diff(
                _mean(win_h.sot_against), _mean(win_a.sot_against)
            ),
            "goal_diff_diff": _diff(_mean(win_h.goal_diff), _mean(win_a.goal_diff)),
            "points_diff": _diff(_mean(win_h.points), _mean(win_a.points)),
        }

    def _absorb(row: LagRow) -> None:
        """本场结果入窗（下一 kickoff 批次起可见）。"""
        win_h = _win(row.league, row.home)
        win_a = _win(row.league, row.away)
        h_pts, a_pts = _points(row.home_goals, row.away_goals)
        # xG 是配对观测：一侧缺则整场不进两侧 xG 窗（不拆对不插补）
        if row.xg_home is not None and row.xg_away is not None:
            win_h.xg_for.append(row.xg_home)
            win_h.xg_against.append(row.xg_away)
            win_a.xg_for.append(row.xg_away)
            win_a.xg_against.append(row.xg_home)
        _push_pair(win_h.shots_for, win_h.shots_against, row.shots_home, row.shots_away)
        _push_pair(win_a.shots_for, win_a.shots_against, row.shots_away, row.shots_home)
        _push_pair(win_h.sot_for, win_h.sot_against, row.sot_home, row.sot_away)
        _push_pair(win_a.sot_for, win_a.sot_against, row.sot_away, row.sot_home)
        win_h.goal_diff.append(row.home_goals - row.away_goals)
        win_a.goal_diff.append(row.away_goals - row.home_goals)
        win_h.points.append(h_pts)
        win_a.points.append(a_pts)

    def _win(league: str, team: str) -> _TeamWindow:
        return windows.setdefault((league, team), _team_window(window))

    # 同 kickoff **日** 批次：先全体快照（严格 lag——同日互不可见，
    # 票面保守 PIT 口径）再全体入窗
    batch: list[LagRow] = []
    batch_key: str | None = None
    for row in rows:
        day = row.kickoff[:10]
        if batch_key is not None and day != batch_key:
            for done in batch:
                _absorb(done)
            batch = []
        batch_key = day
        batch.append(row)
        features[row.sid] = _snapshot(row)
    return features


def lag_features_from_gold(duck_rows: list[tuple[Any, ...]]) -> list[LagRow]:
    """
    Gold match_features 取数行 → LagRow 集（kickoff 升序）。

    消费面 SQL 归 gold_reader（票 02 接线时扩展）；本函数只做投影，
    保持 lag_features 模块对 duckdb 零依赖（纯函数可测性）。
    """
    return [
        LagRow(
            sid=str(r[0]),
            league=str(r[1]),
            kickoff=str(r[2]),
            home=str(r[3]),
            away=str(r[4]),
            home_goals=int(r[5] or 0),
            away_goals=int(r[6] or 0),
            xg_home=float(r[7]) if r[7] is not None else None,
            xg_away=float(r[8]) if r[8] is not None else None,
            shots_home=int(r[9]) if r[9] is not None else None,
            shots_away=int(r[10]) if r[10] is not None else None,
            sot_home=int(r[11]) if r[11] is not None else None,
            sot_away=int(r[12]) if r[12] is not None else None,
            elo_home_pre=float(r[13]) if r[13] is not None else None,
            elo_away_pre=float(r[14]) if r[14] is not None else None,
        )
        for r in sorted(duck_rows, key=lambda r: (str(r[2]), str(r[0])))
    ]
