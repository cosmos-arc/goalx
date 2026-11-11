"""
周批 agent 循环（vision-build 票 03）：L2 提案层——读面→提案→对抗质疑→人审。

节奏=周一 06:00 Prefect deployment（datasets 注册面 ``weekly-agent``）；
agent=心跳内函数调用非长驻。一次周拍：

1. 收缩闸门：contraction deployment_plan 级别 ≥L2（weekly_batch=paused）
   → 只留心跳不产提案（暂停由 deployment_plan 语义表达，不删 deployment）；
2. 空转守卫：台账（edge_findings）无新发现且教训库无新条目（对比上次
   周拍水位，weekly_agent_state）→ 只报心跳不烧 LLM（反模式 7：$15/篇
   量产无自然刹车）；首跑无水位=真跑（人裁 6：历史台账即首批阅读面）；
3. 非暂停拍统一记账（2026-11-11 人裁 mining-loop 票 03）：钉探索钟锚点
   （一次性，contraction.ensure_exploration_anchor）+ 记一轮回路轮次
   （contraction.record_weekly_round，按日幂等——idle 心跳也是轮）；
4. 真跑：读面（edge_findings 全量含 refuted/阴性 + hypothesis_registry +
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
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import duckdb
from loguru import logger

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import atomic, utc_now_iso
from goalx_backend.evaluation import (
    contraction,
    edge_scan,
    hypothesis_registry,
    lessons,
    rag,
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
        ).fetchall()
    ]
    injection = lessons.weekly_injection(lessons_dir)
    digest = edge_scan.build_edge_digest(
        conn, week_end=edge_scan.yesterday_business_date()
    )
    # 票 06：S7 搜索窗特征重要性读数进周批阅读面（LdP 中间层只产读数；
    # 窗冻结 → 每周同一份，最新观测幂等 REPLACE 不膨胀）
    s7_row = conn.execute(
        """
        SELECT payload FROM edge_observations
        WHERE scanner = 's7' ORDER BY window_key DESC LIMIT 1
        """
    ).fetchone()
    return {
        "edge_findings": findings,
        "hypothesis_registry": hypothesis_registry.registry_view(conn),
        # F3：教训注入带 lesson_id（render_injection 一行一条，[L-NN] 头）
        # ——丢 id 则 LLM 无从引用实存教训，refs 校验必拒
        "lessons_injection": lessons.render_injection(injection).splitlines(),
        "digest": digest,
        "s7_feature_reading": json.loads(str(s7_row[0]))
        if s7_row is not None
        else None,
    }


def build_generate_prompt(faces: dict[str, Any]) -> str:
    """提案生成 prompt（统计全程序算：LLM 只组假设，禁止编造数字）。"""
    findings = faces["edge_findings"]
    registry = faces["hypothesis_registry"]
    injection = faces["lessons_injection"]
    digest = faces["digest"]
    s7 = faces.get("s7_feature_reading")
    return (
        "你是量化研究的方法学假设生成器。下面是本系统上周的探索台账与注册表读面。\n"
        "任务：从这些**已有证据**出发，提出至多 5 条新的可证伪研究假设提案。\n\n"
        "硬性要求：\n"
        "1. 只引用下面给出的证据，每条提案必须带 refs（引用 id 列表）；所有"
        ' 字段与 refs 元素一律字符串——台账行 id 写 "42"（不是 42）、教训 id'
        ' 写 "L-01"、假设 id 写 "H2"，expected_n 写 "n≥300/桶"（不是 300）'
        "——refs 缺失即被拒收；\n"
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
        f"== 六轴搜索窗特征重要性读数（S7，样本内描述统计不出结论；"
        "可作提案的上下文证据，但 refs 仍须台账行/教训/假设 id）==\n"
        f"{json.dumps(s7, ensure_ascii=False)}\n\n"
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


def _coerce_scalars(raw: dict[str, Any]) -> dict[str, Any]:
    """
    标量类型宽容归一（correctness F1），返回归一后的浅拷贝。

    prompt 虽钉死全字符串约定，LLM 回 JSON 仍常混数字形态（refs:[42,
    "L-01"] / expected_n:300）——int（不含 bool）→ str 归一后再校验，
    避免首真跑大批误拒；落盘/注册消费的即归一后形态。其余异型（float/
    嵌套）不救，走拒收留痕。
    """
    coerced = dict(raw)
    expected = coerced.get("expected_n")
    if isinstance(expected, int) and not isinstance(expected, bool):
        coerced["expected_n"] = str(expected)
    refs = coerced.get("refs")
    if isinstance(refs, list):
        items = cast("list[object]", refs)
        coerced["refs"] = [
            str(item) if isinstance(item, int) and not isinstance(item, bool) else item
            for item in items
        ]
    return coerced


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
    if ref.isascii() and ref.isdigit():
        # isdigit() Unicode 超集：上标 "²" 可过 isdigit 但 int() 抛、全角
        # "４２" 能 int() 伪通过——ASCII 位先卡（correctness F2）
        return int(ref) in finding_ids
    if _LESSON_REF_RE.match(ref):
        return ref in lesson_ids
    if _HYPOTHESIS_REF_RE.match(ref):
        return ref in hypothesis_ids
    return False


def duplicate_reason(
    ledger_ref: str,
    *,
    batch_refs: set[str],
    refuted_refs: set[str],
    statement: str = "",
    lesson_deduper: rag.LessonDeduper | None = None,
) -> str | None:
    """
    查重（票 03 精确面 + 票 06 语义近邻升级，接口向后兼容）。

    三层序（廉价在前）：①台账精确回指撞 refuted 库（伪新颖）；②本批
    精确重复；③教训索引语义近邻——statement 嵌入对教训命名空间 top-1
    余弦超阈（lesson_deduper 注入；缺席=票 03 v0 行为不动，教训索引
    未回填时亦如实退回精确面）。
    """
    if ledger_ref in refuted_refs:
        return "撞 refuted 库：该台账回指已有 refuted 假设（伪新颖，重开=人裁新 h_id）"
    if ledger_ref in batch_refs:
        return "与本批前序提案台账回指重复（同拍精确去重）"
    if lesson_deduper is not None and statement:
        try:
            hit = lesson_deduper.nearest(statement)
        except duckdb.Error as exc:
            # correctness F6：降级只兜 ATTACH 时刻不够——查询期故障（锁占/
            # 连接失效）同样不得炸周拍；留痕后回精确面，本条按未撞处理
            logger.warning("语义查重降级（duck 查询失败，回精确面）：{}", exc)
            hit = None
        if hit is not None:
            return (
                f"lesson_similar:{hit.item_id}：近邻复现死法"
                + f"（cos={hit.score:.3f}≥{lesson_deduper.threshold}，票 06 语义查重）"
            )
    return None


def _ledger_ref_resolves(
    conn: sqlite3.Connection, ledger_ref: str, all_refs: set[str]
) -> bool:
    """台账回指精确解析：edge:<行id> 实存，或在册账本引用（edge-ledger:E-xx）。"""
    if (
        ledger_ref.startswith("edge:")
        and ledger_ref[5:].isascii()
        and ledger_ref[5:].isdigit()
    ):
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
    lesson_deduper: rag.LessonDeduper | None = None,
) -> list[str]:
    """单条提案的拒收理由（字段/引用/台账回指/查重含语义近邻）。"""
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
            statement = raw.get("statement")
            dup = duplicate_reason(
                ledger_ref,
                batch_refs=batch_refs,
                refuted_refs=faces.refuted_refs,
                statement=statement if isinstance(statement, str) else "",
                lesson_deduper=lesson_deduper,
            )
            if dup is not None:
                reasons.append(dup)
            batch_refs.add(ledger_ref)
    return reasons


def validate_batch(
    conn: sqlite3.Connection,
    raw_proposals: list[dict[str, Any]],
    *,
    id_prefix: str,
    lessons_dir: Path | None = None,
    lesson_deduper: rag.LessonDeduper | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    管线层校验整批 LLM 产出，返回 (过审提案（已编 id）, 拒收清单)。

    校验面：字段齐（标量先经 _coerce_scalars 归一）、引用 id 必带且可
    解析、台账回指精确可解析、查重（票 03 精确面 + 票 06 语义近邻，
    lesson_deduper 注入缺席=v0 行为）、≤5 条/次。拒收只剔除不中断
    （一条坏产出不废整批）；拒收理由进报告留痕。id_prefix 含拍次位
    （同日第二拍起 P-yyyymmdd-<拍>-NN，correctness F4）。
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
        coerced = _coerce_scalars(raw)
        reasons = _proposal_reasons(
            conn, coerced, faces, batch_refs=batch_refs, lesson_deduper=lesson_deduper
        )
        if reasons:
            rejected.append({"raw": raw, "reasons": reasons})
            continue
        seq += 1
        proposal = dict(coerced)
        proposal["id"] = f"{id_prefix}-{seq:02d}"
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
    lesson_deduper: rag.LessonDeduper | None = None,
) -> dict[str, Any]:
    """
    一次周拍（幂等语义=水位守卫：同料不重烧；收缩 ≥L2 只留心跳）。

    返回心跳/生成报告（status: paused_heartbeat | idle_heartbeat |
    generated）；真跑产提案文件（JSON+MD 双视图，受控目录 append-only）
    并逐调用落 LLM 花费。非暂停拍钉探索钟锚点（当天，一次性）+ 记一轮
    回路轮次（2026-11-11 人裁：探索钟=轮次止损，idle 心跳也计轮）；
    报告附回放待排程假设面（hypothesis_registry.replay_pending）。
    lesson_deduper=票 06 语义查重缝（生产=tasks 适配件注入
    FastembedEmbedder+connect_rag；缺席/教训索引空=票 03 精确面）。
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
    # 非暂停拍统一记账（2026-11-11 人裁）：锚=轮 0 标记；轮次按日幂等
    report["anchor"] = contraction.ensure_exploration_anchor(
        conn, resolved_date, actor=BATCH_ACTOR
    )
    report["round"] = contraction.record_weekly_round(conn, run_date=resolved_date)
    report["replay_pending"] = hypothesis_registry.replay_pending(conn)
    previous = read_watermark(conn)
    if previous is not None and previous == watermark:
        report.update(
            {"status": "idle_heartbeat", "detail": "台账无新发现且教训库无新条目"}
        )
        return report
    # F5：拍名独占声明（O_EXCL 哨兵，常驻不删）——手动 CLI 与调度并发
    # 双跑各得不同拍次，append-only 不被同名互覆；F4：拍次进提案/批次
    # id（首拍无后缀，同日第二拍起 P-yyyymmdd-<拍>-NN / B-yyyymmdd-<拍>）
    proposals_dir = store.root / PROPOSALS_SUBDIR
    proposals_dir.mkdir(parents=True, exist_ok=True)
    beat = _claim_beat_suffix(proposals_dir, resolved_date)
    report["beat"] = beat
    beat_tag = "" if beat == 1 else f"-{beat}"
    date_compact = resolved_date.replace("-", "")
    id_prefix = f"P-{date_compact}{beat_tag}"
    faces = read_faces(conn, lessons_dir=lessons_dir)
    report["read_faces"] = {
        "edge_findings": len(faces["edge_findings"]),
        "hypothesis_registry": len(faces["hypothesis_registry"]),
        "lessons_injection": len(faces["lessons_injection"]),
    }
    batch_id = f"B-{date_compact}{beat_tag}"
    generate_outcome = chat(build_generate_prompt(faces))
    generate_cost = record_proposal_cost(
        conn, proposal_id=batch_id, phase=PHASE_GENERATE, outcome=generate_outcome
    )
    raw_proposals, parse_note = parse_proposals(generate_outcome.content)
    validated, rejected = validate_batch(
        conn,
        raw_proposals,
        id_prefix=id_prefix,
        lessons_dir=lessons_dir,
        lesson_deduper=lesson_deduper,
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
    json_path, md_path = write_beat_files(store, report, beat_suffix=beat)
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
    "6. 注册即排历史回放验证（2026-11-11 人裁 7① 升格）：判据验证介质="
    + "十年历史回放（数据面历史可算才注册），注册后假设进 replay_pending"
    + "面直至首次评估留痕——验证不等前向数据积累。",
)


#: 拍次哨兵文件后缀（O_EXCL 独占声明；常驻不删=拍名永久保留不复用）
_CLAIM_SUFFIX = ".claim"
#: 单日拍次上限（到达=管线已病态，fail-loud 不无限自旋）
_MAX_BEATS_PER_DAY = 999


def _claim_beat_suffix(proposals_dir: Path, run_date: str) -> int:
    """
    拍次独占声明（correctness F5）：O_EXCL 抢拍名哨兵，返回本拍拍次。

    哨兵文件常驻（不随拍结束删除）：① 并发双跑（手动 CLI × 调度）各抢
    各的拍次，append-only 不被同名互覆；② 拍名永久保留——崩溃残拍（如
    json 已落 md 未落）不复用旧拍名，下拍 -N 补齐双视图（F7 语义）。
    """
    proposals_dir.mkdir(parents=True, exist_ok=True)  # 自含：调用方可免预建
    base = f"weekly-proposals-{run_date}"
    beat = 1
    while beat <= _MAX_BEATS_PER_DAY:
        tag = "" if beat == 1 else f"-{beat}"
        claim = proposals_dir / f"{base}{tag}{_CLAIM_SUFFIX}"
        try:
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            beat += 1
            continue
        os.close(fd)
        return beat
    raise RuntimeError(f"{run_date} 拍次声明超过 {_MAX_BEATS_PER_DAY}：管线病态")


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
    if "round" in report:
        lines.append(
            f"- 回路轮次: {report['round']}/{contraction.EXPLORATION_WINDOW_ROUNDS}"
            + "（轮次止损钟，2026-11-11 人裁）"
        )
    if report.get("replay_pending"):
        lines.append(
            "- 回放待排程假设（注册即验，历史回放为介质）: "
            + ", ".join(str(h) for h in report["replay_pending"])
        )
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


def write_beat_files(
    store: CorpusStore, report: dict[str, Any], *, beat_suffix: int = 1
) -> tuple[Path, Path]:
    """
    周拍报告落语料树 reports/proposals/（JSON+MD 双视图，temp+rename）。

    拍名=run_date+拍次后缀（claim 独占，append-only 永不覆盖既有拍次）。
    双视图两 rename 非事务：json 先落、md 后落——中途崩溃留残 JSON（可
    解析、下拍 -N 补齐，F7 钉死的行为面，不强求原子化）。返回
    (JSON 路径（人审 CLI 消费口）, MD 路径)。
    """
    proposals_dir = store.root / PROPOSALS_SUBDIR
    proposals_dir.mkdir(parents=True, exist_ok=True)
    tag = "" if beat_suffix == 1 else f"-{beat_suffix}"
    basename = f"weekly-proposals-{report['run_date']}{tag}"
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

    文件级前置（无库面）：提案须在文件内且已附对抗质疑反驳意见。库级
    三哨（撞 refuted 库 / L1+ 停新注册 / 注册本身）在**同一个 atomic
    （BEGIN IMMEDIATE）写事务**内闭环（correctness F6）：并发
    hypothesis-mark/contraction-set 被写锁串行化——要么先于本事务提交
    （哨在事务内重读可见、拒注册零写入），要么等本事务提交后才落——
    哨查与注册之间的 TOCTOU 窗口不存在。注册走 register_hypothesis
    （actor='human:cli'，注册面人审哨，嵌套 SAVEPOINT）；幂等：同 h_id
    重复注册返回既有行（idempotent=True）。
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
    # F6：库级三哨+注册同一写事务（BEGIN IMMEDIATE 串行化并发翻转；
    # 哨拒绝在事务内抛出→回滚→零写入）
    with atomic(conn):
        _, refuted_refs = _registry_refs(conn)
        if ledger_ref in refuted_refs:
            raise ProposalRegistrationError(
                f"{proposal_id} 台账回指 {ledger_ref} 已有 refuted 假设"
                + "（伪新颖；重开=人裁新预测新 h_id，不走本提案）"
            )
        # prereg 附录 E 节（票 08 冻结）：L1+ 停新注册——deployment_plan.
        # new_registrations=False 期间不得冻结新假设（恢复=L0 相邻步进
        # 人 CLI）。全 register_hypothesis 面的统一闸留缝票 02/08；
        # 周批提案的注册口在此消费该映射。
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
