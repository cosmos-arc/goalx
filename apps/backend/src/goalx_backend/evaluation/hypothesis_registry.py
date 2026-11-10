"""
假设注册表（vision-build 票 02）：prereg 纪律的库表表达。

注册即冻结：可证伪措辞/判据与统计门/数据面/预期样本量/台账回指随注册
冻结（库层触发器拒绝改写，v51）；**改预测=新 h_id**（新行继承家族
attempt 计数），refuted 留痕不删、全量结果（含阴性）入账。

状态机（registered→running→supported|refuted）：
- 程序（扫描器 actor）只可把 registered 点亮为 running（数据面在场）；
- 注册与终态翻转恒人审——人 CLI 落账；agent 不自注册（注册面 actor 哨）、
  不自动定生死（终态哨；人审在环不变量，票 01 晋升哨同构）；
- 终态不翻案：重开=改预测=新 h_id（prereg 纪律延续）。

统计全部程序算（定则 5）：record_attempt 落逐次评估留痕
（UNIQUE(h_id, window_key) 吸收同窗重扫），attempt_count 只经属主函数
递增——票 04 月读按总尝试数做 DSR/BH 校正消费本计数字段。周报视图
（edge_scan.build_edge_digest 注册表节）报状态+建议态+尝试计数；建议态
由程序按数据面在场算（registered+关联扫描器已留痕→建议 running），
终态建议恒不出程序（人裁面）。

H1-H8 首批种子在迁移 v51（人审冻结措辞=库表身份，markets/selections
种子先例）；台账回指=edge-ledger 判决条目（E-02/E-23/E-24/E-08/E-17/
E-12/E-25/E-01）。扫描器挂钩示范=S1→H1（外加 S1 现行面恰为 H8 判据面、
S5 现行面恰为 H7 数据底座——research/61 判读节明言）；其余按数据面
点亮，SCANNER_LINKS 留 TODO 缝。
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from goalx_backend.db import atomic, utc_now_iso


class HypothesisState(StrEnum):
    """假设状态（两活态两终态；终态翻转恒人审）。"""

    REGISTERED = "registered"
    RUNNING = "running"
    SUPPORTED = "supported"
    REFUTED = "refuted"


#: 终态（supported|refuted）：留痕不删、不翻案——重开=新 h_id
TERMINAL_STATES: frozenset[HypothesisState] = frozenset(
    {HypothesisState.SUPPORTED, HypothesisState.REFUTED}
)

_ALLOWED_TRANSITIONS: frozenset[tuple[HypothesisState, HypothesisState]] = frozenset(
    # 点亮（程序或人）+ 进终态（恒人审——进终态哨在 transition_hypothesis）
    {
        (HypothesisState.REGISTERED, HypothesisState.RUNNING),
        (HypothesisState.REGISTERED, HypothesisState.SUPPORTED),
        (HypothesisState.REGISTERED, HypothesisState.REFUTED),
        (HypothesisState.RUNNING, HypothesisState.SUPPORTED),
        (HypothesisState.RUNNING, HypothesisState.REFUTED),
    }
)


def transition_allowed(from_state: HypothesisState, to_state: HypothesisState) -> bool:
    """转移谓词：非法转移拒绝（全表单测钉死；终态无出边=不翻案）。"""
    return (from_state, to_state) in _ALLOWED_TRANSITIONS


class InvalidTransition(ValueError):
    """非法状态转移（谓词拒绝或终态哨拦截；转移零写入）。"""


class InvalidRegistration(ValueError):
    """非法注册（agent 不自注册——注册面与终态同为人在环哨）。"""


class HIdFamilyConflict(InvalidRegistration):
    """同 h_id 已被异 family 注册（family 归属不可改；改预测=新 h_id）。"""


class UnknownHypothesis(LookupError):
    """操作未注册的 h_id。"""


def current_status(conn: sqlite3.Connection, h_id: str) -> HypothesisState | None:
    """假设现态（无此 h_id=None；单表现态列，属主函数是唯一写者）。"""
    row = conn.execute(
        "SELECT status FROM hypothesis_registry WHERE h_id = ?", (h_id,)
    ).fetchone()
    if row is None:
        return None
    return HypothesisState(str(row["status"]))


def transition_hypothesis(
    conn: sqlite3.Connection,
    h_id: str,
    to_state: HypothesisState,
    *,
    actor: str,
    reason: str,
) -> None:
    """
    落一次状态翻转（先验谓词，非法抛 InvalidTransition 且零写入）。

    终态哨（人审在环不变量）：目标为 supported|refuted 时只允许 actor 以
    ``human`` 起头的调用方（人 CLI）；程序/agent actor（scanner:*/agent:*
    等）至多点亮 registered→running——agent 不自注册、不自动定生死。
    check-then-act 在 BEGIN IMMEDIATE 写事务内闭环（票 01 correctness F1
    同构）：现态在写锁内重读，并发写者被串行化。
    """
    with atomic(conn):
        from_state = current_status(conn, h_id)
        if from_state is None:
            raise UnknownHypothesis(f"假设未注册: {h_id}")
        if not transition_allowed(from_state, to_state):
            raise InvalidTransition(
                f"非法转移: {from_state.value} → {to_state.value}"
                + "（终态不翻案，重开=改预测=新 h_id）"
            )
        if to_state in TERMINAL_STATES and not actor.startswith("human"):
            raise InvalidTransition(
                f"终态翻转恒人审（actor={actor} 不得定 {to_state.value} 生死）"
            )
        changed_at = utc_now_iso()
        conn.execute(
            """
            UPDATE hypothesis_registry
            SET status = ?, status_actor = ?, status_reason = ?, status_changed_at = ?
            WHERE h_id = ?
            """,
            (to_state.value, actor, reason, changed_at, h_id),
        )


# ---- 注册（改预测=新 h_id；同族新行继承家族 attempt 计数）----


def register_hypothesis(  # noqa: PLR0913 - prereg 字段即列（store.py 同例）
    conn: sqlite3.Connection,
    *,
    h_id: str,
    statement: str,
    judgement: str,
    data_face: str,
    expected_n: str,
    ledger_ref: str,
    family: str | None = None,
    actor: str = "human",
) -> int:
    """
    注册一个假设（幂等：同 h_id 同 family 返回既有行 id，措辞不随后到改写）。

    新行 attempt_count=同族（family，缺省=h_id）既有行的最大计数——家族
    复试递增，DSR/BH 校正分母=家族总尝试数而非行数（票 04 消费口径）。
    注册=冻结（frozen_at/first_registered_at=落账时刻）；措辞改写唯一
    路径=新 h_id 新行；h_id 的 family 归属同样不可改（同 h_id 异 family
    =HIdFamilyConflict 参数冲突，fail-loud 零写入）。

    人审哨（I1，镜像终态哨）：注册恒人审——actor 非 ``human`` 前缀拒绝
    （agent 不自注册）；status_actor 如实落调用方身份。
    """
    if not actor.startswith("human"):
        raise InvalidRegistration(f"注册恒人审（actor={actor} 不得自注册假设）")
    resolved_family = family if family is not None else h_id
    registered_at = utc_now_iso()
    with atomic(conn):
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO hypothesis_registry
                (h_id, family, statement, judgement, data_face, expected_n,
                 ledger_ref, attempt_count, first_registered_at, frozen_at,
                 status, status_actor, status_reason, status_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'registered', ?,
                    '注册', ?)
            """,
            (
                h_id,
                resolved_family,
                statement,
                judgement,
                data_face,
                expected_n,
                ledger_ref,
                registered_at,
                registered_at,
                actor,
                registered_at,
            ),
        )
        # 按 h_id 回读（族冲突在下一行分流，miss 只剩不可达防御面）
        row = conn.execute(
            "SELECT id, family FROM hypothesis_registry WHERE h_id = ?", (h_id,)
        ).fetchone()
        if row is None:  # pragma: no cover - 同事务回读自家 INSERT 必命中
            raise UnknownHypothesis("注册后读不到假设行")
        if str(row["family"]) != resolved_family:
            raise HIdFamilyConflict(
                f"h_id {h_id} 已被 family={row['family']} 注册"
                + "（异 family 复用 h_id=参数冲突；改预测=新 h_id）"
            )
        if cursor.rowcount == 1:
            inherited = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_count), 0) AS m
                FROM hypothesis_registry
                WHERE family = ? AND h_id != ?
                """,
                (resolved_family, h_id),
            ).fetchone()["m"]
            if int(inherited) > 0:
                conn.execute(
                    """
                    UPDATE hypothesis_registry SET attempt_count = ?
                    WHERE h_id = ?
                    """,
                    (int(inherited), h_id),
                )
    return int(row["id"])


# ---- attempt 计数（票 04 月读 DSR/BH 按总尝试数校正的底账）----


def attempt_count(conn: sqlite3.Connection, h_id: str) -> int:
    """该 h_id 行的 attempt 计数（含家族继承面；未注册=0）。"""
    row = conn.execute(
        "SELECT attempt_count FROM hypothesis_registry WHERE h_id = ?", (h_id,)
    ).fetchone()
    return int(row["attempt_count"]) if row is not None else 0


def record_attempt(
    conn: sqlite3.Connection,
    h_id: str,
    *,
    window_key: str,
    scanner: str | None = None,
    note: str | None = None,
) -> bool:
    """
    落一次统计评估尝试（同窗幂等；返回是否新计入）。

    留痕行（hypothesis_attempts，UNIQUE(h_id, window_key) 吸收同窗重扫）
    与计数递增同一写事务——账与计数不漂移。终态假设不计数（refuted/
    supported 后扫描器照跑，但死假设不产新尝试；重开=新 h_id 新计数）。
    """
    with atomic(conn):
        status = current_status(conn, h_id)
        if status is None:
            raise UnknownHypothesis(f"假设未注册: {h_id}")
        if status in TERMINAL_STATES:
            return False
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO hypothesis_attempts
                (h_id, window_key, scanner, note, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (h_id, window_key, scanner, note, utc_now_iso()),
        )
        if cursor.rowcount == 1:
            conn.execute(
                """
                UPDATE hypothesis_registry
                SET attempt_count = attempt_count + 1 WHERE h_id = ?
                """,
                (h_id,),
            )
            return True
    return False


# ---- 扫描器挂钩（交付 4：S1→H1 示范；其余按数据面点亮）----

#: 已接线扫描器 → 其数据面评价的假设（票面示范=S1→H1；H8 判据面
#: （滚动 90 日桶偏差）即 run_s1 现行面；H7 数据底座即 S5 持久面——
#: research/61 判读节明言）。未点亮面不硬造，留缝：
#: TODO(票02 数据面点亮)：s4→H4（s4 现面=决策时点陈盘观测，非滞后案例率）
#: TODO(票02 数据面点亮)：s5→H2/H3（份额校准扩展面"源B 849 行份额"未点亮）
#: TODO(票02 数据面点亮)：s3→H5（S3 未建）、s6→H6（S6 待建）
SCANNER_LINKS: dict[str, tuple[str, ...]] = {
    "s1": ("H1", "H8"),
    "s5": ("H7",),
}


def link_scanner_run(
    conn: sqlite3.Connection, *, scanner: str, window_key: str, note: str | None = None
) -> dict[str, bool]:
    """
    一次扫描窗的假设挂钩落账：每个挂钩**活**假设各记一尝试。

    窗键加 scanner 前缀域（f"{scanner}:{window_key}"）——不同扫描器同日
    窗互不吞并。返回 {h_id: 是否新计入}（终态/同窗重复=False），调用方
    （run_s1）并入观测 payload=扫描结果按 h_id 关联留痕。
    """
    return {
        h_id: record_attempt(
            conn,
            h_id,
            window_key=f"{scanner}:{window_key}",
            scanner=scanner,
            note=note,
        )
        for h_id in SCANNER_LINKS.get(scanner, ())
    }


# ---- 周报视图（交付 3：程序算建议态落周报；人读面，只读）----


def _suggested_state(status: HypothesisState, has_attempts: bool) -> str:
    """
    建议态 v0（程序算，只读）：数据面在场→建议点亮，终态不出程序。

    registered 且已有评估留痕→建议 running；running 持稳；终态恒空串
    ——翻案建议不出程序（人裁面，重开=新 h_id）。逐假设统计门求值器
    （程序算门→建议终态）随各扫描器扩展面点亮接入（S1 双源扩展→H1 门；
    票 04 月读消费计数）。
    """
    if status in TERMINAL_STATES:
        return ""
    if status is HypothesisState.REGISTERED and has_attempts:
        return HypothesisState.RUNNING.value
    return status.value


def registry_view(conn: sqlite3.Connection) -> list[dict[str, str | int]]:
    """周报注册表视图行（状态+建议态+尝试计数；h_id 序，只读）。"""
    rows = conn.execute(
        """
        SELECT r.h_id, r.family, r.statement, r.status, r.attempt_count,
               r.status_reason, r.ledger_ref,
               (SELECT COUNT(*) FROM hypothesis_attempts a WHERE a.h_id = r.h_id)
                   AS n_attempts
        FROM hypothesis_registry r
        ORDER BY r.h_id
        """
    ).fetchall()
    return [
        {
            "h_id": str(row["h_id"]),
            "family": str(row["family"]),
            "statement": str(row["statement"]),
            "status": str(row["status"]),
            "suggested": _suggested_state(
                HypothesisState(str(row["status"])), int(row["n_attempts"]) > 0
            ),
            "attempt_count": int(row["attempt_count"]),
            "status_reason": str(row["status_reason"]),
            "ledger_ref": str(row["ledger_ref"]),
        }
        for row in rows
    ]
