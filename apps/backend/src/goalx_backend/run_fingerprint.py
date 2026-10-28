"""
运行指纹（report-audit-20261027 票 06）：报告/回测工件自证身份。

隔离复现（2026-10-27 外部报告）只能靠"137 文件与历史对象一致"倒推
是哪次提交哪份输入——本模块让每个新工件自带答案：

- ``git``：提交 SHA + 工作树 dirty 旗（dirty 输出重放需额外说明）
- ``deps``：uv.lock / 根与 backend pyproject 的 sha256（依赖与配置身份）
- ``db``：schema 版本 + data/betting 两域核心表内容哈希（表清单与口径
  归属域模块维护，ADR-0008；本模块零 SQL）
- ``corpus``：语料树弱指纹（checkpoint 聚合 + 树计数 + duckdb 内容哈希）

确定性纪律：指纹里**没有任何生成时刻字段**——同输入同指纹，跨运行可
直接比对。接线约定：报告 payload 顶层 ``run_fingerprint`` 键、回测
run_params 同名键。口径升级（表清单/哈希法变化）须递增 ``SCHEMA`` 并
在 docstring 记录，防跨口径误比对。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from goalx_backend.betting import fingerprint as betting_fp
from goalx_backend.data import fingerprint as data_fp
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.gold_reader import read_gold_meta
from goalx_backend.db import current_version

SCHEMA = 1

# deps 面固定文件集（repo 相对路径）：依赖锁 + 两层 pyproject
_DEPS_FILES: tuple[str, ...] = (
    "uv.lock",
    "pyproject.toml",
    "apps/backend/pyproject.toml",
)


def _repo_root() -> Path:
    """仓库根（本文件在 apps/backend/src/goalx_backend/ 下，四级上溯）。"""
    return Path(__file__).resolve().parents[4]


def _is_repo_root(root: Path) -> bool:
    """
    布局自证：根下必须有根 pyproject。

    防文件挪位后 parents[4] 落入别家 git 仓库记错身份——比 None 更糟
    的是记成别人的 SHA。
    """
    return (root / "pyproject.toml").is_file()


def _git_face(root: Path) -> dict[str, Any]:
    """提交身份：SHA + dirty 旗；git 缺席/非仓库时如实置 None。"""
    git = shutil.which("git")
    if git is None:
        return {"sha": None, "dirty": None}
    try:
        sha = subprocess.run(  # noqa: S603
            [git, "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(  # noqa: S603
            [git, "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {"sha": None, "dirty": None}
    return {"sha": sha or None, "dirty": bool(status.strip())}


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _deps_face(root: Path) -> dict[str, str | None]:
    """依赖/配置身份：固定文件集的 sha256（缺席= None）。"""
    return {rel: _file_sha256(root / rel) for rel in _DEPS_FILES}


def _db_face(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    运行面库身份：schema 版本 + 两归属域核心表指纹。

    全部表读包在一个只读事务里（WAL 读快照隔离）——不包的话逐表
    SELECT 间落一次 daily-wrap 提交，会得到不对应任何真实时点的
    "跨表涂抹"指纹（正确性审查 minor：跨表一致性）。
    """
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        tables: dict[str, dict[str, object]] = {}
        tables.update(data_fp.table_fingerprints(conn))
        tables.update(betting_fp.table_fingerprints(conn))
        version = current_version(conn)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")  # 纯读快照，不留事务
    return {"schema_version": version, "tables": tables}


def _corpus_face(store: CorpusStore) -> dict[str, Any]:
    """语料树身份：弱指纹清单 + gold 元数据（版本/构建戳）。"""
    meta = read_gold_meta(store)
    return {
        "inventory": store.dataset_inventory(),
        "gold_version": meta.get("gold_version"),
        "gold_built_at": meta.get("built_at"),
    }


def build_run_fingerprint(
    *,
    conn: sqlite3.Connection | None = None,
    store: CorpusStore | None = None,
) -> dict[str, Any]:
    """组装运行指纹；conn/store 缺席的面如实置 None（不假装有身份）。"""
    root = _repo_root()
    trusted = _is_repo_root(root)
    fingerprint: dict[str, Any] = {
        "schema": SCHEMA,
        "git": _git_face(root) if trusted else {"sha": None, "dirty": None},
        "deps": _deps_face(root) if trusted else None,
    }
    fingerprint["db"] = _db_face(conn) if conn is not None else None
    fingerprint["corpus"] = _corpus_face(store) if store is not None else None
    return fingerprint
