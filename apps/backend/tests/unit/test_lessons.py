"""票 05 教训库读口三接线 + seed 完整性自证。

Seam：`goalx_backend.evaluation.lessons` 公共接口（解析/周批注入/排除
清单/月读视图/证据回指核对）与 `morning_check.check_lessons_review_due`
（复审到期挂项）。fixture 自证用 tmp 目录写最小条目；seed 完整性测试
读真实 `docs/plans/lessons/` 与 `docs/plans/edge-ledger.md`。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from goalx_backend.evaluation import lessons as lesson_mod

LESSON_TEMPLATE = """---
id: L-{num:02d}
status: {status}
confidence: 高
created: {created}
review_due: {review_due}
window: {window}
cause: {cause}
triggers:
  - {trigger_a}
  - {trigger_b}
evidence:
  - {evidence_a}
  - {evidence_b}
---
{body}
"""


def write_lesson(
    directory: Path,
    num: int,
    *,
    status: str = "active",
    created: str = "2026-11-09",
    review_due: str = "2027-11-09",
    window: str = "null",
    cause: str = "测试死因：口径未扣水。",
    trigger_a: str = "场景甲",
    trigger_b: str = "场景乙",
    evidence_a: str = "edge-ledger §3#1",
    evidence_b: str = "edge-ledger E-04",
    body: str = "正文：规矩与边界。",
) -> Path:
    path = directory / f"L-{num:02d}.md"
    path.write_text(
        LESSON_TEMPLATE.format(
            num=num,
            status=status,
            created=created,
            review_due=review_due,
            window=window,
            cause=cause,
            trigger_a=trigger_a,
            trigger_b=trigger_b,
            evidence_a=evidence_a,
            evidence_b=evidence_b,
            body=body,
        ),
        encoding="utf-8",
    )
    return path


def test_repo_root_resolves_and_lessons_dir_under_it() -> None:
    root = lesson_mod.repo_root()
    assert (root / "pyproject.toml").is_file()
    assert lesson_mod.lessons_dir(root) == root / "docs" / "plans" / "lessons"
    assert lesson_mod.lessons_dir().is_dir()


class TestLoadLessons:
    def test_parses_frontmatter_and_sorts_by_numeric_id(self, tmp_path: Path) -> None:
        write_lesson(tmp_path, 2)
        write_lesson(tmp_path, 1, review_due="null", window="2016-2026")

        loaded = lesson_mod.load_lessons(tmp_path)

        assert [lesson.id for lesson in loaded] == ["L-01", "L-02"]
        first = loaded[0]
        assert first.status == "active"
        assert first.confidence == "高"
        assert first.created == date(2026, 11, 9)
        assert first.review_due is None  # 「复审期不设」= null
        assert first.window == "2016-2026"
        assert first.cause == "测试死因：口径未扣水。"
        assert first.triggers == ("场景甲", "场景乙")
        assert first.evidence == ("edge-ledger §3#1", "edge-ledger E-04")
        assert first.body.startswith("正文：")
        assert loaded[1].review_due == date(2027, 11, 9)

    def test_ignores_non_markdown_and_requires_lesson_prefix(
        self, tmp_path: Path
    ) -> None:
        write_lesson(tmp_path, 1)
        (tmp_path / "README.md").write_text("目录说明，不是条目", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("杂文件", encoding="utf-8")

        loaded = lesson_mod.load_lessons(tmp_path)

        assert [lesson.id for lesson in loaded] == ["L-01"]
        # 条目文件名与 frontmatter id 不一致 = 受控文档纪律破损，显式拒绝
        bad = tmp_path / "L-02.md"
        bad.write_text(
            LESSON_TEMPLATE.format(
                num=3,
                status="active",
                created="2026-11-09",
                review_due="2027-11-09",
                window="null",
                cause="x",
                trigger_a="a",
                trigger_b="b",
                evidence_a="edge-ledger §3#2",
                evidence_b="edge-ledger E-05",
                body="b",
            ),
            encoding="utf-8",
        )
        with pytest.raises(lesson_mod.LessonFormatError, match=r"L-02\.md"):
            lesson_mod.load_lessons(tmp_path)


class TestFormatValidation:
    def test_rejects_bad_status_and_missing_field(self, tmp_path: Path) -> None:
        raw = LESSON_TEMPLATE.format(
            num=1,
            status="zombie",
            created="2026-11-09",
            review_due="2027-11-09",
            window="null",
            cause="x",
            trigger_a="a",
            trigger_b="b",
            evidence_a="edge-ledger §3#1",
            evidence_b="edge-ledger E-04",
            body="b",
        )
        (tmp_path / "L-01.md").write_text(raw, encoding="utf-8")
        with pytest.raises(lesson_mod.LessonFormatError, match="status"):
            lesson_mod.load_lessons(tmp_path)

        bad_date = raw.replace("zombie", "active").replace(
            "created: 2026-11-09", "created: 2026/11/09"
        )
        (tmp_path / "L-01.md").write_text(bad_date, encoding="utf-8")
        with pytest.raises(lesson_mod.LessonFormatError, match="created"):
            lesson_mod.load_lessons(tmp_path)

        missing = raw.replace("zombie", "active").replace("cause: x\n", "")
        (tmp_path / "L-01.md").write_text(missing, encoding="utf-8")
        with pytest.raises(lesson_mod.LessonFormatError, match=r"缺字段.*cause"):
            lesson_mod.load_lessons(tmp_path)

    def test_rejects_unclosed_frontmatter_and_bad_id(self, tmp_path: Path) -> None:
        (tmp_path / "L-01.md").write_text(
            "---\nid: L-01\nstatus: active\n", encoding="utf-8"
        )
        with pytest.raises(lesson_mod.LessonFormatError, match="未闭合"):
            lesson_mod.load_lessons(tmp_path)

        write_lesson(tmp_path, 1, evidence_a="edge-ledger §3#1")
        bad_id = (
            (tmp_path / "L-01.md")
            .read_text(encoding="utf-8")
            .replace("id: L-01", "id: X-01")
        )
        (tmp_path / "L-01.md").write_text(bad_id, encoding="utf-8")
        with pytest.raises(lesson_mod.LessonFormatError, match="id 非法"):
            lesson_mod.load_lessons(tmp_path)

    def test_semantically_bad_date_raises_format_error(self, tmp_path: Path) -> None:
        """correctness F1：正则可过但日历不存在的日期不得裸 ValueError 穿透。"""
        # created: 2026-02-30 —— 过 _DATE_RE，date.fromisoformat 拒绝
        write_lesson(tmp_path, 1, created="2026-02-30")
        with pytest.raises(lesson_mod.LessonFormatError, match=r"created 非法日期"):
            lesson_mod.load_lessons(tmp_path)

        # review_due: 2027-11-31 —— 同形态
        write_lesson(tmp_path, 1, review_due="2027-11-31")
        with pytest.raises(lesson_mod.LessonFormatError, match=r"review_due 非法日期"):
            lesson_mod.load_lessons(tmp_path)

    def test_window_list_form_rejected(self, tmp_path: Path) -> None:
        """correctness F2：window 列表形态不得 str() 静默字符串化。"""
        (tmp_path / "L-01.md").write_text(
            "---\n"
            "id: L-01\n"
            "status: active\n"
            "confidence: 高\n"
            "created: 2026-11-09\n"
            "review_due: 2027-11-09\n"
            "window:\n"
            "  - 2016-2026\n"
            "cause: 测试死因。\n"
            "triggers:\n"
            "  - 场景甲\n"
            "evidence:\n"
            "  - edge-ledger §3#1\n"
            "---\n"
            "正文。\n",
            encoding="utf-8",
        )
        with pytest.raises(lesson_mod.LessonFormatError, match="window"):
            lesson_mod.load_lessons(tmp_path)

    def test_duplicate_frontmatter_key_rejected(self, tmp_path: Path) -> None:
        """correctness F3：重复键不得静默取后值。"""
        write_lesson(tmp_path, 1)
        duplicated = (
            (tmp_path / "L-01.md")
            .read_text(encoding="utf-8")
            .replace("cause: 测试死因：口径未扣水。", "cause: 先值。\ncause: 后值。")
        )
        (tmp_path / "L-01.md").write_text(duplicated, encoding="utf-8")
        with pytest.raises(lesson_mod.LessonFormatError, match="重复键"):
            lesson_mod.load_lessons(tmp_path)


class TestReadInterfaces:
    def test_weekly_injection_active_only_and_render(self, tmp_path: Path) -> None:
        write_lesson(tmp_path, 1, trigger_a="意甲出平类语言", trigger_b="德甲排平")
        write_lesson(tmp_path, 2, status="archived")
        write_lesson(tmp_path, 3, status="review_due")

        entries = lesson_mod.weekly_injection(tmp_path)

        assert [entry.lesson_id for entry in entries] == ["L-01"]
        entry = entries[0]
        assert entry.triggers == "意甲出平类语言；德甲排平"
        assert entry.cause == "测试死因：口径未扣水。"
        assert entry.evidence == ("edge-ledger §3#1", "edge-ledger E-04")
        rendered = lesson_mod.render_injection(entries)
        assert "[L-01]" in rendered
        assert "意甲出平类语言；德甲排平" in rendered
        assert "edge-ledger §3#1" in rendered
        assert rendered.count("\n") == 0  # 单条=单行，多条=一行一条

    def test_scanner_exclusions_flatten_triggers_active_only(
        self, tmp_path: Path
    ) -> None:
        write_lesson(tmp_path, 1)
        write_lesson(tmp_path, 2, status="archived")

        rules = lesson_mod.scanner_exclusions(tmp_path)

        assert [(rule.lesson_id, rule.trigger) for rule in rules] == [
            ("L-01", "场景甲"),
            ("L-01", "场景乙"),
        ]
        assert rules[0].cause == "测试死因：口径未扣水。"

    def test_monthly_view_new_and_review_due_buckets(self, tmp_path: Path) -> None:
        # 新入库：上月两条；本月一条与上月月末边界外一条不算
        write_lesson(tmp_path, 1, created="2026-11-10")
        write_lesson(tmp_path, 2, created="2026-11-30")
        write_lesson(tmp_path, 3, created="2026-12-01")
        write_lesson(tmp_path, 4, created="2026-10-31")
        # 到期：review_due 已过（active）；status=review_due 的也算到期；
        # 不设/null 不算；archived 不算
        write_lesson(tmp_path, 5, created="2026-10-15", review_due="2026-12-01")
        write_lesson(
            tmp_path,
            6,
            created="2026-10-15",
            review_due="2026-11-30",
            status="review_due",
        )
        write_lesson(
            tmp_path,
            7,
            created="2026-10-15",
            status="archived",
            review_due="2026-11-30",
        )

        view = lesson_mod.monthly_review_view(tmp_path, today=date(2026, 12, 1))

        assert [lesson.id for lesson in view.new_entries] == ["L-01", "L-02"]
        assert [lesson.id for lesson in view.review_due_entries] == ["L-05", "L-06"]
        # 到期未复审满一年 = 「12 月未复现→建议降置信」的人裁输入面
        assert view.review_due_entries[0].confidence == "高"


class TestEvidenceRefs:
    LEDGER = """# 账本

## 2. 探索登记主账

#### E-04 同源恒等式（2026-09-01 开题）
- 内容

#### E-38 审计（2026-11-09 开题）
- 内容

## 3. 方法学演化史

1. 第一条。
2. 第二条。
## 4. 其他
"""

    def test_unresolved_evidence_refs_reports_each_kind(self, tmp_path: Path) -> None:
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "real.md").write_text("报告", encoding="utf-8")
        write_lesson(
            tmp_path,
            1,
            evidence_a="edge-ledger §3#2",
            evidence_b="edge-ledger E-38",
        )
        write_lesson(
            tmp_path,
            2,
            evidence_a="edge-ledger §3#9",
            evidence_b="edge-ledger E-99",
        )
        write_lesson(
            tmp_path,
            3,
            evidence_a="report:reports/real.md",
            evidence_b="report:.scratch/quoted-in-ledger.md",
        )
        write_lesson(
            tmp_path, 4, evidence_a="report:made/up.md", evidence_b="synthesized"
        )
        ledger_text = self.LEDGER + "…引用 .scratch/quoted-in-ledger.md 一处…"
        lessons = lesson_mod.load_lessons(tmp_path)

        problems = lesson_mod.unresolved_evidence_refs(
            lessons, ledger_text=ledger_text, root=tmp_path
        )

        assert problems == (
            "L-02: §3#9 不在账本 §3",
            "L-02: E-99 不在账本 §2",
            "L-04: 报告路径既不在盘上也不在账本 made/up.md",
            "L-04: 证据格式不认识 'synthesized'",
        )


def _section3_entries(ledger_text: str) -> dict[int, str]:
    """账本 §3 逐条正文（条目号→跨到下一条目前的原文），供回指核对。"""
    entries: dict[int, str] = {}
    current: int | None = None
    chunk: list[str] = []
    in_section = False
    for line in [*ledger_text.splitlines(), "## 终"]:
        if line.startswith("## "):
            if current is not None:
                entries[current] = "\n".join(chunk)
            current = None
            heading = re.match(r"^## (\d+)\. ", line)
            in_section = bool(heading and heading.group(1) == "3")
            continue
        item = re.match(r"^(\d+)\. ", line)
        if in_section and item is not None:
            if current is not None:
                entries[current] = "\n".join(chunk)
            current = int(item.group(1))
            chunk = [line]
        elif current is not None:
            chunk.append(line)
    return entries


class TestSeedIntegrity:
    """seed 29 条迁移完整性：读真实受控文档，即迁移验收面（人裁=29 条全迁）。"""

    def test_29_entries_ids_unique_status_review_policy(self) -> None:
        loaded = lesson_mod.load_lessons()
        assert [lesson.id for lesson in loaded] == [
            f"L-{num:02d}" for num in range(1, 30)
        ]
        for lesson in loaded:
            assert lesson.status == "active", lesson.id
            assert lesson.confidence in lesson_mod.LESSON_CONFIDENCES
            assert lesson.triggers, lesson.id
            assert lesson.evidence, lesson.id
            assert lesson.cause, lesson.id
            if lesson.id == "L-29":
                # 账本明言「复审期：不设」——JC 面水位回流=新票新数据面
                assert lesson.review_due is None
            else:
                # spec：review_due 默认 created+REVIEW_HORIZON_MONTHS 月
                # （seed 定稿日均在 11 月，+12 月=整年平移无进位歧义）
                years = lesson_mod.REVIEW_HORIZON_MONTHS // 12
                assert lesson.review_due == lesson.created.replace(
                    year=lesson.created.year + years
                ), lesson.id
            # seed 定稿日（§3 建 2026-11-09；#21-#29 为 2026-11-10 审计/B 批回写）
            assert lesson.created in {date(2026, 11, 9), date(2026, 11, 10)}

    def test_evidence_refs_resolve_against_real_ledger(self) -> None:
        root = lesson_mod.repo_root()
        ledger_text = (root / "docs" / "plans" / "edge-ledger.md").read_text(
            encoding="utf-8"
        )
        problems = lesson_mod.unresolved_evidence_refs(
            lesson_mod.load_lessons(), ledger_text=ledger_text, root=root
        )
        assert problems == ()

    def test_every_ledger_section3_entry_has_migration_backref(self) -> None:
        root = lesson_mod.repo_root()
        ledger_text = (root / "docs" / "plans" / "edge-ledger.md").read_text(
            encoding="utf-8"
        )
        entries = _section3_entries(ledger_text)
        assert set(entries) == set(range(1, 30))  # 账本 §3 实况=29 条
        for num, text in entries.items():
            assert f"已迁移→docs/plans/lessons/L-{num:02d}.md" in text, num
