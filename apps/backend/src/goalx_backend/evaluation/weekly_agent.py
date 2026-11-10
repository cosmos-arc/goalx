"""
周批 agent 循环（vision-build 票 03）：L2 提案层——读面→提案→对抗质疑→人审。

节奏=周一 06:00 Prefect deployment（datasets 注册面 ``weekly-agent``）；
agent=心跳内函数调用非长驻。一次周拍：

1. 收缩闸门：contraction deployment_plan 级别 ≥L2（weekly_batch=paused）
   → 只留心跳不产提案（暂停由 deployment_plan 语义表达，不删 deployment）；
2. 空转守卫：台账（edge_findings）无新发现且教训库无新条目（对比上次
   周拍水位，weekly_agent_state）→ 只报心跳不烧 LLM（反模式 7：$15/篇
   量产无自然刹车）；首跑无水位=真跑（人裁 6：历史台账即首批阅读面）；
3. 真跑：钉探索钟锚点（一次性，contraction.ensure_exploration_anchor）→
   读面（edge_findings 全量含 refuted/阴性 + hypothesis_registry +
   教训库注入 lessons.weekly_injection + 周报 digest）→ LLM 产提案
   （≤5 条/次，引用 id 必带）→ 每条提案独立 fresh-context 对抗质疑
   （新会话，prompt 只含该提案与质疑指令，无生成上下文）→ 反驳意见
   附提案落受控目录（语料树 reports/proposals/，JSON+MD 双视图，
   append-only 不改写）→ LLM 花费逐调用落账 llm_proposal_costs。

不变量（票面）：
- agent 写权限空集：提案只进受控文件；注册是人 CLI（proposal-register
  → hypothesis_registry.register_hypothesis actor='human:cli'）；agent
  无库面研究写入（花费账/水位/锚点为运维账，非研究内容）。
- 引用 id 缺失的 LLM 产出在管线层直接拒收（票 11 裁定）；台账回指必须
  精确可解析（edge_findings 行 id 或注册表在册 ledger_ref）。
- 查重 v0=台账精确回指+refuted 库去伪新颖；票 06 RAG 落地后
  duplicate_reason 升语义近邻（接口即此函数，不动调用方）。
- LLM 只做假设生成与叙述，统计全程序算（门与读数不出本模块的 LLM 面）。

LLM 调用经 ChatFn 注入（evaluation 低于 llm 层，importlinter 分层）：
tasks.weekly_agent_batch 把 llm.client.glm_chat 适配进来；测试注入桩。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import (
    contraction,
    edge_scan,
    hypothesis_registry,
    lessons,
)

#: 每次周拍提案上限（票面：≤5 条/次，不收裸想法）
MAX_PROPOSALS_PER_RUN = 5
#: 受控提案目录（语料树 reports/ 先例：不进 git，temp+rename 原子写）
PROPOSALS_SUBDIR = Path("reports") / "proposals"
#: 锚点/水位写者的 actor 身份（运维账，非研究内容写入）
BATCH_ACTOR = "agent:weekly-batch"
#: 花费账 phase 枚举（v54 CHECK 同源）
PHASE_GENERATE = "generate"
PHASE_CRITIQUE = "critique"


@dataclass(frozen=True)
class ChatOutcome:
    """
    一次 LLM 调用的结果与记账要素（llm.client.ChatResult 的 evaluation 视角）。

    cost_cny 由适配方按 llm.client.estimate_cost_cny 牌价折算填入（evaluation
    低于 llm 层，importlinter 分层——牌价计算不在本模块）。
    """

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_cny: float


class ChatFn(Protocol):
    """LLM 调用缝：prompt 进、结果出；每次调用=fresh-context 新会话。"""

    def __call__(self, prompt: str) -> ChatOutcome:
        """一次调用（会话无状态，消息序列由适配方组装）。..."""
        ...


# ---- 空转守卫水位（weekly_agent_state，face_derive_state 同款 upsert） ----


def _lessons_count(directory: Path | None) -> int:
    """教训库条目计数（目录缺席=0；条目格式破损 fail-loud 不静默跳过）。"""
    base = lessons.lessons_dir() if directory is None else directory
    if not base.is_dir():
        return 0
    return len(lessons.load_lessons(base))


def current_watermark(
    conn: sqlite3.Connection, *, lessons_dir: Path | None
) -> dict[str, int]:
    """现拍水位：台账最大 id + 教训库条目数（新料=任一上移）。"""
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM edge_findings").fetchone()
    return {
        "edge_findings_max_id": int(row["m"]),
        "lessons_count": _lessons_count(lessons_dir),
    }


def read_watermark(conn: sqlite3.Connection) -> dict[str, int] | None:
    """上次周拍水位（无状态行=首跑，None）。"""
    row = conn.execute(
        "SELECT stats FROM weekly_agent_state WHERE face = 'watermark'"
    ).fetchone()
    if row is None:
        return None
    loaded: dict[str, int] = json.loads(str(row["stats"]))
    return loaded


def write_watermark(conn: sqlite3.Connection, watermark: dict[str, int]) -> None:
    """落本次周拍水位（face 键 upsert；真跑成功后调用）。"""
    with atomic(conn):
        conn.execute(
            """
            INSERT INTO weekly_agent_state (face, updated_at, stats)
            VALUES ('watermark', ?, ?)
            ON CONFLICT (face) DO UPDATE
                SET updated_at = excluded.updated_at, stats = excluded.stats
            """,
            (utc_now_iso(), json.dumps(watermark, ensure_ascii=False)),
        )


# ---- LLM 花费落账（llm_proposal_costs，append-only；票 04 月读消费） ----


def record_proposal_cost(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    phase: str,
    outcome: ChatOutcome,
) -> float:
    """
    逐调用落一笔提案层 LLM 花费，返回花费（元）。

    generate=批次级 B-yyyymmdd；critique=逐条提案 id。金额由适配方按
    llm.client 牌价折算进 ChatOutcome（evaluation 低于 llm 层，本模块
    只落账不算价）。
    """
    with atomic(conn):
        conn.execute(
            """
            INSERT INTO llm_proposal_costs
                (proposal_id, phase, model, prompt_tokens, completion_tokens,
                 cost_cny, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                phase,
                outcome.model,
                outcome.prompt_tokens,
                outcome.completion_tokens,
                outcome.cost_cny,
                utc_now_iso(),
            ),
        )
    return outcome.cost_cny


# ---- 读面（台账全量 + 注册表 + 教训注入 + 周报 digest）与 prompt ----


def read_faces(conn: sqlite3.Connection, *, lessons_dir: Path | None) -> dict[str, Any]:
    """
    周批阅读面装配（只读，不落盘）。

    edge_findings 全量（含 dead/阴性——伪新颖判读需要）+
    hypothesis_registry 视图 + 教训库注入 + 周报 digest。
    """
    findings = [
        {
            "id": int(r["id"]),
            "scanner": str(r["scanner"]),
            "segment_key": str(r["segment_key"]),
            "metric_value": float(r["metric_value"]),
            "n": int(r["n"]),
            "se": float(r["se"]),
            "window": str(r["window"]),
            "status": str(r["status"]),
        }
        for r in conn.execute(
            """
            SELECT id, scanner, segment_key, metric_value, n, se, window, status
            FROM edge_findings ORDER BY id
            """
        )
    ]
    injection = lessons.weekly_injection(lessons_dir)
    digest = edge_scan.build_edge_digest(
        conn, week_end=edge_scan.yesterday_business_date()
    )
    return {
        "edge_findings": findings,
        "hypothesis_registry": hypothesis_registry.registry_view(conn),
        "lessons_injection": [list(entry.triggers) for entry in injection],
        "digest": digest,
    }


def build_generate_prompt(faces: dict[str, Any]) -> str:
    """提案生成 prompt（统计全程序算：LLM 只组假设，禁止编造数字）。"""
    findings = faces["edge_findings"]
    registry = faces["hypothesis_registry"]
    injection = faces["lessons_injection"]
    digest = faces["digest"]
    return (
        "你是量化研究的方法学假设生成器。下面是本系统上周的探索台账与注册表读面。\n"
        "任务：从这些**已有证据**出发，提出至多 5 条新的可证伪研究假设提案。\n\n"
        "硬性要求：\n"
        "1. 只引用下面给出的证据，每条提案必须带 refs（引用 id 列表，元素取自"
        " 台账行 id（整数）、教训 id（L-NN）、假设 id（H<number>)——缺失即被拒收；\n"
        "2. ledger_ref 必须精确回指：`edge:<台账行id>`（如 edge:42）或注册表在册"
        " 账本引用（edge-ledger:E-02 形态，取自下方注册表 ledger_ref 列）；\n"
        "3. 不得编造任何统计量或数字结论——判据描述门与样本量要求，数值门由"
        " 程序计算；\n"
        "4. 不得与注册表已 refuted 的假设同台账回指（伪新颖会被拒收）；\n"
        "5. 只输出一个 JSON 数组（可带```json 围栏），不要任何其他文字。每元素"
        " 字段：statement（可证伪措辞，一句可判真伪的话）、judgement（判据："
        "数据面、统计门、样本量要求）、data_face（数据面：哪个表/哪个扫描器"
        "面）、expected_n（预期样本量）、ledger_ref（台账回指）、refs（引用"
        " id 列表）。\n\n"
        f"== 探索台账（edge_findings 全量，含 dead 阴性）==\n"
        f"{json.dumps(findings, ensure_ascii=False)}\n\n"
        f"== 假设注册表（含 refuted 判读）==\n"
        f"{json.dumps(registry, ensure_ascii=False)}\n\n"
        f"== 教训库注入（别再死一次清单）==\n"
        f"{json.dumps(injection, ensure_ascii=False)}\n\n"
        f"== 周报 digest（摘要）==\n{json.dumps(digest, ensure_ascii=False)}\n"
    )


def build_critique_prompt(proposal: dict[str, Any]) -> str:
    """
    对抗质疑 prompt：fresh-context——只含这一条提案与质疑指令。

    不含生成上下文（台账/注册表/教训/周报不进本 prompt）：质疑者不能被
    生成时的论证带着走。找三类洞：前视（look-ahead）/多重检验/判据漏洞。
    """
    return (
        "你是对抗性质疑者（红队）。下面是一条待人审的研究假设提案。\n"
        "任务：独立攻击它——只找以下三类问题并直说，不做鼓励性总结：\n"
        "1. 前视偏差（判据或数据面是否用了决策时点不可知的信息）；\n"
        "2. 多重检验（是否在已被反复扫描的面上再要一次显著，家族错误率何在）；\n"
        "3. 判据漏洞（统计门/样本量是否可判定、是否存在不可证伪的措辞）。\n"
        "只输出中文反驳意见（一段到三段），不要 JSON。\n\n"
        f"== 提案 ==\n{json.dumps(proposal, ensure_ascii=False)}\n"
    )


# ---- LLM 产出解析与管线层拒收（票 11 裁定：引用 id 缺失直接拒） ----

_REQUIRED_FIELDS = ("statement", "judgement", "data_face", "expected_n", "ledger_ref")
# 引用 id 家族路由正则（路由而已；存在性由各家族实存集合判）
_LESSON_REF_RE = re.compile(r"^L-\d{2,}$")
_HYPOTHESIS_REF_RE = re.compile(r"^H\d+$")


def parse_proposals(content: str) -> tuple[list[dict[str, Any]], str]:
    """
    从 LLM 回文本抽 JSON 数组（容错围栏/前后缀；scout parse_probs 同思路）。

    返回 (原始提案 dict 列表, 解析注记)。坏输出=（空列表, 注记）——管线
    不因 LLM 文本格式崩，注记进周拍报告留痕。
    """
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline != -1 else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return [], "LLM 回文无 JSON 数组"
    try:
        loaded = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return [], "LLM 回文 JSON 解析失败"
    # 子串必以 [ 开头 ] 结尾：loads 成功即 list；非 dict 元素（LLM 混入
    # 标量）剔除，不废整批
    return [item for item in loaded if isinstance(item, dict)], "ok"


def _registry_refs(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    """注册表台账回指集合：全部在册 refs + refuted 假设的 refs（去伪新颖面）。"""
    rows = conn.execute("SELECT ledger_ref, status FROM hypothesis_registry").fetchall()
    all_refs = {str(r["ledger_ref"]) for r in rows}
    refuted = {str(r["ledger_ref"]) for r in rows if str(r["status"]) == "refuted"}
    return all_refs, refuted


def _refs_resolve(
    ref: str,
    *,
    finding_ids: set[int],
    lesson_ids: set[str],
    hypothesis_ids: set[str],
) -> bool:
    """引用 id 可解析：台账行 id（int 形态）/ 教训 L-NN / 假设 H<number>。"""
    if ref.isdigit():
        return int(ref) in finding_ids
    if _LESSON_REF_RE.match(ref):
        return ref in lesson_ids
    if _HYPOTHESIS_REF_RE.match(ref):
        return ref in hypothesis_ids
    return False


def duplicate_reason(
    ledger_ref: str, *, batch_refs: set[str], refuted_refs: set[str]
) -> str | None:
    """
    查重 v0：台账精确回指去伪新颖（refuted 库 + 本批重复）。

    票 06 RAG 落地后本函数升语义近邻（接口不变，调用方不动）：
    TODO(票 06)：semantic_duplicate(statement) 近邻查重替换精确回指面。
    """
    if ledger_ref in refuted_refs:
        return "撞 refuted 库：该台账回指已有 refuted 假设（伪新颖，重开=人裁新 h_id）"
    if ledger_ref in batch_refs:
        return "与本批前序提案台账回指重复（同拍精确去重）"
    return None


def _ledger_ref_resolves(
    conn: sqlite3.Connection, ledger_ref: str, all_refs: set[str]
) -> bool:
    """台账回指精确解析：edge:<行id> 实存，或在册账本引用（edge-ledger:E-xx）。"""
    if ledger_ref.startswith("edge:") and ledger_ref[5:].isdigit():
        row = conn.execute(
            "SELECT 1 FROM edge_findings WHERE id = ?", (int(ledger_ref[5:]),)
        ).fetchone()
        return row is not None
    return ledger_ref in all_refs


@dataclass(frozen=True)
class _RefFaces:
    """校验面的实存集合（台账/教训/假设三家族 + 注册表回指两面）。"""

    finding_ids: set[int]
    lesson_ids: set[str]
    hypothesis_ids: set[str]
    all_refs: set[str]
    refuted_refs: set[str]


def _load_ref_faces(conn: sqlite3.Connection, lessons_dir: Path | None) -> _RefFaces:
    """装配校验面实存集合（教训目录缺席=空集；格式破损 fail-loud）。"""
    lessons_base = lessons.lessons_dir() if lessons_dir is None else lessons_dir
    lesson_ids: set[str] = set()
    if lessons_base.is_dir():
        lesson_ids = {lesson.id for lesson in lessons.load_lessons(lessons_base)}
    all_refs, refuted_refs = _registry_refs(conn)
    finding_ids = {int(r["id"]) for r in conn.execute("SELECT id FROM edge_findings")}
    return _RefFaces(
        finding_ids=finding_ids,
        lesson_ids=lesson_ids,
        hypothesis_ids={
            str(r["h_id"]) for r in conn.execute("SELECT h_id FROM hypothesis_registry")
        },
        all_refs=all_refs,
        refuted_refs=refuted_refs,
    )


def _refs_reasons(raw: dict[str, Any], faces: _RefFaces) -> list[str]:
    """引用 id 面（refs 必带、逐个可解析）的拒收理由。"""
    refs: object = raw.get("refs")
    if not isinstance(refs, list) or not refs:
        return ["引用 id 缺失（refs 必带，票 11 裁定管线层拒收）"]
    items = cast("list[object]", refs)
    if not all(isinstance(item, str) for item in items):
        return ["refs 含非字符串元素"]
    unresolved = [
        ref
        for ref in (str(item) for item in items)
        if not _refs_resolve(
            ref,
            finding_ids=faces.finding_ids,
            lesson_ids=faces.lesson_ids,
            hypothesis_ids=faces.hypothesis_ids,
        )
    ]
    return [f"引用 id 不可解析：{unresolved}"] if unresolved else []


def _proposal_reasons(
    conn: sqlite3.Connection,
    raw: dict[str, Any],
    faces: _RefFaces,
    *,
    batch_refs: set[str],
) -> list[str]:
    """单条提案的拒收理由（字段/引用/台账回指/查重）。"""
    reasons: list[str] = []
    for field in _REQUIRED_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"缺必带字段或为空：{field}")
    reasons += _refs_reasons(raw, faces)
    ledger_ref = raw.get("ledger_ref")
    if isinstance(ledger_ref, str) and ledger_ref.strip():
        if not _ledger_ref_resolves(conn, ledger_ref, faces.all_refs):
            reasons.append(
                f"台账回指不可解析（须 edge:<id> 或在册账本引用）：{ledger_ref}"
            )
        else:
            dup = duplicate_reason(
                ledger_ref, batch_refs=batch_refs, refuted_refs=faces.refuted_refs
            )
            if dup is not None:
                reasons.append(dup)
            batch_refs.add(ledger_ref)
    return reasons


def validate_batch(
    conn: sqlite3.Connection,
    raw_proposals: list[dict[str, Any]],
    *,
    resolved_date: str,
    lessons_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    管线层校验整批 LLM 产出，返回 (过审提案（已编 id）, 拒收清单)。

    校验面：字段齐、引用 id 必带且可解析、台账回指精确可解析、查重
    v0、≤5 条/次。拒收只剔除不中断（一条坏产出不废整批）；拒收理由
    进报告留痕。
    """
    faces = _load_ref_faces(conn, lessons_dir)
    validated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    batch_refs: set[str] = set()
    seq = 0
    for position, raw in enumerate(raw_proposals):
        if position >= MAX_PROPOSALS_PER_RUN:
            rejected.append(
                {
                    "raw": raw,
                    "reasons": [f"超出每次 {MAX_PROPOSALS_PER_RUN} 条上限（截断）"],
                }
            )
            continue
        reasons = _proposal_reasons(conn, raw, faces, batch_refs=batch_refs)
        if reasons:
            rejected.append({"raw": raw, "reasons": reasons})
            continue
        seq += 1
        proposal = dict(raw)
        proposal["id"] = f"P-{resolved_date.replace('-', '')}-{seq:02d}"
        validated.append(proposal)
    return validated, rejected


# ---- 周拍主口（心跳内函数；deployment=datasets 注册面 weekly-agent） ----


def run_weekly_batch(
    conn: sqlite3.Connection,
    *,
    store: CorpusStore,
    chat: ChatFn,
    run_date: str | None = None,
    lessons_dir: Path | None = None,
) -> dict[str, Any]:
    """
    一次周拍（幂等语义=水位守卫：同料不重烧；收缩 ≥L2 只留心跳）。

    返回心跳/生成报告（status: paused_heartbeat | idle_heartbeat |
    generated）；真跑产提案文件（JSON+MD 双视图，受控目录 append-only）
    并逐调用落 LLM 花费。首跑钉探索钟锚点（当天，一次性）。
    """
    resolved_date = run_date or fx_store.beijing_business_date()
    level = contraction.current_level(conn)
    plan = contraction.deployment_plan(level)
    report: dict[str, Any] = {
        "schema": "weekly-proposals/1",
        "generated_at": utc_now_iso(),
        "run_date": resolved_date,
        "level": level.value,
    }
    if plan.weekly_batch != "run":
        report.update(
            {
                "status": "paused_heartbeat",
                "detail": (
                    "deployment_plan.weekly_batch=paused（级别 ≥L2，调度暂停语义）"
                ),
            }
        )
        return report
    watermark = current_watermark(conn, lessons_dir=lessons_dir)
    report["watermark"] = watermark
    previous = read_watermark(conn)
    if previous is not None and previous == watermark:
        report.update(
            {"status": "idle_heartbeat", "detail": "台账无新发现且教训库无新条目"}
        )
        return report
    report["anchor"] = contraction.ensure_exploration_anchor(
        conn, resolved_date, actor=BATCH_ACTOR
    )
    faces = read_faces(conn, lessons_dir=lessons_dir)
    report["read_faces"] = {
        "edge_findings": len(faces["edge_findings"]),
        "hypothesis_registry": len(faces["hypothesis_registry"]),
        "lessons_injection": len(faces["lessons_injection"]),
    }
    batch_id = f"B-{resolved_date.replace('-', '')}"
    generate_outcome = chat(build_generate_prompt(faces))
    generate_cost = record_proposal_cost(
        conn, proposal_id=batch_id, phase=PHASE_GENERATE, outcome=generate_outcome
    )
    raw_proposals, parse_note = parse_proposals(generate_outcome.content)
    validated, rejected = validate_batch(
        conn, raw_proposals, resolved_date=resolved_date, lessons_dir=lessons_dir
    )
    critique_cost = 0.0
    for proposal in validated:
        critique_outcome = chat(build_critique_prompt(proposal))
        critique_cost += record_proposal_cost(
            conn,
            proposal_id=str(proposal["id"]),
            phase=PHASE_CRITIQUE,
            outcome=critique_outcome,
        )
        proposal["critique"] = critique_outcome.content
        proposal["status"] = "awaiting_human_review"
    report.update(
        {
            "status": "generated",
            "parse_note": parse_note,
            "proposals": validated,
            "rejected": rejected,
            "llm_calls": 1 + len(validated),
            "costs": {
                "generate_cny": generate_cost,
                "critique_cny": critique_cost,
                "total_cny": generate_cost + critique_cost,
            },
        }
    )
    json_path, md_path = write_beat_files(store, report)
    report["files"] = {"json": str(json_path), "md": str(md_path)}
    write_watermark(conn, watermark)
    return report


# ---- 受控目录落盘（语料树 reports/ 先例：JSON+MD 双视图，append-only） ----

#: 人审 checklist（历史支持前置检查，人裁 7①）——打进 CLI 输出与提案 MD 模板
HUMAN_REVIEW_CHECKLIST: tuple[str, ...] = (
    "1. 数据面历史可算：判据的数据面在 hist_matches/gold 十年回放面可回放计算"
    + "（历史答不了的先验问题不注册）。",
    "2. 不撞教训库已知阴性：refs 与措辞不落入教训库（docs/plans/lessons/）"
    + "任何 active 条目的触发面。",
    "3. 不撞 edge-ledger 已知阴性：台账回指不指向已 refuted/已搜索未发现的"
    + "判决桶（edge-ledger §0）。",
    "4. 可证伪措辞与判据读得懂、门可判定：冻结即 prereg，注册后不可改写"
    + "（改预测=新 h_id）。",
    "5. 对抗质疑的反驳意见已读并接受（反驳未答复的不得注册）。",
)


def _beat_basename(proposals_dir: Path, run_date: str) -> str:
    """本次周拍文件基名：append-only——同名已存在时递增 -2/-3 后缀不覆盖。"""
    base = f"weekly-proposals-{run_date}"
    if not (proposals_dir / f"{base}.json").exists():
        return base
    n = 2
    while (proposals_dir / f"{base}-{n}.json").exists():
        n += 1
    return f"{base}-{n}"


def render_markdown(report: dict[str, Any]) -> str:
    """周拍 MD 视图（人读面）：心跳行 + 逐提案全字段 + 反驳意见 + 拒收清单。"""
    lines = [
        f"# 周批提案 {report['run_date']}",
        "",
        f"- status: **{report['status']}**（收缩级别 {report['level']}）",
    ]
    if "watermark" in report:
        lines.append(f"- 水位: `{json.dumps(report['watermark'], ensure_ascii=False)}`")
    if "anchor" in report:
        lines.append(f"- 探索钟锚点: {report['anchor']}")
    if "costs" in report:
        lines.append(f"- LLM 花费: `{json.dumps(report['costs'], ensure_ascii=False)}`")
    proposals: list[dict[str, Any]] = report.get("proposals") or []
    rejected: list[dict[str, Any]] = report.get("rejected") or []
    lines += ["", "## 提案（待人审）", ""]
    for proposal in proposals:
        lines += [
            f"### {proposal['id']}",
            "",
            f"- **可证伪措辞**: {proposal['statement']}",
            f"- **判据**: {proposal['judgement']}",
            f"- **数据面**: {proposal['data_face']}",
            f"- **预期样本量**: {proposal['expected_n']}",
            f"- **台账回指**: {proposal['ledger_ref']}",
            f"- **引用 id**: {', '.join(str(r) for r in proposal['refs'])}",
            "",
            "**对抗质疑（反驳意见）**:",
            "",
            str(proposal.get("critique") or "（缺席——不得进入人审）"),
            "",
        ]
    if not proposals:
        lines += ["（本拍无提案）", ""]
    if rejected:
        lines += ["## 管线层拒收", ""]
        for item in rejected:
            raw: dict[str, Any] = item.get("raw") or {}
            raw_id = raw.get("id", "?")
            reasons = "；".join(str(r) for r in item["reasons"])
            lines.append(f"- {raw_id}: {reasons}")
    lines += ["", "## 人审 checklist（proposal-register 前逐条确认）", ""]
    lines += [f"- [ ] {item}" for item in HUMAN_REVIEW_CHECKLIST]
    return "\n".join(lines) + "\n"


def write_beat_files(store: CorpusStore, report: dict[str, Any]) -> tuple[Path, Path]:
    """
    周拍报告落语料树 reports/proposals/（JSON+MD，temp+rename 原子写）。

    受控目录 append-only：文件名含 run_date 且重名递增后缀——永不覆盖
    既有拍次。返回 (JSON 路径（人审 CLI 消费口）, MD 路径)。
    """
    proposals_dir = store.root / PROPOSALS_SUBDIR
    proposals_dir.mkdir(parents=True, exist_ok=True)
    basename = _beat_basename(proposals_dir, str(report["run_date"]))
    json_tmp = proposals_dir / f"{basename}.json.tmp"
    md_tmp = proposals_dir / f"{basename}.md.tmp"
    json_tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_tmp.write_text(render_markdown(report), encoding="utf-8")
    json_path = proposals_dir / f"{basename}.json"
    md_path = proposals_dir / f"{basename}.md"
    json_tmp.replace(json_path)
    md_tmp.replace(md_path)
    return json_path, md_path


# ---- 人审注册（proposal-register：过审提案 freeze 进 hypothesis_registry） ----


class ProposalRegistrationError(ValueError):
    """提案不满足人审前置（无对抗质疑/撞 refuted 库）——拒绝注册，零写入。"""


def parse_beat_file(path: Path) -> dict[str, Any]:
    """读周拍提案文件（JSON 视图；人审 CLI 消费口）。"""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def register_from_proposal(
    conn: sqlite3.Connection,
    path: Path,
    *,
    proposal_id: str,
    h_id: str,
    family: str | None = None,
) -> dict[str, Any]:
    """
    人过审后把一条提案 freeze 进 hypothesis_registry（人 CLI 核心逻辑）。

    前置哨（fail-loud 零写入）：提案须在文件内且已附对抗质疑反驳意见
    （无反驳=未过审）；台账回指不撞 refuted 库（判读后置翻转的场景）。
    注册本身走 hypothesis_registry.register_hypothesis（actor='human:cli'，
    注册面人审哨）——措辞注册即冻结，改预测=新 h_id。幂等：同 h_id 重复
    注册返回既有行（idempotent=True）。
    """
    beat = parse_beat_file(path)
    proposals_raw: object = beat.get("proposals")
    if not isinstance(proposals_raw, list):
        raise ProposalRegistrationError(f"提案文件缺 proposals 数组: {path}")
    proposals = cast("list[dict[str, Any]]", proposals_raw)
    match = next(
        (p for p in proposals if str(p.get("id")) == proposal_id),
        None,
    )
    if match is None:
        raise LookupError(f"提案文件内无此提案 id: {proposal_id} ({path})")
    if not str(match.get("critique") or "").strip():
        raise ProposalRegistrationError(
            f"{proposal_id} 未附对抗质疑反驳意见——不得进入人审注册"
        )
    ledger_ref = str(match["ledger_ref"])
    _, refuted_refs = _registry_refs(conn)
    if ledger_ref in refuted_refs:
        raise ProposalRegistrationError(
            f"{proposal_id} 台账回指 {ledger_ref} 已有 refuted 假设"
            + "（伪新颖；重开=人裁新预测新 h_id，不走本提案）"
        )
    # prereg 附录 E 节（票 08 冻结）：L1+ 停新注册——deployment_plan.
    # new_registrations=False 期间不得冻结新假设（恢复=L0 相邻步进人 CLI）。
    # 全 register_hypothesis 面的统一闸留缝票 02/08；周批提案的注册口
    # 在此消费该映射。
    plan = contraction.deployment_plan(contraction.current_level(conn))
    if not plan.new_registrations:
        raise ProposalRegistrationError(
            "收缩 L1+ 停新注册（prereg 附录 E 节）；恢复=L0 相邻步进人 CLI"
        )
    existed = hypothesis_registry.current_status(conn, h_id) is not None
    row_id = hypothesis_registry.register_hypothesis(
        conn,
        h_id=h_id,
        statement=str(match["statement"]),
        judgement=str(match["judgement"]),
        data_face=str(match["data_face"]),
        expected_n=str(match["expected_n"]),
        ledger_ref=ledger_ref,
        family=family,
        actor="human:cli",
    )
    return {
        "h_id": h_id,
        "proposal_id": proposal_id,
        "row_id": row_id,
        "idempotent": existed,
    }
