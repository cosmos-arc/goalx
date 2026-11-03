"""Dixon-Coles 分池训练器测试（票 26 验收：拟合、工件复载逐位一致、幂等）。"""

from __future__ import annotations

import hashlib
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


def test_training_rows_excludes_admin_matches(db) -> None:
    """票 14 正反用例：行政判赛行（假 3-0）不进 live 训练面，正常场不误伤。"""
    for home, away, day in (
        ("HomeX", "AwayY", "2024-09-01"),  # 窗内任一侧命中 → 剔除
        ("AwayY", "OtherZ", "2024-09-08"),  # 客侧命中 → 剔除
        ("OtherZ", "HomeQ", "2024-09-15"),  # 无关队 → 保留
        ("HomeX", "AwayY", "2024-12-31"),  # 命中队但窗外 → 保留（含端点外）
    ):
        rs_store.upsert_hist_matches(
            db,
            [
                {
                    "competition": "E0",
                    "season": "2425",
                    "match_date": day,
                    "home_team": home,
                    "away_team": away,
                    "fthg": 3,
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
    db.execute(
        "INSERT INTO admin_match_exclusions"
        " (competition, team, date_start, date_end, reason, created_at)"
        " VALUES ('E0', 'HomeX', '2024-09-01', '2024-09-30', '测试排除窗', 't'),"
        " ('E0', 'AwayY', '2024-09-01', '2024-09-30', '测试排除窗', 't')"
    )
    db.commit()
    rows = dcm.training_rows_for(db, "E0", as_of="2024-12-31")
    assert [(r.home_team, r.away_team) for r in rows] == [
        ("OtherZ", "HomeQ"),
        ("HomeX", "AwayY"),
    ]  # 剔除前两场（窗内命中），保留后两场（无关队/窗外）


def test_training_rows_fail_closed_without_exclusion_table(db) -> None:
    """排除表缺席=未迁移面：炸训练优于静默吃假 3-0（hygiene fail-closed 传导）。"""
    db.execute("DROP TABLE admin_match_exclusions")
    db.commit()
    with pytest.raises(Exception, match="admin_match_exclusions"):
        dcm.training_rows_for(db, "E0", as_of="2024-09-14")


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
            "artifact_schema": "2",
        },
    )
    assert run_a.artifact_id() != diff_seed.artifact_id()  # 版本参与身份
    # 票 05：显式版本字段（复载冻结形态）同样参与身份
    frozen = dcm.TrainingRun(base=base, seed=42, versions={"goalx-backend": "0.0.1"})
    assert frozen.artifact_id() != run_a.artifact_id()
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


# --- 票 05：bootstrap 进身份 + 复载冻结版本 + 篡改拒绝 ---


def test_artifact_id_distinguishes_bootstrap_two_files(tmp_path) -> None:
    """同数据同 half-life 同 seed：bootstrap=[] vs 非空 → 不同身份,两文件互不覆盖。"""
    rows = synthetic_rows(n=120, seed=5)
    base = dcm.fit_dc_model(rows, competition="E0", half_life_days=365.0)
    plain = dcm.TrainingRun(base=base, seed=42)
    booted = dcm.TrainingRun(base=base, bootstrap=[base], seed=42)
    assert plain.artifact_id() != booted.artifact_id()
    dcm.save_run(plain, tmp_path)
    dcm.save_run(booted, tmp_path)
    runs = dcm.list_runs(tmp_path, "E0")
    assert len(runs) == 2  # v1 会同身份互相覆盖,这里必须两个文件


def test_from_payload_freezes_versions_and_rejects_tamper(
    tmp_path, monkeypatch
) -> None:
    """环境(implementation_versions)升级后复载:身份与落盘时一致;篡改身份被拒。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    path = dcm.save_run(run, tmp_path)
    original_id = run.artifact_id()

    monkeypatch.setattr(
        dcm,
        "implementation_versions",
        lambda: {"goalx-backend": "99.0.0", "artifact_schema": "99"},
    )
    loaded = dcm.TrainingRun.from_payload(json.loads(path.read_text(encoding="utf-8")))
    assert loaded.artifact_id() == original_id  # 冻结训练环境版本,不随环境漂移

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["artifact_id"] = "0" * 64
    with pytest.raises(ValueError, match="工件身份与内容不一致"):
        dcm.TrainingRun.from_payload(tampered)


def test_from_payload_legacy_schema1_keeps_stored_id(tmp_path) -> None:
    """v1 旧工件（身份公式不含 bootstrap 数量）按落盘 artifact_id 寻址。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    payload = run.to_payload()
    # v1 公式快照（历史形态,勿与现行公式同步）——盘面 42/42 v1 工件实证匹配
    legacy_canonical = json.dumps(
        {
            "data_fingerprint": run.base.data_fingerprint,
            "half_life_days": run.base.half_life_days,
            "seed": run.seed,
            "versions": payload["versions"],
        },
        sort_keys=True,
    )
    legacy_id = hashlib.sha256(legacy_canonical.encode()).hexdigest()
    payload["schema_version"] = 1
    payload["artifact_id"] = legacy_id
    loaded = dcm.TrainingRun.from_payload(payload)
    assert loaded.artifact_id() == legacy_id  # 旧工件身份=落盘值,不重算漂移
    # 环境升级后复载 v1 工件仍稳定（versions 冻结自 payload）
    assert loaded.versions == payload["versions"]


def test_from_payload_v1_tamper_and_schema_downgrade_rejected() -> None:
    """v1 严校验:篡改 v1 身份、把 v2 文件降级 schema 绕过校验,都被拒。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    payload = run.to_payload()

    # 降级攻击:v2 内容贴 schema=1 标签——v1 公式重算对不上 v2 身份
    downgraded = {**payload, "schema_version": 1}
    with pytest.raises(ValueError, match="工件身份与内容不一致"):
        dcm.TrainingRun.from_payload(downgraded)

    # 直接篡改一个合法 v1 工件的身份字段
    loaded = dcm.TrainingRun.from_payload(
        {
            **payload,
            "schema_version": 1,
            "artifact_id": run._v1_artifact_id(),
        }
    )
    assert loaded.artifact_id() == run._v1_artifact_id()
    tampered = {**payload, "schema_version": 1, "artifact_id": "0" * 64}
    with pytest.raises(ValueError, match="工件身份与内容不一致"):
        dcm.TrainingRun.from_payload(tampered)


def test_legacy_roundtrip_resave_stays_loadable(tmp_path) -> None:
    """v1 旧工件复载→重存:仍按 v1 schema/身份写出,再复载不自坏。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    legacy_id = run._v1_artifact_id()
    loaded = dcm.TrainingRun.from_payload(
        {**run.to_payload(), "schema_version": 1, "artifact_id": legacy_id}
    )
    path = dcm.save_run(loaded, tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1  # 不因重存抬升 schema 时代
    again = dcm.TrainingRun.from_payload(on_disk)
    assert again.artifact_id() == legacy_id


# --- 票 05 correctness 轴回归 ---


def test_list_runs_skips_single_bad_artifact(tmp_path) -> None:
    """单个坏工件不毒化整目录:其余照常可载,load_latest_run 不死。"""
    rows = synthetic_rows(n=120, seed=5)
    good = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    dcm.save_run(good, tmp_path)
    # 毒文件:未来 schema(先用新版本训练再回滚的形态)
    poisoned = good.to_payload()
    poisoned["schema_version"] = 3
    (tmp_path / "E0" / "E0-poison.json").write_text(
        json.dumps(poisoned), encoding="utf-8"
    )
    # 篡改 id 的文件也跳过
    tampered = good.to_payload()
    tampered["artifact_id"] = "0" * 64
    (tmp_path / "E0" / "E0-tamper.json").write_text(
        json.dumps(tampered), encoding="utf-8"
    )
    runs = dcm.list_runs(tmp_path, "E0")
    assert len(runs) == 1  # 只有合法件
    latest = dcm.load_latest_run(tmp_path, "E0")
    assert latest is not None
    assert latest.artifact_id() == good.artifact_id()


def test_from_payload_rejects_unknown_schema_and_non_str_id() -> None:
    """未来 schema 显式报不支持(不落 v1 分支误报);非字符串身份拒绝。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    future = {**run.to_payload(), "schema_version": 3}
    with pytest.raises(ValueError, match="不支持的身份 schema: 3"):
        dcm.TrainingRun.from_payload(future)
    # schema=1 有身份却无版本记录:拼装件,同报不支持
    no_versions = {
        **run.to_payload(),
        "schema_version": 1,
        "artifact_id": "0" * 64,
        "versions": None,
    }
    with pytest.raises(ValueError, match="不支持的身份 schema"):
        dcm.TrainingRun.from_payload(no_versions)
    # 非字符串 artifact_id 不得静默当作无身份工件
    with pytest.raises(ValueError, match="artifact_id 类型非法"):
        dcm.TrainingRun.from_payload({**run.to_payload(), "artifact_id": 12345})


def test_resave_after_mutation_rejected(tmp_path) -> None:
    """复载后改内容再以冻结身份重存=写身份与内容不符的坏文件,拒绝。"""
    rows = synthetic_rows(n=120, seed=5)
    run = dcm.TrainingRun(base=dcm.fit_dc_model(rows, competition="E0"), seed=42)
    loaded = dcm.TrainingRun.from_payload(run.to_payload())
    loaded.bootstrap.append(loaded.base)  # 内容变更,冻结身份过期
    with pytest.raises(ValueError, match="冻结身份失效"):
        loaded.to_payload()
    with pytest.raises(ValueError, match="冻结身份失效"):
        dcm.save_run(loaded, tmp_path)
