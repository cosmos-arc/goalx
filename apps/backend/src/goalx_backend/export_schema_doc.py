"""
导出 schema 文档的生成块（列清单/ER 图/唯一键/append-only 触发器）。

docs/db-and-domains.md 里 ``<!-- schema-doc:BEGIN:xxx -->…END:xxx -->`` 标记
块由本模块从迁移后的内存库生成（真库 DDL = 事实源）；标记外的注解、矩阵、
章节结构是手写的，重写不触碰。漂移由 tests/unit/test_schema_doc.py 断言。

    task schema-doc-export   # 或 uv run python -m goalx_backend.export_schema_doc
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from loguru import logger

from goalx_backend.db import connect, migrate

REPO_ROOT = Path(__file__).resolve().parents[4]
DOC_PATH = REPO_ROOT / "docs" / "db-and-domains.md"

_MARKER = re.compile(
    r"(<!-- schema-doc:BEGIN:(?P<name>[\w:.-]+) -->\n)"
    + r".*?(<!-- schema-doc:END:(?P=name) -->)",
    re.S,
)


def _tables(conn: sqlite3.Connection) -> list[str]:
    query = (
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%' AND name != 'schema_migrations'"
        " ORDER BY name"
    )
    rows = conn.execute(query).fetchall()
    return [str(r[0]) for r in rows]


def _columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f'PRAGMA table_info("{table}")').fetchall()


def _fks(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """列 → '表.列' 的 FK 映射（同名 FK 取其一即可）。"""
    out: dict[str, str] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
        out[str(row[3])] = f"{row[2]}.{row[4]}"
    return out


def _uniques(conn: sqlite3.Connection, table: str) -> list[str]:
    """唯一键列表（'(`a`,`b`)' 形式；自动 rowid 索引与非唯一索引跳过）。"""
    keys: list[str] = []
    for idx in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        # idx = (seq, name, unique, origin, partial)：origin 空=PK 自动索引；
        # unique=0 = 普通索引（曾误标"唯一键"，票 45 评审修正）
        if not str(idx[3]) or not int(idx[2]):
            continue
        cols = [
            str(r[2]) for r in conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()
        ]
        keys.append("(" + ", ".join(f"`{c}`" for c in cols if c) + ")")
    return sorted(set(keys))


def _triggers(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name = ?",
        (table,),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def render_table_block(conn: sqlite3.Connection, table: str) -> str:
    """一张表的生成块正文：表名标题 + 全列表格 + 唯一键/触发器行。"""
    fks = _fks(conn, table)
    lines = [f"#### `{table}`", "", "| 列 | 类型 | 约束 |", "| --- | --- | --- |"]
    for col in _columns(conn, table):
        name, ctype = str(col[1]), str(col[2]).upper()
        flags: list[str] = []
        if col[5]:
            flags.append("PK")
        if col[3]:
            flags.append("NOT NULL")
        if col[4] is not None:
            flags.append(f"DEFAULT {col[4]}")
        if name in fks:
            flags.append(f"FK→{fks[name]}")
        lines.append(f"| `{name}` | {ctype} | {'，'.join(flags) or '—'} |")
    for key in _uniques(conn, table):
        lines.append(f"\n唯一键 `UNIQUE{key}`")
    triggers = _triggers(conn, table)
    if triggers:
        lines.append("\nappend-only 触发器：" + "、".join(f"`{t}`" for t in triggers))
    return "\n".join(lines) + "\n"


def render_er_block(conn: sqlite3.Connection) -> str:
    """
    总 ER 图（Mermaid erDiagram）：实体+主键、FK 边（可空 FK 用零或多）。

    生成体含 ```mermaid 围栏——GitHub 只渲染围栏内的 Mermaid，裸
    erDiagram 文本按段落显示（2026-09-21 发现自 PR #45 建档起即坏）。
    """
    tables = _tables(conn)
    lines = ["```mermaid", "erDiagram"]
    for table in tables:
        pk = next((str(c[1]) for c in _columns(conn, table) if c[5]), None)
        pk_type = next(
            (str(c[2]).upper() for c in _columns(conn, table) if c[5]), "INTEGER"
        )
        lines.append(f"    {table} {{")
        if pk:
            lines.append(f"        {pk_type} {pk} PK")
        lines.append("    }")
    for table in tables:
        cols = {str(c[1]): c[3] for c in _columns(conn, table)}
        for col, ref in _fks(conn, table).items():
            # Mermaid 左基数仅两字符：非空 FK=一或多 }|，可空=零或多 }o
            # （}|o 三字符非法——围栏修好后暴露的解析错误，2026-09-21）
            card = "}|" if cols.get(col) else "}o"
            lines.append(f"    {table} {card}--|| {ref.split('.')[0]} : {col}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def generated_blocks() -> dict[str, str]:
    """Marker 名 → 生成内容（'er' 与 'table:<name>'）。"""
    conn = connect(":memory:")
    migrate(conn)
    blocks = {"er": render_er_block(conn)}
    blocks.update({f"table:{t}": render_table_block(conn, t) for t in _tables(conn)})
    conn.close()
    return blocks


def export_schema_doc(target: Path = DOC_PATH) -> Path:
    """重写文档里的全部生成块（手写内容原样保留）。"""
    doc = target.read_text(encoding="utf-8")
    replacements = {
        name: (
            f"<!-- schema-doc:BEGIN:{name} -->\n"
            f"{body.rstrip(chr(10)) + chr(10)}"
            f"<!-- schema-doc:END:{name} -->"
        )
        for name, body in generated_blocks().items()
    }
    for name in replacements:
        if f"BEGIN:{name} -->" not in doc:
            logger.warning("文档缺 marker {}（新表请补章节与注解）", name)
    updated = _MARKER.sub(lambda m: replacements.get(m.group("name"), m.group(0)), doc)
    target.write_text(updated, encoding="utf-8")
    logger.info("schema 文档生成块已刷新: {}", target)
    return target


__all__ = [
    "DOC_PATH",
    "export_schema_doc",
    "generated_blocks",
]


if __name__ == "__main__":
    export_schema_doc()
