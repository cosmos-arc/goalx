"""vision-build 票 07：自学习触发→影子注册→影子双算。

影子重训链的管线级钉住：v57 迁移（shadow_retrain_events 账 +
forecasts 影子轨道/版本标签）、漂移触发读数（模型输出分布 PSI=接
contraction.psi）、月度兜底、challenger 自动注册 shadow 态（永无自动
晋升）、champion/challenger 同流量双写（model_version_counts 两版本
可数）、L2+ 收缩停拍留痕、幂等重跑（auto-paper 03 同缝）。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from goalx_backend.betting import versioning
from goalx_backend.betting.versioning import StrategyState
from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation import contraction, shadow
from goalx_backend.modelling import forecast as fc

_RUN_AT = datetime(2026, 11, 20, 2, 35, tzinfo=UTC)  # 北京 11-20 10:35（日拍）


def _seed_fixture(db: sqlite3.Connection, *, tag: str = "a") -> int:
    """场次脚手架（fx_store 属主面建行）。"""
    comp = fx_store.upsert_competition(db, "英超")
    home = fx_store.upsert_team(db, f"主队{tag}")
    away = fx_store.upsert_team(db, f"客队{tag}")
    return fx_store.upsert_fixture(db, comp, "2026-11-20T19:00:00+00:00", home, away)


# ---- v57 迁移：账表 + forecasts 影子轨道/版本标签 ----


def test_migration_v57_idempotent(db: sqlite3.Connection) -> None:
    """v57 幂等：账表就位、forecasts 重建（shadow 轨道+版本标签列）、重跑零效果。"""
    from goalx_backend.db import migrate

    assert migrate(db) == 60
    assert migrate(db) == 60  # 同版本重跑零效果
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='table'"
            " AND name='shadow_retrain_events'"
        ).fetchone()["c"]
        == 1
    )
    # forecasts 影子轨道可写（track CHECK 已扩）+ 版本标签列就位
    fid = _seed_fixture(db)
    db.execute(
        """
        INSERT INTO forecasts
            (fixture_id, track, model_version, issued_at, content_hash, payload,
             strategy_version_id)
        VALUES (?, 'shadow', 'dc-shadowtest', '2026-11-20T02:00:00+00:00',
                'hash-v57', '{}', NULL)
        """,
        (fid,),
    )
    assert (
        db.execute(
            "SELECT strategy_version_id FROM forecasts WHERE content_hash='hash-v57'"
        ).fetchone()["strategy_version_id"]
        is None
    )


def test_shadow_retrain_events_is_append_only(db: sqlite3.Connection) -> None:
    """账表 append-only：UPDATE/DELETE 库层拒绝（v49/v52 触发器先例）。"""
    db.execute(
        """
        INSERT INTO shadow_retrain_events
            (period, reason, outcome, detail, ts)
        VALUES ('2026-11', 'monthly', 'no_live_champion', '演练', '2026-11-01')
        """
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM shadow_retrain_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE shadow_retrain_events SET outcome = 'registered'")


def test_shadow_retrain_events_absorbs_reruns(db: sqlite3.Connection) -> None:
    """幂等吸收两键：版本行 UNIQUE(period, reason, version)；skip 行（无版本）
    部分唯一索引 UNIQUE(period, reason, outcome)。"""
    version_id = versioning.register_version(
        db,
        estimator_version="dc-e",
        calibration_version="shin",
        params_version="fp",
        label="champion",
    )
    for _ in range(2):
        db.execute(
            """
            INSERT OR IGNORE INTO shadow_retrain_events
                (period, reason, outcome, challenger_version_id, detail, ts)
            VALUES ('2026-11', 'drift', 'registered', ?, '演练', '2026-11-02')
            """,
            (version_id,),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO shadow_retrain_events
                (period, reason, outcome, challenger_version_id, detail, ts)
            VALUES ('2026-11', 'monthly', 'no_live_champion', NULL, '演练',
                    '2026-11-03')
            """
        )
    assert (
        db.execute("SELECT COUNT(*) c FROM shadow_retrain_events").fetchone()["c"] == 2
    )


def test_forecasts_unique_widened_to_track(db: sqlite3.Connection) -> None:
    """UNIQUE 扩 (fixture, track, content_hash)：同 payload 双轨并存——
    challenger 与 champion 输出完全一致时影子行不被吸收（双算可比性前提）。"""
    fid = _seed_fixture(db)
    for track in ("ml", "shadow"):
        db.execute(
            """
            INSERT OR IGNORE INTO forecasts
                (fixture_id, track, model_version, issued_at, content_hash, payload)
            VALUES (?, ?, 'dc-x', '2026-11-20T02:00:00+00:00', 'same-hash', '{}')
            """,
            (fid, track),
        )
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM forecasts WHERE content_hash='same-hash'"
        ).fetchone()["c"]
        == 2
    )
    # 同轨同内容仍吸收（ml 幂等重跑语义不变）
    db.execute(
        """
        INSERT OR IGNORE INTO forecasts
            (fixture_id, track, model_version, issued_at, content_hash, payload)
        VALUES (?, 'ml', 'dc-x', '2026-11-20T03:00:00+00:00', 'same-hash', '{}')
        """,
        (fid,),
    )
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM forecasts WHERE content_hash='same-hash'"
        ).fetchone()["c"]
        == 2
    )


# ---- 注册面：challenger 起手=genesis 行 shadow（注册≠晋升） ----


def test_register_version_supports_shadow_genesis(db: sqlite3.Connection) -> None:
    """initial_state=shadow：genesis 行 to_state=shadow（challenger 起手态）；
    缺省 registered（既有人 CLI 语义不变）；幂等命中不落新行。"""
    challenger = versioning.register_version(
        db,
        estimator_version="dc-challenger",
        calibration_version="shin",
        params_version="fp",
        label="shadow-challenger-2026-11",
        initial_state=StrategyState.SHADOW,
    )
    assert versioning.current_state(db, challenger) is StrategyState.SHADOW
    row = db.execute(
        "SELECT from_state, to_state FROM version_transitions WHERE version_id=?",
        (challenger,),
    ).fetchone()
    assert row["from_state"] is None  # genesis 行（非转移）
    assert row["to_state"] == "shadow"
    # 幂等命中：同三元组同 initial_state 返回同 id，不落新行
    again = versioning.register_version(
        db,
        estimator_version="dc-challenger",
        calibration_version="shin",
        params_version="fp",
        label="shadow-challenger-2026-11",
        initial_state=StrategyState.SHADOW,
    )
    assert again == challenger
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM version_transitions WHERE version_id=?",
            (challenger,),
        ).fetchone()["c"]
        == 1
    )
    # 缺省面：人 CLI 注册仍起手 registered
    human = versioning.register_version(
        db,
        estimator_version="dc-human",
        calibration_version="shin",
        params_version="fp",
        label="auto-v1",
    )
    assert versioning.current_state(db, human) is StrategyState.REGISTERED


# ---- 收缩门：deployment_plan.shadow_retrain 面 ----


def test_deployment_plan_shadow_retrain_face() -> None:
    """L0/L1 跑影子重训；L2+ 停（烧算力面收缩）——prereg L3 采集缩监测
    最集/漂移监测恒开：漂移监测（只读）与影子重训（烧算力）是两面。"""
    for level in (contraction.ContractionLevel.L0, contraction.ContractionLevel.L1):
        assert contraction.deployment_plan(level).shadow_retrain is True, level
    for level in (
        contraction.ContractionLevel.L2,
        contraction.ContractionLevel.L3,
        contraction.ContractionLevel.L4,
    ):
        assert contraction.deployment_plan(level).shadow_retrain is False, level


# ---- modelling 面：影子写口 + champion 流量/漂移读口（forecasts 归 modelling） ----


def _payload(h_bias: float = 0.2) -> dict[str, object]:
    """最小合法 payload（forecast_matrix_from_payload 可重建）。"""
    # 2x2 网格：m[1][0]=主胜格（h_bias），对角=平局格，m[0][1]=客胜格
    return {
        "matrix": [[0.1, 0.2], [h_bias, 0.1]],
        "lambda_home": 1.2,
        "lambda_away": 1.0,
    }


def test_insert_forecast_takes_track_and_version_tag(db: sqlite3.Connection) -> None:
    """写口扩展：track='shadow' 行带 strategy_version_id 标签；缺省行为不变。"""
    fid = _seed_fixture(db)
    version_id = versioning.register_version(
        db,
        estimator_version="dc-c",
        calibration_version="shin",
        params_version="fp",
        label="challenger",
    )
    inserted = fc.insert_forecast(
        db,
        fixture_id=fid,
        track="shadow",
        model_version="dc-challenger",
        content_hash=fc.content_hash(_payload()),
        payload=_payload(),
        strategy_version_id=version_id,
        issued_at="2026-11-20T02:00:00+00:00",
    )
    assert inserted is not None
    row = db.execute(
        "SELECT track, strategy_version_id FROM forecasts WHERE id=?", (inserted,)
    ).fetchone()
    assert row["track"] == "shadow"
    assert row["strategy_version_id"] == version_id


def test_champion_traffic_fixture_ids_reads_ml_coverage(
    db: sqlite3.Connection,
) -> None:
    """champion 流量读口：业务日场次中已有 track='ml' 前瞻的 fixture 集
    （同流量双算的目标集）；影子行不计入（champion 轨=ml）。"""
    from goalx_backend.models import MatchCodeInput

    covered = _seed_fixture(db, tag="a")
    uncovered = _seed_fixture(db, tag="b")
    for fid in (covered, uncovered):
        fx_store.upsert_match_code(
            db,
            MatchCodeInput(
                fixture_id=fid,
                kind="jingcai",
                business_date="2026-11-20",
                code=f"周五00{fid}",
                source_match_id=f"m{fid}",
                is_single=True,
            ),
        )
    db.execute(
        """
        INSERT INTO forecasts
            (fixture_id, track, model_version, issued_at, content_hash, payload)
        VALUES (?, 'ml', 'dc-champ', '2026-11-20T02:00:00+00:00', 'h1', '{}')
        """,
        (covered,),
    )
    db.execute(
        """
        INSERT INTO forecasts
            (fixture_id, track, model_version, issued_at, content_hash, payload)
        VALUES (?, 'shadow', 'dc-chal', '2026-11-20T02:05:00+00:00', 'h2', '{}')
        """,
        (uncovered,),
    )
    db.commit()
    assert fc.champion_traffic_fixture_ids(db, business_date="2026-11-20") == [covered]
    # 非本业务日场次不进流量面
    assert fc.champion_traffic_fixture_ids(db, business_date="2026-11-21") == []


def test_ml_had_probabilities_window_read(db: sqlite3.Connection) -> None:
    """漂移读数取数口：窗内 ml 行的 had 三概率（payload→矩阵重建）；
    影子/他轨与窗外行不计。"""
    fid = _seed_fixture(db)
    for track, issued, bias in (
        ("ml", "2026-11-01T00:00:00+00:00", 0.2),
        ("ml", "2026-11-18T00:00:00+00:00", 0.4),
        ("shadow", "2026-11-18T00:00:00+00:00", 0.9),
        ("ml", "2026-12-01T00:00:00+00:00", 0.6),
    ):
        fc.insert_forecast(
            db,
            fixture_id=fid,
            track=track,
            model_version="dc-x",
            content_hash=f"h-{issued}-{track}",
            payload=_payload(bias),
            issued_at=issued,
        )
    rows = fc.ml_had_probabilities(
        db, ts_from="2026-11-01T00:00:00+00:00", ts_to="2026-12-01T00:00:00+00:00"
    )
    assert [r["h"] for r in rows] == pytest.approx([0.2, 0.4])
    assert rows[0]["d"] == pytest.approx(0.2)
    assert rows[0]["a"] == pytest.approx(0.2)


def test_ml_had_probabilities_excludes_post_kickoff_rows(
    db: sqlite3.Connection,
) -> None:
    """F2 PIT 过滤：开球后才 issued 的 ml 行不进漂移窗（issued≤kickoff
    才算模型输出面——19:00 腿赛后重发行会污染 30/90 窗读数）。"""
    from goalx_backend.data import fixtures as fx_store_mod

    comp = fx_store_mod.upsert_competition(db, "英超")
    home = fx_store_mod.upsert_team(db, "主队p")
    away = fx_store_mod.upsert_team(db, "客队p")
    fid = fx_store_mod.upsert_fixture(db, comp, "2026-11-10T19:00:00+00:00", home, away)
    for tag, issued in (
        ("pre", "2026-11-10T02:00:00+00:00"),
        ("post", "2026-11-10T20:00:00+00:00"),
    ):
        fc.insert_forecast(
            db,
            fixture_id=fid,
            track="ml",
            model_version="dc-x",
            content_hash=f"pit-{tag}",
            payload=_payload(0.4),
            issued_at=issued,
        )
    rows = fc.ml_had_probabilities(
        db, ts_from="2026-11-01T00:00:00+00:00", ts_to="2026-12-01T00:00:00+00:00"
    )
    assert len(rows) == 1  # 只有赛前 issued 行进窗


# ---- 漂移读数：模型输出分布 PSI（接 contraction.psi；当前 30 日 vs 前 90 日） ----


def _seed_ml_rows(
    db: sqlite3.Connection,
    *,
    fid: int,
    issued: str,
    n: int,
    h_center: float,
) -> None:
    """n 条 ml 前瞻行（h 概率围绕 h_center 扰动——确定性步进）。"""
    for i in range(n):
        payload = _payload(h_bias=h_center + (i % 5) * 0.01)
        fc.insert_forecast(
            db,
            fixture_id=fid,
            track="ml",
            model_version="dc-x",
            content_hash=f"h-{issued}-{i}",
            payload=payload,
            issued_at=issued,
        )


def test_forecast_output_drift_fires_on_shift(db: sqlite3.Connection) -> None:
    """参考窗 h≈0.25 vs 当前窗 h≈0.60：分布移位 → PSI 越阈（>0.25 严格）
    fired=True；读数带窗端点与三选向 PSI 值。"""
    fid = _seed_fixture(db)
    _seed_ml_rows(db, fid=fid, issued="2026-08-30T00:00:00+00:00", n=40, h_center=0.25)
    _seed_ml_rows(db, fid=fid, issued="2026-11-18T00:00:00+00:00", n=40, h_center=0.60)
    reading = shadow.forecast_output_drift(db, now=_RUN_AT)
    assert reading.status == "ok"
    assert reading.fired is True
    assert reading.psi["h"] > contraction.PSI_THRESHOLD
    assert reading.n_current == 40
    assert reading.n_reference == 40


def test_forecast_output_drift_stable_not_fired(db: sqlite3.Connection) -> None:
    """同分布不触发（PSI 低于阈）。"""
    fid = _seed_fixture(db)
    _seed_ml_rows(db, fid=fid, issued="2026-08-30T00:00:00+00:00", n=40, h_center=0.3)
    _seed_ml_rows(db, fid=fid, issued="2026-11-18T00:00:00+00:00", n=40, h_center=0.3)
    stable = shadow.forecast_output_drift(db, now=_RUN_AT)
    assert stable.status == "ok"
    assert stable.fired is False


def test_forecast_output_drift_insufficient_only_reports(
    db: sqlite3.Connection,
) -> None:
    """窗样本不足（<SHADOW_DRIFT_MIN_N）：即使分布大幅移位也 insufficient
    只报不判（缺数据的「漂移」不是漂移）。"""
    fid = _seed_fixture(db)
    _seed_ml_rows(db, fid=fid, issued="2026-08-30T00:00:00+00:00", n=10, h_center=0.3)
    _seed_ml_rows(db, fid=fid, issued="2026-11-18T00:00:00+00:00", n=40, h_center=0.9)
    thin = shadow.forecast_output_drift(db, now=_RUN_AT)
    assert thin.status == "insufficient"
    assert thin.fired is False
    assert thin.psi == {}


# ---- challenger 三元组：引擎实现版本面 + 重训工件面（内容寻址） ----


def test_challenger_triple_is_content_addressed() -> None:
    """三元组=engine_version_triple 的基座 + 重训工件摘要：工件变=版本变，
    参数变=版本变（params 指纹同源）；工件不变=三元组不变（幂等注册前提）。"""
    from goalx_backend.betting import auto_strategy, probes

    params = probes.AUTO_V2_PARAMS  # challenger 参数面基座=现役引擎（v1 已退役）
    base = shadow.challenger_triple(params, {"E0": "art-a", "N1": "art-n"})
    same = shadow.challenger_triple(params, {"N1": "art-n", "E0": "art-a"})
    changed = shadow.challenger_triple(params, {"E0": "art-b", "N1": "art-n"})
    assert base == same  # 工件面顺序无关
    assert base != changed  # 任一工件变=新版本
    # 基座可溯源：estimator 以引擎实现版本起头（代码/依赖变更=换版本）
    engine_estimator, engine_calibration, _ = auto_strategy.engine_version_triple(
        params
    )
    assert base[0].startswith(engine_estimator)
    assert base[1] == engine_calibration


# ---- beat 编排：触发→注册 shadow→同流量双写（e2e，真重训管线 fixture 演练） ----


def _seed_league_history(db: sqlite3.Connection) -> None:
    """合成 fd 历史进 hist_matches（test_forecast 同构，E0 八队）。"""
    import numpy as np

    from goalx_backend.data import results as rs_store

    rng = np.random.default_rng(9)
    teams = [
        "Man United",
        "Liverpool",
        "Arsenal",
        "Chelsea",
        "Everton",
        "Fulham",
        "Burnley",
        "Brentford",
    ]
    rows = []
    day = 1
    for _ in range(30):
        for i in range(len(teams) // 2):
            rows.append(
                {
                    "competition": "E0",
                    "season": "2526",
                    "match_date": f"2025-09-{day:02d}",
                    "home_team": teams[i],
                    "away_team": teams[len(teams) - 1 - i],
                    "fthg": int(rng.poisson(1.5)),
                    "ftag": int(rng.poisson(1.1)),
                    "ftr": "H",
                    "psc_home": 2.0,
                    "psc_draw": 3.4,
                    "psc_away": 3.8,
                    "avgc_home": 2.0,
                    "avgc_draw": 3.4,
                    "avgc_away": 3.8,
                }
            )
        day = min(day + 3, 28)
    rs_store.upsert_hist_matches(db, rows)


def _seed_sale_fixture(
    db: sqlite3.Connection, *, code: str, kickoff: str, business_date: str
) -> int:
    """在售竞彩场次（英超+别名对齐，test_forecast 同构）。"""
    from goalx_backend.models import MatchCodeInput, Tier

    competition = fx_store.upsert_competition(
        db, "英超", tier=Tier.TIER1, odds_api_sport_key="soccer_epl"
    )
    home = fx_store.upsert_team(db, "曼联")
    away = fx_store.upsert_team(db, "利物浦")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date=business_date,
            code=code,
            source_match_id=f"m-{code}",
            is_single=True,
        ),
    )
    for canonical, alias in (("曼联", "Manchester United"), ("利物浦", "Liverpool")):
        team_id = fx_store.upsert_team(db, canonical)
        db.execute(
            "INSERT OR IGNORE INTO team_aliases (team_id, source, alias)"
            " VALUES (?, 'odds_api', ?)",
            (team_id, alias),
        )
    db.commit()
    return fixture


def _register_live_champion(db: sqlite3.Connection) -> int:
    """champion 版本注册并人审晋升 live-paper（票 01 人裁路径）。"""
    version_id = versioning.register_version(
        db,
        estimator_version="dc-champion-release",
        calibration_version="shin",
        params_version="fp-champion",
        label="auto-v1-champion",
    )
    for state in (
        StrategyState.SHADOW,
        StrategyState.PROBE,
        StrategyState.CANDIDATE,
        StrategyState.LIVE_PAPER,
    ):
        versioning.transition_version(
            db,
            version_id,
            state,
            actor="human:cli",
            reason="人审晋升（e2e 演练）",
        )
    return version_id


def _beat(
    db: sqlite3.Connection,
    challenger_dir: Path,
    *,
    now: datetime = _RUN_AT,
    retrain: object = None,
) -> dict[str, Any]:
    return shadow.run_shadow_retrain_beat(
        db,
        now=now,
        challenger_models_dir=challenger_dir,
        competitions=("E0",),
        retrain=retrain,
    )


def test_shadow_retrain_chain_e2e(db: sqlite3.Connection, tmp_path: Path) -> None:
    """一次影子重训链 e2e：月度兜底触发→重训（真 fit 管线写独立 challenger
    工件目录）→注册 shadow 态（genesis 一行，永无自动晋升）→同流量双写
    （challenger 行=champion 覆盖的同一批 fixture，带版本标签）→月读③读口
    两版本可数。"""
    from goalx_backend.modelling import dc_model as dcm

    _seed_league_history(db)
    champion_dir = tmp_path / "models"
    challenger_dir = tmp_path / "models-shadow"
    dcm.train_competition(db, "E0", models_dir=champion_dir, n_boot=8, seed=3)
    fixture_id = _seed_sale_fixture(
        db,
        code="周五001",
        kickoff="2026-11-20T19:00:00+00:00",
        business_date="2026-11-20",
    )
    champion_stats = fc.generate_forecasts(
        db, business_date="2026-11-20", models_dir=str(champion_dir)
    )
    assert champion_stats.generated == 1  # champion 生产轨就位
    _register_live_champion(db)

    report = _beat(db, challenger_dir)

    assert report["status"] == "registered"
    assert report["reason"] == "monthly"  # 月度兜底（本期无重训史）
    assert report["generated"] == 1
    # 注册面：challenger shadow 态，genesis 一行（beat 不落任何转移）
    challenger_id = report["challenger_version_id"]
    assert versioning.current_state(db, challenger_id) is StrategyState.SHADOW
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM version_transitions WHERE version_id=?",
            (challenger_id,),
        ).fetchone()["c"]
        == 1
    )
    # F1：自动注册的 genesis actor=trigger 前缀（审计账不冒充人审）；
    # 人 CLI 注册缺省 actor=human:cli（兼容）
    genesis = db.execute(
        "SELECT actor FROM version_transitions WHERE version_id=?", (challenger_id,)
    ).fetchone()
    assert genesis["actor"] == "trigger:shadow-retrain"
    human_version = versioning.register_version(
        db,
        estimator_version="dc-human-f1",
        calibration_version="shin",
        params_version="fp",
        label="cli",
    )
    human_genesis = db.execute(
        "SELECT actor FROM version_transitions WHERE version_id=?", (human_version,)
    ).fetchone()
    assert human_genesis["actor"] == "human:cli"
    # 生产面不动：champion ml 行唯一且无版本标签（不回溯打标）
    ml_rows = db.execute(
        "SELECT model_version, strategy_version_id FROM forecasts WHERE track='ml'"
    ).fetchall()
    assert len(ml_rows) == 1
    assert ml_rows[0]["strategy_version_id"] is None
    # 双算面：challenger 行 track=shadow 带版本标签，同 fixture 键
    shadow_rows = db.execute(
        """
        SELECT fixture_id, model_version, strategy_version_id
        FROM forecasts WHERE track='shadow'
        """
    ).fetchall()
    assert len(shadow_rows) == 1
    assert shadow_rows[0]["fixture_id"] == fixture_id
    assert shadow_rows[0]["strategy_version_id"] == challenger_id
    assert shadow_rows[0]["model_version"].startswith("shadow-dc-")
    # 月读③消费缝：窗内两版本行数可数（各 1）
    counts = fc.model_version_counts(
        db, ts_from="2026-11-01T00:00:00+00:00", ts_to="2026-12-01T00:00:00+00:00"
    )
    assert counts[ml_rows[0]["model_version"]] == 1
    assert counts[shadow_rows[0]["model_version"]] == 1
    # 触发落账（月读⑤消费面）
    events = db.execute(
        "SELECT period, reason, outcome, challenger_version_id"
        " FROM shadow_retrain_events"
    ).fetchall()
    assert [(e["period"], e["reason"], e["outcome"]) for e in events] == [
        ("2026-11", "monthly", "registered")
    ]
    assert events[0]["challenger_version_id"] == challenger_id
    # 票 11 prereg：challenger 注册即冻结质量门指标面（首写恒胜；常数=代码面）
    from goalx_backend.evaluation import quality_gate

    spec = quality_gate.frozen_spec(db, challenger_id)
    assert spec is not None
    assert spec.metric_name == quality_gate.GATE_METRIC_NAME
    assert spec.alpha == quality_gate.GATE_ALPHA
    assert spec.window_era == quality_gate.GATE_WINDOW_ERA
    # 影子重训链不产门评估（评估=人 CLI；beat 零门账行）
    assert db.execute("SELECT COUNT(*) c FROM quality_gate_events").fetchone()["c"] == 0


def test_beat_rerun_is_idempotent(db: sqlite3.Connection, tmp_path: Path) -> None:
    """幂等重跑（auto-paper 03 同缝）：同拍重跑零新副作用——月度兜底已
    落账（no_trigger）、forecasts/版本/事件计数全不变。"""
    from goalx_backend.modelling import dc_model as dcm

    _seed_league_history(db)
    champion_dir = tmp_path / "models"
    challenger_dir = tmp_path / "models-shadow"
    dcm.train_competition(db, "E0", models_dir=champion_dir, n_boot=8, seed=3)
    _seed_sale_fixture(
        db,
        code="周五001",
        kickoff="2026-11-20T19:00:00+00:00",
        business_date="2026-11-20",
    )
    fc.generate_forecasts(db, business_date="2026-11-20", models_dir=str(champion_dir))
    _register_live_champion(db)

    first = _beat(db, challenger_dir)
    assert first["status"] == "registered"
    rerun = _beat(db, challenger_dir)
    assert rerun["status"] == "no_trigger"  # 月度兜底已落账
    assert (
        db.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"] == 2
    )  # ml 1 + shadow 1，重跑零新行
    assert (
        db.execute("SELECT COUNT(*) c FROM strategy_versions").fetchone()["c"] == 2
    )  # champion + challenger，重跑零新注册
    assert (
        db.execute("SELECT COUNT(*) c FROM shadow_retrain_events").fetchone()["c"] == 1
    )


def test_beat_drift_trigger_and_monthly_throttle(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """漂移即时触发（reason=drift）优先于月度兜底；同月漂移已重训→节流
    （不再烧重训，no_trigger）；跨月漂移再触发→工件未变=no_change 留痕。"""
    from goalx_backend.modelling import dc_model as dcm

    _seed_league_history(db)
    champion_dir = tmp_path / "models"
    challenger_dir = tmp_path / "models-shadow"
    dcm.train_competition(db, "E0", models_dir=champion_dir, n_boot=8, seed=3)
    fixture_id = _seed_sale_fixture(
        db,
        code="周五001",
        kickoff="2026-11-20T19:00:00+00:00",
        business_date="2026-11-20",
    )
    fc.generate_forecasts(db, business_date="2026-11-20", models_dir=str(champion_dir))
    _register_live_champion(db)
    # 漂移面：参考窗 vs 当前窗大幅移位（越过 PSI 阈）
    _seed_ml_rows(
        db, fid=fixture_id, issued="2026-08-30T00:00:00+00:00", n=40, h_center=0.25
    )
    _seed_ml_rows(
        db, fid=fixture_id, issued="2026-11-19T00:00:00+00:00", n=40, h_center=0.60
    )

    fired = _beat(db, challenger_dir)
    assert fired["reason"] == "drift"
    assert fired["status"] == "registered"

    throttled = _beat(db, challenger_dir)
    assert throttled["status"] == "no_trigger"  # 同月已漂移重训→节流

    # 跨月（12 月拍）：漂移仍在（当前窗 11-05 起含 11-19 移位行）；12-05
    # 业务日补 champion 覆盖（同工件）→重训工件未变→no_change，但当期
    # 流量照补（新 fixture 新行）；版本不增（三元组幂等命中）
    fixture2 = _seed_sale_fixture(
        db,
        code="周六001",
        kickoff="2026-12-05T19:00:00+00:00",
        business_date="2026-12-05",
    )
    fc.generate_forecasts(db, business_date="2026-12-05", models_dir=str(champion_dir))
    december = datetime(2026, 12, 5, 2, 35, tzinfo=UTC)
    next_month = _beat(db, challenger_dir, now=december)
    assert next_month["reason"] == "drift"
    assert next_month["status"] == "no_change"
    # 票 11 F3：no_change 幂等命中不补冻结面——冻结行数与 registered 拍
    # 相同（challenger 已有冻结面，champion 未注册过 beat 面；beat 复跑
    # 不得新增冻结行=旧版本不被事后补 prereg 面）
    assert db.execute("SELECT COUNT(*) c FROM quality_gate_specs").fetchone()["c"] == 1
    events = db.execute(
        "SELECT period, reason, outcome FROM shadow_retrain_events ORDER BY id"
    ).fetchall()
    assert [(e["period"], e["reason"], e["outcome"]) for e in events] == [
        ("2026-11", "drift", "registered"),
        ("2026-12", "drift", "no_change"),
    ]
    assert next_month["generated"] == 1  # 12-05 流量补覆盖（新 fixture）
    assert (
        db.execute("SELECT COUNT(*) c FROM strategy_versions").fetchone()["c"] == 2
    )  # 工件未变=同三元组=不增版本
    assert (
        db.execute(
            "SELECT COUNT(*) c FROM forecasts WHERE fixture_id=? AND track='shadow'",
            (fixture2,),
        ).fetchone()["c"]
        == 1
    )


def test_beat_skips_on_contraction_level(db: sqlite3.Connection) -> None:
    """L2+ 收缩态停拍留痕：drift 触发被 deployment_plan.shadow_retrain=False
    拦截（skipped_contraction），重训缝不被调用（不烧算力）。"""

    def _must_not_retrain() -> dict[str, str]:
        raise AssertionError("L2+ 不得调用重训缝")

    contraction.set_level(
        db, contraction.ContractionLevel.L1, actor="human:cli", reason="演练"
    )
    contraction.set_level(
        db, contraction.ContractionLevel.L2, actor="human:cli", reason="演练"
    )
    report = _beat(db, Path("unused"), retrain=_must_not_retrain)
    assert report["status"] == "skipped_contraction"
    assert report["reason"] == "monthly"
    events = db.execute("SELECT reason, outcome FROM shadow_retrain_events").fetchall()
    assert [(e["reason"], e["outcome"]) for e in events] == [
        ("monthly", "skipped_contraction")
    ]


def test_beat_requires_live_champion_then_traffic(db: sqlite3.Connection) -> None:
    """前置两面留痕：无 live 态版本（champion 未定义）→no_live_champion；
    有 champion 无当日流量→no_traffic（均不注册不重训——重训缝注入哨兵）。"""
    fid = _seed_fixture(db)

    def _must_not_retrain() -> dict[str, str]:
        raise AssertionError("前置失败不得调用重训缝")

    no_champion = _beat(db, Path("unused"), retrain=_must_not_retrain)
    assert no_champion["status"] == "no_live_champion"

    _register_live_champion(db)
    no_traffic = _beat(db, Path("unused"), retrain=_must_not_retrain)
    assert no_traffic["status"] == "no_traffic"
    # no_traffic 语义：业务日无 ml 前瞻（champion 无覆盖）——补一条非当日
    # 业务日的 ml 行后仍 no_traffic（流量面按业务日取）
    fc.insert_forecast(
        db,
        fixture_id=fid,
        track="ml",
        model_version="dc-x",
        content_hash="other-day",
        payload=_payload(),
        issued_at="2026-11-01T00:00:00+00:00",
    )
    still = _beat(db, Path("unused"), retrain=_must_not_retrain)
    assert still["status"] == "no_traffic"
    # 重拍被 (period, reason, outcome) 部分唯一索引吸收——廉价失败不重训
    # 不膨胀账（兜底仍 due，次日拍廉价重试）
    outcomes = db.execute(
        "SELECT outcome FROM shadow_retrain_events ORDER BY id"
    ).fetchall()
    assert [e["outcome"] for e in outcomes] == ["no_live_champion", "no_traffic"]


# ---- deployment/任务接线：注册面推导 + tasks 薄 adapter ----


def test_tasks_wrapper_wires_challenger_dir_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tasks.shadow_retrain_beat 薄 adapter：task_conn + settings 影子模型
    目录下传 beat（ADR-0005：任务实现唯一住所=evaluation/shadow）。"""
    from goalx_backend import tasks as tasks_mod
    from goalx_backend.config import Settings

    captured: dict[str, Any] = {}

    def _fake_beat(conn: object, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "no_trigger"}

    monkeypatch.setattr(shadow, "run_shadow_retrain_beat", _fake_beat)
    settings = Settings()
    report = tasks_mod.shadow_retrain_beat(settings=settings)
    assert report == {"status": "no_trigger"}
    assert captured["challenger_models_dir"] == settings.shadow_models_dir


def test_v57_upgrade_rehearsal_preserves_forecast_rows() -> None:
    """v56 存量库升级演练（test_db MIGRATIONS[:N] 先例）：既有 forecasts
    行经 v57 重建保行；影子轨道/标签列随后就位；重入幂等。"""
    from unittest.mock import patch

    from goalx_backend import db as db_mod

    with patch.object(db_mod, "MIGRATIONS", db_mod.MIGRATIONS[:56]):
        conn = db_mod.connect(":memory:")
        assert db_mod.migrate(conn) == 56
        fid = _seed_fixture(conn)
        # 双轨存量：ml（生产史行）+ llm（他轨同表共存）
        conn.execute(
            """
            INSERT INTO forecasts
                (id, fixture_id, track, model_version, issued_at, content_hash, payload)
            VALUES (11, ?, 'ml', 'dc-old', '2026-10-01T00:00:00+00:00', 'h-old',
                    '{"matrix": [[0.1, 0.2], [0.3, 0.1]]}')
            """,
            (fid,),
        )
        conn.execute(
            """
            INSERT INTO forecasts
                (id, fixture_id, track, model_version, issued_at, content_hash, payload)
            VALUES (12, ?, 'llm', 'llm-old', '2026-10-02T00:00:00+00:00', 'h-llm', '{}')
            """,
            (fid,),
        )
        conn.commit()
    assert db_mod.migrate(conn) == 60  # v57 施行：重建 forecasts（全链恢复后）
    # F3：全七列逐一保行（id 序/track/model_version/issued_at/content_hash/
    # payload 逐字）+ strategy_version_id 不回溯打标
    rows = conn.execute(
        """
        SELECT id, fixture_id, track, model_version, issued_at, content_hash,
               payload, strategy_version_id
        FROM forecasts ORDER BY id
        """
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        (
            11,
            fid,
            "ml",
            "dc-old",
            "2026-10-01T00:00:00+00:00",
            "h-old",
            '{"matrix": [[0.1, 0.2], [0.3, 0.1]]}',
            None,
        ),
        (12, fid, "llm", "llm-old", "2026-10-02T00:00:00+00:00", "h-llm", "{}", None),
    ]
    conn.execute(
        "INSERT OR IGNORE INTO forecasts"
        " (fixture_id, track, model_version, issued_at, content_hash, payload)"
        " VALUES (?, 'shadow', 'shadow-dc-new', '2026-11-01T00:00:00+00:00',"
        " 'h-new', '{}')",
        (fid,),
    )
    assert db_mod.migrate(conn) == 60  # 重入幂等
    # 两存量行 + 一新影子行；重入不重复重建不丢行
    assert conn.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"] == 3
    conn.close()


def test_beat_scope_follows_champion_coverage_and_gate_fail_closed(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    """F4：同流量 scope 直测——业务日两场（一场 champion 覆盖/一场
    no_mapping 未覆盖），beat 后影子行恰在覆盖场、两轨键集相等；
    challenger 三元组在下注闸 fail-closed（version_not_live——策略消费
    恒=live 态 champion）。"""
    from goalx_backend.modelling import dc_model as dcm
    from goalx_backend.models import MatchCodeInput, Tier

    _seed_league_history(db)
    champion_dir = tmp_path / "models"
    challenger_dir = tmp_path / "models-shadow"
    dcm.train_competition(db, "E0", models_dir=champion_dir, n_boot=8, seed=3)
    # 场一：曼联（有别名→champion 覆盖）
    covered = _seed_sale_fixture(
        db,
        code="周五001",
        kickoff="2026-11-20T19:00:00+00:00",
        business_date="2026-11-20",
    )
    # 场二：同业务日、无别名队（champion generate 跳 no_mapping→未覆盖）
    competition = fx_store.upsert_competition(
        db, "英超", tier=Tier.TIER1, odds_api_sport_key="soccer_epl"
    )
    home = fx_store.upsert_team(db, "陌生队")
    away = fx_store.upsert_team(db, "无名队")
    uncovered = fx_store.upsert_fixture(
        db, competition, "2026-11-20T19:00:00+00:00", home, away
    )
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=uncovered,
            kind="jingcai",
            business_date="2026-11-20",
            code="周五002",
            source_match_id="m-002",
            is_single=True,
        ),
    )
    db.commit()
    champion_stats = fc.generate_forecasts(
        db, business_date="2026-11-20", models_dir=str(champion_dir)
    )
    assert champion_stats.generated == 1  # 场二 no_mapping 被跳过
    assert champion_stats.skipped[0]["reason"] == "no_mapping"
    _register_live_champion(db)

    report = _beat(db, challenger_dir)
    assert report["status"] == "registered"

    def _ids(track: str) -> set[int]:
        return {
            int(r["fixture_id"])
            for r in db.execute(
                "SELECT fixture_id FROM forecasts WHERE track=?", (track,)
            )
        }

    assert _ids("shadow") == {covered}  # 影子行恰在覆盖场（scope 生效）
    assert _ids("ml") == {covered}
    assert _ids("shadow") == _ids("ml")  # 两轨键集相等（同流量）
    # 下注闸 fail-closed：**beat 实际注册的** challenger 三元组非 live 态
    # 拒放行（version_not_live，state=shadow——策略消费恒=live 态 champion）
    row = db.execute(
        """
        SELECT estimator_version, calibration_version, params_version
        FROM strategy_versions WHERE id = ?
        """,
        (report["challenger_version_id"],),
    ).fetchone()
    gate = versioning.engine_gate(
        db,
        estimator_version=str(row["estimator_version"]),
        calibration_version=str(row["calibration_version"]),
        params_version=str(row["params_version"]),
    )
    assert gate.allowed is False
    assert gate.reason == "version_not_live"
    assert gate.state is StrategyState.SHADOW
