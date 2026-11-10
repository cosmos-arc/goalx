r"""
教训库 v1（vision-build 票 05）：`docs/plans/lessons/` 受控 markdown 的纯读口。

教训=研究记忆（为什么死/什么条件下别再试），与假设注册表（可证伪预测
状态）互引不互代。条目载体=git 内受控 markdown（人审定稿制：agent 写
权限空集，写入走 PR）；本模块只读不写——append-only 纪律（条目不删不
改写，只动 status/confidence 字段，且是人改）由文档纪律承担（见
docs/plans/lessons/README.md），代码不强制。

frontmatter schema（极简解析，零新依赖——pyyaml 不在依赖内）：

    ---
    id: L-01                    # ^L-\\d{2,}$，零填充稳定排序，seed 对应 ledger §3 序号
    status: active              # active → review_due → archived
    confidence: 高              # 高/中/低（人改字段）
    created: 2026-11-09
    review_due: 2027-11-09      # 默认 created+12 月；「不设」= null
    window: null                # 观测窗（原文有则记，方法学规矩类=null）
    cause: 一句话死因。
    triggers:                   # 场景/联赛/时间窗（激活本条教训的触发面）
      - 场景甲
    evidence:                   # 只引外部证据（禁纯 synthesized）
      - edge-ledger §3#1        #   账本 §3 序号回指
      - edge-ledger E-04        #   账本 §2 主账条目 id 回指
      - report:docs/plans/x.md  #   报告路径（盘上存在或账本原文可查）
    ---

三个读口（真消费在票 03 周批/autoresearch 扫描器/票 04 月读，本票只建
接口+fixture 自证）：①周批注入 `weekly_injection`+`render_injection`；
②EM/扫描器排除清单 `scanner_exclusions`；③月读视图 `monthly_review_view`
（新入库+复审到期）。复审到期另挂晨检 `morning_check.check_lessons_review_due`
（脚本只报不改）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# 状态生命周期（票面裁定：active → review_due → archived）
LESSON_STATUSES: tuple[str, ...] = ("active", "review_due", "archived")
# 置信档位（账本语言：高/中/低；置信翻转=人改）
LESSON_CONFIDENCES: tuple[str, ...] = ("高", "中", "低")
# 复审期默认跨度（spec：review_due 默认 +12 月）
REVIEW_HORIZON_MONTHS = 12

_ID_RE = re.compile(r"^L-\d{2,}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_RE = re.compile(r"^[a-z_]+$")
_LIST_ITEM_RE = re.compile(r"^\s+- (.*)$")
_EVIDENCE_SECTION_RE = re.compile(r"^## (\d+)\. ")
_EVIDENCE_ENTRY_RE = re.compile(r"^#### (E-\d{2})\b", re.MULTILINE)
_EVIDENCE_LEDGER_SECTION_RE = re.compile(r"^edge-ledger §3#(\d+)$")
_EVIDENCE_LEDGER_ENTRY_RE = re.compile(r"^edge-ledger (E-\d{2})$")
_EVIDENCE_REPORT_RE = re.compile(r"^report:(.+)$")


class LessonFormatError(ValueError):
    """受控条目格式破损（缺字段/坏值/坏 frontmatter）——晨检与加载路径显式上抛。"""


@dataclass(frozen=True)
class Lesson:
    """一条教训（frontmatter 全字段 + 正文原文）。"""

    id: str
    status: str
    confidence: str
    created: date
    review_due: date | None
    window: str | None
    cause: str
    triggers: tuple[str, ...]
    evidence: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class InjectionEntry:
    """周批注入结构（票 03 消费）：提案 prompt 的防重复死法面。"""

    lesson_id: str
    triggers: str
    cause: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ExclusionRule:
    """EM/扫描器排除清单行（autoresearch 扫描器消费）：一条 trigger 一行。"""

    lesson_id: str
    trigger: str
    cause: str


@dataclass(frozen=True)
class MonthlyLessonView:
    """月读教训库视图（票 04 消费）：新入库 + 复审到期两清单。"""

    new_entries: tuple[Lesson, ...]
    review_due_entries: tuple[Lesson, ...]


def repo_root() -> Path:
    """
    仓库根（本文件在 apps/backend/src/goalx_backend/evaluation/ 下，五级上溯）。

    run_fingerprint 同款布局自证：根下必须有根 pyproject——防文件挪位后
    上溯落进别家目录记错身份。
    """
    root = Path(__file__).resolve().parents[5]
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError(f"无法定位仓库根（{root} 下无 pyproject.toml）")
    return root


def lessons_dir(root: Path | None = None) -> Path:
    """教训库目录（repo_root/docs/plans/lessons；缺席由调用方如实降级）。"""
    base = repo_root() if root is None else root
    return base / "docs" / "plans" / "lessons"


def _parse_date(value: str, key: str, source: str) -> date:
    """YYYY-MM-DD 严格解析（坏串显式报错，不静默 None 冒充）。"""
    if not _DATE_RE.match(value):
        raise LessonFormatError(f"{source}: {key} 非法日期 {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        # 语义坏日期（2026-02-30）：正则可过但日历拒绝——correctness F1，
        # 裸 ValueError 会穿透晨检降级面（except 只收 LessonFormatError）
        raise LessonFormatError(f"{source}: {key} 非法日期 {value!r}") from exc


def _accept_list_item(
    result: dict[str, str | list[str]], list_key: str | None, raw: str, *, source: str
) -> None:
    """列表项入宿主键（无宿主键/宿主已转标量=格式破损，显式报错）。"""
    if list_key is None:
        raise LessonFormatError(f"{source}: 列表项无宿主键：{raw!r}")
    values = result[list_key]
    if not isinstance(values, list):
        raise LessonFormatError(f"{source}: {list_key} 标量后又见列表项")
    values.append(raw)


def parse_frontmatter(text: str, *, source: str) -> dict[str, str | list[str]]:
    """
    极简 frontmatter 解析（key: value 标量 + 缩进 `- 项` 列表，一级嵌套）。

    只认教训库 schema 用到的子集；越界形态（无起始/未闭合/裸行/注释/
    重复键）显式报错——受控文档宁可红不可猜。
    """
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise LessonFormatError(f"{source}: 缺起始 --- 行")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise LessonFormatError(f"{source}: frontmatter 未闭合") from exc
    result: dict[str, str | list[str]] = {}
    list_key: str | None = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        item = _LIST_ITEM_RE.match(line)
        if item is not None:
            _accept_list_item(result, list_key, item.group(1).strip(), source=source)
            continue
        match = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if match is None or not _KEY_RE.match(match.group(1)):
            raise LessonFormatError(f"{source}: 无法解析行 {line!r}")
        key, rest = match.group(1), match.group(2).strip()
        if key in result:
            # 重复键静默取后值=受控文档改写通道——correctness F3，宁可红
            raise LessonFormatError(f"{source}: 重复键 {key!r}")
        if rest:
            result[key] = rest
            list_key = None
        else:
            result[key] = []
            list_key = key
    return result


def _validated_header(
    fields: dict[str, str | list[str]], path_stem: str, *, source: str
) -> tuple[str, str, str]:
    """校验 id/status/confidence 三个枚举位（id 兼查文件名一致）。"""
    lesson_id = fields["id"]
    if not isinstance(lesson_id, str) or not _ID_RE.match(lesson_id):
        raise LessonFormatError(f"{source}: id 非法 {lesson_id!r}")
    if path_stem != lesson_id:
        raise LessonFormatError(
            f"{source}: 文件名与 id 不一致（frontmatter id={lesson_id!r}）"
        )
    status = fields["status"]
    if status not in LESSON_STATUSES:
        raise LessonFormatError(f"{source}: status 非法 {status!r}")
    confidence = fields["confidence"]
    if confidence not in LESSON_CONFIDENCES:
        raise LessonFormatError(f"{source}: confidence 非法 {confidence!r}")
    return lesson_id, str(status), str(confidence)


def parse_lesson_file(path: Path) -> Lesson:
    """单条目解析+校验（字段齐、值合法、文件名与 id 一致）。"""
    source = path.name
    text = path.read_text(encoding="utf-8")
    fields = parse_frontmatter(text, source=source)
    expected = {
        "id",
        "status",
        "confidence",
        "created",
        "review_due",
        "window",
        "cause",
        "triggers",
        "evidence",
    }
    missing = expected - fields.keys()
    if missing:
        raise LessonFormatError(f"{source}: 缺字段 {sorted(missing)}")
    unknown = fields.keys() - expected
    if unknown:
        raise LessonFormatError(f"{source}: 未知字段 {sorted(unknown)}")
    lesson_id, status, confidence = _validated_header(fields, path.stem, source=source)

    review_due_raw = fields["review_due"]
    review_due = (
        None
        if review_due_raw == "null"
        else _parse_date(str(review_due_raw), "review_due", source)
    )
    if isinstance(fields["window"], list):
        # 列表形态被 str() 成 "['…']" 冒充标量——correctness F2，宁可红不可猜
        raise LessonFormatError(f"{source}: window 须为标量或 null")
    window = None if fields["window"] == "null" else str(fields["window"])
    cause = fields["cause"]
    if not isinstance(cause, str) or not cause.strip():
        raise LessonFormatError(f"{source}: cause 不能为空")

    triggers = fields["triggers"]
    evidence = fields["evidence"]
    for key, values in (("triggers", triggers), ("evidence", evidence)):
        if not isinstance(values, list) or not values:
            raise LessonFormatError(f"{source}: {key} 不能为空列表")

    body_start = text.splitlines().index("---", 1) + 1
    body = "\n".join(text.splitlines()[body_start:]).strip("\n")
    return Lesson(
        id=lesson_id,
        status=status,
        confidence=confidence,
        created=_parse_date(str(fields["created"]), "created", source),
        review_due=review_due,
        window=window,
        cause=str(cause),
        triggers=tuple(str(item) for item in triggers),
        evidence=tuple(str(item) for item in evidence),
        body=body,
    )


def load_lessons(directory: Path | None = None) -> tuple[Lesson, ...]:
    """
    全量加载（L-NN.md，按 id 数值升序；README.md 等非条目文件不读）。

    任何条目格式破损即整体上抛——受控目录是三读口与晨检的唯一事实源，
    静默跳过=教训悄悄从读口消失（比报错更糟）。
    """
    base = lessons_dir() if directory is None else directory
    if not base.is_dir():
        raise LessonFormatError(f"教训库目录缺席 {base}")
    paths = sorted(
        path
        for path in base.glob("L-*.md")
        if path.is_file() and _ID_RE.match(path.stem)
    )
    lessons = [parse_lesson_file(path) for path in paths]
    return tuple(sorted(lessons, key=lambda lesson: int(lesson.id[2:])))


def _active_lessons(directory: Path | None = None) -> tuple[Lesson, ...]:
    """提取 active 条目子集（三读口共用：archived/review_due 不进注入/排除面）。"""
    return tuple(
        lesson for lesson in load_lessons(directory) if lesson.status == "active"
    )


def weekly_injection(directory: Path | None = None) -> tuple[InjectionEntry, ...]:
    """
    读口①周批注入：active 条目 → 提案 prompt 注入结构。

    archived/review_due 条目不注入（复审面由晨检/月读承担）。
    """
    return tuple(
        InjectionEntry(
            lesson_id=lesson.id,
            triggers="；".join(lesson.triggers),
            cause=lesson.cause,
            evidence=lesson.evidence,
        )
        for lesson in _active_lessons(directory)
    )


def render_injection(entries: Sequence[InjectionEntry]) -> str:
    """读口①渲染：紧凑要点文（每条一行，周批 prompt 直拼）。"""

    def line(entry: InjectionEntry) -> str:
        evidence = "、".join(entry.evidence)
        return (
            f"- [{entry.lesson_id}] 触发：{entry.triggers}"
            + f"——别再死因：{entry.cause}（证据：{evidence}）"
        )

    return "\n".join(line(entry) for entry in entries)


def scanner_exclusions(directory: Path | None = None) -> tuple[ExclusionRule, ...]:
    """
    读口②EM/扫描器排除清单：active 条目 triggers 逐条摊平为降权排除面。

    一条 trigger 一行，携 lesson id 与 cause 供留痕；archived 不排除。
    """
    return tuple(
        ExclusionRule(lesson_id=lesson.id, trigger=trigger, cause=lesson.cause)
        for lesson in _active_lessons(directory)
        for trigger in lesson.triggers
    )


def _previous_month_bounds(today: date) -> tuple[date, date]:
    """上个自然月首尾（月读每月 1 日报送覆盖刚结束的月份）。"""
    first_of_month = today.replace(day=1)
    end = first_of_month - timedelta(days=1)
    start = end.replace(day=1)
    return start, end


def monthly_review_view(
    directory: Path | None = None, *, today: date
) -> MonthlyLessonView:
    """
    读口③月读视图：新入库 + 复审到期两清单。

    新入库=created 落上个自然月（月读每月 1 日报送覆盖刚结束的月份）；
    复审到期=status ≠ archived 且 review_due ≤ today（「不设复审」null
    不到期）。today 必须显式传入（月读报北京业务日；缺省会引入本机时区
    在每月 1 日切月的静默漂移）。
    """
    loaded = load_lessons(directory)
    start, end = _previous_month_bounds(today)
    new_entries = tuple(lesson for lesson in loaded if start <= lesson.created <= end)
    review_due_entries = tuple(
        lesson
        for lesson in loaded
        if lesson.status != "archived"
        and lesson.review_due is not None
        and lesson.review_due <= today
    )
    return MonthlyLessonView(
        new_entries=new_entries, review_due_entries=review_due_entries
    )


def _ledger_section3_numbers(ledger_text: str) -> set[int]:
    """账本 §3 有序条目号集合（用于 §3#N 回指核对）。"""
    numbers: set[int] = set[int]()
    in_section = False
    for line in ledger_text.splitlines():
        heading = _EVIDENCE_SECTION_RE.match(line)
        if heading is not None:
            in_section = heading.group(1) == "3"
            continue
        if line.startswith("## "):
            in_section = False
            continue
        if in_section and (item := re.match(r"^(\d+)\. ", line)):
            numbers.add(int(item.group(1)))
    return numbers


def unresolved_evidence_refs(
    lessons: Sequence[Lesson], *, ledger_text: str, root: Path
) -> tuple[str, ...]:
    """
    证据回指核对（seed 质量自证）：evidence 必须落在三类外部证据之一。

    账本 §3 序号（`edge-ledger §3#N`，N 须在 §3 实存）；账本 §2 条目
    id（`edge-ledger E-NN`，heading 须实存）；报告路径（`report:<path>`，
    盘上存在或账本原文逐字可查——.scratch 工作稿按账本回查）。
    返回未解析清单（空=全部回指成立）。
    """
    section3 = _ledger_section3_numbers(ledger_text)
    entries2 = set(_EVIDENCE_ENTRY_RE.findall(ledger_text))
    problems: list[str] = []
    for lesson in lessons:
        for ref in lesson.evidence:
            if (m := _EVIDENCE_LEDGER_SECTION_RE.match(ref)) is not None:
                if int(m.group(1)) not in section3:
                    problems.append(f"{lesson.id}: §3#{m.group(1)} 不在账本 §3")
            elif (m := _EVIDENCE_LEDGER_ENTRY_RE.match(ref)) is not None:
                if m.group(1) not in entries2:
                    problems.append(f"{lesson.id}: {m.group(1)} 不在账本 §2")
            elif (m := _EVIDENCE_REPORT_RE.match(ref)) is not None:
                path = m.group(1)
                if not (root / path).exists() and path not in ledger_text:
                    problems.append(f"{lesson.id}: 报告路径既不在盘上也不在账本 {path}")
            else:
                problems.append(f"{lesson.id}: 证据格式不认识 {ref!r}")
    return tuple(problems)
