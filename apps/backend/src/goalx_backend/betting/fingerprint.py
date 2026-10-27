"""
betting 域表指纹（report-audit-20261027 票 06：运行指纹）。

与 data/fingerprint.py 同法：强哈希（逐行规范序列化 sha256，rowid 升序）。
betting 域表行量小（冻结账/漏斗面），全量强哈希；表名一律字面量
（ADR-0008 归属执法）。track2_snapshots 通常空表——空表也有确定哈希，
上线首拍即改变指纹，如实反映输入面变化。
"""

from __future__ import annotations

import hashlib
import sqlite3

_TABLE_SQL: dict[str, str] = {
    "frozen_decisions": "SELECT * FROM frozen_decisions ORDER BY rowid",
    "frozen_decision_divergences": (
        "SELECT * FROM frozen_decision_divergences ORDER BY rowid"
    ),
    "funnel_days": "SELECT * FROM funnel_days ORDER BY rowid",
    "funnel_fixture_days": "SELECT * FROM funnel_fixture_days ORDER BY rowid",
    "track2_snapshots": "SELECT * FROM track2_snapshots ORDER BY rowid",
}


def table_fingerprints(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Betting 域表指纹面：{表: {"rows": n, "content_sha256": ...}}。"""
    faces: dict[str, dict[str, object]] = {}
    for table, sql in _TABLE_SQL.items():
        digest = hashlib.sha256()
        cursor = conn.execute(sql)
        columns = tuple(d[0] for d in cursor.description)
        digest.update(repr(columns).encode())
        n = 0
        for row in cursor:
            digest.update(repr(tuple(row)).encode())
            n += 1
        faces[table] = {"rows": n, "content_sha256": digest.hexdigest()}
    return faces
