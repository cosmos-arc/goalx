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
from collections.abc import Sequence
from dataclasses import dataclass, field

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store

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
    rows = rs_store.hist_team_names(conn)
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
    rows = fx_store.fixtures_for_events(conn, list(by_event))
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


def set_manual_alias(conn: sqlite3.Connection, team: str, alias: str) -> None:
    """人工覆盖：给 canonical 球队追加 manual 别名（即时生效，票 25）。"""
    row = fx_store.find_team_by_name(conn, team)
    if row is None:
        raise LookupError(f"球队不存在: {team}")
    conn.execute(
        """
        INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
        VALUES (?, 'manual', ?)
        """,
        (int(row["id"]), alias),
    )
    conn.commit()


def odds_api_aliases_for_team(conn: sqlite3.Connection, team_id: int) -> set[str]:
    """该 canonical 队已知的 Odds API 侧英文名集合（join 匹配用）。"""
    return {
        str(row["alias"])
        for row in conn.execute(
            "SELECT alias FROM team_aliases WHERE team_id = ? AND source = 'odds_api'",
            (team_id,),
        )
    }


def record_odds_api_alias(conn: sqlite3.Connection, team_id: int, alias: str) -> None:
    """Join 命中时回填一条 Odds API 别名（幂等）。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
        VALUES (?, 'odds_api', ?)
        """,
        (team_id, alias),
    )


def record_propline_alias(conn: sqlite3.Connection, team_id: int, alias: str) -> None:
    """Join 命中时回填一条 PropLine 侧英文名（幂等；源标签区分 provenance，票 50）。"""
    conn.execute(
        """
        INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
        VALUES (?, 'propline', ?)
        """,
        (team_id, alias),
    )


def record_srct_aliases(
    conn: sqlite3.Connection, pairs: Sequence[tuple[int, str]]
) -> int:
    """
    映射同步产出的 (team_id, 源T队名) 对落 source='srct'（票 77；幂等）。

    源T 中文名与竞彩 canonical 大体同语但存在命名变体——落别名行后，
    后续源T侧名字（阵容/情报/轨迹）可经统一索引解析到同一 team。
    """
    added = 0
    for team_id, alias in pairs:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
            VALUES (?, 'srct', ?)
            """,
            (team_id, alias),
        )
        if cur.rowcount > 0:
            added += 1
    conn.commit()
    return added


def srct_alias_sets(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """team_id → 源侧已知别名集（srct/manual 行；票 77 变体组解析输入）。"""
    out: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT team_id, alias FROM team_aliases WHERE source IN ('srct', 'manual')"
    ):
        out.setdefault(int(row["team_id"]), set()).add(str(row["alias"]))
    return out


@dataclass
class ClubeloAliasReport:
    """clubelo 英文俱乐部名 ↔ 现有英文别名桥的对齐报告（票 77）。"""

    clubs_total: int = 0
    matched: int = 0
    added: int = 0
    unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """报告 → 日志/CLI 字典（未匹配清单全列，人工补线用）。"""
        return {
            "clubs_total": self.clubs_total,
            "matched": self.matched,
            "added": self.added,
            "unmatched": self.unmatched,
        }


def sync_clubelo_aliases(
    conn: sqlite3.Connection, clubs: Sequence[str]
) -> ClubeloAliasReport:
    """
    Clubelo 英文俱乐部名 → 中文 canonical team 的别名桥（票 77 消费侧）。

    clubelo 与竞彩不同语，不能字符串直配；但 odds_api/propline join 年代
    已沉淀英文别名——以英文别名建三级索引解析 clubelo 俱乐部名，唯一命中
    即落 team_aliases(source='clubelo')。未命中（低级别队/别名未覆盖）
    进报告人工队列，不硬配（定则 1）。
    """
    names: dict[str, int] = {}
    rows = conn.execute(
        """
        SELECT team_id, alias FROM team_aliases
        WHERE source IN ('odds_api', 'propline') ORDER BY id
        """
    ).fetchall()
    for row in rows:
        names.setdefault(str(row["alias"]), int(row["team_id"]))
    index = NameIndex.build(names)
    report = ClubeloAliasReport(clubs_total=len(clubs))
    for club in clubs:
        team_id = index.resolve(club)
        if team_id is None:
            report.unmatched.append(club)
            continue
        report.matched += 1
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO team_aliases (team_id, source, alias)
            VALUES (?, 'clubelo', ?)
            """,
            (team_id, club),
        )
        if cur.rowcount > 0:
            report.added += 1
    conn.commit()
    return report


def clubelo_alias_for_team(conn: sqlite3.Connection, team_id: int) -> str | None:
    """该 team 的 clubelo 侧俱乐部名（未接通返回 None）。"""
    row = conn.execute(
        """
        SELECT alias FROM team_aliases
        WHERE team_id = ? AND source = 'clubelo' ORDER BY id LIMIT 1
        """,
        (team_id,),
    ).fetchone()
    return str(row["alias"]) if row else None


def english_aliases_for_team(conn: sqlite3.Connection, team_id: int) -> set[str]:
    """该队已知英文名（odds_api + propline 并集，join 交叉核对用，票 50）。"""
    return {
        str(row["alias"])
        for row in conn.execute(
            """
            SELECT alias FROM team_aliases
            WHERE team_id = ? AND source IN ('odds_api', 'propline')
            """,
            (team_id,),
        )
    }
