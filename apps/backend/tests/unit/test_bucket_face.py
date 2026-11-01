"""错价桶复盘面测试（misprice-bucket-display 票 02 增量 1）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation import bucket_face as bf
from goalx_backend.models import MatchCodeInput, Tier

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _gold_row(**overrides: Any) -> dict[str, Any]:
    """覆盖式 gold 信号行（只填被测特征，其余缺列=空≠无不判）。"""
    row: dict[str, Any] = {"sid": "1234567", "era": "trajectory"}
    row.update(overrides)
    return row


def test_faces_volume_family_not_live() -> None:
    """书数族/大小线族撤下面（量纲漂移/数据缺口，增量 2 重校）：即使
    gold 行带这些特征值也不得打面（防回潮）。"""
    row = _gold_row(ah_n_pre=300, ah_n_inplay=300, ou_line_drift=0.25)
    assert bf.faces_for_gold_row(row) == []


def test_faces_drift_signs_and_missing() -> None:
    """drift 桶按符号；特征缺列不判（空≠无）。"""
    # 唯一上线的 drift 族=late_move（devig 派生）；符号语义见下
    # late_move 族走 devig 派生：close 主概率高于 q1h 锚 → pos
    late_up = _gold_row(
        close1x2_h=2.0,
        close1x2_d=3.5,
        close1x2_a=4.0,
        q1h1x2_h=2.4,
        q1h1x2_d=3.4,
        q1h1x2_a=3.2,
    )
    assert bf.faces_for_gold_row(late_up) == ["late_move_h:pos"]
    late_down = _gold_row(
        close1x2_h=2.4,
        close1x2_d=3.4,
        close1x2_a=3.2,
        q1h1x2_h=2.0,
        q1h1x2_d=3.5,
        q1h1x2_a=4.0,
    )
    assert bf.faces_for_gold_row(late_down) == []  # 下行不显著面零命中
    # 全缺 → 无面
    assert bf.faces_for_gold_row(_gold_row()) == []


@dataclass
class _Row:
    """今日页行的最小形状（duck typing 接缝）。"""

    fixture_id: int
    kickoff_utc: str
    flags: list[str] = field(default_factory=list)


def _seed_linked_fixture(db: sqlite3.Connection, kickoff: str) -> int:
    comp = fx_store.upsert_competition(db, "英超", tier=Tier.TIER1)
    home = fx_store.upsert_team(db, "阿森纳")
    away = fx_store.upsert_team(db, "切尔西")
    fid = fx_store.upsert_fixture(db, comp, kickoff, home, away)
    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fid, kind="jingcai", business_date="2026-09-12", code="周六001"
        ),
    )
    db.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id, method,"
        " status, mapped_at) VALUES (?, 'srct', '1234567', 'primary_key', 'linked', ?)",
        (fid, "2026-09-12T00:00:00+00:00"),
    )
    db.commit()
    return fid


@dataclass
class _FakeDuck:
    """fetch_signal_rows_by_sids 的替身载体（monkeypatch 消费）。"""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def fetch(self, _duck: object, sids: list[str]) -> dict[str, dict[str, Any]]:
        return {sid: self.rows[sid] for sid in sids if sid in self.rows}


def test_attach_only_past_and_linked(db: sqlite3.Connection, monkeypatch) -> None:
    """PIT 门：未开球不标；已开球且已链 → gold 桶面进 flags；无链跳过。"""
    past = _Row(
        fixture_id=_seed_linked_fixture(db, "2026-11-01T10:00:00+00:00"),
        kickoff_utc="2026-11-01T10:00:00+00:00",
    )
    future = _Row(
        fixture_id=_seed_linked_fixture(db, (NOW + timedelta(hours=3)).isoformat()),
        kickoff_utc=(NOW + timedelta(hours=3)).isoformat(),
    )
    fake = _FakeDuck()
    fake.rows = {
        "1234567": _gold_row(
            close1x2_h=2.0,
            close1x2_d=3.5,
            close1x2_a=4.0,
            q1h1x2_h=2.4,
            q1h1x2_d=3.4,
            q1h1x2_a=3.2,
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.3,
        )
    }
    monkeypatch.setattr(bf.gold_reader, "fetch_signal_rows_by_sids", fake.fetch)
    bf.attach_review_faces(db, object(), [past, future], now=NOW)
    assert past.flags == ["late_move_h:pos", "late_move24_h:pos"]  # 已开球+已链+命中桶
    assert future.flags == []  # 未开球：判据未齐不标


def test_attach_degrades_on_missing_view(db: sqlite3.Connection, monkeypatch) -> None:
    """gold 视图缺席（RuntimeError）→ 静默降级，不炸主表。"""
    past = _Row(
        fixture_id=_seed_linked_fixture(db, "2026-11-01T10:00:00+00:00"),
        kickoff_utc="2026-11-01T10:00:00+00:00",
    )

    def boom(*args: object, **kwargs: object) -> dict[str, dict[str, Any]]:
        raise RuntimeError("match_features 视图不可用")

    monkeypatch.setattr(bf.gold_reader, "fetch_signal_rows_by_sids", boom)
    bf.attach_review_faces(db, object(), [past], now=NOW)
    assert past.flags == []


def test_attach_noop_without_duck(db: sqlite3.Connection) -> None:
    """duck 连接缺席（corpus 桥未建）→ 零动作。"""
    past = _Row(fixture_id=1, kickoff_utc="2026-11-01T10:00:00+00:00")
    bf.attach_review_faces(db, None, [past], now=NOW)
    assert past.flags == []


def test_attach_legacy_naive_kickoff_never_500s(
    db: sqlite3.Connection, monkeypatch
) -> None:
    """correctness F1：v1 legacy 北京墙钟 naive 串（"2026-10-01 19:30:00"）
    由属主转换判开球（CST→UTC），不炸端点；垃圾串静默跳过。"""
    naive_past = _Row(
        fixture_id=_seed_linked_fixture(db, "2026-10-01 19:30:00"),
        kickoff_utc="2026-10-01 19:30:00",  # 北京墙钟=NOW 前 31 天
    )
    garbage = _Row(fixture_id=99999, kickoff_utc="not-a-date")
    fake = _FakeDuck()
    fake.rows = {
        "1234567": _gold_row(
            close1x2_h=2.0,
            close1x2_d=3.5,
            close1x2_a=4.0,
            q1h1x2_h=2.4,
            q1h1x2_d=3.4,
            q1h1x2_a=3.2,
            q24h1x2_h=2.4,
            q24h1x2_d=3.4,
            q24h1x2_a=3.3,
        )
    }
    monkeypatch.setattr(bf.gold_reader, "fetch_signal_rows_by_sids", fake.fetch)
    bf.attach_review_faces(db, object(), [naive_past, garbage], now=NOW)
    assert naive_past.flags == ["late_move_h:pos", "late_move24_h:pos"]
    assert garbage.flags == []


def test_attach_gold_absent_row_stays_unflagged(
    db: sqlite3.Connection, monkeypatch
) -> None:
    """空≠无钉死（F2）：已开球已链但 gold 无该 sid（重建断线形态）→
    零 flag 且不炸——与"不在桶"同形，观测走 serve 日志计数。"""
    past = _Row(
        fixture_id=_seed_linked_fixture(db, "2026-11-01T10:00:00+00:00"),
        kickoff_utc="2026-11-01T10:00:00+00:00",
    )
    fake = _FakeDuck()  # 空 rows：gold 全缺
    monkeypatch.setattr(bf.gold_reader, "fetch_signal_rows_by_sids", fake.fetch)
    bf.attach_review_faces(db, object(), [past], now=NOW)
    assert past.flags == []


_CORPUS_DUCK = Path.home() / "goalx-data" / "duckdb" / "corpus.duckdb"


@pytest.mark.skipif(
    not _CORPUS_DUCK.exists(), reason="本机语料桥缺席（CI 跳过，真树跑）"
)
def test_real_gold_view_bucketing_smoke() -> None:
    """真 gold 视图冒烟：近 30 天已完赛场 3 上线面各至少命中一次。"""
    try:
        con = duckdb.connect(str(_CORPUS_DUCK), read_only=True)
    except duckdb.IOException:
        pytest.skip("本机语料桥不可开（CI 挂载形态），真树跑")
    from goalx_backend.data import gold_reader

    rows = gold_reader.fetch_signal_rows_by_sids(
        con,
        [
            str(r[0])
            for r in con.execute(
                "SELECT sid FROM match_features WHERE kickoff >= '2026-10-01'"
                " ORDER BY kickoff DESC LIMIT 200"
            ).fetchall()
        ],
    )
    assert rows, "gold 十月无行（先跑 gold-build）"
    faces: dict[str, int] = {}
    for row in rows.values():
        for flag in bf.faces_for_gold_row(row):
            faces[flag] = faces.get(flag, 0) + 1
    significant = {f[0] for f in bf.SIGNIFICANT_FACES}
    missing = significant - set(faces)
    assert not missing, f"近 30 天样本缺桶面 {missing}（判据或视图漂移）"
    assert set(faces) <= significant


@pytest.mark.skipif(
    not (
        _CORPUS_DUCK.exists()
        and (
            Path.home() / "goalx-data" / "reports" / "signal-attribution.json"
        ).exists()
    ),
    reason="本机 run5 报告/语料桥缺席（CI 跳过）",
)
def test_live_faces_reproduce_report() -> None:
    """上线面契约验收（票 02 验收①）：run5 样本重放 → 3 drift 面 n 与
    signal-attribution 报告逐位一致（历史归属复算对上）。"""
    import json
    import sqlite3 as sq

    from goalx_backend.data import gold_reader
    from goalx_backend.evaluation import signal_attribution as sa

    repo_db = Path(__file__).resolve().parents[4] / "data" / "goalx.db"
    db = sq.connect(f"file:{repo_db}?mode=ro", uri=True)
    db.row_factory = sq.Row
    pred_rows = db.execute(sa._PREDICTIONS_SQL, (5,)).fetchall()
    db.close()
    try:
        duck = duckdb.connect(str(_CORPUS_DUCK), read_only=True)
    except duckdb.IOException:
        pytest.skip("本机语料桥不可开（CI 挂载形态），真树跑")
    sids = [str(r["match_key"]) for r in pred_rows if r["ftr"] is not None]
    gold = gold_reader.fetch_signal_rows_by_sids(duck, sids)
    samples, _ = sa._collect_samples(pred_rows, gold)
    report = json.loads(
        (Path.home() / "goalx-data" / "reports" / "signal-attribution.json").read_text(
            encoding="utf-8"
        )
    )
    for flag, feat, kind, bucket in bf.SIGNIFICANT_FACES:
        n_hits = sum(
            1
            for s in samples
            if feat in s.features
            and sa.bucket_of(s.features[feat], kind, (0.0, 0.0)) == bucket
        )
        assert n_hits == report["faces"][feat][bucket]["n"], flag
