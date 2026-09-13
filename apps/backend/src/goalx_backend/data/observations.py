"""
报价观测的原始证据存档（票 35）：脱敏原始响应落盘 + 哈希。

只存 HTTP 响应体（不含请求 URL/头——The Odds API 的 apiKey 在查询串、
sporttery 的 Referer 在请求头，均不会进入证据文件）。落盘失败时仍返回
哈希（raw_ref=None），观测本身照常入库。

文件布局 ``{root}/{source}/{sha256}.json.gz``，内容为 gzip 的原始响应体，
可重解析复核（票 35 验收 2）。
"""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path


def sha256_hex(body: bytes) -> str:
    """Evidence hash of a raw response body."""
    return hashlib.sha256(body).hexdigest()


def save_raw(root: Path, source: str, body: bytes) -> tuple[str, str | None]:
    """
    Persist a sanitized raw response; return ``(sha256, relative_ref)``.

    ref 是相对 root 的路径（``source/xx….json.gz``）；目录不可写时 ref 为
    None，调用方仍可凭哈希与观测行证明再次观测。
    """
    digest = sha256_hex(body)
    relative = f"{source}/{digest}.json.gz"
    try:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(gzip.compress(body))
    except OSError:
        return digest, None
    return digest, relative


def read_raw(root: Path, ref: str) -> bytes:
    """Reload a stored raw response (重解析复核)."""
    return gzip.decompress((root / ref).read_bytes())
