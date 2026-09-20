"""
M3 评测协议（票 13，票 05 冻结阈值的工程化）。

三轨前瞻评分（复用 forward_validation 同分母/去重/赛前边界）+ 配对
胜率与 DM + Tier A/B 达标状态行 + 情报质量列 + 盲评参考列。

**冻结阈值（票 05 用户裁决，2026-09-19）——代码内不得放宽**：
- Tier A（融合线去留）：≥200 配对场 + ≥6 周窗口 + 配对 RPS 胜率
  ≥52% + LLM 轨 ECE 无劣化（vs ML 同期）；DM p 值如实报告不设门槛。
- Tier B（真钱切换）：整赛季 + ≥500 配对场 + DM p<0.05 + 复核无
  系统性错误 + 盲评定性不负面；缺一不可。

真钱资格三条件只读 ML 轨（票 04 冻结）——本报告 LLM/Fused 永远是
参考列；样本不足 = 未证明，如实展示。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from goalx_backend.data import fixtures as fx_store
from goalx_backend.evaluation.forward_validation import (
    ForwardSample,
    build_forward_samples,
    forward_skill_report,
)
from goalx_backend.evaluation.metrics import diebold_mariano, rps
from goalx_backend.llm.review import blind_review_counts, verdict_counts
from goalx_backend.llm.store import intel_summary_by_fixture
from goalx_backend.modelling.forecast import forecasts_for_track

# --- 冻结阈值（单一来源；运行时不可放宽） ---
TIER_A_MIN_PAIRS = 200
TIER_A_MIN_WEEKS = 6
TIER_A_MIN_WIN_RATE = 0.52
TIER_B_MIN_PAIRS = 500
TIER_B_MAX_DM_P = 0.05
_INSUFFICIENT = "未证明（样本不足）"


def _sample_rps(sample: ForwardSample, probs: dict[str, float]) -> float:
    """一场的 RPS（had 三项 vs 实际 ftr）。"""
    outcome = {"H": 0, "D": 1, "A": 2}[sample.ftr]
    ordered = [probs[s] for s in ("h", "d", "a")]
    return rps(ordered, outcome)


def paired_fused_vs_ml(conn: sqlite3.Connection) -> dict[str, Any]:
    """配对统计：同场双轨（fused vs ml，冻结赛前样本）的 RPS 对照 + DM。"""
    ml_samples = {s.fixture_id: s for s in build_forward_samples(conn, track="ml")[0]}
    fused_samples = {
        s.fixture_id: s for s in build_forward_samples(conn, track="fused")[0]
    }
    common = sorted(set(ml_samples) & set(fused_samples))
    wins = 0
    loss_diffs: list[float] = []
    for fixture_id in common:
        ml_rps = _sample_rps(ml_samples[fixture_id], ml_samples[fixture_id].had_probs)
        fu_rps = _sample_rps(
            fused_samples[fixture_id], fused_samples[fixture_id].had_probs
        )
        if fu_rps < ml_rps:
            wins += 1
        loss_diffs.append(fu_rps - ml_rps)  # L_fused − L_ml
    win_rate = wins / len(common) if common else None
    dm_stat, dm_p = diebold_mariano(loss_diffs) if loss_diffs else (0.0, 1.0)
    weeks = _span_weeks(common, ml_samples, fused_samples)
    return {
        "pairs": len(common),
        "fused_win_rate": win_rate,
        "mean_rps_delta": (sum(loss_diffs) / len(loss_diffs) if loss_diffs else None),
        "dm_stat": dm_stat,
        "dm_p": dm_p,
        "span_weeks": weeks,
    }


def _span_weeks(
    fixture_ids: list[int],
    *samples_by_fixture: dict[int, ForwardSample],
) -> int:
    """配对样本覆盖的周跨度（最早到最晚开球，向下取整周）。"""
    kickoffs = [
        datetime.fromisoformat(str(s.kickoff_utc))
        for fid in fixture_ids
        for mapping in samples_by_fixture
        if (s := mapping.get(fid)) is not None
    ]
    if not kickoffs:
        return 0
    span_seconds = (max(kickoffs) - min(kickoffs)).total_seconds()
    return int(span_seconds // (7 * 24 * 3600))


def _tier_a(paired: dict[str, Any], ece_clean: bool | None) -> dict[str, Any]:
    """Tier A 达标状态行（融合线去留；阈值冻结）。"""
    checks = {
        "pairs_ge_200": paired["pairs"] >= TIER_A_MIN_PAIRS,
        "weeks_ge_6": paired["span_weeks"] >= TIER_A_MIN_WEEKS,
        "win_rate_ge_52": (paired["fused_win_rate"] or 0) >= TIER_A_MIN_WIN_RATE,
        "ece_no_degradation": ece_clean is True,
    }
    sufficient = checks["pairs_ge_200"] and checks["weeks_ge_6"]
    passed = sufficient and all(checks.values())
    if not sufficient:
        verdict = _INSUFFICIENT
    elif passed:
        verdict = "达标（融合线保留）"
    else:
        verdict = "未达标（继续观察或证伪路径）"
    return {"checks": checks, "verdict": verdict, "dm_p_reported": paired["dm_p"]}


def _tier_b(paired: dict[str, Any], review_clean: bool | None) -> dict[str, Any]:
    """Tier B 达标状态行（真钱切换；缺一不可）。"""
    checks = {
        "full_season": paired["span_weeks"] >= _SEASON_MIN_WEEKS,
        "pairs_ge_500": paired["pairs"] >= TIER_B_MIN_PAIRS,
        "dm_p_lt_005": paired["dm_p"] < TIER_B_MAX_DM_P,
        "review_no_systematic_error": review_clean is True,
    }
    sufficient = checks["pairs_ge_500"]
    verdict = (
        _INSUFFICIENT
        if not sufficient
        else ("达标候选（须用户显式裁决切换）" if all(checks.values()) else "未达标")
    )
    return {"checks": checks, "verdict": verdict}


def _ece_clean(conn: sqlite3.Connection) -> bool | None:
    """LLM 轨 ECE 无劣化：与 ML 同分母比较（样本不足 None=未知）。"""
    ml = forward_skill_report(conn, track="ml")
    llm = forward_skill_report(conn, track="llm")
    ml_n = sum(g["n_fixtures"] for g in ml["groups"].values())
    llm_n = sum(g["n_fixtures"] for g in llm["groups"].values())
    if llm_n < _REVIEW_MIN_SAMPLES * 3 or ml_n < _REVIEW_MIN_SAMPLES * 3:
        return None
    ml_ece = _mean_ece(ml)
    llm_ece = _mean_ece(llm)
    if ml_ece is None or llm_ece is None:
        return None
    return llm_ece <= ml_ece * 1.10  # 10% 容差内的口径噪声


def _mean_ece(report: dict[str, Any]) -> float | None:
    """报告组内 ECE 加权平均（缺指标 None）。"""
    total_n = 0
    acc = 0.0
    for group in report["groups"].values():
        ece = group.get("ece")
        n = group.get("n_fixtures", 0)
        if isinstance(ece, (int, float)) and n:
            acc += float(ece) * n
            total_n += n
    return acc / total_n if total_n else None


def intel_quality(conn: sqlite3.Connection) -> dict[str, Any]:
    """情报质量列：覆盖率/时点新鲜度中位数/来源多样性（报告列不设门槛）。"""
    fixtures_with_forecast = {
        int(r["fixture_id"]) for r in forecasts_for_track(conn, "llm")
    }
    if not fixtures_with_forecast:
        return {
            "covered": 0,
            "coverage": None,
            "median_hours_to_kickoff": None,
            "avg_sources": None,
        }
    summaries = intel_summary_by_fixture(conn, sorted(fixtures_with_forecast))
    covered = 0
    hours: list[float] = []
    sources_per_fixture: list[int] = []
    for fixture_id, entries in summaries.items():
        if not entries:
            continue
        covered += 1
        sources_per_fixture.append(len({src for src, _ in entries}))
        info = fx_store.fixture_team_info(conn, fixture_id)
        if info is None:
            continue
        try:
            ko = datetime.fromisoformat(str(info["kickoff_utc"]))
        except ValueError:
            continue
        for _, collected_at in entries:
            try:
                delta = (ko - datetime.fromisoformat(collected_at)).total_seconds()
            except ValueError:
                continue
            if delta >= 0:
                hours.append(delta / 3600)
    hours.sort()
    median = hours[len(hours) // 2] if hours else None
    return {
        "covered": covered,
        "coverage": covered / len(fixtures_with_forecast),
        "median_hours_to_kickoff": median,
        "avg_sources": (
            sum(sources_per_fixture) / len(sources_per_fixture)
            if sources_per_fixture
            else None
        ),
    }


def review_blind_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """盲评参考列（双周匿名二选一；统计力弱不作证明支柱）。"""
    counts = blind_review_counts(conn)
    total = sum(counts.values())
    return {
        "total": total,
        "counts": counts,
        "llm_share": counts.get("llm", 0) / total if total else None,
    }


_REVIEW_MIN_SAMPLES = 10  # 复核结论低于此数视为未知
_MISLEADING_MAX_SHARE = 0.20  # 误导类占比超过视为系统性错误
_SEASON_MIN_WEEKS = 30  # 整赛季口径（周）


def _review_systematic_error(conn: sqlite3.Connection) -> bool | None:
    """复核无系统性错误：误导类占比（样本 <10 返回 None=未知）。"""
    counts = verdict_counts(conn)
    total = sum(counts.values())
    if total < _REVIEW_MIN_SAMPLES:
        return None
    return counts.get("misleading", 0) / total <= _MISLEADING_MAX_SHARE


def m3_protocol_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """M3 评测协议总报告（三列参考 + 两档达标状态 + 情报/盲评列）。"""
    paired = paired_fused_vs_ml(conn)
    ece_clean = _ece_clean(conn)
    review_clean = _review_systematic_error(conn)
    return {
        "rule": "m3_protocol_v1（票 05 冻结阈值，不得放宽）",
        "tracks": {
            track: forward_skill_report(conn, track=track)
            for track in ("ml", "llm", "fused")
        },
        "paired_fused_vs_ml": paired,
        "tier_a": _tier_a(paired, ece_clean),
        "tier_b": _tier_b(paired, review_clean),
        "llm_ece_clean": ece_clean,
        "review_clean": review_clean,
        "intel_quality": intel_quality(conn),
        "blind_review": review_blind_summary(conn),
        "note": "LLM/Fused 为参考列；真钱资格只读 ML 轨（票 04 冻结）",
        "generated_at": datetime.now(UTC).isoformat(),
    }


def report_summary(report: dict[str, Any]) -> str:
    """报告一行摘要（flow 日志用）。"""
    paired = report["paired_fused_vs_ml"]
    win_rate = paired["fused_win_rate"]
    head = "M3 报告：配对 {pairs} 场 / 胜率 {wr} / DM p={dm:.3f}".format(
        pairs=paired["pairs"],
        wr=f"{win_rate:.1%}" if win_rate is not None else "n/a",
        dm=paired["dm_p"],
    )
    tail = " | ".join(
        (
            "Tier A: " + str(report["tier_a"]["verdict"]),
            "Tier B: " + str(report["tier_b"]["verdict"]),
        )
    )
    return head + " | " + tail
