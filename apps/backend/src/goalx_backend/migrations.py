"""SQLite schema 迁移：v1 建 CONTEXT.md 六域全部实体表并播种参考数据。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from goalx_backend.markets import CRS_EXACT_SCORES

MigrationFn = Callable[[sqlite3.Connection], None]

_V1_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS competitions (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        tier TEXT NOT NULL DEFAULT 'tier2'
            CHECK (tier IN ('tier1', 'tier2', 'excluded')),
        odds_api_sport_key TEXT,
        api_football_league_id INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS teams (
        id INTEGER PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_aliases (
        id INTEGER PRIMARY KEY,
        team_id INTEGER NOT NULL REFERENCES teams(id),
        source TEXT NOT NULL,
        alias TEXT NOT NULL,
        UNIQUE (source, alias)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fixtures (
        id INTEGER PRIMARY KEY,
        competition_id INTEGER NOT NULL REFERENCES competitions(id),
        kickoff_utc TEXT NOT NULL,
        home_team_id INTEGER NOT NULL REFERENCES teams(id),
        away_team_id INTEGER NOT NULL REFERENCES teams(id),
        stage TEXT,
        odds_api_event_id TEXT,
        odds_api_sport_key TEXT,
        join_method TEXT
            CHECK (join_method IN ('time_window', 'manual') OR join_method IS NULL),
        joined_at TEXT,
        UNIQUE (competition_id, kickoff_utc, home_team_id, away_team_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff ON fixtures(kickoff_utc)",
    """
    CREATE TABLE IF NOT EXISTS match_codes (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        kind TEXT NOT NULL CHECK (kind IN ('jingcai', 'pool')),
        business_date TEXT NOT NULL,
        code TEXT NOT NULL,
        source_match_id TEXT,
        is_single INTEGER,
        UNIQUE (kind, business_date, code)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_match_codes_fixture ON match_codes(fixture_id)",
    """
    CREATE TABLE IF NOT EXISTS markets (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('fixed', 'pool'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS selections (
        id INTEGER PRIMARY KEY,
        market_code TEXT NOT NULL REFERENCES markets(code),
        code TEXT NOT NULL,
        label TEXT NOT NULL,
        UNIQUE (market_code, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        source TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'live_capture'
            CHECK (purpose IN ('live_capture', 'closing', 'backtest')),
        odds REAL NOT NULL CHECK (odds > 0),
        captured_at TEXT NOT NULL,
        meta TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (fixture_id, market_code, selection_code, source, captured_at, odds),
        FOREIGN KEY (market_code, selection_code)
            REFERENCES selections(market_code, code)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
        ON odds_snapshots(fixture_id, market_code, source, captured_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_periods (
        id INTEGER PRIMARY KEY,
        market_code TEXT NOT NULL REFERENCES markets(code),
        period_no TEXT NOT NULL,
        sales_deadline TEXT,
        UNIQUE (market_code, period_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_states (
        pool_period_id INTEGER PRIMARY KEY REFERENCES pool_periods(id),
        sales_amount REAL,
        rollover_in REAL,
        prize_tiers TEXT,
        published_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public_shares (
        id INTEGER PRIMARY KEY,
        pool_period_id INTEGER NOT NULL REFERENCES pool_periods(id),
        match_seq INTEGER NOT NULL,
        selection_code TEXT NOT NULL,
        share REAL NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('estimated', 'published')),
        source TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        UNIQUE (pool_period_id, match_seq, selection_code, origin, source, captured_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forecasts (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        track TEXT NOT NULL CHECK (track IN ('ml', 'llm', 'fused')),
        model_version TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE (fixture_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS match_intels (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        agent TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS divergences (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        metric TEXT NOT NULL,
        reference TEXT NOT NULL,
        value REAL NOT NULL,
        computed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ev_assessments (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER REFERENCES fixtures(id),
        pool_period_id INTEGER REFERENCES pool_periods(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        ev REAL NOT NULL,
        ci_low REAL,
        ci_high REAL,
        cost_adjusted_ev REAL,
        kelly_fraction REAL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bet_slips (
        id INTEGER PRIMARY KEY,
        mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
        source TEXT NOT NULL DEFAULT 'manual',
        pool_period_id INTEGER REFERENCES pool_periods(id),
        placed_at TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER REFERENCES bet_slips(id),
        mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
        market_kind TEXT NOT NULL CHECK (market_kind IN ('fixed', 'pool')),
        purchased INTEGER NOT NULL DEFAULT 0,
        stake REAL NOT NULL CHECK (stake >= 0),
        placed_at TEXT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'won', 'lost', 'void', 'partial')),
        payout REAL,
        profit REAL,
        settled_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bet_legs (
        id INTEGER PRIMARY KEY,
        bet_id INTEGER NOT NULL REFERENCES bets(id),
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        locked_odds REAL NOT NULL CHECK (locked_odds > 0),
        snapshot_id INTEGER REFERENCES odds_snapshots(id),
        meta TEXT,
        FOREIGN KEY (market_code, selection_code)
            REFERENCES selections(market_code, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pool_picks (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER NOT NULL REFERENCES bet_slips(id),
        match_seq INTEGER NOT NULL,
        fixture_id INTEGER REFERENCES fixtures(id),
        selection_code TEXT NOT NULL,
        UNIQUE (slip_id, match_seq, selection_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS combinations (
        id INTEGER PRIMARY KEY,
        slip_id INTEGER NOT NULL REFERENCES bet_slips(id),
        seq INTEGER NOT NULL,
        stake REAL NOT NULL,
        selections TEXT NOT NULL,
        hit INTEGER,
        payout REAL,
        UNIQUE (slip_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cost_ledger (
        id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        category TEXT NOT NULL,
        units REAL NOT NULL DEFAULT 1,
        amount_cny REAL NOT NULL DEFAULT 0,
        note TEXT,
        meta TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS draw_results (
        id INTEGER PRIMARY KEY,
        fixture_id INTEGER NOT NULL UNIQUE REFERENCES fixtures(id),
        home_goals INTEGER NOT NULL CHECK (home_goals >= 0),
        away_goals INTEGER NOT NULL CHECK (away_goals >= 0),
        half_home_goals INTEGER,
        half_away_goals INTEGER,
        void INTEGER NOT NULL DEFAULT 0,
        void_reason TEXT,
        source TEXT NOT NULL,
        published_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS settlements (
        id INTEGER PRIMARY KEY,
        bet_id INTEGER UNIQUE REFERENCES bets(id),
        slip_id INTEGER UNIQUE REFERENCES bet_slips(id),
        status TEXT NOT NULL CHECK (status IN ('won', 'lost', 'void', 'partial')),
        stake REAL NOT NULL,
        payout REAL NOT NULL,
        profit REAL NOT NULL,
        detail TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        CHECK ((bet_id IS NULL) <> (slip_id IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bankroll_events (
        id INTEGER PRIMARY KEY,
        occurred_at TEXT NOT NULL,
        kind TEXT NOT NULL
            CHECK (kind IN ('deposit', 'withdraw', 'bet_stake', 'bet_payout', 'cost')),
        amount_cny REAL NOT NULL,
        balance_after REAL NOT NULL,
        bet_id INTEGER REFERENCES bets(id),
        slip_id INTEGER REFERENCES bet_slips(id),
        note TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hist_matches (
        id INTEGER PRIMARY KEY,
        competition TEXT NOT NULL,
        season TEXT NOT NULL,
        match_date TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        fthg INTEGER NOT NULL,
        ftag INTEGER NOT NULL,
        ftr TEXT NOT NULL CHECK (ftr IN ('H', 'D', 'A')),
        psc_home REAL,
        psc_draw REAL,
        psc_away REAL,
        avgc_home REAL,
        avgc_draw REAL,
        avgc_away REAL,
        UNIQUE (competition, season, match_date, home_team, away_team)
    )
    """,
)


def _selection_rows() -> list[tuple[str, str, str]]:
    """Build the reference selection seed (market_code, code, label)."""
    rows: list[tuple[str, str, str]] = [
        ("had", "h", "主胜"),
        ("had", "d", "平"),
        ("had", "a", "客胜"),
        ("hhad", "h", "让球主胜"),
        ("hhad", "d", "让球平"),
        ("hhad", "a", "让球客胜"),
    ]
    crs_rows: list[tuple[str, str, str]] = [
        ("crs", f"{home}:{away}", f"比分 {home}:{away}")
        for home, away in CRS_EXACT_SCORES
    ] + [
        ("crs", "h_other", "胜其他"),
        ("crs", "d_other", "平其他"),
        ("crs", "a_other", "负其他"),
    ]
    rows += crs_rows
    ttg_labels = ["0 球", "1 球", "2 球", "3 球", "4 球", "5 球", "6 球", "7+ 球"]
    rows += [("ttg", str(n), label) for n, label in enumerate(ttg_labels)]
    hafu_rows = [
        ("hafu", code, label)
        for code, label in {
            "hh": "胜胜",
            "hd": "胜平",
            "ha": "胜负",
            "dh": "平胜",
            "dd": "平平",
            "da": "平负",
            "ah": "负胜",
            "ad": "负平",
            "aa": "负负",
        }.items()
    ]
    rows += hafu_rows
    for market in ("ttt14", "pick9"):
        rows += [
            (market, "3", "胜"),
            (market, "1", "平"),
            (market, "0", "负"),
        ]
    rows += [("goals4", code, label) for _, code, label in crs_rows]
    rows += [("htft6", code, label) for _, code, label in hafu_rows]
    return rows


def _apply_v1(conn: sqlite3.Connection) -> None:
    """Create the v1 domain schema and seed reference data."""
    for statement in _V1_STATEMENTS:
        conn.execute(statement)
    conn.executemany(
        "INSERT OR IGNORE INTO markets (code, name, kind) VALUES (?, ?, ?)",
        [
            ("had", "胜平负", "fixed"),
            ("hhad", "让球胜平负", "fixed"),
            ("crs", "比分", "fixed"),
            ("ttg", "总进球", "fixed"),
            ("hafu", "半全场", "fixed"),
            ("ttt14", "胜负彩十四场", "pool"),
            ("pick9", "任选九", "pool"),
            ("goals4", "四场进球", "pool"),
            ("htft6", "六场半全场", "pool"),
        ],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO selections (market_code, code, label) VALUES (?, ?, ?)",
        _selection_rows(),
    )


_APPEND_ONLY_TABLES = ("odds_snapshots", "forecasts")


def _apply_v2(conn: sqlite3.Connection) -> None:
    """v2：append-only 触发器（ADR 0001）——禁止修改/删除时点快照。"""
    for table in _APPEND_ONLY_TABLES:
        for action in ("UPDATE", "DELETE"):
            sql = (
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                f"BEFORE {action} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
            conn.execute(sql)


_V3_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL,
        params TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'done', 'failed')),
        created_at TEXT NOT NULL,
        finished_at TEXT,
        summary TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_predictions (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
        hist_match_id INTEGER NOT NULL REFERENCES hist_matches(id),
        competition TEXT NOT NULL,
        season TEXT NOT NULL,
        match_date TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        had_probs TEXT NOT NULL,
        fair_probs TEXT NOT NULL,
        fair_source TEXT NOT NULL CHECK (fair_source IN ('psc', 'avgc')),
        model_fingerprint TEXT NOT NULL,
        train_window_end TEXT NOT NULL,
        UNIQUE (run_id, hist_match_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_bets (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
        kind TEXT NOT NULL CHECK (kind IN ('single', 'parlay2')),
        competition TEXT NOT NULL,
        placed_week TEXT NOT NULL,
        stake REAL NOT NULL,
        legs TEXT NOT NULL,
        ev REAL NOT NULL,
        kelly REAL,
        status TEXT NOT NULL CHECK (status IN ('won', 'lost', 'void')),
        payout REAL NOT NULL,
        profit REAL NOT NULL,
        detail TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_backtest_bets_run ON backtest_bets(run_id)",
    """
    CREATE TABLE IF NOT EXISTS backtest_metrics (
        id INTEGER PRIMARY KEY,
        run_id INTEGER NOT NULL REFERENCES backtest_runs(id),
        scope TEXT NOT NULL,
        metrics TEXT NOT NULL,
        UNIQUE (run_id, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS haircut_calibrations (
        id INTEGER PRIMARY KEY,
        scope TEXT NOT NULL,
        market_code TEXT NOT NULL,
        haircut REAL NOT NULL,
        n_samples INTEGER NOT NULL,
        quartiles TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('calibrated', 'default')),
        computed_at TEXT NOT NULL,
        UNIQUE (scope, market_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clv_records (
        id INTEGER PRIMARY KEY,
        bet_id INTEGER NOT NULL REFERENCES bets(id),
        fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
        market_code TEXT NOT NULL,
        selection_code TEXT NOT NULL,
        taken_odds REAL NOT NULL,
        close_prob REAL NOT NULL,
        clv_prob REAL NOT NULL,
        close_source TEXT NOT NULL
            CHECK (close_source IN ('odds_api_closing', 'fd_psc')),
        minutes_to_kickoff REAL,
        computed_at TEXT NOT NULL,
        UNIQUE (bet_id, fixture_id)
    )
    """,
)


def _apply_v3(conn: sqlite3.Connection) -> None:
    """v3：M2 回测/指标/haircut 校准/CLV 表（票 28-32）。"""
    for statement in _V3_STATEMENTS:
        conn.execute(statement)


def _apply_v4(conn: sqlite3.Connection) -> None:
    """Append-only correction evidence; existing results and ledger stay unchanged."""
    conn.execute("""
        CREATE TABLE draw_result_revisions (
            id INTEGER PRIMARY KEY,
            fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
            previous TEXT NOT NULL,
            replacement TEXT NOT NULL,
            reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
            recorded_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE settlement_revisions (
            id INTEGER PRIMARY KEY,
            settlement_id INTEGER NOT NULL REFERENCES settlements(id),
            previous TEXT,
            replacement TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )
    """)
    for table in ("draw_result_revisions", "settlement_revisions"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""
                CREATE TRIGGER {table}_no_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, 'revision history is append-only'); END
            """)


def _apply_v5(conn: sqlite3.Connection) -> None:
    """
    v5（票 35）：报价观测证据层——时间语义、原始证据与销售状态。

    - quote_observations：一次 HTTP 观测的脱敏原始证据（哈希/文件引用、
      解析版本、observed_at 本机收到时间、可空的源更新/历史快照时间）。
    - odds_snapshots 增列 observed_at/source_updated_at/observation_id；
      旧行保持 NULL（时间语义按源解释，未知不倒填）。
    - sale_statuses：国内销售状态与 had 单固资格（append-only 时序）。
    """
    conn.execute(
        """
        CREATE TABLE quote_observations (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            purpose TEXT NOT NULL DEFAULT 'live'
                CHECK (purpose IN ('live', 'historical')),
            observed_at TEXT NOT NULL,
            source_updated_at TEXT,
            snapshot_at TEXT,
            endpoint TEXT,
            parse_version TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            raw_ref TEXT,
            summary TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_source_time
            ON quote_observations(source, observed_at)
        """
    )
    conn.execute("ALTER TABLE odds_snapshots ADD COLUMN observed_at TEXT")
    conn.execute("ALTER TABLE odds_snapshots ADD COLUMN source_updated_at TEXT")
    conn.execute(
        """
        ALTER TABLE odds_snapshots ADD COLUMN observation_id INTEGER
            REFERENCES quote_observations(id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_observation
            ON odds_snapshots(observation_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE sale_statuses (
            id INTEGER PRIMARY KEY,
            fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
            market_code TEXT,
            sale_state TEXT NOT NULL
                CHECK (sale_state IN ('on_sale', 'stopped', 'unknown')),
            single_eligible INTEGER,
            observed_at TEXT NOT NULL,
            source_updated_at TEXT,
            observation_id INTEGER REFERENCES quote_observations(id),
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sale_statuses_lookup
            ON sale_statuses(fixture_id, market_code, observed_at)
        """
    )
    for table in ("quote_observations", "sale_statuses"):
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f"""
                CREATE TRIGGER {table}_no_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END
            """)
    conn.execute(
        """
        ALTER TABLE haircut_calibrations ADD COLUMN n_fixtures INTEGER
            NOT NULL DEFAULT 0
        """
    )
    conn.execute(
        """
        ALTER TABLE haircut_calibrations ADD COLUMN method_version TEXT
            NOT NULL DEFAULT 'shin_mean_v0'
        """
    )


def _apply_v6(conn: sqlite3.Connection) -> None:
    """
    v6（票 36）：建议与实际执行条款分离——结算用实际条款，原建议不覆盖。

    - bets.strategy_version：建注时声明的策略/方法版本（paper 锁定语义）。
    - bets.actual_stake / bet_legs.actual_odds：真实回录的实际金额/赔率；
      NULL 表示按建议条款（paper 锁定即建议条款）。
    """
    conn.execute("ALTER TABLE bets ADD COLUMN strategy_version TEXT")
    conn.execute("ALTER TABLE bets ADD COLUMN actual_stake REAL")
    conn.execute("ALTER TABLE bet_legs ADD COLUMN actual_odds REAL")


def _apply_v7(conn: sqlite3.Connection) -> None:
    """
    v7（票 40）：CLV 基准分层——clv_records 增 close_basis 标注。

    新行写 'pinnacle' | 'betfair_ex' | 'consensus'（分层取锚级别，见
    evaluation/clv.py）；存量行保持 NULL，报表层解释为 'legacy'（分层前
    共识口径，历史行不重算——新旧口径窗口期并行呈现）。
    """
    conn.execute("ALTER TABLE clv_records ADD COLUMN close_basis TEXT")


def _apply_v8(conn: sqlite3.Connection) -> None:
    """
    v8（票 41）：注级 EV 概率快照——建注锁定时刻双口径概率与 EV。

    bets.snap_prob_consensus / snap_ev_consensus：欧共识口径（多 book 完整
    三向共识 Shin）；snap_prob_model / snap_ev_model：模型口径（锁定时点
    已发出的最新赛前 Forecast 的 DC 概率）。任一口径不可得存 NULL；
    存量注与 v1 可映射口径之外的腿（非 had）无快照（NULL）。
    """
    conn.execute("ALTER TABLE bets ADD COLUMN snap_prob_consensus REAL")
    conn.execute("ALTER TABLE bets ADD COLUMN snap_prob_model REAL")
    conn.execute("ALTER TABLE bets ADD COLUMN snap_ev_consensus REAL")
    conn.execute("ALTER TABLE bets ADD COLUMN snap_ev_model REAL")


def _apply_v9(conn: sqlite3.Connection) -> None:
    """
    v9（票 42）：赛果自动同步元信息——draw_sync_runs。

    一次同步一行（append-only）：来源/时点（observed_at=收到页面响应的
    本机时间）/业务日集合/页面与场次计数/待人工清单（JSON：无效场次、对不上、
    与库内不一致等 fail-closed 场次）。最新行即"上次同步"状态；同步结果
    本体仍在 draw_results（source 用 caiguo.SOURCE 常量标记）。
    """
    conn.execute(
        """
        CREATE TABLE draw_sync_runs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            business_dates TEXT NOT NULL,
            pages INTEGER NOT NULL,
            fetched INTEGER NOT NULL,
            imported INTEGER NOT NULL,
            unchanged INTEGER NOT NULL,
            unmatched INTEGER NOT NULL,
            pending_manual TEXT NOT NULL,
            parse_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_draw_sync_runs_time
            ON draw_sync_runs(observed_at)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER draw_sync_runs_no_update
            BEFORE UPDATE ON draw_sync_runs
            BEGIN SELECT RAISE(ABORT, 'draw_sync_runs is append-only'); END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER draw_sync_runs_no_delete
            BEFORE DELETE ON draw_sync_runs
            BEGIN SELECT RAISE(ABORT, 'draw_sync_runs is append-only'); END
        """
    )


def _apply_v10(conn: sqlite3.Connection) -> None:
    """
    v10（票 43）：传统足彩彩池数据——pool_matches + pool_sync_runs。

    - pool_matches：一个期次一行的 14 场对阵（场序/联赛/开赛时间/主客队/
      期次页三向"99 家平均欧指"/源内部场次 id），随同步刷新当前态；
      历史欧赔时序不在本表（欧赔时序归 odds_snapshots 域）。
    - pool_sync_runs：一次同步一行（append-only）：来源/时点/期次集合/
      计数/分布缺失场次。最新行即"上次同步"状态。
      v9 留给并行的 feat/draw-sync 分支（draw_sync_runs）——两分支独立
      建表互不依赖，先后合并皆可（迁移按号跳过已应用版本，空洞无害）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pool_matches (
            id INTEGER PRIMARY KEY,
            pool_period_id INTEGER NOT NULL REFERENCES pool_periods(id),
            match_seq INTEGER NOT NULL,
            source_match_id TEXT,
            league TEXT,
            kickoff_utc TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            euro_odds_h REAL,
            euro_odds_d REAL,
            euro_odds_a REAL,
            UNIQUE (pool_period_id, match_seq)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_matches ON pool_matches(pool_period_id)"
    )
    # 份额快照附票数等量级参考（v1 建表无此列，源B number 口径实证可得）
    conn.execute("ALTER TABLE public_shares ADD COLUMN meta TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pool_sync_runs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            period_nos TEXT NOT NULL,
            pages INTEGER NOT NULL,
            matches INTEGER NOT NULL,
            share_rows INTEGER NOT NULL,
            missing_shares INTEGER NOT NULL,
            parse_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pool_sync_runs_time
            ON pool_sync_runs(observed_at)
        """
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""
            CREATE TRIGGER pool_sync_runs_no_{operation.lower()}
            BEFORE {operation} ON pool_sync_runs
            BEGIN SELECT RAISE(ABORT, 'pool_sync_runs is append-only'); END
        """)


def _apply_v11(conn: sqlite3.Connection) -> None:
    """
    v11（票 09）：LLM 线情报存证——intel_observations；废弃 match_intels。

    - intel_observations：情报证据层工件（append-only + 哈希）：一场一条
      采集记录，携带来源/采集时点/采集器/原始素材 JSON 与其 sha256。
      唯一键 (fixture_id, collector, raw_hash) 天然幂等——重复采集同内容
      零新行。SQL 归 llm 域（ADR-0008，OWNERS 登记）。
    - match_intels：v1 空脚手架（无来源/哈希列，无业务代码引用）——
      票 03 废弃定案，DROP。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS intel_observations (
            id INTEGER PRIMARY KEY,
            fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            source TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            collector TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            raw_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (fixture_id, collector, raw_hash)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_intel_fixture ON intel_observations(fixture_id)"
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""
            CREATE TRIGGER intel_observations_no_{operation.lower()}
            BEFORE {operation} ON intel_observations
            BEGIN SELECT RAISE(ABORT, 'intel_observations is append-only'); END
        """)
    conn.execute("DROP TABLE IF EXISTS match_intels")


def _apply_v12(conn: sqlite3.Connection) -> None:
    """
    v12（票 11）：复核队列 review_items（llm 域）。

    赛前（gate JS>0.06 Tier1）与赛后（结算一对一错，票 13）双路入队；
    结论三分类（情报关键贡献/无关/误导）只进评测集，不改预测工件
    （票 05 冻结）。UNIQUE(fixture_id, route) 幂等入队。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_items (
            id INTEGER PRIMARY KEY,
            fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
            route TEXT NOT NULL CHECK (route IN ('pre_match', 'post_settle')),
            js_value REAL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
            verdict TEXT
                CHECK (verdict IN ('key_contribution', 'irrelevant', 'misleading')
                       OR verdict IS NULL),
            note TEXT,
            created_at TEXT NOT NULL,
            decided_at TEXT,
            UNIQUE (fixture_id, route)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_open ON review_items(status, route)"
    )


def _apply_v13(conn: sqlite3.Connection) -> None:
    """
    v13（票 13）：盲评记录 blind_reviews（llm 域）。

    双周匿名二选一（ML 摘要 vs 证据卡）结果落评测集——参考列，
    不作任何档证明支柱（票 05 冻结）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blind_reviews (
            id INTEGER PRIMARY KEY,
            cycle TEXT NOT NULL,
            fixture_id INTEGER NOT NULL REFERENCES fixtures(id),
            choice TEXT NOT NULL CHECK (choice IN ('ml', 'llm')),
            note TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (cycle, fixture_id)
        )
        """
    )


def _apply_v14(conn: sqlite3.Connection) -> None:
    """
    v14（票 44）：官方赛果事实源观测 + 对账 + 覆盖维表（data 域，ADR-0008）。

    - uniform_result_observations：源A uniform 族赛果观测（append-only +
      UNIQUE(match_id, observed_at) 幂等）。观测是事实导入的依据与证据；
      同 match 多次观测自然留痕（poolStatus 空 → Payout 的状态迁移）。
      2026-09-20 用户裁决直接切换：官方终态观测经 ingest/uniform 落
      draw_results 事实（源D 降审计源）。
    - draw_reconciliation_runs：每次对账一行（append-only）：参照源
      （sporttery.cn / openfootball）vs draw_results 的一致率与待人工清单。
    - source_coverage（定则 4）：每源每覆盖日"看到了什么"的现态维表
      （UPSERT，非证据表——证据在观测/run 表）。coverage_date 语义随源
      而定（uniform=matchDate、源D=业务日、openfootball=赛季键）；
      absent 断言仅当 coverage_status='covered' 且采集成功——空≠无
      （fetched_empty=采集成功无场次；fetch_failed=暂时失败可重试；
      not_covered=源声明无此覆盖，如 openfootball 无 2026-27 文件）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS uniform_result_observations (
            id INTEGER PRIMARY KEY,
            match_id INTEGER NOT NULL,
            match_num_str TEXT NOT NULL,
            match_date TEXT NOT NULL,
            business_date TEXT,
            fixture_id INTEGER REFERENCES fixtures(id),
            league_id INTEGER,
            league_name TEXT,
            result_status TEXT NOT NULL,
            pool_status TEXT,
            full_score_raw TEXT,
            half_score_raw TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            half_home_goals INTEGER,
            half_away_goals INTEGER,
            win_flag TEXT,
            odds_h TEXT,
            odds_d TEXT,
            odds_a TEXT,
            goal_line TEXT,
            betting_single INTEGER,
            void_flag INTEGER NOT NULL DEFAULT 0,
            void_reason TEXT,
            observed_at TEXT NOT NULL,
            parse_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (match_id, observed_at)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_uniform_obs_fixture
            ON uniform_result_observations(fixture_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_uniform_obs_match_date
            ON uniform_result_observations(match_date)
        """
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""
            CREATE TRIGGER uniform_result_observations_no_{operation.lower()}
            BEFORE {operation} ON uniform_result_observations
            BEGIN SELECT RAISE(ABORT, 'uniform_result_observations is append-only'); END
        """)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS draw_reconciliation_runs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            business_dates TEXT NOT NULL,
            compared INTEGER NOT NULL DEFAULT 0,
            consistent INTEGER NOT NULL DEFAULT 0,
            score_mismatch INTEGER NOT NULL DEFAULT 0,
            void_mismatch INTEGER NOT NULL DEFAULT 0,
            missing_result INTEGER NOT NULL DEFAULT 0,
            unmatched INTEGER NOT NULL DEFAULT 0,
            pending_manual TEXT NOT NULL,
            parse_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""
            CREATE TRIGGER draw_reconciliation_runs_no_{operation.lower()}
            BEFORE {operation} ON draw_reconciliation_runs
            BEGIN SELECT RAISE(ABORT, 'draw_reconciliation_runs is append-only'); END
        """)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_coverage (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            coverage_date TEXT NOT NULL,
            league_key TEXT NOT NULL DEFAULT '',
            league_name TEXT,
            match_count INTEGER NOT NULL DEFAULT 0,
            coverage_status TEXT NOT NULL
                CHECK (
                    coverage_status IN
                        ('covered', 'fetched_empty', 'fetch_failed', 'not_covered')
                ),
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source, coverage_date, league_key)
        )
        """
    )


def _apply_v15(conn: sqlite3.Connection) -> None:
    """
    v15（票 45）：Understat xG 特征层（data 域，SQL 归 data/ingest/understat.py）。

    - understat_matches：一个 understat 场次一行的特征现态表（按源 match_id
      UPSERT，非证据表——证据在 sync run 行与 coverage）。三时间口径（定则 2）：
      event_time=datetime_utc（开球）、published_at=源不提供（无列，不伪造）、
      observed_at=本机收到该状态的时间（first_seen_at 保留首次观测）。
      防前视红线（定则 2）：prior_* 列为本季**开球日严格早于本场**的已完场
      累计 npxG/npxGA——getLeagueData 赛后滚动更新，同日场次互不可见
      （同日错峰早场的最终 npxG 不得泄入晚场，保守按日截断）。
      fixture_id 为竞彩场次确定性 join（开球日 ±1 + 双方队名解析唯一命中才落，
      否则 NULL，禁模糊合并）。
    - understat_sync_runs：一次同步一行（append-only）：拉取的联赛×赛季、
      计数与 join 计数。最新行即"上次同步"状态。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS understat_matches (
            id INTEGER PRIMARY KEY,
            match_id TEXT NOT NULL UNIQUE,
            league TEXT NOT NULL,
            season TEXT NOT NULL,
            datetime_utc TEXT NOT NULL,
            home_team_id TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team_id TEXT NOT NULL,
            away_team TEXT NOT NULL,
            is_result INTEGER NOT NULL DEFAULT 0,
            goals_home INTEGER,
            goals_away INTEGER,
            xg_home REAL,
            xg_away REAL,
            npxg_home REAL,
            npxg_away REAL,
            prior_npxg_home REAL,
            prior_npxga_home REAL,
            prior_matches_home INTEGER,
            prior_npxg_away REAL,
            prior_npxga_away REAL,
            prior_matches_away INTEGER,
            forecast_w REAL,
            forecast_d REAL,
            forecast_l REAL,
            fixture_id INTEGER REFERENCES fixtures(id),
            first_seen_at TEXT NOT NULL,
            observed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_understat_matches_lookup
            ON understat_matches(league, season, datetime_utc)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS understat_sync_runs (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            seasons TEXT NOT NULL,
            matches INTEGER NOT NULL,
            results INTEGER NOT NULL,
            joined INTEGER NOT NULL,
            unmatched INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            parse_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_understat_sync_runs_time
            ON understat_sync_runs(observed_at)
        """
    )
    for operation in ("UPDATE", "DELETE"):
        conn.execute(f"""
            CREATE TRIGGER understat_sync_runs_no_{operation.lower()}
            BEFORE {operation} ON understat_sync_runs
            BEGIN SELECT RAISE(ABORT, 'understat_sync_runs is append-only'); END
        """)


def _apply_v16(conn: sqlite3.Connection) -> None:
    """
    v16（票 48）：DROP ev_assessments——自建库起无写入方的脚手架表。

    陈盘信号等派生读模型不落表：append-only 原料（odds_snapshots）+
    as-of 纯函数重放即决策存证（五定则：程序计算、重放确定）。
    EVAssessment 实际口径在视图与 betting 包函数（表 03 预留时已注记）。
    """
    conn.execute("DROP TABLE IF EXISTS ev_assessments")


def _apply_v17(conn: sqlite3.Connection) -> None:
    """
    v17（票 50）：fixtures 加 PropLine join 列——第二欧赔源的独立映射。

    PropLine event id 与 The Odds API 不同命名空间，互备/交叉验证要求
    两列各自独立可空（一场竞彩可同时 join 两个源）。
    """
    conn.execute("ALTER TABLE fixtures ADD COLUMN propline_event_id TEXT")
    conn.execute("ALTER TABLE fixtures ADD COLUMN propline_sport_key TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fixtures_propline_event
        ON fixtures(propline_event_id)
        """
    )


MIGRATIONS: tuple[tuple[int, MigrationFn], ...] = (
    (1, _apply_v1),
    (2, _apply_v2),
    (3, _apply_v3),
    (4, _apply_v4),
    (5, _apply_v5),
    (6, _apply_v6),
    (7, _apply_v7),
    (8, _apply_v8),
    (9, _apply_v9),
    (10, _apply_v10),
    (11, _apply_v11),
    (12, _apply_v12),
    (13, _apply_v13),
    (14, _apply_v14),
    (15, _apply_v15),
    (16, _apply_v16),
    (17, _apply_v17),
)
