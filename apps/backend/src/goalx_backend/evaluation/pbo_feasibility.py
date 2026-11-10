"""
CSCV-PBO 元指标可行性核验（票 33，账本 §4.2#9 G-10 条件裁定）。

判定冻结 EM 试验账（轮 1，196 条）可否只读复原**逐切片时间序列**——
CSCV-PBO（Bailey et al. 2017；AFML ch.7）需要 M(T×N) 矩阵：N 个试验
（切片）× T 个观测期的逐期收益/指标列。三要素核验：

1. ``slice_identity``——切片身份列（facet/family/slice）齐且非空；
2. ``per_period_timestamps``——每切片 ≥ ``MIN_PERIODS_FOR_CSCV`` 个
   互不相同的观测时间戳（T 的下界）。批发写入指纹（同一 ts 挂几十条
   不同切片）= 落账时间戳冒充观测时间戳的直接证据；
3. ``per_period_returns``——每行 value 为单一有限数值（一期收益列），
   窗口聚合复合串（如 ev/gap "0.7411/0.0467"）不算。

任一要素缺失 → 不可（verdict.feasible=False，missing 列名机器可读），
按 G-10 裁定记待办不动协议；三要素齐 → 可，报告层 CSCV-PBO 才有料。

**畸形行契约（fail-closed 写死，independent 复核 F1）**：ts 或切片身份列
任一行空白/缺失/纯空白（含 CSV 短行 restval=None）= 不可判证据，直接判
不可——空白 ts **不进** distinct 计数（防虚增期数把 T 认证成真），畸形行数
记 ``n_malformed_ts``/``n_malformed_identity`` 证据字段；对应要素
（per_period_timestamps / slice_identity）记缺。

红线：本模块对试验账**只读**（``load_trial_ledger`` 以 r 模式打开，
无任何写路径）——账本/台账 append-only，PBO 只读复原不改写历史行。
"""

from __future__ import annotations

import csv
import math
from dataclasses import asdict, dataclass
from pathlib import Path

# CSCV 把 T 期切为偶数 S 块做组合对称交叉验证（S≥2 ⇒ 理论下界 T≥4）；
# 取 8 作工作地板：低于此 PBO 估计退化为期序噪声的抛硬币，不具报告价值。
MIN_PERIODS_FOR_CSCV = 8
_SLICE_KEY_COLUMNS = ("facet", "family", "slice")


@dataclass(frozen=True)
class FeasibilityVerdict:
    """可/不可结论 + 证据计数（报告与 CLI 直接 JSON 化）。"""

    feasible: bool
    n_entries: int
    n_slices: int
    min_periods_per_slice: int  # 全切片最少的互异 ts 数 = 可用 T 的上界
    max_periods_per_slice: int
    distinct_ts: int
    max_slices_per_ts: int  # 批发写入指纹：同一 ts 挂的切片数峰值
    single_value_ratio: float  # value 可解析为单一有限数值的行占比
    min_periods_required: int
    missing: tuple[str, ...]  # 未过要素名（稳定机器可读）
    n_malformed_ts: int = 0  # ts 空白/缺失/纯空白（含短行 None）行数
    n_malformed_identity: int = 0  # 切片身份列空白/纯空白行数


def _clean(raw: str | None) -> str:
    """None/纯空白 → ""（CSV 短行 restval=None 与空串同判；strip 防空白逃逸）。"""
    return "" if raw is None else raw.strip()


def load_trial_ledger(path: str | Path) -> list[dict[str, str]]:
    """只读装载试验账 CSV（r 模式；本模块不存在任何写路径）。"""
    with Path(path).open("r", encoding="utf-8", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def _is_single_finite_value(raw: str | None) -> bool:
    """一行 value 是否为单一有限数值（一期收益列的形态）。"""
    if raw is None:
        return False
    try:
        return math.isfinite(float(raw))
    except ValueError:
        return False


def assess_pbo_feasibility(rows: list[dict[str, str]]) -> FeasibilityVerdict:
    """三要素核验 → 可/不可。缺列/空账 fail-closed，不抛异常毒化报告。"""
    fieldnames: frozenset[str] = frozenset(rows[0]) if rows else frozenset()

    def has(col: str) -> bool:
        return col in fieldnames

    slices: dict[tuple[str, str, str], set[str]] = {}
    ts_slices: dict[str, set[tuple[str, str, str]]] = {}
    n_malformed_ts = 0
    n_malformed_identity = 0
    for row in rows:
        facet, family, slice_name = (_clean(row.get(col)) for col in _SLICE_KEY_COLUMNS)
        slice_key = (facet, family, slice_name)
        ts = _clean(row.get("ts"))
        if not all(slice_key):
            # 身份列空白行不进切片计数（fail-closed，F1）：计入即虚增证据
            n_malformed_identity += 1
            continue
        slices.setdefault(slice_key, set())
        if ts:
            slices[slice_key].add(ts)
            ts_slices.setdefault(ts, set()).add(slice_key)
        else:
            # 空白/缺失 ts 不进 distinct 计数（防 MIN−2 真 ts 被 2 空 ts
            # 垫成 T=MIN 认证）——记畸形数，timestamps 要素判缺
            n_malformed_ts += 1
    n_slices = len(slices)
    periods = [len(ts_set) for ts_set in slices.values()] or [0]
    min_periods = min(periods)

    # 三要素各自归因：缺哪列、哪列形态不对，就记在哪个要素名下。
    slice_identity_ok = (
        bool(rows)
        and all(has(col) for col in _SLICE_KEY_COLUMNS)
        and n_malformed_identity == 0
        and n_slices > 0
    )
    timestamps_ok = (
        bool(rows)
        and has("ts")
        and slice_identity_ok
        and n_malformed_ts == 0
        and min_periods >= MIN_PERIODS_FOR_CSCV
        and len(ts_slices) >= MIN_PERIODS_FOR_CSCV
    )
    single_ratio = (
        sum(1 for row in rows if _is_single_finite_value(row.get("value"))) / len(rows)
        if rows
        else 0.0
    )
    # per_period_returns 只看 value 形态（单一有限数值）；期数粒度由
    # timestamps 要素独立把关——失败要素各自归因，不串联误报。
    returns_ok = bool(rows) and has("value") and single_ratio == 1.0

    if not rows:
        missing = ["ledger_not_empty"]  # 空账三要素无从谈起，只记此一条
    else:
        missing = [
            name
            for name, ok in (
                ("slice_identity", slice_identity_ok),
                ("per_period_timestamps", timestamps_ok),
                ("per_period_returns", returns_ok),
            )
            if not ok
        ]

    return FeasibilityVerdict(
        feasible=not missing,
        n_entries=len(rows),
        n_slices=n_slices,
        min_periods_per_slice=min_periods,
        max_periods_per_slice=max(periods),
        distinct_ts=len(ts_slices),
        max_slices_per_ts=max((len(v) for v in ts_slices.values()), default=0),
        single_value_ratio=single_ratio,
        min_periods_required=MIN_PERIODS_FOR_CSCV,
        missing=tuple(missing),
        n_malformed_ts=n_malformed_ts,
        n_malformed_identity=n_malformed_identity,
    )


def verdict_payload(verdict: FeasibilityVerdict) -> dict[str, object]:
    """CLI/报告用的 JSON 形态（tuple → list）。"""
    payload = asdict(verdict)
    payload["missing"] = list(verdict.missing)
    return payload
