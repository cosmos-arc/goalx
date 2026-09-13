"""原始证据存档测试（票 35：脱敏、哈希、可重读）。"""

from __future__ import annotations

import gzip
import json

from goalx_backend import observations


def test_save_and_read_roundtrip(tmp_path) -> None:
    body = json.dumps({"value": {"ok": 1}}).encode()
    sha, ref = observations.save_raw(tmp_path, "sporttery", body)
    assert ref == f"sporttery/{sha}.json.gz"
    assert (tmp_path / ref).exists()
    assert observations.read_raw(tmp_path, ref) == body
    # 同内容重存幂等（内容寻址）
    sha2, ref2 = observations.save_raw(tmp_path, "sporttery", body)
    assert (sha2, ref2) == (sha, ref)


def test_unwritable_root_degrades_to_hash_only(tmp_path, monkeypatch) -> None:
    body = b"{}"
    monkeypatch.setattr(
        observations.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError)
    )
    sha, ref = observations.save_raw(tmp_path, "odds_api", body)
    assert sha == observations.sha256_hex(body)
    assert ref is None  # 落盘失败仍可凭哈希证明观测


def test_gzip_payload_is_compressed_on_disk(tmp_path) -> None:
    body = json.dumps({"big": ["x" * 100] * 50}).encode()
    _, ref = observations.save_raw(tmp_path, "odds_api", body)
    stored = (tmp_path / ref).read_bytes()
    assert gzip.decompress(stored) == body
    assert len(stored) < len(body)
