"""赛程/市场域仓储：赛事、球队、场次、销售编号与赔率快照。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any

from goalx_backend.db import utc_now_iso
from goalx_backend.models import (
    MatchCodeInput,
    ObservationInput,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

CST = timezone(timedelta(hours=8))  # 竞彩官方时区：北京时间


def beijing_business_date(now: datetime | None = None) -> str:
    """业务日 = 北京时区日历日（spec §3；全仓唯一出处）。"""
    return (now or datetime.now(UTC)).astimezone(CST).strftime("%Y-%m-%d")


def beijing_naive(kickoff_utc: str) -> datetime:
    """
    ISO 串（含偏移或 Z）→ 北京墙钟 naive（票 75 兜底/票 77 映射共用）。

    naive 输入按 UTC 解释（防御：运行面 kickoff 恒带偏移，银层墙钟恒 naive）。
    """
    parsed = datetime.fromisoformat(kickoff_utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(CST).replace(tzinfo=None)


def kickoff_utc_iso(beijing_naive: str) -> str:
    """
    北京墙钟 naive 串 → UTC aware ISO 串（写侧；票 07 silver v2）。

    非法串 raise（写侧错误=数据错误，fail-loud 不静默）。
    """
    parsed = beijing_naive_to_utc(beijing_naive)
    if parsed is None:
        raise ValueError(f"非法北京墙钟 kickoff: {beijing_naive!r}")
    return parsed.isoformat(timespec="seconds")


def kickoff_to_utc(value: str) -> datetime | None:
    """
    Kickoff 串 → UTC aware（双模读侧；票 07）。

    v2 行=UTC aware（写侧已转，原样用）；v1 legacy=北京墙钟 naive
    （经 ``beijing_naive_to_utc`` 真转换）。两代共存期单一读口。
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return beijing_naive_to_utc(value)


def beijing_naive_to_utc(value: str) -> datetime | None:
    """
    北京墙钟 naive 串 → UTC aware（``beijing_naive`` 的逆；非法串=None）。

    kickoff 时刻口径单一真相源（caliber-loop 票 01）：抽取器 PIT 判定与
    评测侧时距面共用，不许两处各写一份。aware 输入一律 None（fail-loud：
    重贴时区标签正是被修掉的 −8h 偏差来源，不许静默复活）。
    """
    try:
        naive = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    if naive.tzinfo is not None:
        return None
    return naive.replace(tzinfo=CST).astimezone(UTC)


def _lookup_id(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    """Fetch an id by natural key; raises LookupError when absent."""
    row = conn.execute(sql, params).fetchone()
    if row is None:
        raise LookupError(f"upsert id lookup failed: {params}")
    return int(row["id"])


def upsert_competition(
    conn: sqlite3.Connection,
    name: str,
    *,
    tier: Tier = Tier.TIER2,
    odds_api_sport_key: str | None = None,
    api_football_league_id: int | None = None,
) -> int:
    """Insert or update a competition by name; return its id."""
    conn.execute(
        """
            INSERT INTO competitions
(name, tier, odds_api_sport_key,
            api_football_league_id, created_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(name)
            DO UPDATE SET tier=excluded.tier,
            odds_api_sport_key=COALESCE(
excluded.odds_api_sport_key,
            competitions.odds_api_sport_key),
            api_football_league_id=COALESCE(
excluded.api_football_league_id,
            competitions.api_football_league_id)
        """,
        (name, tier.value, odds_api_sport_key, api_football_league_id, utc_now_iso()),
    )
    return _lookup_id(conn, "SELECT id FROM competitions WHERE name = ?", (name,))


def find_team_by_name(
    conn: sqlite3.Connection, canonical_name: str
) -> sqlite3.Row | None:
    """按 canonical 队名查球队行（人工别名覆盖用）。"""
    return conn.execute(
        "SELECT id FROM teams WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()


def upsert_team(conn: sqlite3.Connection, canonical_name: str) -> int:
    """Insert a team by canonical name (idempotent); return its id."""
    conn.execute(
        """
            INSERT INTO teams (canonical_name, created_at) VALUES (?, ?)
ON
            CONFLICT(canonical_name) DO NOTHING
        """,
        (canonical_name, utc_now_iso()),
    )
    return _lookup_id(
        conn, "SELECT id FROM teams WHERE canonical_name = ?", (canonical_name,)
    )


def upsert_fixture(
    conn: sqlite3.Connection,
    competition_id: int,
    kickoff_utc: str,
    home_team_id: int,
    away_team_id: int,
    *,
    stage: str | None = None,
) -> int:
    """Insert a fixture on its natural key (idempotent); return its id."""
    conn.execute(
        """
            INSERT INTO fixtures
(competition_id, kickoff_utc, home_team_id,
            away_team_id, stage)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(competition_id,
            kickoff_utc, home_team_id, away_team_id)
DO NOTHING
        """,
        (competition_id, kickoff_utc, home_team_id, away_team_id, stage),
    )
    return _lookup_id(
        conn,
        """
            SELECT id FROM fixtures WHERE competition_id = ? AND kickoff_utc = ?
AND
            home_team_id = ? AND away_team_id = ?
        """,
        (competition_id, kickoff_utc, home_team_id, away_team_id),
    )


def update_fixture_kickoff(
    conn: sqlite3.Connection, fixture_id: int, kickoff_utc: str
) -> None:
    """Reschedule: move an existing fixture's kickoff（票 35：改期不新造比赛）。"""
    conn.execute(
        "UPDATE fixtures SET kickoff_utc = ? WHERE id = ?", (kickoff_utc, fixture_id)
    )


def find_fixture_by_source_match(
    conn: sqlite3.Connection, kind: str, source_match_id: str
) -> sqlite3.Row | None:
    """Locate a fixture via a source-system match id (e.g. sporttery matchId)."""
    return conn.execute(
        """
            SELECT f.* FROM fixtures f JOIN match_codes mc ON mc.fixture_id = f.id
WHERE
            mc.kind = ? AND mc.source_match_id = ?
        """,
        (kind, source_match_id),
    ).fetchone()


def unique_jc_fixture_by_source_match(
    conn: sqlite3.Connection, source_match_id: str
) -> tuple[int, str] | None:
    """
    源T match id → (fixture_id, kickoff_utc)；仅当 id 唯一命中一个竞彩场次。

    池映射 id 直连（pool-mapping-goals4/01）专用：多 fixture 命中=歧义，
    返回 None 由消费端回落名字+时间窗路径，不猜。
    """
    rows = conn.execute(
        """
            SELECT DISTINCT f.id AS fid, f.kickoff_utc AS kickoff
            FROM fixtures f
            JOIN match_codes mc ON mc.fixture_id = f.id
            WHERE mc.kind = 'jingcai' AND mc.source_match_id = ?
        """,
        (source_match_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return int(rows[0]["fid"]), str(rows[0]["kickoff"])


def jc_board_rows_in_horizon(
    conn: sqlite3.Connection,
    *,
    business_dates: tuple[str, ...],
    kickoff_from_iso: str,
    kickoff_to_iso: str,
) -> list[sqlite3.Row]:
    """
    竞彩业务日 × [from, to] 开球窗内的板上场次（票 05 等源T 面）。

    fid/编号/开球/队名；窗含已开球场（from 可早于 now——晨检看昨夜
    深夜场残差）。fixtures/match_codes 属主查询（ADR-0008）。
    """
    marks = ",".join("?" for _ in business_dates)
    return conn.execute(
        f"""
            SELECT DISTINCT f.id AS fid, f.kickoff_utc AS kickoff,
                   mc.code AS code, ht.canonical_name AS home, at.canonical_name AS away
            FROM fixtures f
            JOIN match_codes mc ON mc.fixture_id = f.id
              AND mc.kind = 'jingcai' AND mc.business_date IN ({marks})
            JOIN teams ht ON ht.id = f.home_team_id
            JOIN teams at ON at.id = f.away_team_id
            WHERE f.kickoff_utc >= ? AND f.kickoff_utc <= ?
        """,  # noqa: S608 — 占位符由 business_dates 定宽构造，非用户输入
        (*business_dates, kickoff_from_iso, kickoff_to_iso),
    ).fetchall()


def upsert_match_code(conn: sqlite3.Connection, code: MatchCodeInput) -> int:
    """Insert or refresh an official sales code for a fixture; return its id."""
    conn.execute(
        """
            INSERT INTO match_codes
(fixture_id, kind, business_date, code,
            source_match_id, is_single)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(kind,
            business_date, code) DO UPDATE SET
fixture_id=excluded.fixture_id,
            source_match_id=excluded.source_match_id,
is_single=excluded.is_single
        """,
        (
            code.fixture_id,
            code.kind,
            code.business_date,
            code.code,
            code.source_match_id,
            code.is_single,
        ),
    )
    return _lookup_id(
        conn,
        "SELECT id FROM match_codes WHERE kind = ? AND business_date = ? AND code = ?",
        (code.kind, code.business_date, code.code),
    )


def record_quote_observation(conn: sqlite3.Connection, obs: ObservationInput) -> int:
    """Append one raw-observation evidence row; return its id."""
    cur = conn.execute(
        """
            INSERT INTO quote_observations
(source, purpose, observed_at, source_updated_at, snapshot_at,
            endpoint, parse_version, raw_sha256, raw_ref, summary, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            obs.source,
            obs.purpose.value,
            obs.observed_at,
            obs.source_updated_at,
            obs.snapshot_at,
            obs.endpoint,
            obs.parse_version,
            obs.raw_sha256,
            obs.raw_ref,
            obs.summary,
            utc_now_iso(),
        ),
    )
    row_id = int(cur.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("quote_observation insert failed")
    return row_id


def latest_observation_time(conn: sqlite3.Connection) -> str | None:
    """最新原始观测时刻（无行 None）——采集活性读数（晨检消费，ADR-0008）。"""
    row = conn.execute(
        "SELECT MAX(observed_at) AS m FROM quote_observations"
    ).fetchone()
    return None if row is None or row["m"] is None else str(row["m"])


def record_had_confirmation(
    conn: sqlite3.Connection, fixture_id: int, observed_at: str, observation_id: int
) -> None:
    """
    追加一条 had 报价确认观测关联（票 03；v40 表）。

    每次成功观测（无论价格是否变化）都留 (observation, fixture) 关联——
    同价重采被快照自然键去重后，本表仍是"最近一次成功确认"的证据源。
    UNIQUE(observation_id, fixture_id) 吸收同证据重解析；observed_at 冗余
    自观测行（as-of 单表 MAX 免 join，与 sale_statuses 同范式）。
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO had_quote_confirmations
            (observation_id, fixture_id, observed_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (observation_id, fixture_id, observed_at, utc_now_iso()),
    )


def latest_had_confirmation_asof(
    conn: sqlite3.Connection, fixture_id: int, as_of: str
) -> str | None:
    """as_of 前最近一次 had 报价成功确认的观测时刻（无行返回 None）。"""
    row = conn.execute(
        """
        SELECT MAX(observed_at) AS latest FROM had_quote_confirmations
        WHERE fixture_id = ? AND observed_at <= ?
        """,
        (fixture_id, as_of),
    ).fetchone()
    return str(row["latest"]) if row["latest"] else None


def append_sale_status(conn: sqlite3.Connection, status: SaleStatusInput) -> int:
    """Append a sale-status observation (append-only 时序, 票 35)."""
    cur = conn.execute(
        """
            INSERT INTO sale_statuses
(fixture_id, market_code, sale_state, single_eligible, observed_at,
            source_updated_at, observation_id, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status.fixture_id,
            status.market_code,
            status.sale_state,
            None if status.single_eligible is None else int(status.single_eligible),
            status.observed_at,
            status.source_updated_at,
            status.observation_id,
            utc_now_iso(),
        ),
    )
    row_id = int(cur.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("sale_status insert failed")
    return row_id


def sale_status_row_exists(conn: sqlite3.Connection, status: SaleStatusInput) -> bool:
    """
    同自然键的销售状态行是否已存在（票 38 重解析幂等用）。

    自然键 = 证据身份（observation_id + observed_at）+ 判定内容
    （fixture/market/sale_state/single_eligible）。实时采集每次观测有
    新 observation_id，不会被本判重吸收（票 35"报价未变也保留观测证据"）；
    只有对同一证据的重解析才会命中跳过。observation_id 为空的调用没有
    证据身份，由调用方决定是否判重。
    """
    row = conn.execute(
        """
            SELECT 1 FROM sale_statuses
            WHERE fixture_id = ? AND observed_at = ?
              AND market_code IS ? AND sale_state = ?
              AND single_eligible IS ?
              AND observation_id IS ?
            LIMIT 1
        """,
        (
            status.fixture_id,
            status.observed_at,
            status.market_code,
            status.sale_state,
            None if status.single_eligible is None else int(status.single_eligible),
            status.observation_id,
        ),
    ).fetchone()
    return row is not None


#: 竞彩每日销售窗缺省界（北京时；票09 考据 ~11:30–22:00）。开售侧由当日
#: 首个 on_sale 观测锚覆写（开售是全局事件；开得晚的日子缺省界自愈）。
SALE_WINDOW_DEFAULT_OPEN = dtime(11, 30)
SALE_WINDOW_DEFAULT_CLOSE = dtime(22, 0)


def beijing_day(ts: str) -> str:
    """ISO 时刻 → 北京日历日（销售窗锚的日键）。"""
    return beijing_naive(ts).strftime("%Y-%m-%d")


def sale_window_anchor(conn: sqlite3.Connection, beijing_day: str) -> str | None:
    """
    该北京日首个 on_sale 观测时刻（ISO UTC）；当日未观测到开售则 None。

    前提（correctness W1/I1/I3，2026-11-01 审；票 04 观测加密后语义更新）：
    锚是首**观测**非真开售时刻——[缺省开, 锚) 盲带内的窗外归因偏保守。
    观测密度（票 04 起）：10:00/12:30/16:30/19:00 定拍 + probe 开售轮询
    （北京 [11:00,12:30) 零候选带内 5 分钟一拍至翻转）→ 销售日锚精度
    ≈5 分钟，盲带收窄至轮询拍距；单条异常早观测会前移当日锚；区间
    比较按字典序，仅限 observed_at 全库 UTC 归一秒精度格式（现役写入
    方恒如此）；不钳位 at 之后的行——回溯归因取当日终锚是设计，live
    前视由写入侧 observed_at≤墙钟保证。
    """
    start = beijing_naive_to_utc(f"{beijing_day} 00:00")
    if start is None:
        return None
    end = start + timedelta(hours=24)
    row = conn.execute(
        """
            SELECT MIN(observed_at) FROM sale_statuses
            WHERE sale_state = 'on_sale'
              AND observed_at >= ? AND observed_at < ?
        """,
        (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def sale_window_anchor_for(conn: sqlite3.Connection, at: str) -> str | None:
    """At 时刻所属北京日的开售锚（had-quote 单点判定用）。"""
    return sale_window_anchor(conn, beijing_day(at))


def sale_window_phase(at: str, *, anchor: str | None = None) -> str:
    """
    时刻的每日销售窗相位：``pre_open | in_window | post_close``。

    锚存在用锚的开售墙钟（跨北京日的锚弃用——相位按当日窗解释），
    否则缺省 11:30；收市恒缺省 22:00。区间半开 [open, close)。
    sellStatus 窗外=未开售是相位事实，消费方据此归因
    （sale-window-derivation 票 01）。
    """
    wall = beijing_naive(at).time()
    open_wall = SALE_WINDOW_DEFAULT_OPEN
    if anchor and beijing_day(anchor) == beijing_day(at):
        open_wall = beijing_naive(anchor).time()
    if wall >= SALE_WINDOW_DEFAULT_CLOSE:
        return "post_close"
    if wall >= open_wall:
        return "in_window"
    return "pre_open"


def not_on_sale_reject_reason(phase: str) -> str:
    """未开售拒绝理由（两消费方单一出处）：窗内=场次级，窗外=相位事实。"""
    return "sale_not_on_sale" if phase == "in_window" else "sale_window_closed"


def latest_sale_status_asof(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    as_of: str,
) -> sqlite3.Row | None:
    """
    Latest known sale status for a fixture/market at a decision time.

    市场级行（market_code 精确匹配）优先于比赛级行（market_code IS NULL）；
    只取 observed_at 不晚于 as_of 的观测——之后的观测不能证明当时状态。
    """
    return conn.execute(
        """
            SELECT * FROM sale_statuses
            WHERE fixture_id = ? AND observed_at <= ?
              AND (market_code = ? OR market_code IS NULL)
            ORDER BY (market_code = ?) DESC, observed_at DESC, id DESC
            LIMIT 1
        """,
        (fixture_id, as_of, market_code, market_code),
    ).fetchone()


def insert_odds_snapshot(conn: sqlite3.Connection, snap: SnapshotInput) -> int | None:
    """
    Append an odds snapshot; returns new id, None when an identical row exists.

    Append-only（ADR 0001）：同自然键的重复采集以 ``INSERT OR IGNORE`` 吸收，
    永不修改既有行。observed_at/source_updated_at/observation_id 是票 35
    证据列；旧行为 NULL（时间语义按源解释，未知不倒填）。
    """
    cur = conn.execute(
        """
            INSERT OR IGNORE INTO odds_snapshots
            (fixture_id, market_code, selection_code, source, purpose, odds,
             captured_at, observed_at, source_updated_at, observation_id,
             meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snap.fixture_id,
            snap.market_code,
            snap.selection_code,
            snap.source,
            snap.purpose.value,
            snap.odds,
            snap.captured_at,
            snap.observed_at,
            snap.source_updated_at,
            snap.observation_id,
            json.dumps(snap.meta, ensure_ascii=False) if snap.meta else None,
            utc_now_iso(),
        ),
    )
    if cur.rowcount > 0 and cur.lastrowid:
        return int(cur.lastrowid)
    return None


def latest_odds_by_selection(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    source: str,
) -> dict[str, tuple[float, str]]:
    """Latest snapshot per selection for one (fixture, market, source)."""
    rows = conn.execute(
        """
            SELECT selection_code, odds, captured_at FROM odds_snapshots
WHERE fixture_id
            = ? AND market_code = ? AND source = ?
ORDER BY captured_at, odds
        """,
        (fixture_id, market_code, source),
    ).fetchall()
    latest: dict[str, tuple[float, str]] = {}
    for row in rows:
        latest[str(row["selection_code"])] = (
            float(row["odds"]),
            str(row["captured_at"]),
        )
    return latest


def latest_odds_by_selection_asof(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    source: str,
    as_of: str,
) -> dict[str, tuple[float, str]]:
    """
    Latest snapshot per selection for one (fixture, market, source) at as_of.

    只取 captured_at 不晚于 as_of 的行——之后的观测不能证明该时点可见的
    价（auto-paper 票 01 锚冻结口径，与 latest_sale_status_asof 同语义）。
    """
    rows = conn.execute(
        """
        SELECT selection_code, odds, captured_at FROM odds_snapshots
        WHERE fixture_id = ? AND market_code = ? AND source = ?
          AND captured_at <= ?
        ORDER BY captured_at, odds
        """,
        (fixture_id, market_code, source, as_of),
    ).fetchall()
    latest: dict[str, tuple[float, str]] = {}
    for row in rows:
        latest[str(row["selection_code"])] = (
            float(row["odds"]),
            str(row["captured_at"]),
        )
    return latest


def odds_history(
    conn: sqlite3.Connection,
    fixture_id: int,
    market_code: str,
    *,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Full append-only odds timeline for a fixture/market (票 19 验收)."""
    if source is not None:
        return conn.execute(
            """
                SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market_code = ?
AND
                source = ? ORDER BY captured_at, id
            """,
            (fixture_id, market_code, source),
        ).fetchall()
    return conn.execute(
        """
            SELECT * FROM odds_snapshots WHERE fixture_id = ? AND market_code = ?
ORDER
            BY captured_at, id
        """,
        (fixture_id, market_code),
    ).fetchall()


def fixtures_for_business_date(
    conn: sqlite3.Connection, business_date: str
) -> list[sqlite3.Row]:
    """All jingcai fixtures carrying a sales code on a business date."""
    return fixtures_for_business_dates(conn, [business_date])


def fixture_detail(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """
    One fixture joined with sales code/competition/team names (研究页输入, 票 wb-02).

    与场次列表同构的对照行；无竞彩销售编号（非在售场次）返回 None。
    """
    return conn.execute(
        """
        SELECT f.*, mc.code AS match_code, mc.is_single, mc.business_date,
c.name AS
        competition_name, c.tier AS competition_tier,
ht.canonical_name AS home_team,
        at2.canonical_name AS away_team
FROM match_codes mc
JOIN fixtures f ON f.id
        = mc.fixture_id
JOIN competitions c ON c.id = f.competition_id
JOIN teams ht ON
        ht.id = f.home_team_id
JOIN teams at2 ON at2.id = f.away_team_id
WHERE
        mc.kind = 'jingcai' AND f.id = ?
ORDER BY mc.business_date DESC, mc.code
LIMIT 1
        """,
        (fixture_id,),
    ).fetchone()


def fixtures_for_business_dates(
    conn: sqlite3.Connection, business_dates: Sequence[str]
) -> list[sqlite3.Row]:
    """
    All jingcai fixtures carrying a sales code on any of the given dates.

    工作台 v2 票 01：场次列表 3 日化的多业务日查询；行内携带 ``mc.business_date``
    供调用方按日分组（单日查询同样带该列，响应形状一致）。
    """
    if not business_dates:
        return []
    placeholders = ", ".join("?" for _ in business_dates)
    return conn.execute(
        f"""
            SELECT f.*, mc.code AS match_code, mc.is_single, mc.source_match_id,
mc.business_date AS
            business_date, c.name
AS competition_name, c.tier AS competition_tier,
ht.canonical_name AS
            home_team, at2.canonical_name AS away_team
FROM match_codes mc
JOIN fixtures
            f ON f.id = mc.fixture_id
JOIN competitions c ON c.id = f.competition_id
JOIN teams ht ON ht.id = f.home_team_id
JOIN teams at2 ON at2.id =
            f.away_team_id
WHERE mc.kind = 'jingcai' AND mc.business_date IN ({placeholders})
ORDER BY
            f.kickoff_utc, mc.code
        """,  # noqa: S608
        tuple(business_dates),
    ).fetchall()


def set_odds_api_join(
    conn: sqlite3.Connection,
    fixture_id: int,
    event_id: str,
    sport_key: str,
    method: str,
) -> None:
    """Persist a sporttery↔Odds API fixture join (票 20 映射表)."""
    conn.execute(
        """
            UPDATE fixtures SET odds_api_event_id = ?, odds_api_sport_key = ?,
            join_method = ?, joined_at = ? WHERE id = ?
        """,
        (event_id, sport_key, method, utc_now_iso(), fixture_id),
    )


def fixtures_for_events(
    conn: sqlite3.Connection, event_ids: list[str]
) -> list[sqlite3.Row]:
    """按 Odds API event id 批量取场次行（别名回填用）。"""
    if not event_ids:
        return []
    placeholders = ", ".join("?" for _ in event_ids)
    return conn.execute(
        f"""
        SELECT id, odds_api_event_id, home_team_id, away_team_id FROM fixtures
        WHERE odds_api_event_id IN ({placeholders})
        """,  # noqa: S608
        event_ids,
    ).fetchall()


def pending_jingcai_fixtures(
    conn: sqlite3.Connection, now_utc: str
) -> list[sqlite3.Row]:
    """Fixtures kicked off but without a draw result yet (结算扫描输入)."""
    return conn.execute(
        """
            SELECT f.* FROM fixtures f
WHERE f.kickoff_utc <= ?
AND f.id IN (SELECT
            fixture_id FROM match_codes WHERE kind = 'jingcai')
AND f.id NOT IN (SELECT
            fixture_id FROM draw_results)
ORDER BY f.kickoff_utc
        """,
        (now_utc,),
    ).fetchall()


def get_fixture(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """Fetch one fixture row."""
    return conn.execute("SELECT * FROM fixtures WHERE id = ?", (fixture_id,)).fetchone()


def joined_fixtures_in_window(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[sqlite3.Row]:
    """
    已 join 且开赛时间落在 [start, end] 的场次（closing 前置检查，票 37）。

    join 判定仍是 odds_api 原口径（本查询的消费者 oddsapi 锚拉/closing
    前置都是 odds_api 原生面）；广义 joined（odds_api ∪ srct 链）候选面
    用 fixtures_in_window + mapping 判定。
    """
    return conn.execute(
        """
        SELECT * FROM fixtures
        WHERE odds_api_event_id IS NOT NULL
          AND kickoff_utc >= ? AND kickoff_utc <= ?
        ORDER BY kickoff_utc
        """,
        (start_utc, end_utc),
    ).fetchall()


def fixtures_in_window(
    conn: sqlite3.Connection, start_utc: str, end_utc: str
) -> list[sqlite3.Row]:
    """开赛时间落在 [start, end] 的全部场次（join 判定归调用方）。"""
    return conn.execute(
        """
        SELECT * FROM fixtures
        WHERE kickoff_utc >= ? AND kickoff_utc <= ?
        ORDER BY kickoff_utc
        """,
        (start_utc, end_utc),
    ).fetchall()


def kickoffs_for_fixtures(
    conn: sqlite3.Connection, fixture_ids: list[int]
) -> dict[int, str]:
    """Bulk fetch kickoff_utc keyed by fixture id（复盘前瞻资格用，票 36）。"""
    if not fixture_ids:
        return {}
    placeholders = ", ".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"SELECT id, kickoff_utc FROM fixtures WHERE id IN ({placeholders})",  # noqa: S608
        fixture_ids,
    ).fetchall()
    return {int(row["id"]): str(row["kickoff_utc"]) for row in rows}


def fixture_id_for_match_code(conn: sqlite3.Connection, code: str) -> int | None:
    """竞彩编号（周日002）→ 最新 fixture_id（无该编号 None；票 09 新浪源映射）。"""
    row = conn.execute(
        """
        SELECT fixture_id FROM match_codes
        WHERE kind = 'jingcai' AND code = ?
        ORDER BY id DESC LIMIT 1
        """,
        (code,),
    ).fetchone()
    return int(row["fixture_id"]) if row else None


def fixtures_with_teams(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    全部场次 + 主客队 id/名称 + 联赛（票 77 映射同步的左侧面）。

    ``business_date`` = 最新竞彩业务日（无码场 NULL；多码取 MAX——改期
    重挂以末次销售日为准）。live 映射范围门消费（odds-read-switch 票 01）。
    """
    return conn.execute(
        """
        SELECT f.id AS fixture_id, f.kickoff_utc,
               f.home_team_id, f.away_team_id,
               th.canonical_name AS home_name,
               ta.canonical_name AS away_name,
               c.name AS competition_name, c.tier AS competition_tier,
               (SELECT MAX(mc.business_date) FROM match_codes mc
                WHERE mc.fixture_id = f.id AND mc.kind = 'jingcai'
               ) AS business_date
        FROM fixtures f
        JOIN teams th ON th.id = f.home_team_id
        JOIN teams ta ON ta.id = f.away_team_id
        JOIN competitions c ON c.id = f.competition_id
        ORDER BY f.id
        """
    ).fetchall()


def fixture_team_info(conn: sqlite3.Connection, fixture_id: int) -> sqlite3.Row | None:
    """Fixture → 主客队 id/名称与联赛标识（票 09 情报采集桥接用）。"""
    return conn.execute(
        """
        SELECT f.id AS fixture_id, f.kickoff_utc,
               f.home_team_id, f.away_team_id,
               th.canonical_name AS home_name,
               ta.canonical_name AS away_name,
               c.name AS competition_name, c.tier AS competition_tier,
               c.odds_api_sport_key AS sport_key
        FROM fixtures f
        JOIN teams th ON th.id = f.home_team_id
        JOIN teams ta ON ta.id = f.away_team_id
        JOIN competitions c ON c.id = f.competition_id
        WHERE f.id = ?
        """,
        (fixture_id,),
    ).fetchone()


def sale_stop_probe_candidates(
    conn: sqlite3.Connection, now_utc: str, horizon_utc: str
) -> list[sqlite3.Row]:
    """
    停售探测候选（票 47 双锚）：已 join、未来开球、最新销售态非 stopped。

    粗筛交给 SQL（kickoff ∈ (now, horizon]）；「临场 3h 内 或 北京 ≥19 点
    的次日内场次」细分在调用方（探测两形态：晚间场临场停售 / 凌晨场前夜
    墙钟停售）。返回行带 latest_state 供调用方过滤。
    """
    return conn.execute(
        """
        SELECT f.id, f.kickoff_utc, f.odds_api_event_id, f.odds_api_sport_key,
               COALESCE(
                   (SELECT ss.sale_state FROM sale_statuses ss
                    WHERE ss.fixture_id = f.id
                    ORDER BY ss.observed_at DESC, ss.id DESC LIMIT 1),
                   'unknown') AS latest_state
        FROM fixtures f
        WHERE f.odds_api_event_id IS NOT NULL AND f.odds_api_sport_key IS NOT NULL
          AND f.kickoff_utc > ? AND f.kickoff_utc <= ?
        ORDER BY f.kickoff_utc
        """,
        (now_utc, horizon_utc),
    ).fetchall()
