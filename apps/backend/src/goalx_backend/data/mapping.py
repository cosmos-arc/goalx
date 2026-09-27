"""
跨源实体映射层（票 77）：运行面 fixture ↔ 源侧场次 id 的物化 join。

定则 1（映射确定性键）由雏形转正——场次匹配两步确定性键（票 75 收敛）：

- ① 主客中文队名 + 北京日期 精确；
- ② 兜底 kickoff 时刻（北京 naive）精确 + 主队名精确（解命名变体）；
- 变体组形态（``resolve_variants``）：canonical 名 + team_aliases 的
  srct/manual 别名逐一参与上述两步（真树点测发现竞彩/源T 系统性命名
  变体如「赫塔费/赫塔菲」，单名形态对其结构性卡死）——仍是确定性
  精确串匹配，命中路径记 meta provenance；
- 唯一命中才落链；多候选记 ``ambiguous``（候选进 meta，人工裁决后
  ``manual`` 覆写）；零命中不落行——禁自信合并。

比票 75 查询期兜底更严：兜底 SQL 对多命中静默取 ORDER BY sid 首行
（对账场景可接受），物化层多候选一律不硬配（错配污染对账/结算全链）。

- ``source_match_links``（v23）：fixture_id ↔ source_match_id 物化，
  一源一行；``method`` 记录命中路径（provenance）；manual 行永不被
  自动同步覆写；同步不再解析的历史链撤除（镜像真值）。
- kickoff canonical=源T：已链场次 ``fixtures.kickoff_utc`` 向
  fixture_universe 对齐——≤12h 漂移自动校准（meta 留前值档案），
  超限只标记不硬改（改期/错链进人工队列）。
- team_aliases 补源行归 modelling（ADR-0008 属主红线）：本模块只产出
  ``(team_id, 源T队名)`` 对（``MappingSyncStats.srct_alias_pairs``），
  由编排层（tasks）交给 ``team_align.record_srct_aliases`` 落库。
- audit：源×联赛分桶的未映射/歧义率报告（起步门：tier1 未映射 <2%）。

消费端只读本模块入口（``srct_sid_for_fixture`` 等），不自行重写匹配。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import duckdb
from loguru import logger

from goalx_backend.data import fixtures as fx_store
from goalx_backend.db import utc_now_iso

SRCT_SOURCE = "srct"
METHOD_PRIMARY_KEY = "primary_key"
METHOD_KICKOFF_EXACT = "kickoff_exact"
METHOD_MANUAL = "manual"
STATUS_LINKED = "linked"
STATUS_AMBIGUOUS = "ambiguous"

# kickoff canonical=源T 的自动校准上限：超过视为改期/错链，只标记不硬改
KICKOFF_ALIGN_MAX_DRIFT_SECONDS = 12 * 3600

_BEIJING = timezone(timedelta(hours=8))

DuckCon = duckdb.DuckDBPyConnection


def _beijing_naive(kickoff_utc: str) -> datetime:
    """ISO 串 → 北京墙钟 naive（fixtures 属主口径，票 77 映射侧薄封装）。"""
    return fx_store.beijing_naive(kickoff_utc)


@dataclass(frozen=True)
class UniverseRow:
    """fixture_universe 单场（join 地基行的映射投影）。"""

    sid: str
    home: str
    away: str
    kickoff_bj: datetime  # 北京墙钟 naive（与 silver 口径一致）


@dataclass(frozen=True)
class Resolution:
    """
    两步法解析结果：sid+method=命中；candidates 非空=歧义；否则无。

    variant 字段记录非 canonical 名命中路径（provenance，进 meta）。
    """

    sid: str | None = None
    method: str | None = None
    candidates: tuple[str, ...] = ()
    home_variant: str | None = None
    away_variant: str | None = None

    def with_variants(self, home: str | None, away: str | None) -> Resolution:
        """附变体命中路径（canonical 名命中时为 None）。"""
        return Resolution(
            sid=self.sid,
            method=self.method,
            candidates=self.candidates,
            home_variant=home,
            away_variant=away,
        )


class SidIndex:
    """fixture_universe 全量内存索引（十万行级；两步确定性键解析）。"""

    def __init__(self) -> None:
        self._by_names: dict[tuple[str, str, str], list[str]] = {}
        self._by_kickoff_home: dict[tuple[datetime, str], list[str]] = {}
        self._by_sid: dict[str, UniverseRow] = {}

    @classmethod
    def build(cls, rows: Iterable[UniverseRow]) -> SidIndex:
        """Build an index; duplicate sids keep the first row（silver 唯一键）。"""
        index = cls()
        for row in rows:
            index._by_sid.setdefault(row.sid, row)
            index._by_names.setdefault(
                (row.home, row.away, row.kickoff_bj.date().isoformat()), []
            ).append(row.sid)
            index._by_kickoff_home.setdefault((row.kickoff_bj, row.home), []).append(
                row.sid
            )
        return index

    def resolve(self, home: str, away: str, kickoff_utc: str) -> Resolution:
        """
        两步确定性键解析（禁自信合并；单名形态，变体组见 ``resolve_variants``）。

        ① (home, away, 北京日期) 唯一命中 → primary_key；
        ② (北京 kickoff 精确, home 精确) 唯一命中 → kickoff_exact；
        任一步多候选（含两步合并后仍多）→ ambiguous；零候选 → 无。
        """
        return self.resolve_variants((home,), (away,), kickoff_utc)

    def resolve_variants(
        self,
        home_names: Iterable[str],
        away_names: Iterable[str],
        kickoff_utc: str,
    ) -> Resolution:
        """
        变体组两步法（票 77）：canonical 名 + 已知别名逐一精确匹配。

        竞彩与源T 中文命名存在系统性变体（如「赫塔费/赫塔菲」）——单名
        两步法对这类场结构性卡死。别名组（canonical + team_aliases 的
        srct/manual 行）参与后仍是**确定性精确串匹配**：任一 (主,客) 变体
        对合计唯一命中 → primary_key；任一主队名变体在 kickoff 精确下
        唯一命中 → kickoff_exact；跨变体候选并集多解 → ambiguous。
        变体命中路径记入 meta（provenance：home_variant/away_variant）。
        """
        beijing = _beijing_naive(kickoff_utc)
        day = beijing.date().isoformat()
        primary: list[tuple[str, str, str]] = []  # (sid, home_variant, away_variant)
        for home in home_names:
            for away in away_names:
                for sid in self._by_names.get((home, away, day), []):
                    primary.append((sid, home, away))
        sids = {sid for sid, _, _ in primary}
        if len(sids) == 1:
            sid, home_used, away_used = primary[0]
            return Resolution(sid=sid, method=METHOD_PRIMARY_KEY).with_variants(
                home_used, away_used
            )
        fallback: list[tuple[str, str]] = []  # (sid, home_variant)
        for home in home_names:
            for sid in self._by_kickoff_home.get((beijing, home), []):
                fallback.append((sid, home))
        fallback_sids = {sid for sid, _ in fallback}
        # ① 歧义时 ② 仍可消歧：同名同日多行 + fixture 开球精确等其中一行
        # = 强区分键（kickoff_exact），不算 ambiguous
        if len(fallback_sids) == 1:
            sid, home_used = fallback[0]
            return Resolution(sid=sid, method=METHOD_KICKOFF_EXACT).with_variants(
                home_used, None
            )
        candidates = tuple(sorted(sids or fallback_sids))
        if candidates:
            return Resolution(candidates=candidates)
        return Resolution()

    def row_for(self, sid: str) -> UniverseRow | None:
        """Sid → universe 行（kickoff 校准/别名产出用）。"""
        return self._by_sid.get(sid)


def load_universe(con: DuckCon) -> Iterator[UniverseRow]:
    """
    fixture_universe → 映射投影行（视图缺席抛 duckdb.Error，调用方降级）。

    silver 的 kickoff 是站点墙钟（北京 naive）；TIMESTAMPTZ 形态（视图
    升级等）统一折回北京 naive，与两步法口径一致。
    """
    rows = con.execute(
        "SELECT sid, home, away, kickoff FROM fixture_universe"
    ).fetchall()
    for sid, home, away, kickoff in rows:
        moment = _as_beijing_naive(kickoff)
        if moment is None:
            continue  # 开球缺行不参与映射（历史浅层场），audit 侧诚实计数
        yield UniverseRow(
            sid=str(sid), home=str(home), away=str(away), kickoff_bj=moment
        )


def _as_beijing_naive(value: object) -> datetime | None:
    """Duckdb 时间标量（datetime/TIMESTAMPTZ/None）→ 北京 naive。"""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_BEIJING).replace(tzinfo=None)


@dataclass(frozen=True)
class UniverseResultRow:
    """fixture_universe 赛果投影行（票 78 Elo/对账消费；比分可缺）。"""

    sid: str
    league: str
    home: str
    away: str
    kickoff_bj: datetime  # 北京墙钟 naive
    home_goals: int | None
    away_goals: int | None


def load_universe_results(con: DuckCon) -> list[UniverseResultRow]:
    """fixture_universe 全量赛果行（视图缺席抛 duckdb.Error，调用方降级）。"""
    rows = con.execute(
        """
        SELECT sid, league, home, away, kickoff, home_goals, away_goals
        FROM fixture_universe
        """
    ).fetchall()
    out: list[UniverseResultRow] = []
    for sid, league, home, away, kickoff, home_goals, away_goals in rows:
        moment = _as_beijing_naive(kickoff)
        if moment is None:
            continue
        out.append(
            UniverseResultRow(
                sid=str(sid),
                league=str(league),
                home=str(home),
                away=str(away),
                kickoff_bj=moment,
                home_goals=None if home_goals is None else int(home_goals),
                away_goals=None if away_goals is None else int(away_goals),
            )
        )
    return out


@dataclass
class MappingSyncStats:
    """一次映射同步的统计（degraded 非空 = 语料桥缺席，本次零动作）。"""

    fixtures: int = 0
    linked: int = 0
    ambiguous: int = 0
    unmapped: int = 0
    kickoff_aligned: int = 0
    kickoff_conflicts: int = 0
    srct_alias_pairs: list[tuple[int, str]] = field(default_factory=list)
    degraded: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """统计 → 日志/CLI 字典（alias 对不回显全清单，只给计数）。"""
        return {
            "fixtures": self.fixtures,
            "linked": self.linked,
            "ambiguous": self.ambiguous,
            "unmapped": self.unmapped,
            "kickoff_aligned": self.kickoff_aligned,
            "kickoff_conflicts": self.kickoff_conflicts,
            "srct_alias_pairs": len(self.srct_alias_pairs),
            "degraded": self.degraded,
        }


def _manual_sid(conn: sqlite3.Connection, fixture_id: int) -> str | None:
    """该场 srct 链的 manual sid（非 manual/无链返回 None）。"""
    row = conn.execute(
        """
        SELECT method, source_match_id FROM source_match_links
        WHERE fixture_id = ? AND source = ?
        """,
        (fixture_id, SRCT_SOURCE),
    ).fetchone()
    if row is None or row["method"] != METHOD_MANUAL:
        return None
    return str(row["source_match_id"])


def _upsert_link(
    conn: sqlite3.Connection,
    fixture_id: int,
    *,
    source_match_id: str | None,
    method: str | None,
    status: str,
    meta: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO source_match_links
            (fixture_id, source, source_match_id, method, status, meta, mapped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(fixture_id, source) DO UPDATE SET
            source_match_id = excluded.source_match_id,
            method = excluded.method,
            status = excluded.status,
            meta = excluded.meta,
            mapped_at = excluded.mapped_at
        """,
        (
            fixture_id,
            SRCT_SOURCE,
            source_match_id,
            method,
            status,
            json.dumps(meta, ensure_ascii=False),
            utc_now_iso(),
        ),
    )


def _clear_non_manual(conn: sqlite3.Connection, fixture_id: int) -> None:
    """撤除不再解析的非 manual 链（镜像真值：silver 重建清理更名联赛等）。"""
    conn.execute(
        """
        DELETE FROM source_match_links
        WHERE fixture_id = ? AND source = ? AND method IS NOT ?
        """,
        (fixture_id, SRCT_SOURCE, METHOD_MANUAL),
    )


def _align_kickoff(
    conn: sqlite3.Connection,
    fixture_id: int,
    universe_bj: datetime,
    current_utc: str,
    meta: dict[str, Any],
) -> bool:
    """
    Kickoff canonical=源T：fixtures.kickoff_utc 对齐 universe（票 77）。

    漂移 ≤12h 自动校准并留前值档案；超限只标记（改期/错链人工队列）。
    UPDATE 可能撞自然键唯一约束（同联赛同队目标时刻已有行）——SAVEPOINT
    内回滚该次，标记冲突不动全局事务。
    """
    target_utc = universe_bj.replace(tzinfo=_BEIJING).astimezone(UTC)
    current = datetime.fromisoformat(current_utc.replace("Z", "+00:00"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    drift = (target_utc - current).total_seconds()
    if drift == 0:
        return False
    if abs(drift) > KICKOFF_ALIGN_MAX_DRIFT_SECONDS:
        meta["kickoff_conflict_seconds"] = int(drift)
        return False
    conn.execute("SAVEPOINT align_kickoff")
    try:
        fx_store.update_fixture_kickoff(conn, fixture_id, target_utc.isoformat())
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO align_kickoff")
        meta["kickoff_conflict_unique"] = True
        return False
    finally:
        conn.execute("RELEASE align_kickoff")
    meta["kickoff_previous"] = current_utc
    meta["kickoff_drift_seconds"] = int(drift)
    return True


def _record_linked(
    conn: sqlite3.Connection,
    index: SidIndex,
    row: sqlite3.Row,
    resolution: Resolution,
    stats: MappingSyncStats,
) -> None:
    """Linked 场落链：meta 变体路径 + kickoff 校准 + srct 别名对产出。"""
    fixture_id = int(row["fixture_id"])
    home_name = str(row["home_name"])
    away_name = str(row["away_name"])
    sid = resolution.sid
    if sid is None:  # 调用方约定仅 linked 分支进入；防御不可达
        return
    meta: dict[str, Any] = {"method": resolution.method}
    if resolution.home_variant not in (None, home_name):
        meta["home_variant"] = resolution.home_variant
    if resolution.away_variant not in (None, away_name):
        meta["away_variant"] = resolution.away_variant
    universe = index.row_for(sid)
    if universe is not None:
        if _align_kickoff(
            conn, fixture_id, universe.kickoff_bj, str(row["kickoff_utc"]), meta
        ):
            stats.kickoff_aligned += 1
        if "kickoff_conflict_seconds" in meta or "kickoff_conflict_unique" in meta:
            stats.kickoff_conflicts += 1
        stats.srct_alias_pairs.append((int(row["home_team_id"]), universe.home))
        stats.srct_alias_pairs.append((int(row["away_team_id"]), universe.away))
    _upsert_link(
        conn,
        fixture_id,
        source_match_id=sid,
        method=resolution.method,
        status=STATUS_LINKED,
        meta=meta,
    )


def sync_fixture_links(
    conn: sqlite3.Connection,
    duck_con: DuckCon | None,
    *,
    team_aliases: MappingABC[int, set[str]] | None = None,
) -> MappingSyncStats:
    """
    全量同步 fixture ↔ 源T sid 链（幂等；语料桥缺席降级零动作）。

    逐场变体组两步法解析（canonical 名 + ``team_aliases`` 注入的
    srct/manual 别名——team_aliases 属 modelling，编排层经
    ``team_align.srct_alias_sets`` 取数注入，ADR-0008 属主红线）→
    linked 落链（``_record_linked``）+ ambiguous 落候选 + unmapped 撤
    历史 non-manual 链。srct 别名对由编排层交属主落库。
    """
    stats = MappingSyncStats()
    if duck_con is None:
        stats.degraded = "duck_con unavailable"
        return stats
    try:
        index = SidIndex.build(load_universe(duck_con))
    except duckdb.Error as exc:
        logger.warning("mapping sync degraded, fixture_universe unavailable: {}", exc)
        stats.degraded = str(exc)
        return stats
    aliases: MappingABC[int, set[str]] = team_aliases or {}
    for row in fx_store.fixtures_with_teams(conn):
        stats.fixtures += 1
        fixture_id = int(row["fixture_id"])
        home_variants = (
            str(row["home_name"]),
            *sorted(aliases.get(int(row["home_team_id"]), ())),
        )
        away_variants = (
            str(row["away_name"]),
            *sorted(aliases.get(int(row["away_team_id"]), ())),
        )
        resolution = index.resolve_variants(
            home_variants, away_variants, str(row["kickoff_utc"])
        )
        manual_sid = _manual_sid(conn, fixture_id)
        if manual_sid is not None:
            # manual 链视为已映射，自动同步不覆写——但 kickoff canonical=源T
            # 不豁免（评审修正：人工裁决的是「这场=这个 sid」，开球仍以源T 为准）
            stats.linked += 1
            universe = index.row_for(manual_sid)
            if universe is not None:
                meta: dict[str, Any] = {}
                if _align_kickoff(
                    conn, fixture_id, universe.kickoff_bj, str(row["kickoff_utc"]), meta
                ):
                    stats.kickoff_aligned += 1
                if "kickoff_conflict_seconds" in meta or (
                    "kickoff_conflict_unique" in meta
                ):
                    stats.kickoff_conflicts += 1
        elif resolution.sid is not None:
            stats.linked += 1
            _record_linked(conn, index, row, resolution, stats)
        elif resolution.candidates:
            stats.ambiguous += 1
            _upsert_link(
                conn,
                fixture_id,
                source_match_id=None,
                method=None,
                status=STATUS_AMBIGUOUS,
                meta={"candidates": list(resolution.candidates)},
            )
        else:
            stats.unmapped += 1
            _clear_non_manual(conn, fixture_id)
    conn.commit()
    return stats


def srct_sid_for_fixture(conn: sqlite3.Connection, fixture_id: int) -> str | None:
    """已链场次的源T sid（未链/歧义返回 None；消费端兜底查询期匹配）。"""
    row = conn.execute(
        """
        SELECT source_match_id FROM source_match_links
        WHERE fixture_id = ? AND source = ? AND status = ?
        """,
        (fixture_id, SRCT_SOURCE, STATUS_LINKED),
    ).fetchone()
    return str(row["source_match_id"]) if row else None


def pending_srct_results(
    conn: sqlite3.Connection,
    now_iso: str,
    *,
    lookback_days: int = 7,
) -> list[sqlite3.Row]:
    """
    待出赛果场次（已开赛、无开奖、近 N 天）× 源T 链状态（票 76 物化输入）。

    返回行带 fixture_id/kickoff_utc/主客 canonical 名/联赛与 sid
    （unlinked 场 sid 为 NULL——CorpusScope 外差集，uniform 兜底通道的口径）。
    窗口语义与 uniform.candidate_business_dates 同源（票 44 待出推导）。
    """
    floor = (
        datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        - timedelta(days=lookback_days)
    ).isoformat(timespec="seconds")
    return conn.execute(
        """
        SELECT f.id AS fixture_id, f.kickoff_utc,
               th.canonical_name AS home_name, ta.canonical_name AS away_name,
               c.name AS competition_name, c.tier AS competition_tier,
               l.source_match_id AS sid
        FROM fixtures f
        JOIN teams th ON th.id = f.home_team_id
        JOIN teams ta ON ta.id = f.away_team_id
        JOIN competitions c ON c.id = f.competition_id
        LEFT JOIN draw_results d ON d.fixture_id = f.id
        LEFT JOIN source_match_links l
            ON l.fixture_id = f.id AND l.source = 'srct' AND l.status = 'linked'
        WHERE f.kickoff_utc <= ? AND f.kickoff_utc >= ? AND d.id IS NULL
        ORDER BY f.kickoff_utc
        """,
        (now_iso, floor),
    ).fetchall()


def srct_kickoff_overrides(
    conn: sqlite3.Connection, duck_con: DuckCon | None
) -> dict[str, int]:
    """
    Jc matchId → 源T canonical 开球 epoch ms（票 77：jc_silver 冗余列改走映射）。

    链路 = match_codes(kind='jingcai') → fixture → link sid →
    fixture_universe.kickoff（同包表，ADR-0008 归 data）。语料桥缺席/
    视图异常返回空 dict（调用方回退 jc 台账值），不静默吞错误以外的路径。
    """
    if duck_con is None:
        return {}
    links = {
        int(row["fixture_id"]): str(row["source_match_id"])
        for row in conn.execute(
            """
            SELECT fixture_id, source_match_id FROM source_match_links
            WHERE source = ? AND status = ?
            """,
            (SRCT_SOURCE, STATUS_LINKED),
        )
    }
    if not links:
        return {}
    try:
        index = SidIndex.build(load_universe(duck_con))
    except duckdb.Error as exc:
        logger.warning("kickoff overrides degraded, universe unavailable: {}", exc)
        return {}
    overrides: dict[str, int] = {}
    for row in conn.execute(
        """
        SELECT source_match_id AS jc_match_id, fixture_id FROM match_codes
        WHERE kind = 'jingcai' AND source_match_id IS NOT NULL
        """
    ):
        sid = links.get(int(row["fixture_id"]))
        if sid is None:
            continue
        universe = index.row_for(sid)
        if universe is None:
            continue
        moment = universe.kickoff_bj.replace(tzinfo=_BEIJING)
        overrides[str(row["jc_match_id"])] = int(moment.timestamp() * 1000)
    return overrides


@dataclass
class AuditBucket:
    """一联赛分桶（audit 聚合行，构建时可变）。"""

    competition: str
    tier: str
    total: int
    linked: int
    ambiguous: int
    unmapped: int

    @property
    def unmapped_rate(self) -> float:
        """未映射率（分母为桶内全部场次）。"""
        return self.unmapped / self.total if self.total else 0.0


CORE_TIER_UNMAPPED_THRESHOLD = 0.02  # 起步门：tier1 未映射 <2%（票 77）


def audit_mapping(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    映射审计报告：源×联赛分桶未映射/歧义率 + 歧义人工队列 + 起步门。

    表空 = 同步从未跑过（``sync_pending`` 置位），此时分桶仍按未映射
    呈现（诚实计数，不伪装成通过）。
    """
    links: dict[int, sqlite3.Row] = {
        int(row["fixture_id"]): row
        for row in conn.execute(
            """
            SELECT fixture_id, status, meta FROM source_match_links
            WHERE source = ?
            """,
            (SRCT_SOURCE,),
        )
    }
    buckets: dict[str, AuditBucket] = {}
    ambiguous_queue: list[dict[str, Any]] = []
    for row in fx_store.fixtures_with_teams(conn):
        fixture_id = int(row["fixture_id"])
        name = str(row["competition_name"])
        bucket = buckets.setdefault(
            name,
            AuditBucket(
                competition=name,
                tier=str(row["competition_tier"]),
                total=0,
                linked=0,
                ambiguous=0,
                unmapped=0,
            ),
        )
        bucket.total += 1
        link = links.get(fixture_id)
        if link is None:
            bucket.unmapped += 1
        elif str(link["status"]) == STATUS_LINKED:
            bucket.linked += 1
        else:
            bucket.ambiguous += 1
            meta = json.loads(str(link["meta"] or "{}"))
            ambiguous_queue.append(
                {
                    "fixture_id": fixture_id,
                    "competition": name,
                    "home": str(row["home_name"]),
                    "away": str(row["away_name"]),
                    "kickoff_utc": str(row["kickoff_utc"]),
                    "candidates": meta.get("candidates", []),
                }
            )
    tier1 = [b for b in buckets.values() if b.tier == "tier1"]
    tier1_total = sum(b.total for b in tier1)
    tier1_unmapped = sum(b.unmapped for b in tier1)
    tier1_rate = (tier1_unmapped / tier1_total) if tier1_total else 0.0
    total = sum(b.total for b in buckets.values())
    return {
        "source": SRCT_SOURCE,
        "fixtures": total,
        "linked": sum(b.linked for b in buckets.values()),
        "ambiguous": sum(b.ambiguous for b in buckets.values()),
        "unmapped": sum(b.unmapped for b in buckets.values()),
        "sync_pending": not links and total > 0,
        "per_competition": {
            b.competition: {
                "tier": b.tier,
                "total": b.total,
                "linked": b.linked,
                "ambiguous": b.ambiguous,
                "unmapped": b.unmapped,
                "unmapped_rate": round(b.unmapped_rate, 4),
            }
            for b in sorted(buckets.values(), key=lambda b: b.competition)
        },
        "ambiguous_queue": sorted(ambiguous_queue, key=lambda item: item["fixture_id"]),
        "core_league_gate": {
            "tier1_unmapped_rate": round(tier1_rate, 4),
            "threshold": CORE_TIER_UNMAPPED_THRESHOLD,
            "pass": tier1_rate < CORE_TIER_UNMAPPED_THRESHOLD,
            "tier1_fixtures": tier1_total,
        },
    }


__all__ = [
    "AuditBucket",
    "MappingSyncStats",
    "Resolution",
    "SidIndex",
    "UniverseRow",
    "audit_mapping",
    "load_universe",
    "srct_kickoff_overrides",
    "srct_sid_for_fixture",
    "sync_fixture_links",
]
