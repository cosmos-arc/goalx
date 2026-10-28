"""运行指纹测试（report-audit-20261027 票 06）。

确定性纪律（无易变字段）、内容敏感性（改行必换哈希）、缺席面如实置
None、空表弱指纹、空语料树清单。真 git/真仓库路径：测试在 repo 内跑，
git 面必有 SHA。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from goalx_backend.betting import fingerprint as betting_fp
from goalx_backend.data import fingerprint as data_fp
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import connect, migrate
from goalx_backend.run_fingerprint import build_run_fingerprint

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _migrated_db(tmp_path: Path):
    conn = connect(tmp_path / "fp.db")
    migrate(conn)
    return conn


def test_bare_fingerprint_has_no_volatile_fields() -> None:
    """conn/store 缺席 → db/corpus 面 None；顶层键集固定无生成时刻。"""
    fp = build_run_fingerprint()
    assert set(fp) == {"schema", "git", "deps", "db", "corpus"}
    assert fp["db"] is None
    assert fp["corpus"] is None
    # 测试在 repo 内跑：git/deps 面必有身份
    assert re.fullmatch(r"[0-9a-f]{40}", str(fp["git"]["sha"]))
    assert isinstance(fp["git"]["dirty"], bool)
    assert set(fp["deps"]) == {
        "uv.lock",
        "pyproject.toml",
        "apps/backend/pyproject.toml",
    }
    assert all(_SHA256.match(str(v)) for v in fp["deps"].values())


def test_db_face_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    """同库两次指纹逐位相同；改一行强哈希必变。"""
    conn = _migrated_db(tmp_path)
    first = build_run_fingerprint(conn=conn)
    second = build_run_fingerprint(conn=conn)
    assert first["db"] == second["db"]

    conn.execute(
        "INSERT INTO competitions (name, tier, created_at) "
        "VALUES ('指纹联赛', 'tier2', '2026-10-28T00:00:00+00:00')"
    )
    conn.commit()
    third = build_run_fingerprint(conn=conn)
    before = first["db"]["tables"]["competitions"]
    after = third["db"]["tables"]["competitions"]
    assert after["rows"] == int(str(before["rows"])) + 1
    assert after["content_sha256"] != before["content_sha256"]
    # 未动的表哈希不变（分域互不污染）
    assert (
        third["db"]["tables"]["frozen_decisions"]
        == first["db"]["tables"]["frozen_decisions"]
    )


def test_inplace_update_flips_strong_hash(tmp_path: Path) -> None:
    """F2 回归：rowid 保留型就地 UPDATE（clubelo 日更/understat 回填/
    hist 纠错重灌三类设计内路径）必须换哈希——弱指纹对此全盲已废除。"""
    conn = _migrated_db(tmp_path)
    faces = data_fp.table_fingerprints(conn)
    assert "admin_match_exclusions" in faces  # F4：gate 门①依赖表在 data 面
    conn.execute(
        "INSERT INTO elo_ratings (club, country, elo, valid_from, valid_to) "
        "VALUES ('指纹队', '测试国', 1800.0, '2026-01-01', '2026-06-01')"
    )
    conn.commit()
    seeded = data_fp.table_fingerprints(conn)
    conn.execute("UPDATE elo_ratings SET elo = 2050.0")  # rowid 不变的就地改写
    conn.commit()
    updated = data_fp.table_fingerprints(conn)
    assert seeded["elo_ratings"]["rows"] == updated["elo_ratings"]["rows"] == 1
    assert (
        updated["elo_ratings"]["content_sha256"]
        != seeded["elo_ratings"]["content_sha256"]
    )


def test_empty_tables_have_deterministic_strong_hash(tmp_path: Path) -> None:
    """空表也有确定强哈希（列名前缀入哈希）；两次一致。"""
    conn = _migrated_db(tmp_path)
    faces_a = betting_fp.table_fingerprints(conn)
    faces_b = betting_fp.table_fingerprints(conn)
    for table in ("frozen_decisions", "track2_snapshots"):
        assert faces_a[table]["rows"] == 0
        assert _SHA256.match(str(faces_a[table]["content_sha256"]))
        assert faces_a[table] == faces_b[table]


def test_corpus_face_on_fresh_tree(tmp_path: Path) -> None:
    """空语料树：raw 聚合空、duckdb 只记在场、gold 元数据缺席。"""
    store = CorpusStore(tmp_path / "corpus")
    store.ensure_tree()
    fp = build_run_fingerprint(store=store)
    inventory = fp["corpus"]["inventory"]
    assert inventory["raw"] == {}
    assert inventory["duckdb"] == {"present": False}
    assert inventory["silver"] == {"files": 0, "bytes": 0}
    assert fp["corpus"]["gold_version"] is None
    assert fp["corpus"]["gold_built_at"] is None


def test_inventory_fails_closed_on_checkpoint_lock(tmp_path: Path) -> None:
    """F3 回归：checkpoint 被独占锁时不静默降级 raw:None——同树两次指纹
    不同还无从分辨比报错更糟，必须抛 OperationalError 让调用方重试。"""
    store = CorpusStore(tmp_path / "corpus")
    store.ensure_tree()
    holder = sqlite3.connect(store.checkpoint_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            store.dataset_inventory()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_inventory_walk_survives_concurrent_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5 回归：树 walk 遇 is_file→stat 间被并发删除的文件不炸整份清单。"""
    store = CorpusStore(tmp_path / "corpus")
    store.ensure_tree()
    victim = store.root / "silver" / "probe" / "f.parquet"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"x")
    real_stat = Path.stat

    def racing_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "f.parquet":
            raise FileNotFoundError(str(self))
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", racing_stat)
    inventory = store.dataset_inventory()
    assert inventory["silver"] == {"files": 0, "bytes": 0}  # 竞态文件跳过不炸
