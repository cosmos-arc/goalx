"""票 05 corpus 接入前瞻评分：corpus-only 场次进评分集合（报告一案例 8）。

前瞻评分的市场基准原本只走 legacy odds_snapshots——corpus 欧赔主源独有
场次（有 srct 链、无 legacy 行）全部落 no_market_baseline。修复后
duck_con+asof_batch 透传：srct 链场次 corpus 唯一真值，历史 issued_at
时点经青铜重放读当时可见世界；legacy-only 场次口径逐值不变。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from goalx_backend.data import corpus_duckdb, mapping
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_asof, srct_odds
from goalx_backend.evaluation import forward_validation as fwd
from goalx_backend.models import DrawResultInput, MatchCodeInput, SnapshotInput, Tier

SID = "95001"
KICKOFF = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
FETCHED_1 = "2026-11-04T05:30:00+00:00"
FETCHED_2 = "2026-11-04T06:00:00+00:00"
_ROW_A = "2.10|3.30|3.40|11-04 12:00|0.9|0.9|0.9|2026"
_ROW_A2 = "2.02|3.40|3.55|11-04 13:50|0.9|0.9|0.9|2026"  # 05:50 UTC 变价
_ROW_B = "3.50|3.60|1.90|11-04 11:30|0.9|0.9|0.9|2026"


def _envelope(fetched_at: str, book_a_rows: list[str] | None = None) -> dict:
    """一个 odds_1x2d bronze 信封（书 A 轨迹可多行=变价历史）。"""
    a_rows = ";".join(book_a_rows or [_ROW_A])
    return {
        "provider": srct.SRCT_PROVIDER,
        "dataset": srct.ODDS_DATASET,
        "sid": SID,
        "fetched_at": fetched_at,
        "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        "raw_sha": "0" * 64,
        "payload": {
            "meta": {},
            "game": [
                "9501|800001|TestSharpA|2.10|3.30|3.40|",
                "9502|800002|TestSharpB|3.50|3.60|1.90|",
            ],
            "game_detail": [
                f"800001^{a_rows};",
                f"800002^{_ROW_B};",
            ],
        },
    }


def _build_corpus(
    tmp_path: Path, envelopes: list[dict]
) -> tuple[CorpusStore, duckdb.DuckDBPyConnection]:
    """攒 bronze → silver → duck 库；返回 (store, 只读 duck 连接)。"""
    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(srct.SRCT_PROVIDER, srct.ODDS_DATASET, envelopes)
    srct_odds.build_odds_change_events(
        store, meta={SID: {"sid": SID, "kickoff": KICKOFF, "league": "英超"}}
    )
    con = duckdb.connect(str(corpus_duckdb.build_corpus_duckdb(store)), read_only=True)
    return store, con


def _seed_fixture(
    db, *, label: str = "a", kickoff: str = "2026-11-05T19:00:00+00:00"
) -> int:
    competition = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, f"英超主{label}")
    away = fx_store.upsert_team(db, f"英超客{label}")
    fixture = fx_store.upsert_fixture(db, competition, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture,
            kind="jingcai",
            business_date="2026-11-05",
            code=f"周四{fixture:03d}",
        ),
    )
    db.commit()
    return fixture


def _add_forecast(db, fixture: int, issued_at: str) -> None:
    payload = {"had_probs": {"h": 0.4, "d": 0.3, "a": 0.3}}
    db.execute(
        "INSERT INTO forecasts (fixture_id, track, model_version, issued_at,"
        " content_hash, payload) VALUES (?, 'ml', 'dc-a', ?, ?, ?)",
        (fixture, issued_at, f"hash{fixture}-{issued_at}", json.dumps(payload)),
    )
    db.commit()


def _add_result(db, fixture: int, home: int = 2, away: int = 0) -> None:
    rs_store.upsert_draw_result(
        db, DrawResultInput(fixture_id=fixture, home_goals=home, away_goals=away)
    )


def _link(db, fixture: int) -> None:
    mapping.link_fixture_sid(db, fixture, SID, method="manual")


def _add_legacy_consensus(db, fixture: int, observed: str) -> None:
    for sel, odds in zip(("h", "d", "a"), (2.0, 3.5, 3.5), strict=True):
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


def test_corpus_only_fixture_scores_via_replay(tmp_path: Path, db) -> None:
    """验收 2（报告一案例 8）：只有 corpus 报价、无 legacy 欧赔的场次，
    预测 → 评分进集合；无接线时钉死旧缺口（no_market_baseline=1）。"""
    store, con = _build_corpus(
        tmp_path,
        [_envelope(FETCHED_1), _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2])],
    )
    fixture = _seed_fixture(db)
    _link(db, fixture)
    _add_forecast(db, fixture, "2026-11-04T05:45:00+00:00")
    _add_result(db, fixture)
    try:
        legacy = fwd.forward_skill_report(db).model_dump()  # 无接线：旧口径
        assert legacy["coverage"]["scored"] == 0
        assert legacy["coverage"]["no_market_baseline"] == 1

        wired = fwd.forward_skill_report(
            db, duck_con=con, asof_batch=srct_asof.batch_asof_resolver(store, con)
        ).model_dump()
        assert wired["coverage"]["scored"] == 1
        assert wired["coverage"]["no_market_baseline"] == 0
        (metrics,) = wired["groups"].values()
        assert metrics["n"] == 1
    finally:
        con.close()
        store.close()


def test_replayed_baseline_is_issued_at_world(tmp_path: Path, db) -> None:
    """评分依据=issued_at 当时可见世界：05:45 预测读 05:30 信封（2.10），
    06:05 预测走 silver 最新视图（书 A 已变价 2.02）——主胜隐含概率升。"""
    store, con = _build_corpus(
        tmp_path,
        [_envelope(FETCHED_1), _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2])],
    )
    f_early = _seed_fixture(db, label="early")
    f_late = _seed_fixture(db, label="late")
    for fixture in (f_early, f_late):
        _link(db, fixture)
        _add_result(db, fixture)
    _add_forecast(db, f_early, "2026-11-04T05:45:00+00:00")
    _add_forecast(db, f_late, "2026-11-04T06:05:00+00:00")
    try:
        samples, _ = fwd.build_forward_samples(
            db, duck_con=con, asof_batch=srct_asof.batch_asof_resolver(store, con)
        )
        by_fixture = {s.fixture_id: s for s in samples}
        assert len(samples) == 2
        assert (
            by_fixture[f_early].fair_probs["h"]
            < by_fixture[f_late].fair_probs["h"]  # 2.10 → 2.02 赔率降=概率升
        )
    finally:
        con.close()
        store.close()


def test_legacy_only_scoring_unchanged_with_wiring(tmp_path: Path, db) -> None:
    """验收 4：无 srct 链的 legacy-only 场次，接线前后评分逐值不变。"""
    store, con = _build_corpus(tmp_path, [_envelope(FETCHED_1)])
    fixture = _seed_fixture(db)
    _add_legacy_consensus(db, fixture, "2026-11-05T10:00:00+00:00")
    _add_forecast(db, fixture, "2026-11-05T11:00:00+00:00")
    _add_result(db, fixture)
    try:
        before = fwd.forward_skill_report(db).model_dump()
        after = fwd.forward_skill_report(
            db, duck_con=con, asof_batch=srct_asof.batch_asof_resolver(store, con)
        ).model_dump()
        assert after == before  # 防双计/漂移：无链场次不进 corpus 行源
    finally:
        con.close()
        store.close()


def test_scoring_stable_across_rematerialization(tmp_path: Path, db) -> None:
    """验收 3（与票 04 联合）：同载荷再抓+再物化后，同一 issued_at 的评分
    依据可复现——重放读 bronze 可见信封，不随 silver 最新视图漂移。"""
    fixture = _seed_fixture(db)
    _link(db, fixture)
    _add_forecast(db, fixture, "2026-11-04T05:45:00+00:00")
    _add_result(db, fixture)

    store1, con1 = _build_corpus(tmp_path, [_envelope(FETCHED_1)])
    try:
        before = fwd.forward_skill_report(
            db, duck_con=con1, asof_batch=srct_asof.batch_asof_resolver(store1, con1)
        ).model_dump()
    finally:
        con1.close()
        store1.close()

    shutil.rmtree(tmp_path / "corpus")  # 再物化：追加 06:00 信封（书 A 变价）
    store2, con2 = _build_corpus(
        tmp_path,
        [_envelope(FETCHED_1), _envelope(FETCHED_2, book_a_rows=[_ROW_A, _ROW_A2])],
    )
    try:
        after = fwd.forward_skill_report(
            db, duck_con=con2, asof_batch=srct_asof.batch_asof_resolver(store2, con2)
        ).model_dump()
        assert after == before
    finally:
        con2.close()
        store2.close()
