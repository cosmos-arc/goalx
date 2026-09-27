"""
自算俱乐部 Elo（票 78）：赛果序列 → 逐场赛前 Elo，纯函数层。

clubelo 外采关闭（API 服务端死亡，票 74 终局）后的唯一 Elo 通路：输入
（赛果语料）全部自有——零外部依赖、PIT 天然正确（赛前值只由更早的比赛
决定）、任意历史日期可回溯；且在自己的队名空间计算，顺带消灭
"clubelo 英文名↔中文队名映射"消费侧挂账（票 74 备注）。

参数 v1（clubelo 同族，文献值起步；校准后置不阻塞 v1）：

- base/newcomer = 1500（首见队与回归队同值，快速收敛靠 K 缩放）；
- K = 20 × (1 + ln(1 + |净胜球|))（净胜球放大更新幅度）；
- HFA = +80（主队加成，进期望分）。

热身与双名空间（fd 英文 hist ↔ 源T 中文语料）：fd 十一联赛 2016/17 起
（比 CorpusScope 少欧战/瑞超/挪超）先在英文名空间自热身；era 边界
（语料首场）经重叠期确定性配对桥（``build_name_bridge``：联赛+日+比分
双侧唯一 → 队名对投票，一致多数才映射）把评级搬运进中文名空间。
无映射队/其余联赛从语料起点自热身并接受首季贬值（票面裁决）。

1X2 概率（报告用近似，非投注模型）：Davidson 族拆分
P(draw)=ν·√(E·(1−E))，ν=0.55；校准后置。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date

BASE_RATING = 1500.0
K_BASE = 20.0
HFA = 80.0
DRAW_NU = 0.55  # 1X2 报告近似的平局宽度（Davidson 族）
ELO_VERSION = "elo_algo_v1"


@dataclass(frozen=True)
class EloParams:
    """v1 参数束（版本戳随行落 silver，参数变更须递增 ELO_VERSION）。"""

    base: float = BASE_RATING
    k_base: float = K_BASE
    hfa: float = HFA
    draw_nu: float = DRAW_NU


DEFAULT_PARAMS = EloParams()


@dataclass(frozen=True)
class EloMatch:
    """一场已赛完的比赛（Elo 输入单元；seq=sid 等稳定并列键，热身行空）。"""

    home: str
    away: str
    home_goals: int
    away_goals: int
    kickoff: date  # 日粒度（fd 侧只有日期；同日内并列由 (home,away,seq) 决）
    seq: str = ""


@dataclass(frozen=True)
class EloPre:
    """一场比赛的赛前双方 Elo（PIT：不含本场及之后的任何信息）。"""

    home: str
    away: str
    kickoff: date
    seq: str
    elo_home_pre: float
    elo_away_pre: float


def expected_score(elo_home: float, elo_away: float, hfa: float = HFA) -> float:
    """主队期望得分（0..1；HFA 进主队侧）。"""
    return 1.0 / (1.0 + 10.0 ** (-(elo_home + hfa - elo_away) / 400.0))


def k_factor(goal_diff: int, k_base: float = K_BASE) -> float:
    """净胜球缩放的更新幅度 K = k_base×(1+ln(1+|gd|))。"""
    return k_base * (1.0 + math.log1p(abs(goal_diff)))


def one_x_two_probs(
    e_home: float, draw_nu: float = DRAW_NU
) -> tuple[float, float, float]:
    """期望得分 → 1X2 三向概率（Davidson 族拆分，报告用近似）。"""
    draw = min(
        draw_nu * math.sqrt(e_home * (1.0 - e_home)), 2.0 * min(e_home, 1.0 - e_home)
    )
    return (e_home - draw / 2.0, draw, 1.0 - e_home - draw / 2.0)


def sort_matches(matches: Iterable[EloMatch]) -> list[EloMatch]:
    """确定性时间序（kickoff, seq, home, away——同日并列稳定）。"""
    return sorted(matches, key=lambda m: (m.kickoff, m.seq, m.home, m.away))


def compute_elo(
    matches: Iterable[EloMatch],
    params: EloParams = DEFAULT_PARAMS,
    *,
    initial: Mapping[str, float] | None = None,
) -> list[EloPre]:
    """
    时间序折叠：逐场赛前 Elo（PIT 天然正确——pre 值只由排序更早的比赛决定）。

    initial 注入起始状态（era 搬运用：英文空间热身末态 → 中文名空间起点）；
    首见队 base。返回按确定性时间序对齐的 EloPre 列表。
    """
    state: dict[str, float] = dict(initial or {})
    out: list[EloPre] = []
    for match in sort_matches(matches):
        home_pre = state.get(match.home, params.base)
        away_pre = state.get(match.away, params.base)
        out.append(
            EloPre(
                home=match.home,
                away=match.away,
                kickoff=match.kickoff,
                seq=match.seq,
                elo_home_pre=home_pre,
                elo_away_pre=away_pre,
            )
        )
        e_home = expected_score(home_pre, away_pre, params.hfa)
        actual = (
            1.0
            if match.home_goals > match.away_goals
            else (0.5 if match.home_goals == match.away_goals else 0.0)
        )
        delta = k_factor(match.home_goals - match.away_goals, params.k_base) * (
            actual - e_home
        )
        state[match.home] = home_pre + delta
        state[match.away] = away_pre - delta
    return out


def ratings_asof(
    matches: Iterable[EloMatch],
    asof: date,
    params: EloParams = DEFAULT_PARAMS,
) -> dict[str, float]:
    """
    as-of 日末评级（PIT 查询面）：只折叠 kickoff < asof 的比赛。

    防前视由构造保证——同日比赛不计（评级供 asof 当日赛前使用）。
    """
    prior = [m for m in matches if m.kickoff < asof]
    state: dict[str, float] = {}
    for match in sort_matches(prior):
        home_pre = state.get(match.home, params.base)
        away_pre = state.get(match.away, params.base)
        e_home = expected_score(home_pre, away_pre, params.hfa)
        actual = (
            1.0
            if match.home_goals > match.away_goals
            else (0.5 if match.home_goals == match.away_goals else 0.0)
        )
        delta = k_factor(match.home_goals - match.away_goals, params.k_base) * (
            actual - e_home
        )
        state[match.home] = home_pre + delta
        state[match.away] = away_pre - delta
    return state


# --- fd 英文名 ↔ 源T 中文名 确定性桥（重叠期投票） ---


@dataclass(frozen=True)
class BridgeRow:
    """桥投票输入行（两侧各一）：联赛（已归一中文名）、日、比分、主客名。"""

    league: str
    day: date
    home: str
    away: str
    home_goals: int
    away_goals: int


def build_name_bridge(
    hist_rows: Iterable[BridgeRow],
    corpus_rows: Iterable[BridgeRow],
    *,
    min_votes: int = 3,
) -> tuple[dict[str, str], dict[str, object]]:
    """
    重叠期确定性配对桥：fd 英文名 → 源T 中文名（票 78）。

    配对键 = (联赛, 日, 比分)——键在**两侧都唯一**才配（同日同比分的
    多场并列整组跳过，禁自信合并）。主客各投一票，fd 名 → 得票唯一的
    中文名且票数 ≥ min_votes 才映射；分歧/票不足跳过（队从语料起点
    自热身，不阻塞）。返回 (映射, 报告)——报告含票数分布供审计。
    """

    def unique_by_key(
        rows: Iterable[BridgeRow],
    ) -> dict[tuple[str, date, int, int], BridgeRow]:
        grouped: dict[tuple[str, date, int, int], list[BridgeRow]] = defaultdict(list)
        for row in rows:
            grouped[(row.league, row.day, row.home_goals, row.away_goals)].append(row)
        return {key: group[0] for key, group in grouped.items() if len(group) == 1}

    hist_unique = unique_by_key(hist_rows)
    corpus_unique = unique_by_key(corpus_rows)
    votes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pairs = 0
    for key, hist_row in hist_unique.items():
        corpus_row = corpus_unique.get(key)
        if corpus_row is None:
            continue
        pairs += 1
        votes[hist_row.home][corpus_row.home] += 1
        votes[hist_row.away][corpus_row.away] += 1
    mapping: dict[str, str] = {}
    skipped_conflict = 0
    skipped_votes = 0
    for fd_name, tally in votes.items():
        if len(tally) > 1:
            skipped_conflict += 1
            continue
        zh_name, count = next(iter(tally.items()))
        if count < min_votes:
            skipped_votes += 1
            continue
        mapping[fd_name] = zh_name
    report: dict[str, object] = {
        "unique_keys_hist": len(hist_unique),
        "unique_keys_corpus": len(corpus_unique),
        "matched_keys": pairs,
        "mapped_names": len(mapping),
        "skipped_conflict": skipped_conflict,
        "skipped_low_votes": skipped_votes,
    }
    return mapping, report


def transfer_state(
    state: Mapping[str, float], mapping: Mapping[str, str]
) -> dict[str, float]:
    """
    Era 搬运：英文空间热身末态 → 中文名空间起点（票 78）。

    一对一映射（build_name_bridge 保证唯一）；未映射英文队留在原名
    空间（语料层无对应行，自然失效）；目标名已有值不覆盖（确定性）。
    """
    out: dict[str, float] = {}
    for zh_name in set(mapping.values()):
        # 多个 fd 名映射到同一中文名时取映射源中评级最高者（确定性平局规则）
        sources = [fd for fd, zh in mapping.items() if zh == zh_name]
        best = max(sources, key=lambda fd: (state.get(fd, 0.0), fd))
        if state.get(best) is not None:
            out[zh_name] = state[best]
    return out
