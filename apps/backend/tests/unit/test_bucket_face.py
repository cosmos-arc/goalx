"""错价桶复盘面测试（misprice-bucket-display 票 02 增量 1）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation import bucket_face as bf
from goalx_backend.models import MatchCodeInput, Tier

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _gold_row(**overrides: Any) -> dict[str, Any]:
    """覆盖式 gold 信号行（只填被测特征，其余缺列=空≠无不判）。"""
    row: dict[str, Any] = {"sid": "1234567", "era": "trajectory"}
    row.update(overrides)
    return row


def test_faces_dist_cut_boundaries() -> None:
    """dist 桶按冻结切点（run5 q33/q67）：low/mid/high 边界与显著面命中。"""
    # ah_n_pre 冻结切点 (461, 693)：300→low（显著）、500→mid（显著）、
    # 700→high（非显著面）
    assert bf.faces_for_gold_row(_gold_row(ah_n_pre=300)) == ["ah_phase_pre:low"]
    assert bf.faces_for_gold_row(_gold_row(ah_n_pre=500)) == ["ah_phase_pre:mid"]
    assert bf.faces_for_gold_row(_gold_row(ah_n_pre=700)) == []
    # ah_n_inplay 冻结切点 (142, 214)：300→high（显著）、100→low（非显著面）
    assert bf.faces_for_gold_row(_gold_row(ah_n_inplay=300)) == ["ah_phase_inplay:high"]
    assert bf.faces_for_gold_row(_gold_row(ah_n_inplay=100)) == []


def test_faces_drift_signs_and_missing() -> None:
    """drift 桶按符号；特征缺列不判（空≠无）。"""
    assert bf.faces_for_gold_row(_gold_row(ou_line_drift=0.25)) == ["ou_line_drift:pos"]
    assert bf.faces_for_gold_row(_gold_row(ou_line_drift=-0.25)) == []
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
    fake.rows = {"1234567": _gold_row(ah_n_pre=300)}
    monkeypatch.setattr(bf.gold_reader, "fetch_signal_rows_by_sids", fake.fetch)
    bf.attach_review_faces(db, object(), [past, future], now=NOW)
    assert past.flags == ["ah_phase_pre:low"]  # 已开球+已链+命中桶
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


def test_real_gold_view_bucketing_smoke() -> None:
    """真 gold 视图冒烟：近 30 天已完赛场有非空桶面分布（7 面可命中）。"""
    con = duckdb.connect("/Users/chevy/goalx-data/duckdb/corpus.duckdb", read_only=True)
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
    assert faces, "近 30 天样本零桶面命中（判据或视图漂移）"
    assert all(flag in {f[0] for f in bf.SIGNIFICANT_FACES} for flag in faces)


def test_frozen_cuts_reproduce_report(monkeypatch) -> None:
    """冻结契约钉死：切点/run5 溯源常量不被手改漂移（重放脚本对齐票面）。"""
    assert bf.FROZEN_CUTS["ah_phase_pre"] == (461.0, 693.0)
    assert bf.FROZEN_CUTS["ah_phase_inplay"] == (142.0, 214.0)
