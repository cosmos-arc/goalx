"""前瞻评分集合测试（票 34 验收 3：赛前证据边界与冻结规则）。"""

from __future__ import annotations

import json

from goalx_backend import forward_validation as fwd
from goalx_backend.models import DrawResultInput, MatchCodeInput, SnapshotInput, Tier
from goalx_backend.store import fixtures as fx_store
from goalx_backend.store import results as rs_store

KICKOFF = "2026-09-13T18:00:00+00:00"


def seed_fixture(db, *, league: str = "英超", kickoff: str = KICKOFF) -> int:
    competition = fx_store.upsert_competition(db, league, tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"{league}主")
    away = fx_store.upsert_team(db, f"{league}客")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-09-13",
            code=f"周日{fixture:03d}",
        ),
    )
    db.commit()
    return fixture


def add_forecast(
    db, fixture: int, issued_at: str, *, model_version: str = "dc-v1", tag: int = 0
) -> None:
    payload = {"had_probs": {"h": 0.4 + 0.01 * tag, "d": 0.3, "a": 0.3}, "tag": tag}
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', ?, ?, ?, ?)",
        (
            fixture,
            model_version,
            issued_at,
            f"hash{fixture}-{tag}",
            json.dumps(payload),
        ),
    )
    db.commit()


def add_eu_consensus(
    db,
    fixture: int,
    observed: str,
    prices: tuple[float, float, float] = (2.0, 3.5, 3.5),
) -> None:
    for sel, odds in zip(("h", "d", "a"), prices, strict=True):
        fx_store.insert_odds_snapshot(
            db,
            SnapshotInput(
                fixture_id=fixture,
                market_code="had",
                selection_code=sel,
                source="odds_api:pin",
                odds=odds,
                captured_at=observed,
                observed_at=observed,
            ),
        )
    db.commit()


def add_result(db, fixture: int, home: int = 2, away: int = 0) -> None:
    rs_store.upsert_draw_result(
        db, DrawResultInput(fixture_id=fixture, home_goals=home, away_goals=away)
    )


def test_pre_kickoff_forecast_scored_with_baseline(db) -> None:
    fixture = seed_fixture(db)
    add_eu_consensus(db, fixture, "2026-09-13T10:00:00+00:00")
    add_forecast(db, fixture, "2026-09-13T11:00:00+00:00", model_version="dc-a")
    add_result(db, fixture)
    report = fwd.forward_skill_report(db)
    assert report["coverage"]["scored"] == 1
    assert report["coverage"]["settled_fixtures"] == 1
    metrics = report["groups"]["dc-a"]
    assert metrics["n"] == 1
    assert metrics["n_fixtures"] == 1
    assert "skill_rps" in metrics


def test_post_kickoff_forecast_is_replay_only(db) -> None:
    """赛后才生成的预测只是历史 replay,不进前瞻集合（票 34）。"""
    fixture = seed_fixture(db)
    add_eu_consensus(db, fixture, "2026-09-13T10:00:00+00:00")
    add_forecast(db, fixture, "2026-09-13T19:00:00+00:00")  # kickoff 之后
    add_result(db, fixture)
    report = fwd.forward_skill_report(db)
    assert report["coverage"]["post_kickoff_only"] == 1
    assert report["coverage"]["scored"] == 0
    assert report["groups"] == {}


def test_unsettled_and_unforecast_fixtures_counted_honestly(db) -> None:
    fixture = seed_fixture(db)  # 无结果(未来工件):不进集合也不计已结
    add_forecast(db, fixture, "2026-09-13T11:00:00+00:00")
    other = seed_fixture(db, league="西甲")
    add_result(db, other)  # 已结但无预测
    report = fwd.forward_skill_report(db)
    assert report["coverage"]["settled_fixtures"] == 1
    assert report["coverage"]["no_forecast"] == 1
    assert report["coverage"]["scored"] == 0


def test_duplicate_forecasts_freeze_latest_pre_kickoff(db) -> None:
    """同场多条赛前 Forecast:冻结最新一条（固定规则）,不重复计样本。"""
    fixture = seed_fixture(db)
    add_eu_consensus(db, fixture, "2026-09-13T09:00:00+00:00")
    add_forecast(db, fixture, "2026-09-13T10:00:00+00:00", tag=0)
    add_forecast(db, fixture, "2026-09-13T12:00:00+00:00", tag=5)
    add_result(db, fixture)
    report = fwd.forward_skill_report(db)
    assert report["coverage"]["scored"] == 1  # 一场一个样本
    (metrics,) = report["groups"].values()
    assert metrics["n"] == 1


def test_baseline_uses_only_observations_before_issued_at(db) -> None:
    fixture = seed_fixture(db)
    add_eu_consensus(db, fixture, "2026-09-13T09:00:00+00:00", (2.5, 3.3, 3.0))
    # 预测时点之后才观测到的共识不能用
    add_eu_consensus(db, fixture, "2026-09-13T12:00:00+00:00", (1.5, 4.0, 6.0))
    add_forecast(db, fixture, "2026-09-13T10:00:00+00:00")
    add_result(db, fixture)
    samples, _ = fwd.build_forward_samples(db)
    (sample,) = samples
    # 用 09:00 的共识(2.5/3.3/3.0),不是 12:00 的
    assert sample.fair_probs["h"] < 0.45


def test_no_market_baseline_excluded_with_denominator(db) -> None:
    fixture = seed_fixture(db)
    add_forecast(db, fixture, "2026-09-13T10:00:00+00:00")
    add_result(db, fixture)
    _, counts = fwd.build_forward_samples(db)
    assert counts["no_market_baseline"] == 1
    assert counts["scored"] == 0


def test_groups_split_by_model_version(db) -> None:
    """不同策略/模型版本分组,不混成一个数字（票 34）。"""
    f1 = seed_fixture(db, league="英超")
    f2 = seed_fixture(db, league="西甲")
    for fixture in (f1, f2):
        add_eu_consensus(db, fixture, "2026-09-13T09:00:00+00:00")
        add_result(db, fixture)
    add_forecast(db, f1, "2026-09-13T10:00:00+00:00", model_version="dc-a")
    add_forecast(db, f2, "2026-09-13T10:00:00+00:00", model_version="dc-b")
    report = fwd.forward_skill_report(db)
    assert set(report["groups"]) == {"dc-a", "dc-b"}
    assert all(m["n"] == 1 for m in report["groups"].values())
    assert all(m["insufficient_samples"] for m in report["groups"].values())
