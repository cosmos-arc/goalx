"""
data 域表指纹（report-audit-20261027 票 06：运行指纹）。

强哈希 = 逐行规范序列化（含列名前缀）sha256，按 rowid 升序——同逻辑
内容同哈希，与 WAL/checkpoint 落盘进度无关，且对 rowid 保留型
``ON CONFLICT DO UPDATE``（clubelo 日更快照/understat 赛后回填/
hist_matches 纠错重灌三类设计内就地改写）同样敏感——正确性审查 F2：
行数+max(rowid) 弱指纹对这些路径全盲，故**全表强哈希、无弱分支**。
表清单固定为报告面实际读取的核心集，新增依赖表时同步维护（口径变化
须升 run_fingerprint.SCHEMA）。表名一律字面量（ADR-0008 归属执法 +
防插值盲区）。
"""

from __future__ import annotations

import hashlib
import sqlite3

_TABLE_SQL: dict[str, str] = {
    "fixtures": "SELECT * FROM fixtures ORDER BY rowid",
    "teams": "SELECT * FROM teams ORDER BY rowid",
    "competitions": "SELECT * FROM competitions ORDER BY rowid",
    "match_codes": "SELECT * FROM match_codes ORDER BY rowid",
    "sale_statuses": "SELECT * FROM sale_statuses ORDER BY rowid",
    "quote_observations": "SELECT * FROM quote_observations ORDER BY rowid",
    "price_deviation_ledger": "SELECT * FROM price_deviation_ledger ORDER BY rowid",
    "odds_snapshots": "SELECT * FROM odds_snapshots ORDER BY rowid",
    "hist_matches": "SELECT * FROM hist_matches ORDER BY rowid",
    "understat_matches": "SELECT * FROM understat_matches ORDER BY rowid",
    "elo_ratings": "SELECT * FROM elo_ratings ORDER BY rowid",
    # corpus_gate 门①读它筛对账集（正确性审查 F4）：变化必须入指纹
    "admin_match_exclusions": "SELECT * FROM admin_match_exclusions ORDER BY rowid",
}


def table_fingerprints(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Data 域表指纹面：{表: {"rows": n, "content_sha256": ...}}。"""
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
