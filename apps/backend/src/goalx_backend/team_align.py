"""
球队名对齐层（票 25）：fd 训练域英文名 ↔ 竞彩预测域。

fd（football-data.co.uk）用缩写英文名（Man United、Nott'm Forest、Ath
Madrid）；竞彩场次的 canonical 队名是中文，但 odds_api join 时事件英文队名
会持久化进 team_aliases（source=odds_api）。本模块提供确定性规范化与三级
匹配（精确 → 规范化等价 → 词元子集唯一命中），把训练域名字解析到预测域
team，或反向把 fixture 球队映射到模型工件里的训练域队名；并输出五大 hist
队名对当前别名的覆盖率与缺口报告。

人工覆盖：team_aliases(source=manual) 参与同一索引，写入即生效；重跑幂等
（全部 INSERT OR IGNORE / 只读查询）。
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field

# 俱乐部法律形态词元（两侧都剥离，使 "FC Koln"↔"Koln" 等价）
STRIP_TOKENS = frozenset(
    {
        "fc",
        "cf",
        "afc",
        "cfc",
        "ac",
        "as",
        "ca",
        "cd",
        "sc",
        "ss",
        "sk",
        "bk",
        "fk",
        "if",
        "ud",
        "sd",
        "us",
        "sv",
        "vfb",
        "vfl",
        "de",
    }
)

# fd 缩写 → 规范展开（键值均为规范化后的全名串；词元子集覆盖不了的情形）
ABBREVIATIONS: dict[str, str] = {
    "man united": "manchester united",
    "man city": "manchester city",
    "nottm forest": "nottingham forest",
    "wolves": "wolverhampton",
    "spurs": "tottenham",
    "ein frankfurt": "eintracht frankfurt",
    "m gladbach": "borussia monchengladbach",
    "mgladbach": "borussia monchengladbach",
    "gladbach": "borussia monchengladbach",
    "ath bilbao": "athletic bilbao",
    "ath madrid": "atletico madrid",
    "paris sg": "paris saint germain",
    "psg": "paris saint germain",
    "inter": "inter milan",
    "internazionale": "inter milan",
    "espanol": "espanyol",
    "hamburger": "hamburg",
    "st etienne": "saint etienne",
    "stoke": "stoke city",
}


def normalize_team_name(name: str) -> str:
    """
    确定性规范化：去重音 → 小写 → 去标点 → 剥离法律词元/纯数字 → 缩写展开。

    返回空格连接的词元串；同名恒同值（幂等）。
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    # 撇号直接删除（Nott'm→nottm），其余标点转空格
    no_apostrophe = lowered.replace("'", "")
    cleaned = "".join(c if c.isalnum() else " " for c in no_apostrophe)
    tokens = [
        t for t in cleaned.split() if t and t not in STRIP_TOKENS and not t.isdigit()
    ]
    joined = " ".join(tokens)
    expanded = ABBREVIATIONS.get(joined, joined)
    return expanded


@dataclass(frozen=True)
class NameIndex:
    """名字 → id 的三级匹配索引（exact → normalized → token-subset 唯一）。"""

    entries: dict[int, str]  # id → 原名（报告用）
    _normalized: dict[str, int] = field(default_factory=dict)
    _tokens: dict[tuple[str, ...], int] = field(default_factory=dict)

    @classmethod
    def build(cls, names_to_id: dict[str, int]) -> NameIndex:
        """Build an index; later duplicate ids for one name are ignored (first wins)."""
        entries: dict[int, str] = {}
        normalized_index: dict[str, int] = {}
        for name, target in names_to_id.items():
            entries[target] = name
            normalized_index.setdefault(normalize_team_name(name), target)
        token_index: dict[tuple[str, ...], int] = {}
        for normalized, target in normalized_index.items():
            token_index.setdefault(tuple(normalized.split()), target)
        return cls(entries=entries, _normalized=normalized_index, _tokens=token_index)

    def resolve(self, name: str) -> int | None:
        """Resolve a name to an id; ambiguity across teams yields None."""
        normalized = normalize_team_name(name)
        exact = self._normalized.get(normalized)
        if exact is not None:
            return exact
        key = set(normalized.split())
        if not key:
            return None
        hits: set[int] = {
            target
            for tokens, target in self._tokens.items()
            if key < set(tokens) or set(tokens) < key
        }
        if len(hits) == 1:
            return hits.pop()
        return None


def alias_index(conn: sqlite3.Connection) -> NameIndex:
    """team_aliases 全量（含 manual）构建的解析索引。"""
    names: dict[str, int] = {}
    rows = conn.execute(
        "SELECT team_id, alias FROM team_aliases ORDER BY id"
    ).fetchall()
    for row in rows:
        names.setdefault(str(row["alias"]), int(row["team_id"]))
    return NameIndex.build(names)


def resolve_hist_team(conn: sqlite3.Connection, hist_name: str) -> int | None:
    """把 fd hist 队名解析到当前 team（无/歧义返回 None）。"""
    return alias_index(conn).resolve(hist_name)


def match_model_team(model_teams: list[str], aliases: list[str]) -> str | None:
    """
    反向映射：fixture 的别名列表 → 模型工件中的训练域队名。

    以模型队名建索引，逐别名解析；全部别名都未命中或跨队歧义返回 None。
    """
    index = NameIndex.build({name: pos for pos, name in enumerate(model_teams)})
    for alias in aliases:
        pos = index.resolve(alias)
        if pos is not None:
            return model_teams[pos]
    return None


@dataclass
class AlignmentReport:
    """五大 hist 队名 ↔ 当前别名的覆盖率报告（票 25 验收）。"""

    per_competition: dict[str, dict[str, int]] = field(default_factory=dict)
    unmatched: list[dict[str, str]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        """全部 hist 队名的总体映射覆盖率（0..1）。"""
        total = sum(comp["total"] for comp in self.per_competition.values())
        matched = sum(comp["matched"] for comp in self.per_competition.values())
        return matched / total if total else 0.0


def build_alignment_report(conn: sqlite3.Connection) -> AlignmentReport:
    """逐联赛报告 hist 队名解析覆盖率；未匹配清单逐条列出（明确可查）。"""
    report = AlignmentReport()
    index = alias_index(conn)
    rows = conn.execute(
        """
        SELECT competition, home_team AS team FROM hist_matches
        UNION
        SELECT competition, away_team AS team FROM hist_matches
        ORDER BY competition, team
        """
    ).fetchall()
    by_comp: dict[str, list[str]] = {}
    for row in rows:
        by_comp.setdefault(str(row["competition"]), []).append(str(row["team"]))
    for competition, teams in by_comp.items():
        matched = 0
        for team in teams:
            if index.resolve(team) is not None:
                matched += 1
            else:
                report.unmatched.append({"competition": competition, "team": team})
        report.per_competition[competition] = {
            "total": len(teams),
            "matched": matched,
        }
    return report


def backfill_aliases_from_events(
    conn: sqlite3.Connection,
    events: list[tuple[str, str, str]],
) -> int:
    """
    从 (event_id, home_team, away_team) 回填 team_aliases(source=odds_api)。

    事件按 fixtures.odds_api_event_id 关联到已 join 场次；幂等（重复写忽略）。
    返回新增别名数。
    """
    if not events:
        return 0
    by_event = {event_id: (home, away) for event_id, home, away in events}
    placeholders = ", ".join("?" for _ in by_event)
    rows = conn.execute(
        f"""
        SELECT odds_api_event_id, home_team_id, away_team_id FROM fixtures
        WHERE odds_api_event_id IN ({placeholders})
        """,  # noqa: S608
        tuple(by_event),
    ).fetchall()
    added = 0
    for row in rows:
        names = by_event.get(str(row["odds_api_event_id"]))
        if names is None:
            continue
        for team_id, alias in (
            (int(row["home_team_id"]), names[0]),
            (int(row["away_team_id"]), names[1]),
        ):
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
                VALUES (?, 'odds_api', ?)
                """,
                (team_id, alias),
            )
            if cur.rowcount > 0:
                added += 1
    return added
