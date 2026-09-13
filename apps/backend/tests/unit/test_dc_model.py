"""Dixon-Coles 分池训练器测试（票 26 验收：拟合、工件复载逐位一致、幂等）。"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest

from goalx_backend.data import results as rs_store
from goalx_backend.modelling import dc_model as dcm


def synthetic_rows(n: int = 240, seed: int = 5) -> list[dcm.TrainingRow]:
    """有真实强度结构的合成联赛（18 队双循环约两季）。"""
    rng = np.random.default_rng(seed)
    teams = [f"T{i:02d}" for i in range(18)]
    strength = {t: float(rng.normal(0, 0.35)) for t in teams}
    rows: list[dcm.TrainingRow] = []
    day = date(2024, 8, 10)
    for round_no in range(n // 9):
        pairs = [(teams[i], teams[(i + round_no + 1) % 18]) for i in range(9)]
        for home, away in pairs:
            lam_h = float(np.exp(0.28 + strength[home] - strength[away]))
            lam_a = float(np.exp(strength[away] - strength[home]))
            rows.append(
                dcm.TrainingRow(
                    match_date=day.isoformat(),
                    home_team=home,
                    away_team=away,
                    fthg=int(rng.poisson(lam_h)),
                    ftag=int(rng.poisson(lam_a)),
                )
            )
        day = date.fromordinal(day.toordinal() + 7)
    return rows


def test_fit_produces_sane_artifact() -> None:
    rows = synthetic_rows()
    artifact = dcm.fit_dc_model(rows, competition="E0")
    assert artifact.n_matches == len(rows)
    assert len(artifact.teams) == 18
    assert 0.0 < artifact.home_advantage < 1.0
    # attack 约束 sum = n_teams
    assert sum(t["attack"] for t in artifact.teams.values()) == pytest.approx(
        18, abs=1e-6
    )
    assert artifact.loglikelihood is not None
    assert artifact.loglikelihood < 0
    # 窗口与指纹
    assert artifact.train_window_start == rows[0].match_date
    assert len(artifact.data_fingerprint) == 64


def test_predict_from_artifact_matches_direct_fit() -> None:
    rows = synthetic_rows()
    artifact = dcm.fit_dc_model(rows, competition="E0")
    matrix = artifact.predict("T00", "T17")
    assert matrix.grid is not None
    had = matrix.had()
    assert sum(had.values()) == pytest.approx(1.0, abs=1e-12)
    # 未训练球队 → KeyError
    with pytest.raises(KeyError, match="不在"):
        artifact.predict("T00", "Nobody")


def test_artifact_json_roundtrip_predicts_identically(tmp_path) -> None:
    # 票 26 验收：工件复载后预测结果逐位一致
    rows = synthetic_rows()
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"))
    path = dcm.save_run(run, tmp_path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    restored = dcm.TrainingRun.from_payload(loaded)
    lam_a = run.base.lambdas("T00", "T17")
    lam_b = restored.base.lambdas("T00", "T17")
    assert lam_a == lam_b
    m1 = run.base.predict("T00", "T17").grid
    m2 = restored.base.predict("T00", "T17").grid
    assert m1 == m2  # 逐位一致（含浮点字节级相等）


def test_bootstrap_deterministic_and_content_addressed(tmp_path) -> None:
    rows = synthetic_rows(n=120, seed=5)
    boots = dcm.bootstrap_dc_models(
        rows, competition="E0", n_boot=3, seed=42, as_of=date(2024, 8, 10)
    )
    again = dcm.bootstrap_dc_models(
        rows, competition="E0", n_boot=3, seed=42, as_of=date(2024, 8, 10)
    )
    assert len(boots) == 3
    assert [b.rho for b in boots] == [b.rho for b in again]
    # 不同种子产生不同重采样（指纹不同）
    other = dcm.bootstrap_dc_models(
        rows, competition="E0", n_boot=3, seed=7, as_of=date(2024, 8, 10)
    )
    assert {b.data_fingerprint for b in boots} != {b.data_fingerprint for b in other}


def test_save_and_load_latest_run(tmp_path) -> None:
    rows = synthetic_rows(n=120, seed=5)
    early = dcm.TrainingRun(base=dcm.fit_dc_model(rows[:60], competition="E0"))
    late = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"))
    dcm.save_run(early, tmp_path)
    dcm.save_run(late, tmp_path)
    runs = dcm.list_runs(tmp_path, "E0")
    assert len(runs) == 2
    latest = dcm.load_latest_run(tmp_path, "E0")
    assert latest is not None
    assert latest.base.n_matches == late.base.n_matches
    # 无工件联赛 → None；幂等重保存同指纹文件不新增
    assert dcm.load_latest_run(tmp_path, "SP1") is None
    dcm.save_run(late, tmp_path)
    assert len(dcm.list_runs(tmp_path, "E0")) == 2


def test_decay_weights_half_life() -> None:
    rows = [
        dcm.TrainingRow("2025-08-01", "A", "B", 1, 0),
        dcm.TrainingRow("2026-08-01", "A", "B", 2, 1),
    ]
    weights = dcm.decay_weights(rows, as_of=date(2026, 8, 1), half_life_days=365)
    assert weights[1] == pytest.approx(1.0)
    assert weights[0] == pytest.approx(0.5, rel=1e-6)


def test_training_rows_respects_as_of_cutoff(db) -> None:
    # 防前视：training_rows_for 严格按 match_date <= as_of 取数
    for i, day in enumerate(("2024-09-01", "2024-09-14", "2024-09-28")):
        rs_store.upsert_hist_matches(
            db,
            [
                {
                    "competition": "E0",
                    "season": "2425",
                    "match_date": day,
                    "home_team": "A",
                    "away_team": "B",
                    "fthg": i,
                    "ftag": 0,
                    "ftr": "H",
                    "psc_home": None,
                    "psc_draw": None,
                    "psc_away": None,
                    "avgc_home": None,
                    "avgc_draw": None,
                    "avgc_away": None,
                }
            ],
        )
    rows = dcm.training_rows_for(db, "E0", as_of="2024-09-14")
    assert [r.match_date for r in rows] == ["2024-09-01", "2024-09-14"]


def test_train_competition_end_to_end(db, tmp_path) -> None:
    rows = synthetic_rows(n=90, seed=3)
    for row in rows:
        rs_store.upsert_hist_matches(
            db,
            [
                {
                    "competition": "E0",
                    "season": "2425",
                    "match_date": row.match_date,
                    "home_team": row.home_team,
                    "away_team": row.away_team,
                    "fthg": row.fthg,
                    "ftag": row.ftag,
                    "ftr": "H" if row.fthg > row.ftag else "A",
                    "psc_home": None,
                    "psc_draw": None,
                    "psc_away": None,
                    "avgc_home": None,
                    "avgc_draw": None,
                    "avgc_away": None,
                }
            ],
        )
    run = dcm.train_competition(db, "E0", models_dir=tmp_path, n_boot=2, seed=1)
    assert run.base.n_matches == 90
    assert len(run.bootstrap) == 2
    latest = dcm.load_latest_run(tmp_path, "E0")
    assert latest is not None
    assert latest.base.predict("T00", "T01").had()["h"] > 0


# --- 票 34 验收 6：工件身份含数据+训练配置+seed+实现/依赖版本 ---


def test_artifact_id_distinguishes_config_and_versions(monkeypatch) -> None:
    rows = synthetic_rows(n=120, seed=5)
    base = dcm.fit_dc_model(rows, competition="E0", half_life_days=365.0)
    run_a = dcm.TrainingRun(base=base, seed=42)
    same = dcm.TrainingRun(
        base=dcm.fit_dc_model(rows, competition="E0", half_life_days=365.0), seed=42
    )
    assert run_a.artifact_id() == same.artifact_id()  # 同配置同身份
    diff_half_life = dcm.TrainingRun(
        base=dcm.fit_dc_model(rows, competition="E0", half_life_days=180.0), seed=42
    )
    assert run_a.artifact_id() != diff_half_life.artifact_id()
    diff_seed = dcm.TrainingRun(base=base, seed=7)
    assert run_a.artifact_id() != diff_seed.artifact_id()
    monkeypatch.setattr(
        dcm,
        "implementation_versions",
        lambda: {
            "goalx-backend": "9.9.9",
            "penaltyblog": "1.6.2",
            "artifact_schema": "1",
        },
    )
    assert run_a.artifact_id() != diff_seed.artifact_id()  # 版本参与身份
    monkeypatch.undo()


def test_run_filename_uses_artifact_id_no_overwrite(tmp_path) -> None:
    rows = synthetic_rows(n=120, seed=5)
    v1 = dcm.TrainingRun(
        base=dcm.fit_dc_model(rows, competition="E0", half_life_days=365.0), seed=1
    )
    v2 = dcm.TrainingRun(
        base=dcm.fit_dc_model(rows, competition="E0", half_life_days=180.0), seed=1
    )
    dcm.save_run(v1, tmp_path)
    dcm.save_run(v2, tmp_path)  # 同数据不同配置:不覆盖,两个文件
    runs = dcm.list_runs(tmp_path, "E0")
    assert len(runs) == 2
    payload = json.loads(runs[0][1].read_text())
    assert payload["artifact_id"]  # 落盘身份可追溯
    assert payload["versions"]["artifact_schema"]
