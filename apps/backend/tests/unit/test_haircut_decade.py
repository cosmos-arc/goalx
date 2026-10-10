"""haircut 十年重估测试（backtest-decade 票 17）。

合成运行面（uniform 彩果 + sid 链）× 合成 gold（gold_env）喂真实配对
全链：SP×fair 数学、样本门槛纪律（n<30 落 default）、scope 命名隔离
（不覆写票 30 行）、overround 统计。真源网络永不进测试。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from goalx_backend.data import gold as gold_mod
from goalx_backend.evaluation import haircut_decade as hcd

DAY = "2026-09-20"


def _gold_row(sid: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sid": sid,
        "league": "英超",
        "kickoff": datetime.fromisoformat(f"{DAY}T20:00:00"),
        "season": "2025-26",
        "home": "A",
        "away": "B",
        "home_goals": 2,
        "away_goals": 1,
        "stage": "常规轮",
        "era": gold_mod.ERA_TRAJECTORY,
        "admin_excluded": False,
        "close1x2_h": 2.0,
        "close1x2_d": 3.4,
        "close1x2_a": 3.8,
        "version": gold_mod.GOLD_VERSION,
    }
    return base | overrides


def _seed_face(face, fixtures: list[tuple[int, str, str, str, str, str]]) -> None:
    """种 uniform 彩果 + sid 链。fixture 元组=(fixture_id, sid, 联赛, 日, SP三向)。"""
    face.execute(
        "INSERT INTO competitions (id, name, created_at) VALUES (1, '英超', 't')"
    )
    face.execute(
        "INSERT INTO teams (id, canonical_name, created_at)"
        " VALUES (1, 'A', 't'), (2, 'B', 't')"
    )
    for fixture_id, _, _, _, _ in fixtures:
        face.execute(  # kickoff 逐场错开（UNIQUE(competition,kickoff,home,away)）
            "INSERT INTO fixtures (id, competition_id, kickoff_utc, home_team_id,"
            " away_team_id) VALUES (?, 1, ?, 1, 2)",
            (fixture_id, f"{DAY}T12:{fixture_id:02d}:00+00:00"),
        )
    for fixture_id, sid, league, day, sp in fixtures:
        face.execute(
            """
            INSERT INTO uniform_result_observations
                (match_id, match_num_str, match_date, fixture_id, league_name,
                 result_status, odds_h, odds_d, odds_a, betting_single, void_flag,
                 observed_at, parse_version, created_at)
            VALUES (?, 'm', ?, ?, ?, 'FIN', ?, ?, ?, 1, 0, 't', 'v1', 't')
            """,
            (fixture_id, day, fixture_id, league, *sp),
        )
        face.execute(
            """
            INSERT INTO source_match_links
                (fixture_id, source, source_match_id, method, status, mapped_at)
            VALUES (?, 'srct', ?, 'primary_key', 'linked', 't')
            """,
            (fixture_id, sid),
        )
    face.commit()


def test_sp_pair_haircut_math(gold_env) -> None:
    """SP×fair 数学：fair=Shin(close1x2)，h = 1 − SP×p 逐 selection。"""
    sids = [f"7{i:03d}" for i in range(12)]
    with gold_env([_gold_row(s) for s in sids]) as (face, duck_con, _store):
        _seed_face(
            face,
            [
                (i + 1, s, "英超", DAY, ("2.10", "3.30", "3.60"))
                for i, s in enumerate(sids)
            ],
        )
        try:
            pairs, counted = hcd.collect_sp_pairs(face, duck_con)
            assert len(pairs) == 12
            assert counted["no_sid_link"] == 0
            assert counted["no_gold_close"] == 0
            first = pairs[0]
            assert first.era == gold_mod.ERA_TRAJECTORY
            assert first.fair_source == "traj_anchor"
            # SP 2.10 × fair_p(h)：haircut = 1 − 2.10×p（正值=竞彩折价）
            assert first.haircuts()[0] == pytest.approx(
                1.0 - 2.10 * first.fair_probs["h"], abs=1e-9
            )
        finally:
            duck_con.close()
            face.close()


def test_calibrate_scope_gating_and_overround(gold_env) -> None:
    """样本门槛（<30 落 default）+ sp-close:* scope 隔离 + overround 统计。"""
    sids = [f"8{i:03d}" for i in range(10)]
    rows = [_gold_row(s) for s in sids]
    with gold_env(rows) as (face, duck_con, store):
        _seed_face(
            face,
            [
                (i + 1, s, "英超", DAY, ("2.20", "3.20", "3.10"))
                for i, s in enumerate(sids)
            ]
            + [(99, "8999", "西甲", DAY, ("2.20", "3.20", "3.10"))],  # 无 gold 行
        )
        try:
            payload = hcd.calibrate_decade(
                face, store, duck_con, today=date(2026, 10, 10)
            )
            by_scope = {r["scope"]: r for r in payload["calibrations"]}
            # 10 场×3 selection=30 样本恰过门槛 → overall calibrated
            assert by_scope["sp-close:overall"]["n_samples"] == 30
            assert by_scope["sp-close:overall"]["source"] == "calibrated"
            # 联赛层 30 样本 <MIN_SAMPLES？英超同 30 → calibrated；西甲 0 行不在表
            assert "sp-close:西甲" not in by_scope
            # 落库行：sp-close:* 独立，票 30 的 overall 行不被动
            db_rows = face.execute(
                "SELECT scope, source, method_version FROM haircut_calibrations"
                " WHERE scope LIKE 'sp-close:%'"
            ).fetchall()
            assert {str(r["method_version"]) for r in db_rows} == {
                hcd.HAIRCUT_SP_METHOD_VERSION
            }
            legacy = face.execute(
                "SELECT COUNT(*) FROM haircut_calibrations WHERE scope='overall'"
            ).fetchone()[0]
            assert legacy == 0  # 未写 overall（票 30 行在真库，本测试面为空）
            # overround：11 行全量统计（含无 gold 的 8999）
            assert payload["overround"]["n_rows"] == 11
            assert payload["overround"]["single_sale"]["n"] == 11
            # SP 2.20/3.20/3.10 → overround = 1/2.2+1/3.2+1/3.1 ≈ 1.2045
            assert payload["overround"]["single_sale"]["median"] == pytest.approx(
                1 / 2.2 + 1 / 3.2 + 1 / 3.1, abs=1e-3
            )
            assert payload["coverage"]["no_gold_close"] == 1
            assert (store.root / "reports" / "haircut-decade.json").exists()
        finally:
            duck_con.close()
            face.close()


def test_calibrate_below_floor_falls_back_default(gold_env) -> None:
    """样本不足（<30）→ source=default、haircut=DEFAULT_HAIRCUT（票 30 纪律）。"""
    sids = ["8001", "8002"]
    with gold_env([_gold_row(s) for s in sids]) as (face, duck_con, store):
        _seed_face(
            face,
            [
                (1, sids[0], "英超", DAY, ("2.0", "3.4", "3.8")),
                (2, sids[1], "英超", DAY, ("2.0", "3.4", "3.8")),
            ],
        )
        try:
            payload = hcd.calibrate_decade(face, store, duck_con)
            overall = next(
                r for r in payload["calibrations"] if r["scope"] == "sp-close:overall"
            )
            assert overall["source"] == "default"
            assert overall["haircut"] == pytest.approx(0.10)
            assert overall["n_samples"] == 6
        finally:
            duck_con.close()
            face.close()


def test_non_numeric_odds_rows_skipped(gold_env) -> None:
    """correctness F1 回归：TEXT 赔率列的坏值行（'abc'/'0.5'）不进配对/统计。"""
    sids = ["8601"]
    with gold_env([_gold_row(s) for s in sids]) as (face, duck_con, _store):
        _seed_face(face, [(31, sids[0], "英超", DAY, ("2.0", "3.4", "3.8"))])
        # 坏行：非数值 SP（字母）与亚 1 值——CAST 数值比较须拦下
        for fid, sp in ((32, ("abc", "3.4", "3.8")), (33, ("0.5", "3.4", "3.8"))):
            face.execute(
                "INSERT INTO fixtures (id, competition_id, kickoff_utc, home_team_id,"
                " away_team_id) VALUES (?, 1, ?, 1, 2)",
                (fid, f"{DAY}T12:{fid:02d}:00+00:00"),
            )
            face.execute(
                """
                INSERT INTO uniform_result_observations
                    (match_id, match_num_str, match_date, fixture_id, league_name,
                     result_status, odds_h, odds_d, odds_a, betting_single,
                     void_flag, observed_at, parse_version, created_at)
                VALUES (?, 'm', ?, ?, '英超', 'FIN', ?, ?, ?, 1, 0, 't', 'v1', 't')
                """,
                (fid, DAY, fid, *sp),
            )
        face.commit()
        try:
            payload = hcd.calibrate_decade(face, _store, duck_con)
            assert payload["overround"]["n_rows"] == 1  # 只有 31 号好行
            pairs, _ = hcd.collect_sp_pairs(face, duck_con)
            assert [p.fixture_id for p in pairs] == [31]
        finally:
            duck_con.close()
            face.close()
