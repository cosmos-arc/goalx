"""db.connect/migrate 行为测试。"""

from __future__ import annotations

import sqlite3

import pytest

from goalx_backend import db, migrations


def test_migrate_applies_v1_and_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.current_version(conn) == 0
    # v7..v26 = 票 40/41/42/43/09/11/13/44/45/48/50 + A4 排除表 + 票 13 换键
    # + caliber-loop 票 04 机会漏斗表
    assert db.migrate(conn) == 54
    assert db.migrate(conn) == 54  # 重跑幂等
    # v16（票 48）：ev_assessments 脚手架 DROP
    dropped = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "ev_assessments" not in dropped

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "backtest_runs",
        "backtest_predictions",
        "backtest_bets",
        "backtest_metrics",
        "haircut_calibrations",
        "clv_records",
        "quote_observations",
        "sale_statuses",
        # v10（票 43）：彩池期次对阵与同步元信息
        "pool_matches",
        "pool_sync_runs",
        # v15（票 45）：Understat xG 特征层
        "understat_matches",
        "understat_sync_runs",
        # v18（票 49 采集先行）：源B变化时序
        "srcb_change_rows",
        "srcb_change_runs",
    } <= tables
    snapshot_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(odds_snapshots)")
    }
    assert {"observed_at", "source_updated_at", "observation_id"} <= snapshot_cols
    # v7（票 40）：CLV 基准分层标注列
    clv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(clv_records)")}
    assert "close_basis" in clv_cols
    # 六域核心表（票 18 验收：全部实体建表）
    expected = {
        "competitions",
        "teams",
        "team_aliases",
        "fixtures",
        "match_codes",
        "markets",
        "selections",
        "odds_snapshots",
        "pool_periods",
        "pool_states",
        "public_shares",
        "forecasts",
        "intel_observations",
        "divergences",
        "bet_slips",
        "bets",
        "bet_legs",
        "pool_picks",
        "combinations",
        "cost_ledger",
        "draw_results",
        "settlements",
        "bankroll_events",
        "hist_matches",
    }
    assert expected <= tables

    markets = {row["code"] for row in conn.execute("SELECT code FROM markets")}
    assert markets == {
        "had",
        "hhad",
        "crs",
        "ttg",
        "hafu",
        "ttt14",
        "pick9",
        "goals4",
        "htft6",
    }
    crs = conn.execute(
        "SELECT COUNT(*) AS n FROM selections WHERE market_code = 'crs'"
    ).fetchone()["n"]
    assert crs == 31  # 28 精确比分 + 三档「其他」
    conn.close()


def test_connect_sets_pragmas(tmp_path) -> None:
    conn = db.connect(tmp_path / "goalx.db")
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1
    conn.close()


def test_connect_readonly(tmp_path) -> None:
    path = tmp_path / "ro.db"
    conn = db.connect(path)
    db.migrate(conn)
    conn.close()
    ro = db.connect(path, readonly=True)
    row = ro.execute("SELECT COUNT(*) AS n FROM markets").fetchone()
    assert row["n"] == 9
    ro.close()


def test_append_only_triggers_block_mutation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    conn.execute(
        """
        INSERT INTO competitions (name, created_at) VALUES ('x', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO teams (canonical_name, created_at) VALUES ('a', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO teams (canonical_name, created_at) VALUES ('b', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO fixtures
        (competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (1, '2026-01-01', 1, 2)
        """
    )
    conn.execute(
        """
        INSERT INTO odds_snapshots
        (fixture_id, market_code, selection_code, source, odds, captured_at, created_at)
        VALUES (1, 'had', 'h', 'sporttery', 2.0, 't', 'x')
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE odds_snapshots SET odds = 3.0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM odds_snapshots")
    # v24（A4）：行政判赛排除表续添只 INSERT（窗口写错也走人工落库手术）
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE admin_match_exclusions SET date_end = '2024-01-01'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM admin_match_exclusions")
    conn.close()


def test_upgrade_from_v3_preserves_data_and_audit_cli_is_readonly(
    tmp_path, monkeypatch, capsys
) -> None:
    import json

    from goalx_backend.betting.ledger_audit import audit_ledger
    from goalx_backend.cli import main

    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    with monkeypatch.context() as patch:
        patch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:3])
        assert db.migrate(conn) == 3
    conn.execute(
        "INSERT INTO bankroll_events (occurred_at, kind, amount_cny, balance_after)"
        " VALUES ('t', 'deposit', 100, 100)"
    )
    conn.commit()
    before = [dict(row) for row in conn.execute("SELECT * FROM bankroll_events")]
    assert audit_ledger(conn)["schema_version"] == 3
    monkeypatch.setenv("GOALX_DB_PATH", str(path))
    assert main(["audit-ledger"]) == 0
    assert json.loads(capsys.readouterr().out)["repairs_applied"] is False
    assert db.current_version(conn) == 3
    assert db.migrate(conn) == 54
    assert [
        dict(row) for row in conn.execute("SELECT * FROM bankroll_events")
    ] == before
    conn.close()


def test_upgrade_from_v24_carries_legacy_predictions(tmp_path, monkeypatch) -> None:
    """v25（票 13）：预测表换键 gold sid，旧 M2 行保留迁移（hist id 转 text）。"""
    path = tmp_path / "legacy.db"
    conn = db.connect(path)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:24])
            assert db.migrate(conn) == 24
        conn.execute(
            "INSERT INTO hist_matches (competition, season, match_date, home_team,"
            " away_team, fthg, ftag, ftr)"
            " VALUES ('E0','2324','2024-01-01','A','B',2,1,'H')"
        )
        conn.execute(
            "INSERT INTO backtest_runs (label, params, status, created_at)"
            " VALUES ('legacy', '{}', 'done', 't')"
        )
        conn.execute(
            """
            INSERT INTO backtest_predictions
                (run_id, hist_match_id, competition, season, match_date, home_team,
                 away_team, had_probs, fair_probs, fair_source, model_fingerprint,
                 train_window_end)
            VALUES (1, 1, 'E0', '2324', '2024-01-01', 'A', 'B',
                    '{"h":0.4}', '{"h":0.5}', 'psc', 'fp', '2023-12-31')
            """
        )
        assert db.migrate(conn) == 54
        row = conn.execute(
            "SELECT * FROM backtest_predictions WHERE run_id = 1"
        ).fetchone()
        assert row["match_key"] == "1"  # 旧 hist id 转 text，审计不丢
        assert row["ftr"] is None  # 旧行无此面
        assert row["era"] is None
        # 新键与扩展 CHECK 生效
        conn.execute(
            """
            INSERT INTO backtest_predictions
                (run_id, match_key, competition, season, match_date, home_team,
                 away_team, era, ftr, had_probs, fair_probs, fair_source,
                 model_fingerprint, train_window_end)
            VALUES (1, 'srct:1', 'E0', '2023-24', '2024-01-02', 'A', 'B',
                    'trajectory', 'D', '{}', '{}', 'traj_anchor', 'fp', '2023-12-31')
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="fair_source"):
            conn.execute(
                """
                INSERT INTO backtest_predictions
                    (run_id, match_key, competition, season, match_date, home_team,
                     away_team, had_probs, fair_probs, fair_source,
                     model_fingerprint, train_window_end)
                VALUES (1, 'srct:2', 'E0', '2023-24', '2024-01-03', 'A', 'B',
                        '{}', '{}', 'bogus', 'fp', '2023-12-31')
                """
            )
    finally:
        conn.close()


def test_v32_survives_orphan_temp_table_from_crash_window() -> None:
    """correctness F1（实证复现）：CREATE 已落盘、INSERT 前死进程留孤儿
    sale_statuses_v32——重跑 migrate 不得 table already exists 卡死全栈。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    conn.execute("INSERT INTO competitions (name, created_at) VALUES ('x', 't')")
    conn.execute("INSERT INTO teams (canonical_name, created_at) VALUES ('a', 't')")
    conn.execute("INSERT INTO teams (canonical_name, created_at) VALUES ('b', 't')")
    conn.execute(
        """
        INSERT INTO fixtures
        (competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (1, '2026-01-01', 1, 2)
        """
    )
    conn.execute(
        """
        INSERT INTO sale_statuses
        (fixture_id, market_code, sale_state, observed_at, created_at)
        VALUES (1, 'had', 'stopped', 't', 't')
        """
    )
    conn.commit()
    # 模拟崩溃窗：抹 v32 戳 + 手造孤儿临时表（DDL 已落、拷行未发生）
    conn.execute("DELETE FROM schema_migrations WHERE version = 32")
    conn.execute("CREATE TABLE sale_statuses_v32 (junk INTEGER)")
    conn.commit()
    assert db.migrate(conn) == 54  # 残迹被清扫，不抛 already exists
    row = conn.execute(
        "SELECT sale_state FROM sale_statuses WHERE fixture_id = 1"
    ).fetchone()
    assert row["sale_state"] == "stopped"  # 数据经重建保住
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE sale_statuses SET sale_state = 'unknown'")
    conn.close()


def test_v32_not_on_sale_value_domain_and_reentrant() -> None:
    """v32（票 09）：not_on_sale 入值域、表重建保行保索引保防线、重入幂等。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    conn.execute("INSERT INTO competitions (name, created_at) VALUES ('x', 't')")
    conn.execute("INSERT INTO teams (canonical_name, created_at) VALUES ('a', 't')")
    conn.execute("INSERT INTO teams (canonical_name, created_at) VALUES ('b', 't')")
    conn.execute(
        """
        INSERT INTO fixtures
        (competition_id, kickoff_utc, home_team_id, away_team_id)
        VALUES (1, '2026-01-01', 1, 2)
        """
    )
    conn.execute(
        """
        INSERT INTO sale_statuses
        (fixture_id, market_code, sale_state, observed_at, created_at)
        VALUES (1, 'had', 'not_on_sale', 't', 't')
        """
    )
    # 索引随重建恢复（lookup 走得动）
    row = conn.execute(
        """
        SELECT sale_state FROM sale_statuses
        WHERE fixture_id = 1 AND market_code = 'had' AND observed_at <= 't'
        """
    ).fetchone()
    assert row["sale_state"] == "not_on_sale"
    # 防线随重建恢复
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE sale_statuses SET sale_state = 'unknown'")
    # 中断窗重入：抹 32 戳重跑，CHECK 已含 not_on_sale → 守卫短路
    conn.execute("DELETE FROM schema_migrations WHERE version = 32")
    conn.commit()
    assert db.migrate(conn) == 54
    assert conn.execute("SELECT COUNT(*) AS n FROM sale_statuses").fetchone()["n"] == 1
    conn.close()


def test_v40_reentrant_after_interrupted_migration() -> None:
    """v40 幂等守卫（票 03 correctness F1）：触发器/索引已落而版本戳
    未提交的崩溃窗后重跑 migrate 不得 already exists 卡死。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    conn.execute("DELETE FROM schema_migrations WHERE version = 40")
    conn.commit()
    assert db.migrate(conn) == 54  # 重入不抛（IF NOT EXISTS 短路）
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
            " AND tbl_name='had_quote_confirmations'"
        )
    }
    assert triggers == {
        "had_quote_confirmations_no_update",
        "had_quote_confirmations_no_delete",
    }
    conn.close()


def test_v31_reentrant_after_interrupted_migration() -> None:
    """v31 幂等守卫（correctness W1）：DDL 已落而版本戳未提交的崩溃窗
    （或中间版代码先动过主库）后重跑 migrate 不得 duplicate column 卡死。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    # 模拟中断窗：抹掉版本戳但保留 v31 列（DDL 已提交、戳未提交）
    conn.execute("DELETE FROM schema_migrations WHERE version = 31")
    conn.commit()
    assert db.migrate(conn) == 54  # 重入不抛，列存在守卫短路 ALTER
    cols = {r[1] for r in conn.execute("PRAGMA table_info(frozen_decisions)")}
    assert {"anchor_ref", "close_ref"} <= cols
    conn.close()


def test_v33_haircut_calibrations_era_rebuild() -> None:
    """v33（odds-read-switch 留账）：source_era 维度入表 + 三列唯一键，旧行标注
    （sp-close:*→jc_sp，其余→odds_api），重入幂等（崩溃窗守卫短路重建）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 先迁到 v32 形态，再换回旧形态表（真实 v32 生产库形状：source_era 列不存在；
    # 新 base DDL 已带 source_era，直接顺序迁移会让 v33 守卫短路、测不到重建腿）
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version, apply_fn in migrations.MIGRATIONS:
        if version > 32:
            break
        apply_fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 't')",
            (version,),
        )
    conn.execute("DROP TABLE haircut_calibrations")
    conn.execute(
        """
        CREATE TABLE haircut_calibrations (
            id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL,
            market_code TEXT NOT NULL,
            haircut REAL NOT NULL,
            n_samples INTEGER NOT NULL,
            n_fixtures INTEGER NOT NULL DEFAULT 0,
            method_version TEXT NOT NULL DEFAULT 'shin_mean_v0',
            quartiles TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('calibrated', 'default')),
            computed_at TEXT NOT NULL,
            UNIQUE (scope, market_code)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO haircut_calibrations
        (scope, market_code, haircut, n_samples, quartiles, source, computed_at)
        VALUES ('overall', 'had', 0.08, 33, '[]', 'calibrated', 't'),
               ('sp-close:overall', 'had', 0.05, 100, '[]', 'calibrated', 't')
        """
    )
    conn.commit()
    assert db.migrate(conn) == 54  # v33 重建腿：旧形态表 → source_era 维度入表
    rows = conn.execute(
        "SELECT scope, source_era FROM haircut_calibrations ORDER BY scope"
    ).fetchall()
    assert [(r["scope"], r["source_era"]) for r in rows] == [
        ("overall", "odds_api"),
        ("sp-close:overall", "jc_sp"),
    ]
    # 三列唯一键：同 scope 不同 source_era 可并存
    conn.execute(
        """
        INSERT INTO haircut_calibrations
        (scope, market_code, source_era, haircut, n_samples, quartiles,
         source, computed_at)
        VALUES ('overall', 'had', 'srct', 0.07, 40, '[]', 'calibrated', 't')
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO haircut_calibrations
            (scope, market_code, source_era, haircut, n_samples, quartiles, source,
             computed_at)
            VALUES ('overall', 'had', 'srct', 0.07, 40, '[]', 'calibrated', 't')
            """
        )
    # 崩溃窗重入：抹 33 戳重跑，source_era 列已在 → 守卫短路，数据保住
    conn.execute("DELETE FROM schema_migrations WHERE version = 33")
    conn.commit()
    assert db.migrate(conn) == 54
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM haircut_calibrations").fetchone()["n"]
        == 3
    )
    conn.close()


def test_v33_survives_orphan_temp_table_from_crash_window() -> None:
    """v33 崩溃窗（correctness F1 同 v32 族）：DDL 已落盘而拷行未提交的
    孤儿 haircut_calibrations_v33 不卡死重跑 migrate（残迹被清扫）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    # 退回旧形态表 + 在册行（模拟 v32 生产库）
    conn.execute("DROP TABLE haircut_calibrations")
    conn.execute(
        """
        CREATE TABLE haircut_calibrations (
            id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL,
            market_code TEXT NOT NULL,
            haircut REAL NOT NULL,
            n_samples INTEGER NOT NULL,
            n_fixtures INTEGER NOT NULL DEFAULT 0,
            method_version TEXT NOT NULL DEFAULT 'shin_mean_v0',
            quartiles TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('calibrated', 'default')),
            computed_at TEXT NOT NULL,
            UNIQUE (scope, market_code)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO haircut_calibrations
        (scope, market_code, haircut, n_samples, quartiles, source, computed_at)
        VALUES ('overall', 'had', 0.08, 33, '[]', 'calibrated', 't')
        """
    )
    # 模拟崩溃窗：33 戳未落 + 孤儿临时表已落盘（legacy 模式 DDL 即时持久）
    conn.execute("DELETE FROM schema_migrations WHERE version = 33")
    conn.execute("CREATE TABLE haircut_calibrations_v33 (junk INTEGER)")
    conn.commit()
    assert db.migrate(conn) == 54  # 残迹被清扫，不抛 already exists
    row = conn.execute(
        "SELECT haircut, source_era FROM haircut_calibrations WHERE scope='overall'"
    ).fetchone()
    assert row["haircut"] == 0.08
    assert row["source_era"] == "odds_api"
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE name ="
            " 'haircut_calibrations_v33'"
        ).fetchone()["n"]
        == 0
    )
    conn.close()


def test_v34_reentrant_after_interrupted_migration() -> None:
    """v34 幂等守卫（correctness F2）：DDL 已落而版本戳未提交的崩溃窗后
    重跑 migrate 不得 duplicate column 卡死；empty_markets 列在位。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert db.migrate(conn) == 54
    conn.execute("DELETE FROM schema_migrations WHERE version = 34")
    conn.commit()
    assert db.migrate(conn) == 54  # 重入不抛，列存在守卫短路 ALTER
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pool_sync_runs)")}
    assert "empty_markets" in cols
    conn.close()


def test_research_db_fresh_migrate_and_idempotent(tmp_path) -> None:
    """票 07②：研究库连接自动建 backtest 四表（终态形状），幂等重开。"""
    path = tmp_path / "research.db"
    conn = db.connect_research(path)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "backtest_runs",
            "backtest_predictions",
            "backtest_bets",
            "backtest_metrics",
        } <= tables
        # 终态形状锚点（v25 后）：predictions 键 match_key + era/ftr 列
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(backtest_predictions)")
        }
        assert {"match_key", "era", "ftr"} <= cols
        assert db.current_version(conn) == len(migrations.RESEARCH_MIGRATIONS)
    finally:
        conn.close()
    # 重开幂等（schema_migrations 记账后不再执行 DDL）
    again = db.connect_research(path)
    again.close()


def test_research_backfill_copies_rows_id_preserving(tmp_path) -> None:
    """票 07②：CLI research-backfill 保 id 搬运、幂等可重跑。"""
    from unittest.mock import patch

    from goalx_backend import cli
    from goalx_backend.config import Settings

    ops = db.connect(tmp_path / "ops.db")
    db.migrate(ops)
    ops.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('legacy', '{}', 'done', '2026-01-01T00:00:00+00:00')"
    )
    run_id = int(ops.execute("SELECT id FROM backtest_runs").fetchone()["id"])
    ops.execute(
        "INSERT INTO backtest_predictions (run_id, match_key, competition, season,"
        " match_date, home_team, away_team, had_probs, fair_probs, fair_source,"
        " model_fingerprint, train_window_end)"
        " VALUES (?, '9001', 'E0', '2024-25', '2025-01-01', 'A', 'B', '[]', '[]',"
        " 'psc', 'fp', '2024-12-31')",
        (run_id,),
    )
    ops.commit()
    ops.close()

    settings = Settings(
        db_path=tmp_path / "ops.db", research_db_path=tmp_path / "research.db"
    )
    with patch("goalx_backend.cli.get_settings", return_value=settings):
        cli._cmd_research_backfill()

    rconn = db.connect_research(settings.research_db_path)
    try:
        copied_run = rconn.execute("SELECT id, label FROM backtest_runs").fetchone()
        assert copied_run["id"] == run_id  # id 保序：报告引用不漂移
        assert copied_run["label"] == "legacy"
        n_preds = rconn.execute(
            "SELECT COUNT(*) AS n FROM backtest_predictions"
        ).fetchone()["n"]
        assert n_preds == 1
        # 幂等重跑：不重复不报错
        with patch("goalx_backend.cli.get_settings", return_value=settings):
            cli._cmd_research_backfill()
        n_again = rconn.execute(
            "SELECT COUNT(*) AS n FROM backtest_predictions"
        ).fetchone()["n"]
        assert n_again == 1
    finally:
        rconn.close()


def test_research_backfill_refuses_id_collision(tmp_path) -> None:
    """票 07② review 修复：研究库已有非历史 run（先跑过新回测）时
    拒绝搬运——INSERT OR IGNORE 会静默吞历史行。"""
    from goalx_backend.config import Settings
    from goalx_backend.evaluation import backtest as bt_mod

    ops = db.connect(tmp_path / "ops.db")
    db.migrate(ops)
    ops.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('legacy', '{}', 'done', '2026-01-01T00:00:00+00:00')"
    )
    ops.commit()
    ops.close()

    settings = Settings(
        db_path=tmp_path / "ops.db", research_db_path=tmp_path / "research.db"
    )
    # 研究库先落一条"新回测"（id=1，与运行面 legacy id=1 撞号但内容不同）
    rconn = db.connect_research(settings.research_db_path)
    rconn.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('new-run', '{}', 'done', '2026-11-03T00:00:00+00:00')"
    )
    rconn.commit()
    rconn.close()

    ops2 = db.connect(settings.db_path)
    try:
        with pytest.raises(RuntimeError, match="撞号"):
            bt_mod.backfill_research(ops2, settings.research_db_path)
    finally:
        ops2.close()
    # 拒绝时零搬运：legacy 行未进研究库（新 run 行原样保留）
    check = db.connect_research(settings.research_db_path)
    try:
        labels = {
            str(r["label"]) for r in check.execute("SELECT label FROM backtest_runs")
        }
        assert labels == {"new-run"}
    finally:
        check.close()


def test_connect_research_concurrent_first_open(tmp_path) -> None:
    """票 07② correctness F2：全新研究库并发首开（如前端首屏两请求各开
    一连接）不因 journal_mode 切换/即席迁移竞态报 database is locked。"""
    import threading

    path = tmp_path / "research.db"
    barrier = threading.Barrier(2, timeout=10)
    errors: list[Exception] = []

    def opener() -> None:
        try:
            barrier.wait()  # 两线程同时进入首开路径
            conn = db.connect_research(path)
            conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()
            conn.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors


def test_research_backfill_constraint_violation_is_loud_and_atomic(tmp_path) -> None:
    """票 07② correctness F4/F5：历史行违反研究库 CHECK 时声性报错（不
    静默吞行），且单事务回滚——无半截搬运。"""
    from goalx_backend.evaluation import backtest as bt_mod

    # 伪造"约束更松的旧时代"运行面库：predictions 无 fair_source CHECK
    ops_path = tmp_path / "ops-loose.db"
    ops = sqlite3.connect(ops_path)
    ops.execute(
        "CREATE TABLE backtest_runs (id INTEGER PRIMARY KEY, label TEXT,"
        " params TEXT, status TEXT, created_at TEXT, finished_at TEXT, summary TEXT)"
    )
    ops.execute(
        "CREATE TABLE backtest_predictions (id INTEGER PRIMARY KEY,"
        " run_id INTEGER, match_key TEXT, competition TEXT, season TEXT,"
        " match_date TEXT, home_team TEXT, away_team TEXT, era TEXT, ftr TEXT,"
        " had_probs TEXT, fair_probs TEXT, fair_source TEXT,"
        " model_fingerprint TEXT, train_window_end TEXT)"
    )
    ops.execute(
        "INSERT INTO backtest_runs VALUES (1, 'legacy', '{}', 'done', 't', NULL, NULL)"
    )
    ops.execute(
        "INSERT INTO backtest_predictions VALUES (1, 1, 'k1', 'E0', '2020-21',"
        " '2020-10-01', 'H', 'A', NULL, 'H', '[]', '[]', 'bogus_source', 'fp', 't')"
    )
    ops.commit()
    ops.close()

    research_path = tmp_path / "research.db"
    warm = db.connect_research(research_path)
    warm.close()

    ops2 = db.connect(ops_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            bt_mod.backfill_research(ops2, research_path)
    finally:
        ops2.close()
    # 单事务回滚：runs 行不残留（守卫过后搬运整体失败）
    check = db.connect_research(research_path)
    try:
        n_runs = check.execute("SELECT COUNT(*) AS n FROM backtest_runs").fetchone()[
            "n"
        ]
        n_preds = check.execute(
            "SELECT COUNT(*) AS n FROM backtest_predictions"
        ).fetchone()["n"]
    finally:
        check.close()
    assert n_runs == 0
    assert n_preds == 0


def test_apply_chain_concurrent_migrate_applies_once(tmp_path) -> None:
    """审计批C #11：并发 migrate 的 check-then-apply 竞态——每版本
    check+apply+记账包 BEGIN IMMEDIATE，第二个连接在写锁内复查已施用
    即跳过；非幂等迁移恰施用一次（不再输家 duplicate column 崩一次）。"""
    import threading
    import time

    path = tmp_path / "race.db"
    calls = 0

    def non_idempotent(conn: sqlite3.Connection) -> None:
        nonlocal calls
        calls += 1
        time.sleep(0.1)  # 撑开竞态窗：无写锁串行时两线程都会进入本函数

    chain = (
        (1, lambda conn: conn.execute("CREATE TABLE t (x)")),
        (2, non_idempotent),
    )
    results: dict[str, int] = {}
    conns: dict[str, sqlite3.Connection] = {}

    def run(name: str) -> None:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conns[name] = conn
        try:
            results[name] = db._apply_chain(conn, chain)
        finally:
            conn.close()

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {"a": 2, "b": 2}
    assert calls == 1  # 无锁版此处=2（两线程都过 applied 检查都施用）


def test_v47_repairs_pre_column_freshness_shadow() -> None:
    """v47（v1-release 票 07）：主库 v38 表停在缺列形态（DDL 落库后被
    9c5b3ce 原地补列未升版本，CREATE IF NOT EXISTS 不重建）——日拍
    INSERT 逐班降级。守卫 ALTER 补列；重跑幂等；写入路径恢复。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 复现主库形态：04:21 版 DDL 先建表（无 clock_gap_seconds），v38 的
    # CREATE IF NOT EXISTS 随后短路
    conn.execute(
        """
        CREATE TABLE freshness_shadow (
            id INTEGER PRIMARY KEY,
            fixture_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            as_of TEXT NOT NULL,
            rule_stale_source INTEGER NOT NULL,
            observed_stale INTEGER NOT NULL,
            price_age_seconds REAL,
            observed_age_seconds REAL,
            sale_age_seconds REAL,
            eu_age_seconds REAL,
            pair_gap_seconds REAL,
            UNIQUE(fixture_id, business_date)
        )
        """
    )
    conn.commit()
    assert db.migrate(conn) == 54
    cols = {r[1] for r in conn.execute("PRAGMA table_info(freshness_shadow)")}
    assert "clock_gap_seconds" in cols
    assert db.migrate(conn) == 54  # 列存在守卫：重入不抛 duplicate column
    # 日拍写入路径恢复（INSERT 携带 clock_gap_seconds 列不再降级）
    conn.execute(
        """
        INSERT INTO freshness_shadow
            (fixture_id, business_date, as_of, rule_stale_source,
             observed_stale, clock_gap_seconds)
        VALUES (1, '2026-11-09', '2026-11-09T12:00:00+00:00', 0, 0, -3590.0)
        """
    )
    gap = conn.execute(
        "SELECT clock_gap_seconds FROM freshness_shadow WHERE fixture_id = 1"
    ).fetchone()[0]
    assert gap == -3590.0
    conn.close()
