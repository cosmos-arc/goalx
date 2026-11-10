"""
探索轨扫描器阵列（autoresearch-20261107 票 01/04）。

常设机制=扫描器注册表 + 发现台账 + 周报：每天自己跑、自己留痕、自己报
候选，不再依赖 episodic 手工分析。两表（v45，SQL 归本包 ADR-0008）：

- ``edge_findings``：发现台账（append-only，探索轨唯一事实源）。越门发现
  一行，UNIQUE(scanner, segment_key, window) 吸收同窗重跑；status
  生命周期 new→promoted|dead 由人裁经 ``mark_finding_status``（晋升=
  开验证票，不自动建）。
- ``edge_observations``：运行/观测留痕（s1 逐窗统计快照、s4 观测日——
  无信号日如实记零、s5 期次回看）。派生态非证据，同键重跑 REPLACE。

扫描器 v0（S2/S3 随数据面点亮后向 ``SCANNERS`` 注册）：

- **s1 市场校准**：滚动 90 日窗已结算场，JC had as-of 开球三向比例去水，
  按价格带×方向分桶累计实际频率 vs 均值概率，二项 SE 越门
  （n≥门 且 |dev|>2SE）落发现。票 32 起挂 Romano-Wolf stepdown
  （21 段族 FWER α=0.05；落账口径=未校正门 ∧ RW 门，只收紧不放宽，
  清「21 段×逐日」未校正债）。``run_s1_decade`` = 研究库
  backtest_predictions 十年回放（口径验证面；只落观测不落发现——十年
  手工版已裁决"长赔高估 3-4pp、扣 13% 抽水后无正 EV 桶"，research/60）。
- **s2 模型校准/EV 实现**（票 02）：评分层报告面的台账化输出（不重算）。
  面 A=逐玩法最热向置信 10 箱 vs 实际命中（accuracy 报告同款派生）；
  面 B=已结算纸面注逐注实现单位损益 − 建注模型 EV 快照的配对差。两面
  同窗（90 日滚动）同门（n≥200 且 |dev|>2SE）落发现；纸面注未起量期
  如实记零面。面 C=log loss 第三指标（票 32；Wheatcroft 2021，RPS 之争
  对冲+尾部概率判别）：模型分布对实现向的 −ln(p)，对均匀锚 ln(k) 报
  skill；只留观测不进发现门，判读分层=S2_LOSS_READING_RULE。
- **s4 陈盘观测日**（票 04）：逐场决策时点（开球−60min）陈盘信号
  confirmed/fired/命中计数留痕——"有分辨观测日"积累器（had_quote 闸 +
  ``stale_line_signal`` 既有面，B1 已关闭可交易性→纯监测不产候选）。
  票 19：sharp 端点读法从 as-of 覆盖缺（silver 最新视图）切 corpus
  as-of 视图（青铜重放注入缝，payload 口径注记 sharp_source）。
- **s5 滚存回看**（票 04）：池期次兑付后回看奖池结构（滚存标记/池规模/
  派彩分布）；按缺席自补（近 7 日发布期次夜刷保鲜），首跑即历史回放。
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from statistics import fmean, pstdev
from typing import Any, cast

from goalx_backend import odds_math as om
from goalx_backend import run_fingerprint as rfp
from goalx_backend.betting import store as bet_store
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import pool as pool_store
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data import results as rs_store
from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.db import utc_now_iso
from goalx_backend.evaluation import accuracy, hypothesis_registry

REPORTS_DIR = "reports"
REPORT_BASENAME = "edge-scan-digest"

# ---- S1 口径常量（v0；显著性门形式化随票 05，先小后改须过人裁） ----
S1_WINDOW_DAYS = 90
S1_MIN_N = 100
S1_GATE_SE = 2.0

# ---- 票 32：S1 多重校正（RW stepdown；账本 §4.2#6/E-28⑥'/E-33 审计注记） ----
S1_RW_ALPHA = 0.05
S1_RW_REPLICATES = 2000

# ---- S2 口径常量（票 02 冻结：门=n≥200 且 |dev|>2SE；窗与 S1 同 90 日） ----
S2_MIN_N = 200
#: log loss 第三指标判读分层（票 32；冻结文字随报告展示，Wheatcroft 2021）
S2_LOSS_READING_RULE: tuple[str, ...] = (
    "层 0 结构：LL 无上界（尾部过自信即爆）、各玩法类数不同不可比——"
    + "跨玩法只读 skill=1−LL/ln(k)（对均匀无信息锚），LL 绝对值只在玩法内读。",
    "层 1 读数：skill>0=优于均匀锚；≈0=无信息；<0=反向（典型形态=尾部过自信，"
    + "RPS 对分级误差温和而 LL 爆——正是两指标分歧的判别点，Wheatcroft 2021）。",
    "层 2 纪律：n<200 探针规模不判；LL 与校准/RPS 分歧读「尾部概率质量差」"
    + "而非单指标定优劣；本面只留观测不进发现门（不新增多重比较面）。",
)
#: 价格带边界（上界, 标签）——与十年手工版/edge 板 03 同一分桶口径
BUCKET_EDGES: tuple[tuple[float, str], ...] = (
    (0.10, "p00-10"),
    (0.20, "p10-20"),
    (0.30, "p20-30"),
    (0.40, "p30-40"),
    (0.50, "p40-50"),
    (0.65, "p50-65"),
    (1.01, "p65+"),
)

# ---- S4 口径常量 ----
S4_DECISION_LEAD_MINUTES = 60
#: 陈旧门=15min（H4 清洁口径：15min 未跟概率 92.5%，票 08）
S4_STALE_MINUTES = 15
#: 有分辨门=sharp 漂移≥1.5pp（v0 常量，形式化随票 05）
S4_DRIFT_THRESHOLD = 0.015

#: s5 已观测期次的保鲜窗：近 7 日发布期次夜刷（官方 state 可后补/更正）
S5_REFRESH_DAYS = 7

DIRECTIONS: tuple[str, str, str] = ("h", "d", "a")
_LETTER_BY_DIRECTION = {"h": "H", "d": "D", "a": "A"}
_SELECTION_INDEX = {"h": 0, "d": 1, "a": 2}


@dataclass(frozen=True)
class ScanContext:
    """
    一次扫描的运行上下文（注册表统一签名；各扫描器按需取用）。

    asof_resolver（票 19）：(sid, as_of) → 青铜重放行 | None（不适用）。
    实现在 ingest.srct_asof（分层位于本包之上）——组合根（tasks/cli）
    注入；S4 sharp 端点双端历史时点先走重放（silver 最新视图回答不了
    夜扫查昨日决策时点，账本 §4.1#7 的 0 有分辨根因）。缺省 None=回退
    corpus silver 最新视图口径（修复前行为）。
    """

    business_date: str
    duck_con: qe.DuckCon | None = None
    resolve_sid: Callable[[int], str | None] | None = None
    asof_resolver: Callable[[str, str], list[qe.SnapshotRow] | None] | None = None


@dataclass(frozen=True)
class ScannerSpec:
    """扫描器注册项：名字/节奏/描述/统一入口。"""

    name: str
    cadence: str
    description: str
    run: Callable[[sqlite3.Connection, ScanContext], dict[str, Any]]


# ---- 分桶核心（s1 两面共用；纯函数，测试直证口径） ----


@dataclass(frozen=True)
class ObsRow:
    """一个方向观测：去水概率 + 是否命中。"""

    direction: str
    prob: float
    hit: bool


def bucket_label(prob: float) -> str:
    """去水概率 → 价格带标签（0≤p<1.01 全覆盖）。"""
    for upper, label in BUCKET_EDGES:
        if prob < upper:
            return label
    msg = f"去水概率越界: {prob}"
    raise ValueError(msg)


def _binomial_gate(
    mean_prob: float, actual_freq: float, n: int, *, min_n: int
) -> dict[str, Any]:
    """一段的二项 SE 越门判定（S1/S2 校准面共口径；dev=af−mean）。"""
    dev = actual_freq - mean_prob
    se = math.sqrt(mean_prob * (1.0 - mean_prob) / n)
    return {
        "n": n,
        "mean_prob": round(mean_prob, 6),
        "actual_freq": round(actual_freq, 6),
        "dev": round(dev, 6),
        "se": round(se, 6),
        "crossed": n >= min_n and abs(dev) > S1_GATE_SE * se,
    }


def calibration_segments(rows: list[ObsRow]) -> dict[str, dict[str, Any]]:
    """
    分桶累计 + 二项 SE 越门判定（十年手工版同款口径）。

    每段：n / mean_prob（去水概率均值）/ actual_freq（实测频率）/
    dev=af−mean / se=sqrt(p̄(1−p̄)/n)；越门 = n≥S1_MIN_N 且
    |dev|>S1_GATE_SE·se。
    """
    grouped: dict[str, list[ObsRow]] = {}
    for row in rows:
        key = f"{bucket_label(row.prob)}:{row.direction}"
        grouped.setdefault(key, []).append(row)
    return {
        key: _binomial_gate(
            sum(r.prob for r in group) / len(group),
            sum(r.hit for r in group) / len(group),
            len(group),
            min_n=S1_MIN_N,
        )
        for key, group in sorted(grouped.items())
    }


# ---- 票 32：Romano-Wolf stepdown（S1 多重校正，只收紧不放宽） ----

#: 注册族=7 价格带 × 3 方向（缺组段以 p=1 入族，E-32 修订 C 同精神）
_S1_FAMILY_KEYS: tuple[str, ...] = tuple(
    f"{label}:{direction}" for _, label in BUCKET_EDGES for direction in DIRECTIONS
)
_BUCKET_INDEX = {label: idx for idx, (_, label) in enumerate(BUCKET_EDGES)}


def _rw_segment_stats(
    segments: dict[str, dict[str, Any]],
) -> dict[str, tuple[int, float, float]]:
    """
    族段观测统计量预备（票 32）：{段键: (n, mp̄, se)}。

    段概率由赔率定，不随结果抽样变动 → n/mp̄/se 在 MC 复制间固定；
    se 不可估的退化段（mp̄∈{0,1}）不进 MC 统计量——fail-closed 只收紧。
    """
    stats: dict[str, tuple[int, float, float]] = {}
    for key in _S1_FAMILY_KEYS:
        seg = segments.get(key)
        if seg is None:
            continue
        n = int(seg["n"])
        mp, se = float(seg["mean_prob"]), float(seg["se"])
        if n > 0 and se > 0:
            stats[key] = (n, mp, se)
    return stats


def _rw_null_counts(
    match_probs: list[tuple[float, float, float]],
    segments: dict[str, dict[str, Any]],
    *,
    order: list[str],
    t_obs: dict[str, float],
    replicates: int,
    seed: str,
) -> list[int]:
    """
    完全零假设 Monte Carlo（票 32）：逐步计数 #{Q_l^b ≥ T_(l)}。

    每场按**自身去水三向**抽实现向（一次 random 定向，命中恰好进该向
    所在段——同场三向互斥 → 每场只动一个段计数，h/d/a 相依结构原样
    保留）；T_b 重算后按族序取后缀 max（剩余族 max）逐步计数。
    """
    counts = [0] * len(order)
    rng = random.Random(seed)  # noqa: S311 — 统计复现用种子，非密码学
    draw_plan = [
        (
            triple[0],
            triple[0] + triple[1],
            _BUCKET_INDEX[bucket_label(triple[0])],
            _BUCKET_INDEX[bucket_label(triple[1])],
            _BUCKET_INDEX[bucket_label(triple[2])],
        )
        for triple in match_probs
    ]
    # (族位置, 段平铺下标, n, mp̄, se)——T_b 计算与计数按族位置对齐
    stats = _rw_segment_stats(segments)
    flat_stats = [
        (pos, _SELECTION_INDEX[key[-1]] * 7 + _BUCKET_INDEX[key[:-2]], *stats[key])
        for pos, key in enumerate(order)
        if key in stats
    ]
    for _ in range(replicates):
        hits = [0] * 21
        for c1, c2, b_h, b_d, b_a in draw_plan:
            u = rng.random()
            if u < c1:
                hits[b_h] += 1
            elif u < c2:
                hits[7 + b_d] += 1
            else:
                hits[14 + b_a] += 1
        t_boot = [0.0] * len(order)
        for pos, flat, n, mp, se in flat_stats:
            t_boot[pos] = abs(hits[flat] / n - mp) / se
        suffix_max = 0.0  # 后缀 max=stepdown 剩余族 max
        for pos in range(len(order) - 1, -1, -1):
            suffix_max = max(suffix_max, t_boot[pos])
            if suffix_max >= t_obs[order[pos]]:
                counts[pos] += 1
    return counts


def romano_wolf_stepdown(
    match_probs: list[tuple[float, float, float]],
    segments: dict[str, dict[str, Any]],
    *,
    alpha: float = S1_RW_ALPHA,
    replicates: int = S1_RW_REPLICATES,
    seed: str = "s1",
) -> dict[str, dict[str, Any]]:
    """
    Romano-Wolf stepdown 多重校正（票 32；清 S1「21 段×逐日」未校正债）。

    公式（自写等价实现，cite）：

    - 检验族 H_j: dev_j=0（段校准），j=1..21（7 价格带×3 方向，缺组段
      T=0/p=1 入族不缺席）；统计量 T_j=|dev_j|/se_j（与注册未校正门同口径）。
    - 零假设分布=完全零假设下的 Monte Carlo（``_rw_null_counts``）：
      每场按自身去水三向抽实现向（B 次）重算全段统计量——同场 h/d/a
      互斥相依结构被原样保留。Romano & Wolf (2005) "Stepwise Multiple
      Testing as Formalized Data Snooping", Econometrica 73(4) 1237–1282；
      adjusted p 的 Monte-Carlo 形式见 Romano & Wolf (2016), Statist.
      Probab. Lett. 113:38–44；工程实现参照 Clarke (2019) rwolf（Stata SSC）。
    - stepdown：按 T 观测值降序 j_(1)..j_(m)，第 l 步零分布统计量
      Q_l^b = max_{k≥l} T_(k)^b（剩余族 max），p_rw(j_(l)) =
      max_{l'≤l} (1+#{Q_{l'}^b ≥ T_(l')})/(B+1)（单调化）。
    - **只收紧不放宽**：crossed_rw = 注册未校正门（n≥100 且 |z|>2）∧
      p_rw ≤ α——RW 通过但未过未校正门者不越门（门叠加不替换，红线）。

    固定种子（seed 串）→ 同输入逐位可复现（台账幂等/审计可复算的前提）。
    返回全 21 段 {p_raw, p_rw, crossed_rw}；观测段才有真读数，缺组段
    p_raw=p_rw=1.0、crossed_rw=False。
    """
    stats = _rw_segment_stats(segments)
    t_obs = {
        key: (abs(float(segments[key]["dev"])) / stats[key][2] if key in stats else 0.0)
        for key in _S1_FAMILY_KEYS
    }
    # 排序族（T 降序；并列按段键稳定排序——确定性）
    order = sorted(_S1_FAMILY_KEYS, key=lambda key: (-t_obs[key], key))
    counts = _rw_null_counts(
        match_probs,
        segments,
        order=order,
        t_obs=t_obs,
        replicates=replicates,
        seed=seed,
    )
    # 单调化调整 p（Romano & Wolf 2016）：p_rw(j_(l)) = max_{l'≤l} 前缀 max
    result: dict[str, dict[str, Any]] = {}
    running = 0.0
    for pos, key in enumerate(order):
        p_step = (1 + counts[pos]) / (replicates + 1)
        running = max(running, p_step)
        observed = segments.get(key)
        eligible = key in stats
        p_raw = (
            math.erfc(t_obs[key] / math.sqrt(2.0))
            if observed is not None and eligible
            else 1.0
        )
        result[key] = {
            "p_raw": round(p_raw, 6),
            "p_rw": round(running, 6),
            # 只收紧红线：未过注册未校正门（或统计量不可估）者 RW 永不放行
            "crossed_rw": bool(
                eligible and observed is not None and observed.get("crossed")
            )
            and running <= alpha,
        }
    return result


# ---- 台账读写（两表 SQL 归本包） ----


def _record_observation(
    conn: sqlite3.Connection, scanner: str, window_key: str, payload: dict[str, Any]
) -> None:
    """观测留痕（派生态非证据，同键 REPLACE 重建）。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO edge_observations
            (scanner, window_key, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (scanner, window_key, json.dumps(payload, ensure_ascii=False), utc_now_iso()),
    )


def insert_findings(
    conn: sqlite3.Connection,
    *,
    scanner: str,
    window: str,
    segments: dict[str, dict[str, Any]],
    extra_notes: dict[str, Any],
) -> int:
    """
    越门段落发现台账（INSERT OR IGNORE：同窗重跑不重复落行）。

    已人裁段（promoted|dead）粘滞：滚动窗滑日后同段再越门不再落新行——
    死刑/晋升是段级终态（spec"死刑=计数留痕"），重开须人裁翻案。
    守卫读+插入同一写事务（BEGIN IMMEDIATE）：与 CLI --mark 人裁并发时
    不落"刚裁死段"的新行（check-then-act 关窗）。
    """
    conn.execute("BEGIN IMMEDIATE")
    adjudicated = {
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT segment_key FROM edge_findings
            WHERE scanner = ? AND status != 'new'
            """,
            (scanner,),
        )
    }
    landed = 0
    for key, seg in segments.items():
        # 票 32：带 RW 面的扫描器（s1）按校正口径落账；无 RW 面（s2 等）回退
        # 未校正 crossing——多重校正只作用于已挂校正的族，不替别的族放宽
        if not seg.get("crossed_rw", seg["crossed"]) or key in adjudicated:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO edge_findings
                (scanner, segment_key, metric_value, n, se, window, status,
                 found_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                scanner,
                key,
                float(seg["dev"]),
                int(seg["n"]),
                float(seg["se"]),
                window,
                utc_now_iso(),
                json.dumps({**extra_notes, **seg}, ensure_ascii=False),
            ),
        )
        landed += cur.rowcount
    return landed


def mark_finding_status(
    conn: sqlite3.Connection, *, scanner: str, segment_key: str, status: str
) -> int:
    """发现状态人裁（new→promoted=开验证票 / dead；返回受影响行数）。"""
    if status not in ("promoted", "dead"):
        msg = f"status 只接受 promoted|dead: {status}"
        raise ValueError(msg)
    cur = conn.execute(
        "UPDATE edge_findings SET status = ? WHERE scanner = ? AND segment_key = ?",
        (status, scanner, segment_key),
    )
    changed = cur.rowcount
    conn.commit()
    return changed


def latest_observation_at(conn: sqlite3.Connection, scanner: str) -> str | None:
    """
    某扫描器最新留痕时点（晨检面，只认运行面窗）。

    decade 验证行是一次性手动面——计入会让晨检把死掉的日频面读成
    新鲜（correctness F1），排除之。
    """
    row = conn.execute(
        """
        SELECT MAX(created_at) FROM edge_observations
        WHERE scanner = ? AND window_key NOT LIKE 'decade:%'
        """,
        (scanner,),
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


# ---- S1：市场校准日扫 ----


def _outcome_letter(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "H"
    if home_goals < away_goals:
        return "A"
    return "D"


def _settled_jc_rows(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> tuple[list[ObsRow], list[tuple[float, float, float]], dict[str, int]]:
    """
    窗内已结算场 × JC had as-of 开球三向 → 方向观测行 + match 概率三元组 + 覆盖账。

    已结算=draw_results 非 void（settled_jingcai_fixtures 同底盘）；JC 价
    =latest_odds_evidence_asof（有效观测时刻 ≤ 开球，重放确定性）。
    match 三元组（票 32）=RW stepdown 的重抽样单元（同场 h/d/a 互斥相依
    结构的载体）。
    """
    coverage = {"n_settled": 0, "n_with_jc": 0, "n_incomplete_triple": 0}
    rows: list[ObsRow] = []
    match_probs: list[tuple[float, float, float]] = []
    for fixture in rs_store.settled_jingcai_fixtures(conn):
        business_date = str(fixture["business_date"])
        if not date_from <= business_date <= date_to:
            continue
        coverage["n_settled"] += 1
        kickoff = str(fixture["kickoff_utc"])
        evidence = fx_store.latest_odds_evidence_asof(
            conn, fixture["fixture_id"], "had", kickoff
        )
        if not evidence:
            continue
        prices = [evidence.get(sel) for sel in DIRECTIONS]
        if any(price is None or price["odds"] is None for price in prices):
            coverage["n_incomplete_triple"] += 1
            continue
        coverage["n_with_jc"] += 1
        triple = tuple(float(price["odds"]) for price in prices if price is not None)
        probs = om.normalized_implied(triple)
        match_probs.append((probs[0], probs[1], probs[2]))
        letter = _outcome_letter(int(fixture["home_goals"]), int(fixture["away_goals"]))
        for position, direction in enumerate(DIRECTIONS):
            rows.append(
                ObsRow(
                    direction=direction,
                    prob=probs[position],
                    hit=letter == _LETTER_BY_DIRECTION[direction],
                )
            )
    return rows, match_probs, coverage


def run_s1(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """S1 市场校准滚动窗扫描（发现+观测留痕；同窗重跑幂等）。"""
    window_end = _validated_business_date(ctx.business_date)
    window_from = (
        date.fromisoformat(window_end) - timedelta(days=S1_WINDOW_DAYS - 1)
    ).isoformat()
    window = f"{window_from}..{window_end}"
    # 读相一个 deferred 读事务（price_edge 同款）：场列表+逐场 odds 不被
    # 并发采集拍涂改；读毕回滚再进写相（correctness F3）
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        rows, match_probs, coverage = _settled_jc_rows(
            conn, date_from=window_from, date_to=window_end
        )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    segments = calibration_segments(rows)
    # 票 32：RW stepdown 挂日扫（21 段族 FWER 校正；seed=窗键 → 同窗重跑
    # 逐位复现）；每段并入 p_raw/p_rw/crossed_rw，落账口径切到校正门
    rw = romano_wolf_stepdown(match_probs, segments, seed=f"rw-s1:{window}")
    for key, seg in segments.items():
        seg.update(rw[key])
    landed = insert_findings(
        conn,
        scanner="s1",
        window=window,
        segments=segments,
        extra_notes={"face": "runtime", "window_days": S1_WINDOW_DAYS},
    )
    # vision-build 票 02：S1→H1/H8 假设挂钩——本窗各活假设记一尝试
    # （同窗幂等；H1 完整判据的双源扩展面未点亮，当前面=JC 单源校准，
    # 关联留痕先行=票面示范挂钩）
    hyp_landed = hypothesis_registry.link_scanner_run(
        conn, scanner="s1", window_key=window_end, note=window
    )
    _record_observation(
        conn,
        "s1",
        window_end,
        {
            "face": "runtime",
            "window": window,
            "window_days": S1_WINDOW_DAYS,
            "gate": {
                "min_n": S1_MIN_N,
                "gate_se": S1_GATE_SE,
                "rw_alpha": S1_RW_ALPHA,
            },
            "rw": {
                "method": "romano-wolf-stepdown/1",
                "alpha": S1_RW_ALPHA,
                "replicates": S1_RW_REPLICATES,
                "family_size": len(_S1_FAMILY_KEYS),
            },
            "coverage": coverage,
            "segments": segments,
            "hypotheses": {
                "linked": sorted(hypothesis_registry.SCANNER_LINKS.get("s1", ())),
                "attempts_landed": {k: v for k, v in hyp_landed.items() if v},
            },
        },
    )
    conn.commit()
    return {
        "scanner": "s1",
        "window": window,
        "n_rows": len(rows),
        "coverage": coverage,
        "n_segments": len(segments),
        # 双口径并列：未校正（对照读数）与 RW 后（落账门）互不吞
        "crossed": sorted(k for k, v in segments.items() if v["crossed"]),
        "crossed_rw": sorted(k for k, v in segments.items() if v["crossed_rw"]),
        "findings_landed": landed,
        "hypotheses": hyp_landed,
    }


def run_s1_decade(
    conn: sqlite3.Connection, *, research_conn: sqlite3.Connection
) -> dict[str, Any]:
    """
    S1 十年回放（口径验证面）：研究库 backtest_predictions fair_probs。

    与手工版（research/60 十年市场自校准表）同分桶同判定；只落观测不落
    发现——该面结论已人裁归档，发现台账不重复收录历史已知偏差。
    """
    run = research_conn.execute(
        """
        SELECT r.id, r.label FROM backtest_runs r
        WHERE r.status = 'done' AND EXISTS (
            SELECT 1 FROM backtest_predictions p
            WHERE p.run_id = r.id AND p.ftr IN ('H', 'D', 'A')
        )
        ORDER BY r.id DESC LIMIT 1
        """
    ).fetchone()
    if run is None:
        msg = "研究库无可判读回放 run（backtest_predictions 无已结算行）"
        raise FileNotFoundError(msg)
    rows: list[ObsRow] = []
    for probs_json, ftr in research_conn.execute(
        """
        SELECT fair_probs, ftr FROM backtest_predictions
        WHERE run_id = ? AND ftr IN ('H', 'D', 'A')
        """,
        (run["id"],),
    ):
        probs = json.loads(str(probs_json))
        for direction in DIRECTIONS:
            rows.append(
                ObsRow(
                    direction=direction,
                    prob=float(probs[direction]),
                    hit=str(ftr) == _LETTER_BY_DIRECTION[direction],
                )
            )
    segments = calibration_segments(rows)
    window_key = f"decade:{run['id']}"
    _record_observation(
        conn,
        "s1",
        window_key,
        {
            "face": "decade",
            "run_id": int(run["id"]),
            "run_label": str(run["label"]),
            "gate": {"min_n": S1_MIN_N, "gate_se": S1_GATE_SE},
            "segments": segments,
        },
    )
    conn.commit()
    return {
        "scanner": "s1-decade",
        "run_id": int(run["id"]),
        "run_label": str(run["label"]),
        "n_rows": len(rows),
        "segments": segments,
    }


# ---- S2：模型校准/EV 实现扫描（票 02） ----


def _calib_bin_segments(
    bins: tuple[accuracy.CalibBin, ...], *, market: str
) -> dict[str, dict[str, Any]]:
    """面 A 段落：一个玩法的置信 10 箱 → 台账段（二项门与 S1 共口径）。"""
    return {
        f"{market}:b{b.bin_index}": _binomial_gate(
            b.mean_conf, b.hit_rate, b.n, min_n=S2_MIN_N
        )
        for b in bins
    }


def _ev_realization_segments(
    rows: list[sqlite3.Row],
) -> dict[str, dict[str, Any]]:
    """
    面 B 段落：纸面注 EV 实现差（分来源=策略版本；无版本行=manual）。

    逐注配对差 = 实现单位损益(profit/stake) − 建注模型 EV 快照；段 SE=
    配对差标准误（pstdev/√n，accuracy._se 同款）——EV 与实现都随注波动，
    二项 SE 不适用。零方差段（同质注批 diff 恒定）SE 不可估 → 不越门：
    恒定差只证批内无分辨，不构成台账级证据（Spec 审查裁定）。
    """
    triples_by_source: dict[str, list[tuple[float, float, float]]] = {}
    for row in rows:
        stake = float(row["stake"])
        if stake <= 0:
            continue  # CHECK(stake>=0) 的零注无单位损益，非实现观测
        realized = float(row["profit"]) / stake
        expected = float(row["snap_ev_model"])
        source = str(row["strategy_version"] or "manual")
        triples_by_source.setdefault(source, []).append(
            (expected, realized, realized - expected)
        )
    segments: dict[str, dict[str, Any]] = {}
    for source, triples in sorted(triples_by_source.items()):
        n = len(triples)
        dev = fmean(t[2] for t in triples)
        sd = pstdev(t[2] for t in triples)
        se = sd / math.sqrt(n)
        segments[f"ev:{source}"] = {
            "n": n,
            "mean_ev": round(fmean(t[0] for t in triples), 6),
            "mean_realized": round(fmean(t[1] for t in triples), 6),
            "dev": round(dev, 6),
            "se": round(se, 6),
            # sd=0（恒定 diff）SE 不可估不越门：dev≠0 也不构成证据
            "crossed": sd > 0 and n >= S2_MIN_N and abs(dev) > S1_GATE_SE * se,
        }
    return segments


def run_s2(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """
    S2 模型校准/EV 实现扫描（票 02）：评分层报告面 → 台账化输出（不重算）。

    面 A（校准）：forecast_scores（acc-v1）逐玩法最热向置信 10 箱 vs 实际
    命中，accuracy 报告同款派生（payload 即时算，ADR-0006）；窗=kickoff
    UTC 日期。面 B（EV 实现）：已结算纸面注逐注配对差（betting 读面）；
    窗=settled 日期。两窗同 90 日滚动；越门（n≥200 且 |dev|>2SE）落
    发现，其余留观测。同窗重跑幂等；纸面注未起量=面 B 如实空面。
    """
    window_end = _validated_business_date(ctx.business_date)
    window_from = (
        date.fromisoformat(window_end) - timedelta(days=S1_WINDOW_DAYS - 1)
    ).isoformat()
    window = f"{window_from}..{window_end}"
    # 读相一个 deferred 读事务（s1 同款）：评分行/纸面注不被并发拍涂改
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        calib_bins = accuracy.calibration_bins_by_market(
            conn, date_from=window_from, date_to=window_end
        )
        ll_faces = accuracy.log_loss_by_market(
            conn, date_from=window_from, date_to=window_end
        )
        ev_rows = bet_store.paper_ev_realization_rows(
            conn, settled_from=window_from, settled_to=window_end
        )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    calib_segments: dict[str, dict[str, Any]] = {}
    for market, bins in sorted(calib_bins.items()):
        calib_segments.update(_calib_bin_segments(bins, market=market))
    ev_segments = _ev_realization_segments(ev_rows)
    # 面 C（票 32）log loss：观测面，不进发现门（判读分层 S2_LOSS_READING_RULE）
    ll_payload = {market: asdict(face) for market, face in sorted(ll_faces.items())}
    # coverage 与段分母同源（零注额行不进段也不进 coverage——两数可对账）
    calib_binned = sum(s["n"] for s in calib_segments.values())
    ev_used = sum(s["n"] for s in ev_segments.values())
    segments = {**calib_segments, **ev_segments}
    landed = insert_findings(
        conn,
        scanner="s2",
        window=window,
        segments=segments,
        extra_notes={
            "window_days": S1_WINDOW_DAYS,
            "faces": "calibration+ev_realization+log_loss(observation)",
        },
    )
    _record_observation(
        conn,
        "s2",
        window_end,
        {
            "window": window,
            "window_days": S1_WINDOW_DAYS,
            "gate": {"min_n": S2_MIN_N, "gate_se": S1_GATE_SE},
            "coverage": {"calib_binned": calib_binned, "ev_bets": ev_used},
            "calibration": calib_segments,
            "ev": ev_segments,
            "log_loss": ll_payload,
        },
    )
    conn.commit()
    return {
        "scanner": "s2",
        "window": window,
        "calib_binned": calib_binned,
        "ev_bets": ev_used,
        "n_segments": len(segments),
        "crossed": sorted(k for k, v in segments.items() if v["crossed"]),
        "findings_landed": landed,
    }


# ---- S4：陈盘观测日（票 04） ----


def _triple_groups(
    rows: list[sqlite3.Row],
) -> dict[str, tuple[tuple[float, float, float], str]]:
    """
    JC had 全时线 → {captured_at: (三向赔率, 组内最晚有效观测时刻)}。

    仅保留 h/d/a 齐全的调盘组（残组不可比价）。
    """
    odds_by_move: dict[str, dict[str, float]] = {}
    seen_by_move: dict[str, str] = {}
    for row in rows:
        move = str(row["captured_at"])
        seen = str(row["observed_at"] or row["captured_at"])
        if row["selection_code"] in DIRECTIONS and row["odds"] is not None:
            odds_by_move.setdefault(move, {})[str(row["selection_code"])] = float(
                row["odds"]
            )
        seen_by_move[move] = max(seen_by_move.get(move, ""), seen)
    return {
        move: ((g["h"], g["d"], g["a"]), seen_by_move[move])
        for move, g in odds_by_move.items()
        if set(g) >= set(DIRECTIONS)
    }


def run_s4(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """
    S4 陈盘观测日：决策时点（开球−60min）信号计数留痕（无信号日记零）。

    confirmed=陈盘（≥15min 未调）且 sharp 漂移≥1.5pp；fired=决策后到
    开球前 JC 再调盘（分母=confirmed）；命中=再调方向与漂移同号。

    票 19：sharp 端点读法=corpus as-of（asof_resolver 在场时双端历史
    时点青铜重放，口径注记 sharp_source=corpus_asof）；无信号日如实记
    零的纪律不因数据源升级而变（B1 已关闭可交易性→纯监测不产候选，
    E-09 观测≠下注）。
    """
    counts = {
        "n_fixtures": 0,
        "n_observed": 0,
        "n_stale": 0,
        "n_confirmed": 0,
        "n_fired": 0,
        "n_hit": 0,
    }
    business_date = _validated_business_date(ctx.business_date)
    # 读相一个 deferred 读事务：逐场时线+sharp 锚不被并发采集拍涂改
    # （correctness F3）；循环零写，读毕回滚再落观测
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        _s4_read_phase(conn, ctx, counts)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    _record_observation(
        conn,
        "s4",
        business_date,
        {
            "business_date": business_date,
            **counts,
            "hit_rate": (
                round(counts["n_hit"] / counts["n_fired"], 6)
                if counts["n_fired"]
                else None
            ),
            "thresholds": {
                "decision_lead_minutes": S4_DECISION_LEAD_MINUTES,
                "stale_minutes": S4_STALE_MINUTES,
                "drift_threshold": S4_DRIFT_THRESHOLD,
            },
            # 口径注记（票 19 验收）：sharp 端点数据来源——corpus_asof=
            # 青铜重放注入缝在场；corpus=只走 silver 最新视图（历史时点
            # 会缺）；legacy=无 corpus 桥。计数读法随注记解读。
            "sharp_source": (
                "corpus_asof"
                if ctx.asof_resolver is not None
                else ("corpus" if ctx.duck_con is not None else "legacy")
            ),
        },
    )
    conn.commit()
    return {"scanner": "s4", **counts}


def _s4_read_phase(
    conn: sqlite3.Connection,
    ctx: ScanContext,
    counts: dict[str, int],
) -> None:
    """S4 逐场读相（调用方负责读事务壳；只递增 counts 不写库）。"""
    for fixture in fx_store.fixtures_for_business_date(conn, ctx.business_date):
        counts["n_fixtures"] += 1
        kickoff = str(fixture["kickoff_utc"])
        decision = (
            datetime.fromisoformat(kickoff)
            - timedelta(minutes=S4_DECISION_LEAD_MINUTES)
        ).isoformat()
        history = fx_store.odds_history(conn, fixture["id"], "had", source="sporttery")
        # as-of 开球准入（重放确定性）：开球后补录行不进"决策后触发"判定
        admitted = [
            row
            for row in history
            if str(row["observed_at"] or row["captured_at"]) <= kickoff
        ]
        groups = _triple_groups(admitted)
        visible = {m: v for m, v in groups.items() if v[1] <= decision}
        if not visible:
            continue  # had_quote 闸：决策时点无可见完整三向不可观测
        counts["n_observed"] += 1
        # ponytail: 重放逐 (sid, as_of) 调用（定格端 as_of 在 stale_line_signal
        # 内部才知，无法预取批扫）；夜扫 JC 板量级（数十 sid）在 srct_asof
        # shortcut 声明的"检查点够用"面内。板量级上百后换 batch_asof_resolver
        # 预取（票 05 先例：需把定格端 JC 查询上提预收集）。
        signal = qe.stale_line_signal(
            conn,
            fixture["id"],
            as_of=decision,
            duck_con=ctx.duck_con,
            sid=ctx.resolve_sid(fixture["id"]) if ctx.resolve_sid else None,
            asof_resolver=ctx.asof_resolver,
        )
        if signal is None:
            continue
        stale = signal.minutes_since_move >= S4_STALE_MINUTES
        if stale:
            counts["n_stale"] += 1
        confirmed = (
            stale
            and signal.drift is not None
            and abs(signal.drift) >= S4_DRIFT_THRESHOLD
            and signal.drift_selection is not None
        )
        if not confirmed or signal.drift is None or signal.drift_selection is None:
            continue
        counts["n_confirmed"] += 1
        final_move = max(groups)
        if final_move <= decision:
            continue  # 决策后再无调盘：信号未触发
        counts["n_fired"] += 1
        p_decision = om.normalized_implied(visible[max(visible)][0])
        p_final = om.normalized_implied(groups[final_move][0])
        idx = _SELECTION_INDEX[signal.drift_selection]
        moved = p_final[idx] - p_decision[idx]
        if moved != 0 and (moved > 0) == (signal.drift > 0):
            counts["n_hit"] += 1


# ---- S5：池期次滚存回看（票 04） ----


def _summarize_tiers(prize_tiers: str | None) -> dict[str, dict[str, Any]]:
    """官方 prize_tiers JSON → 玩法级结构摘要（pool_after/档位数/头奖）。"""
    games: dict[str, dict[str, Any]] = {}
    if not prize_tiers:
        return games
    try:
        raw: dict[str, Any] = json.loads(prize_tiers)
    except ValueError:
        return games  # 坏档如实空摘要（payload 无 games 不冒充）
    for game, face in raw.items():
        if not isinstance(face, dict):
            continue
        # isinstance 把 Any 收窄成 dict[Unknown, Unknown]——cast 回显式键型
        game_face = cast("dict[str, Any]", face)
        tiers: list[dict[str, Any]] = list(game_face.get("tiers") or [])
        top = max(
            (t for t in tiers if t.get("prize_amount") is not None),
            key=lambda t: float(t["prize_amount"]),
            default=None,
        )
        games[str(game)] = {
            "pool_after": game_face.get("pool_after"),
            "n_tiers": len(tiers),
            "top_prize": float(top["prize_amount"]) if top else None,
            "top_stake_count": float(top["stake_count"]) if top else None,
        }
    return games


def run_s5(conn: sqlite3.Connection, ctx: ScanContext) -> dict[str, Any]:
    """
    S5 滚存回看：已发布奖池结构期次落观测留痕（按缺席自补+近窗保鲜）。

    首跑=存量全量历史回放；此后每晚只补未观测期次 + 刷新近 7 日发布
    （官方 state 可后补更正）。滚存期 EV 结构标注=rollover_in 携带额与
    销量比（boost_ratio），池结构证据在 payload 不另算 EV（H7 已裁决）。
    """
    business_date = _validated_business_date(ctx.business_date)
    refresh_from = (
        date.fromisoformat(business_date) - timedelta(days=S5_REFRESH_DAYS)
    ).isoformat()
    # 读相一个 deferred 读事务：已观测集+期次面同快照（correctness F3）；
    # 计算纯函数化进 pending，读毕回滚再落观测
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        observed = {
            str(r[0])
            for r in conn.execute(
                "SELECT window_key FROM edge_observations WHERE scanner = 's5'"
            )
        }
        pending: list[tuple[str, dict[str, Any], bool, bool]] = []
        for row in pool_store.settled_state_rows(conn):
            market = str(row["market_code"])
            period_no = str(row["period_no"])
            key = f"{market}:{period_no}"
            published_day = str(row["published_at"] or "")[:10]
            # 已观测且明确非近窗才冻结；发布日未知（NULL published_at）
            # 恒保鲜——官方 state 可后补/更正，未知不得冒充古老（F2）
            if key in observed and published_day and published_day < refresh_from:
                continue
            games = _summarize_tiers(row["prize_tiers"])
            sales = row["sales_amount"]
            carry = float(row["rollover_in"] or 0.0)
            rolled_out = any(
                float(g.get("pool_after") or 0) > 0 for g in games.values()
            )
            pending.append(
                (
                    key,
                    {
                        "market": market,
                        "period_no": period_no,
                        "sales_deadline": row["sales_deadline"],
                        "published_at": row["published_at"],
                        "sales_amount": sales,
                        "rollover_in": row["rollover_in"],
                        "boost_ratio": (
                            round(carry / float(sales), 4) if sales else None
                        ),
                        "rollover_out": rolled_out,
                        "games": games,
                    },
                    carry > 0,
                    rolled_out,
                )
            )
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    scanned = rollover_in = rollover_out = 0
    for key, payload, carries, rolled_out in pending:
        _record_observation(conn, "s5", key, payload)
        scanned += 1
        rollover_in += carries
        rollover_out += rolled_out
    conn.commit()
    return {
        "scanner": "s5",
        "periods_scanned": scanned,
        "rollover_in_periods": rollover_in,
        "rollover_out_periods": rollover_out,
    }


# ---- 注册表 + 日扫编排（wrap 挂步/CLI 共用） ----

_S1_SPEC = ScannerSpec(
    name="s1",
    cadence="daily",
    description="市场校准滚动窗（90 日，价格带×方向，二项 SE 越门）",
    run=run_s1,
)
_S2_SPEC = ScannerSpec(
    name="s2",
    cadence="daily",
    description="模型校准/EV 实现滚动窗（分玩法×置信箱 + 纸面注配对差）",
    run=run_s2,
)
_S4_SPEC = ScannerSpec(
    name="s4",
    cadence="daily",
    description="陈盘观测日（决策时点 confirmed/fired/命中计数留痕）",
    run=run_s4,
)
_S5_SPEC = ScannerSpec(
    name="s5",
    cadence="daily",
    description="池期次滚存回看（奖池结构/滚存标注，缺席自补）",
    run=run_s5,
)

SCANNERS: dict[str, ScannerSpec] = {
    spec.name: spec for spec in (_S1_SPEC, _S2_SPEC, _S4_SPEC, _S5_SPEC)
}
#: 日扫定拍序（wrap 23:30 收尾后；S3 点亮后追加）
ACTIVE_SCANNERS: tuple[str, ...] = ("s1", "s2", "s4", "s5")


def yesterday_business_date(now: datetime | None = None) -> str:
    """扫描日=北京业务日昨日（wrap 夜跑时昨日场全部已结算）。"""
    today = fx_store.beijing_business_date(now)
    return (date.fromisoformat(today) - timedelta(days=1)).isoformat()


def _validated_business_date(business_date: str) -> str:
    """
    观察日防御（correctness F5）。

    未来日不可有已结算面，笔误远未来窗会以空 payload 顶掉"最新窗"位
    （_latest_s1_face 按 window_key 排序，周报主位被空窗霸占 14 个月）。
    上限=北京今日（当日凌晨场可日间补扫）。
    """
    day = date.fromisoformat(business_date)  # 坏串 ValueError fail-loud
    if day > date.fromisoformat(fx_store.beijing_business_date()):
        msg = f"观察日 {business_date} 晚于北京今日（未来日无已结算面）"
        raise ValueError(msg)
    return business_date


def edge_scan_day(
    conn: sqlite3.Connection,
    *,
    business_date: str | None = None,
    duck_con: qe.DuckCon | None = None,
    resolve_sid: Callable[[int], str | None] | None = None,
    asof_resolver: Callable[[str, str], list[qe.SnapshotRow] | None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    日扫编排：全部在册扫描器对同一观察日跑一轮（读写同连接）。

    扫描器间独立异常边界（m3/freshness_shadow 先例）：单扫描器炸只降级
    自身（未提交事务随之回滚，半套写入不落库），其余照常留痕——
    "无信号日如实记零"的保证不因部分失败丢失。asof_resolver（票 19）
    只服务 S4 sharp 端点；其余扫描器不取用。
    """
    ctx = ScanContext(
        business_date=business_date or yesterday_business_date(now),
        duck_con=duck_con,
        resolve_sid=resolve_sid,
        asof_resolver=asof_resolver,
    )
    stats: dict[str, Any] = {"business_date": ctx.business_date}
    for name in ACTIVE_SCANNERS:
        try:
            stats[name] = SCANNERS[name].run(conn, ctx)
        except Exception as exc:
            # 炸在写相的扫描器回滚其未提交事务——半套写入不得被后续扫描器
            # 的 commit 顺带落库（correctness F3：发现行无观测行的泄漏账）
            if conn.in_transaction:
                conn.rollback()
            stats[name] = {"error": str(exc), "degraded": True}
    return stats


# ---- 周报 digest（票 04） ----


def _latest_face(conn: sqlite3.Connection, scanner: str) -> dict[str, Any] | None:
    """最新运行面观测（s1 decade 验证面不进周报主位；键=日期序）。"""
    row = conn.execute(
        """
        SELECT payload FROM edge_observations
        WHERE scanner = ? AND window_key NOT LIKE 'decade:%'
        ORDER BY window_key DESC LIMIT 1
        """,
        (scanner,),
    ).fetchone()
    return json.loads(str(row[0])) if row is not None else None


def build_edge_digest(
    conn: sqlite3.Connection,
    *,
    week_end: str,
    store: CorpusStore | None = None,
    duck_con: qe.DuckCon | None = None,
) -> dict[str, Any]:
    """
    周报：各扫描器窗口统计 + 本周新台账行 + 晋升建议（只读，两视图落盘）。

    晋升建议=本周仍是 new 且最新 s1 观测里仍越门的段（人裁开验证票，
    不自动晋升）。窗口过滤按台账 window 末日（s1 约定 "from..end"）。
    全读一个 deferred 读事务（price_edge 同款）：期次面/留痕/台账同
    快照——并发 pool-sync 落新期不让周报永久漏期（correctness F3）。
    duck_con（票 19）：锚书当季入库节奏节（数据积累面）；缺席=节 None。
    """
    week_end = _validated_business_date(week_end)
    week_start = (date.fromisoformat(week_end) - timedelta(days=6)).isoformat()
    own_txn = not conn.in_transaction
    if own_txn:
        conn.execute("BEGIN")
    try:
        report = _assemble_digest(conn, week_start=week_start, week_end=week_end)
    finally:
        if own_txn:
            conn.execute("ROLLBACK")
    # duck 面在读事务外查（corpus 只读桥不参与运行面事务；缺席/降级 None）
    report["srct_rhythm"] = (
        qe.srct_anchor_season_face(
            duck_con, season=silver.season_of(datetime.fromisoformat(week_end))
        )
        if duck_con is not None
        else None
    )
    if store is not None:
        report["run_fingerprint"] = rfp.build_run_fingerprint(conn=conn, store=store)
        _write_two_views(store, report)
    return report


def _assemble_digest(
    conn: sqlite3.Connection, *, week_start: str, week_end: str
) -> dict[str, Any]:
    """周报读相装配（调用方负责读事务壳与落盘）。"""
    latest_s1 = _latest_face(conn, "s1")
    latest_segments: dict[str, Any] = (latest_s1 or {}).get("segments") or {}
    # 票 32：s1「仍越门」按 RW 后口径（旧 payload 无 RW 字段时回退未校正——
    # 部署前留痕的段按当时注册口径读，不误杀也不放宽）
    crossed_now: set[str] = {
        key
        for key, seg in latest_segments.items()
        if seg.get("crossed_rw", seg.get("crossed"))
    }
    latest_s2 = _latest_face(conn, "s2")
    s2_calib: dict[str, Any] = (latest_s2 or {}).get("calibration") or {}
    s2_ev: dict[str, Any] = (latest_s2 or {}).get("ev") or {}
    # 晋升建议的"仍越门"集按扫描器各查各面（s1 段表 / s2 校准+EV 两面）
    crossed_by_scanner: dict[str, set[str]] = {"s1": crossed_now, "s2": set()}
    for face in (s2_calib, s2_ev):
        crossed_by_scanner["s2"].update(
            key for key, seg in face.items() if seg.get("crossed")
        )
    findings_week: list[dict[str, Any]] = [
        {
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
            SELECT scanner, segment_key, metric_value, n, se, window, status
            FROM edge_findings
            WHERE substr(window, -10) BETWEEN ? AND ?
            ORDER BY found_at
            """,
            (week_start, week_end),
        )
    ]
    status_counts = {
        f"{r['scanner']}:{r['status']}": int(r["c"])
        for r in conn.execute(
            """
            SELECT scanner, status, COUNT(*) AS c FROM edge_findings
            GROUP BY scanner, status
            """
        )
    }
    s4_days = [
        {"day": str(r["window_key"]), **json.loads(str(r["payload"]))}
        for r in conn.execute(
            """
            SELECT window_key, payload FROM edge_observations
            WHERE scanner = 's4' AND window_key BETWEEN ? AND ?
            ORDER BY window_key
            """,
            (week_start, week_end),
        )
    ]
    s5_week_keys = [
        f"{r['market_code']!s}:{r['period_no']!s}"
        for r in pool_store.settled_state_rows(
            conn, published_from=week_start, published_to=week_end
        )
    ]
    s5_payload_by_key = {
        str(r["window_key"]): json.loads(str(r["payload"]))
        for r in conn.execute(
            "SELECT window_key, payload FROM edge_observations WHERE scanner = 's5'"
        )
    }
    s5_week = [
        s5_payload_by_key[key] for key in s5_week_keys if key in s5_payload_by_key
    ]
    n_states, n_rollover_in = pool_store.pool_state_counts(conn)
    s5_all_time = {"n_states": n_states, "n_rollover_in": n_rollover_in}
    suggestions = sorted(
        {
            f"{f['scanner']}:{f['segment_key']}"
            for f in findings_week
            if f["status"] == "new"
            and f["segment_key"] in crossed_by_scanner.get(f["scanner"], set())
        }
    )
    # vision-build 票 02：注册表节（状态+建议态+尝试计数；人读面）
    hyp_rows = hypothesis_registry.registry_view(conn)
    hyp_status_counts: dict[str, int] = {}
    for hyp in hyp_rows:
        hyp_status_counts[str(hyp["status"])] = (
            hyp_status_counts.get(str(hyp["status"]), 0) + 1
        )
    report: dict[str, Any] = {
        "schema": "edge-scan-digest/1",
        "generated_at": utc_now_iso(),
        "week": {"from": week_start, "to": week_end},
        "s1": {
            "latest_window": latest_s1.get("window") if latest_s1 else None,
            "coverage": latest_s1.get("coverage") if latest_s1 else None,
            "segments_crossed": sorted(crossed_now),
            "segments": latest_s1.get("segments") if latest_s1 else None,
        },
        "s2": {
            "latest_window": latest_s2.get("window") if latest_s2 else None,
            "coverage": latest_s2.get("coverage") if latest_s2 else None,
            "calibration": s2_calib,
            "ev": s2_ev,
            "log_loss": (latest_s2 or {}).get("log_loss") or {},
        },
        "s4": {"days": s4_days},
        "s5": {"week_periods": len(s5_week), "week": s5_week, "all_time": s5_all_time},
        "findings": {
            "week": findings_week,
            "status_counts": status_counts,
            "promotion_suggestions": suggestions,
        },
        "hypothesis_registry": {
            "rows": hyp_rows,
            "status_counts": hyp_status_counts,
            "notes": [
                "终态（supported|refuted）翻转恒人 CLI 人审落账，agent 不自动定生死；"
                + "终态不翻案，重开=改预测=新 h_id。",
                "建议态由程序按数据面在场算（registered+已有评估留痕→running）；"
                + "逐假设统计门求值随扫描器扩展面点亮接入。",
                "attempt 计数=该假设族统计评估总尝试数（同窗幂等），月读 DSR/BH"
                + " 校正按总尝试数消费（票 04）。",
            ],
        },
        "notes": [
            "晋升=人裁开验证票（六步门），不自动建；dead 同理。",
            "S1 越门自票 32 起=RW stepdown 校正口径（21 段族 FWER α=0.05；"
            + "未校正读数保留在段 crossed 字段供对照；只收紧不放宽）。",
            "S2 面 A 置信箱（10×5 玩法）多重比较未校正注记仍在；负 dev=模型高估。",
            "S5 标注不另算 EV——池面 EV 结论已由 H7 裁决归档。",
            "S4 sharp 端点数据来源=corpus as-of（青铜重放，票 19）；纯观测"
            + "不产候选（B1 可交易性已关闭，E-09 观测≠下注）。",
        ],
    }
    return report


def _write_two_views(store: CorpusStore, report: dict[str, Any]) -> None:
    """报告落语料树 reports/（JSON+MD 两视图，temp+rename；同款先例×3）。"""
    reports = store.root / REPORTS_DIR
    reports.mkdir(parents=True, exist_ok=True)
    json_tmp = reports / f"{REPORT_BASENAME}.json.tmp"
    md_tmp = reports / f"{REPORT_BASENAME}.md.tmp"
    json_tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_tmp.write_text(_render_markdown(report), encoding="utf-8")
    json_tmp.replace(reports / f"{REPORT_BASENAME}.json")
    md_tmp.replace(reports / f"{REPORT_BASENAME}.md")


def _rhythm_line(rhythm: dict[str, Any] | None) -> str:
    """周报数据积累面一行（票 19）：锚书当季入库节奏；缺席如实降级。"""
    if rhythm is None:
        return "- 源T 当季锚书入库节奏：duck 桥缺席/降级（积累面断线，如实不冒充）"
    latest = rhythm.get("latest_observed") or "无"
    return (
        f"- 源T 当季锚书入库节奏（{rhythm['season']}，数据积累面）：cid177"
        + f" {rhythm['n_sids']} 场在库 · 最新观测 {latest}"
    )


def _md_cell(text: str) -> str:
    """
    MD 表格单元格转义（vision-build 票 02 I2）：管符劈裂与换行错位。

    冻结措辞是自由文本（含 `|`/换行即劈裂表行）——渲染层转义，JSON 面
    保持原文不转义。
    """
    return text.replace("|", "\\|").replace("\n", " ")


def _render_markdown(report: dict[str, Any]) -> str:
    """MD 视图（周报人读面；数字与 JSON 同源）。"""
    lines: list[str] = []
    w = lines.append
    w("# 探索轨周报（扫描器阵列 digest）")
    w("")
    w(
        f"- 周窗 {report['week']['from']} → {report['week']['to']}"
        + f"；生成 {report['generated_at']}"
    )
    s1 = report["s1"]
    w(
        f"- S1 最新窗 {s1['latest_window']}：越门段 "
        + (", ".join(s1["segments_crossed"]) or "无")
    )
    s2 = report["s2"]
    s2_ev: dict[str, Any] = s2["ev"] or {}
    s2_crossed = sorted(
        {
            key
            for face in (s2["calibration"] or {}, s2_ev)
            for key, seg in face.items()
            if seg.get("crossed")
        }
    )
    w(
        f"- S2 最新窗 {s2['latest_window']}：校准 {len(s2['calibration'] or {})} 段"
        + f" / EV 实现差 {len(s2_ev)} 段"
        + f"：越门 {', '.join(s2_crossed) or '无'}"
    )
    s4_days = report["s4"]["days"]
    totals = {
        k: sum(d.get(k, 0) for d in s4_days)
        for k in ("n_observed", "n_stale", "n_confirmed", "n_fired", "n_hit")
    }
    w(
        f"- S4 观测日 {len(s4_days)} 天：观察 {totals['n_observed']} 场"
        + f" / 陈盘 {totals['n_stale']} / 有分辨 {totals['n_confirmed']}"
        + f" / 触发 {totals['n_fired']} / 命中 {totals['n_hit']}"
    )
    s5 = report["s5"]
    w(
        f"- S5 本周兑付 {s5['week_periods']} 期（存量 {s5['all_time'].get('n_states')}"
        + f" 期，滚存携带 {s5['all_time'].get('n_rollover_in')} 期）"
    )
    w(_rhythm_line(report.get("srct_rhythm")))
    w("")
    w(_render_s1_table(report))
    w(_render_s2_tables(report))
    w(_render_s2_log_loss(report))
    w("## 假设注册表")
    w("")
    hyp = report["hypothesis_registry"]
    w(
        "- 状态分布 "
        + "，".join(f"{k}={v}" for k, v in sorted(hyp["status_counts"].items()))
        + "；建议态由程序算，终态翻转恒人 CLI"
    )
    w("| h_id | 状态 | 建议态 | 尝试 | 措辞（冻结） |")
    w("|---|---|---|---|---|")
    for row in hyp["rows"]:
        w(
            f"| {row['h_id']} | {row['status']} | {row['suggested'] or '—'}"
            + f" | {row['attempt_count']} | {_md_cell(str(row['statement']))} |"
        )
    w("")
    w("## 本周新台账行")
    w("")
    findings = report["findings"]["week"]
    if findings:
        w("| 扫描器 | 段 | dev | n | 窗 | 状态 |")
        w("|---|---|---|---|---|---|")
        for f in findings:
            w(
                f"| {f['scanner']} | {f['segment_key']} | {f['metric_value']}"
                + f" | {f['n']} | {f['window']} | {f['status']} |"
            )
    else:
        w("- 无新发现行")
    w("")
    suggestions = report["findings"]["promotion_suggestions"]
    w(
        "- 晋升建议："
        + (", ".join(suggestions) if suggestions else "无（本周无持续越门 new 段）")
    )
    for note in report["notes"]:
        w(f"- 注：{note}")
    return "\n".join(lines) + "\n"


def _render_s1_table(report: dict[str, Any]) -> str:
    """S1 分桶表 MD（票 32 加 RW p/越门(RW) 两列；旧 payload 无 RW 字段占位 —）。"""
    lines = ["## S1 分桶表（最新窗）", ""]
    lines.append(
        "| 段 | n | mean_prob | actual_freq | dev | se | 越门 | RW p | 越门(RW) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    render_segments: dict[str, Any] = report["s1"]["segments"] or {}
    for key, seg in sorted(render_segments.items()):
        rw_p = seg.get("p_rw", "—")  # 旧 payload 无 RW 字段（票 32 前）
        lines.append(
            f"| {key} | {seg['n']} | {seg['mean_prob']} | {seg['actual_freq']}"
            + f" | {seg['dev']} | {seg['se']} | {'✓' if seg['crossed'] else ''}"
            + f" | {rw_p} | {'✓' if seg.get('crossed_rw') else ''} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_s2_tables(report: dict[str, Any]) -> str:
    """S2 模型分箱表 + EV 实现差表 MD。"""
    s2 = report["s2"]
    lines = ["## S2 模型分箱表（最新窗）", ""]
    lines.append("| 段 | n | 期望 | 实测 | dev | se | 越门 |")
    lines.append("|---|---|---|---|---|---|---|")
    render_calib: dict[str, Any] = s2["calibration"] or {}
    for key, seg in sorted(render_calib.items()):
        lines.append(
            f"| {key} | {seg['n']} | {seg['mean_prob']} | {seg['actual_freq']}"
            + f" | {seg['dev']} | {seg['se']} | {'✓' if seg['crossed'] else ''} |"
        )
    render_ev: dict[str, Any] = s2["ev"] or {}
    for key, seg in sorted(render_ev.items()):
        lines.append(
            f"| {key} | {seg['n']} | {seg.get('mean_ev')}"
            + f" | {seg.get('mean_realized')}"
            + f" | {seg['dev']} | {seg['se']} | {'✓' if seg['crossed'] else ''} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_s2_log_loss(report: dict[str, Any]) -> str:
    """S2 log loss 第三指标表 + 冻结判读分层（票 32；Wheatcroft 2021）。"""
    lines = ["## S2 log loss 第三指标（最新窗；票 32，观测面不进发现门）", ""]
    render_ll: dict[str, Any] = report["s2"].get("log_loss") or {}
    if render_ll:
        lines.append("| 玩法 | n | 类数 k | mean LL | ln(k) 锚 | skill=1−LL/ln(k) |")
        lines.append("|---|---|---|---|---|---|")
        for key, face in sorted(render_ll.items()):
            lines.append(
                f"| {key} | {face['n']} | {face['n_classes']}"
                + f" | {face['mean_log_loss']} | {face['uniform_anchor']}"
                + f" | {face['skill_vs_uniform']} |"
            )
    else:
        lines.append("- 无评分行（未起量期如实空面）")
    lines.append("")
    lines.append("log loss 判读分层（Wheatcroft 2021，RPS 之争对冲）：")
    lines.append("")
    lines.extend(f"- {line}" for line in S2_LOSS_READING_RULE)
    lines.append("")
    return "\n".join(lines)
