"""schema 文档漂移断言：docs/db-and-domains.md 生成块 = 迁移后真库。"""

from __future__ import annotations

from pathlib import Path

from goalx_backend.export_schema_doc import DOC_PATH, generated_blocks


def test_schema_doc_blocks_match_database() -> None:
    """文档里每个 marker 块内容与生成结果一致；新表必须补章节。"""
    doc = DOC_PATH.read_text(encoding="utf-8")
    import re

    marker = re.compile(
        r"(<!-- schema-doc:BEGIN:([\w:.-]+) -->\n).*?(<!-- schema-doc:END:\2 -->)",
        re.S,
    )
    in_doc = {m.group(2): m.group(0) for m in marker.finditer(doc)}
    fresh = generated_blocks()
    problems: list[str] = []
    for name, body in fresh.items():
        expected = (
            f"<!-- schema-doc:BEGIN:{name} -->\n{body.rstrip('\n') + chr(10)}"
            f"<!-- schema-doc:END:{name} -->"
        )
        if name not in in_doc:
            problems.append(f"缺 marker {name}（新表请补章节+注解）")
        elif in_doc[name] != expected:
            problems.append(f"{name} 与真库漂移：跑 task schema-doc-export 后提交")
    stale = sorted(set(in_doc) - set(fresh))
    problems.extend(f"文档有但真库无：{name}（表已删？删掉章节）" for name in stale)
    assert not problems, "\n".join(problems)


def test_doc_exists_and_has_frontmatter() -> None:
    """文档在位且有生成说明（防误删）。"""
    doc = Path(DOC_PATH).read_text(encoding="utf-8")
    assert "schema-doc-export" in doc
    assert "CONTEXT.md" in doc  # 术语单一事实源链接
