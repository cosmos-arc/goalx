"""
data 域表指纹（report-audit-20261027 票 06：运行指纹）。

强哈希 = 逐行规范序列化（含列名前缀）sha256，按 rowid 升序——同逻辑
内容同哈希，与 WAL/checkpoint 落盘进度无关；弱指纹 = 行数 + max(rowid)，
只给重表（十万行级训练史）用。表清单固定为报告面实际读取的核心集，
新增依赖表时同步维护（口径变化须升 run_fingerprint.SCHEMA）。表名一律
字面量（ADR-0008 归属执法 + 防插值盲区）。
"""

from __future__ import annotations

import hashlib
import sqlite3

# 强哈希 SQL：运营核心表（报告输入面，行量 ≤ 十万）
_STRONG_SQL: dict[str, str] = {
    "fixtures": "SELECT * FROM fixtures ORDER BY rowid",
    "teams": "SELECT * FROM teams ORDER BY rowid",
    "competitions": "SELECT * FROM competitions ORDER BY rowid",
    "match_codes": "SELECT * FROM match_codes ORDER BY rowid",
    "sale_statuses": "SELECT * FROM sale_statuses ORDER BY rowid",
    "quote_observations": "SELECT * FROM quote_observations ORDER BY rowid",
    "price_deviation_ledger": "SELECT * FROM price_deviation_ledger ORDER BY rowid",
    "odds_snapshots": "SELECT * FROM odds_snapshots ORDER BY rowid",
}
# 弱指纹 SQL：重表（十万行级训练史；内容变化绝大多数伴随行数/rowid 变化）
_WEAK_SQL: dict[str, str] = {
    "hist_matches": "SELECT COUNT(*), MAX(rowid) FROM hist_matches",
    "understat_matches": "SELECT COUNT(*), MAX(rowid) FROM understat_matches",
    "elo_ratings": "SELECT COUNT(*), MAX(rowid) FROM elo_ratings",
}


def table_fingerprints(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Data 域表指纹面：{表: {"rows": n, "content_sha256" | "max_rowid": ...}}。"""
    faces: dict[str, dict[str, object]] = {}
    for table, sql in _STRONG_SQL.items():
        digest = hashlib.sha256()
        cursor = conn.execute(sql)
        columns = tuple(d[0] for d in cursor.description)
        digest.update(repr(columns).encode())
        n = 0
        for row in cursor:
            digest.update(repr(tuple(row)).encode())
            n += 1
        faces[table] = {"rows": n, "content_sha256": digest.hexdigest()}
    for table, sql in _WEAK_SQL.items():
        row = conn.execute(sql).fetchone()
        faces[table] = {"rows": int(row[0] or 0), "max_rowid": row[1]}
    return faces
