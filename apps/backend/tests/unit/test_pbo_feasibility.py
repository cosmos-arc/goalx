"""PBO/CSCV 可行性核验测试（票 33，账本 §4.2#9 G-10 条件裁定）。

冻结试验账（轮 1）只读复原判定：可/不可 + 机器可读证据；两条分支
（可/不可）都在合成台账上验证。红线：分析全程只读，不得写试验账。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import stat
from pathlib import Path

from goalx_backend.cli import _cmd_pbo_feasibility
from goalx_backend.evaluation.pbo_feasibility import (
    MIN_PERIODS_FOR_CSCV,
    assess_pbo_feasibility,
    load_trial_ledger,
)

_HEADER = ("entry_id", "ts", "ticket", "facet", "family", "slice", "window", "value")


def _write_ledger(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _per_period_rows(n_slices: int = 3, n_periods: int = 10) -> list[dict[str, str]]:
    """可复原形态：每切片 n_periods 个观测期（逐期时间戳+单一数值收益）。"""
    return [
        {
            "entry_id": f"F1-s{i}-t{t}",
            "ts": f"2026-01-{t + 1:02d}T00:00:00",
            "ticket": "99",
            "facet": "A",
            "family": "F1",
            "slice": f"s{i}",
            "window": "search",
            "value": f"{0.01 * (i + t) - 0.02:.4f}",
        }
        for i in range(n_slices)
        for t in range(n_periods)
    ]


def _frozen_shape_rows() -> list[dict[str, str]]:
    """轮 1 冻结档形态：批发写入时间戳（一次跑数几十条共用同一 ts）、
    每切片每窗恰好一行、value=窗口聚合复合串（如 ev/gap "0.7411/0.0467"）。"""
    rows: list[dict[str, str]] = []
    bulk_ts = ("2026-11-08T15:58:41", "2026-11-08T16:01:44")
    for i in range(3):
        for window, ts in zip(("search", "validation"), bulk_ts, strict=False):
            rows.append(
                {
                    "entry_id": f"F1-s{i}-{window}",
                    "ts": ts,
                    "ticket": "09",
                    "facet": "A",
                    "family": "F1",
                    "slice": f"s{i}",
                    "window": window,
                    "value": "0.7411/0.0467",
                }
            )
    return rows


def test_per_period_ledger_is_feasible(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path / "ledger.csv", _per_period_rows())
    verdict = assess_pbo_feasibility(load_trial_ledger(ledger))
    assert verdict.feasible is True
    assert verdict.missing == ()
    assert verdict.n_slices == 3
    assert verdict.min_periods_per_slice == 10
    assert verdict.single_value_ratio == 1.0


def test_frozen_shape_ledger_is_infeasible(tmp_path: Path) -> None:
    """轮 1 冻结档：切片身份在、但时间戳=批发写入（每切片 ≤2 期）、
    value=窗口聚合复合串——逐切片时间序列不可复原。"""
    ledger = _write_ledger(tmp_path / "ledger.csv", _frozen_shape_rows())
    verdict = assess_pbo_feasibility(load_trial_ledger(ledger))
    assert verdict.feasible is False
    assert "slice_identity" not in verdict.missing
    assert "per_period_timestamps" in verdict.missing
    assert "per_period_returns" in verdict.missing
    assert verdict.min_periods_per_slice == 2  # search/validation 两窗
    assert verdict.max_slices_per_ts == 3  # 批发写入指纹：同一 ts 覆盖全切片
    assert verdict.single_value_ratio == 0.0  # 复合串无一行可解析为单值


def test_boundary_periods_exactly_min(tmp_path: Path) -> None:
    at_min = _write_ledger(
        tmp_path / "min.csv", _per_period_rows(n_periods=MIN_PERIODS_FOR_CSCV)
    )
    assert assess_pbo_feasibility(load_trial_ledger(at_min)).feasible is True
    below = _write_ledger(
        tmp_path / "below.csv", _per_period_rows(n_periods=MIN_PERIODS_FOR_CSCV - 1)
    )
    verdict = assess_pbo_feasibility(load_trial_ledger(below))
    assert verdict.feasible is False
    assert verdict.missing == ("per_period_timestamps",)


def test_missing_identity_column_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "entry_id,ts,window,value\nx,2026-01-01,search,0.1\n", encoding="utf-8"
    )
    verdict = assess_pbo_feasibility(load_trial_ledger(path))
    assert verdict.feasible is False
    assert "slice_identity" in verdict.missing


def test_empty_ledger_fails_closed(tmp_path: Path) -> None:
    ledger = _write_ledger(tmp_path / "empty.csv", [])
    verdict = assess_pbo_feasibility(load_trial_ledger(ledger))
    assert verdict.feasible is False
    assert "ledger_not_empty" in verdict.missing


def test_analysis_is_read_only(tmp_path: Path) -> None:
    """PBO 只读红线：对只读权限（0444）的台账照常完成分析，字节与
    mtime 均不变——分析路径结构上不可能写试验账。"""
    ledger = _write_ledger(tmp_path / "frozen.csv", _frozen_shape_rows())
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    ledger.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0444
    verdict = assess_pbo_feasibility(load_trial_ledger(ledger))
    assert verdict.feasible is False  # 只读权限下照常出结论
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before


def test_cli_prints_verdict_json(tmp_path: Path, capsys) -> None:
    ledger = _write_ledger(tmp_path / "ledger.csv", _frozen_shape_rows())
    _cmd_pbo_feasibility(argparse.Namespace(ledger=str(ledger)))
    payload = json.loads(capsys.readouterr().out)
    assert payload["feasible"] is False
    assert "per_period_timestamps" in payload["missing"]
    assert payload["n_entries"] == 6


def test_missing_column_attributed_to_own_element(tmp_path: Path) -> None:
    """correctness 回归（探针 1）：缺列归因到所属要素——缺 ts 列记
    timestamps、缺 value 列记 returns，不串记到 slice_identity。"""
    no_value = tmp_path / "no_value.csv"
    no_value.write_text(
        "entry_id,ts,facet,family,slice,window\nx,2026-01-01,A,F1,s1,search\n",
        encoding="utf-8",
    )
    verdict = assess_pbo_feasibility(load_trial_ledger(no_value))
    assert "slice_identity" not in verdict.missing
    assert "per_period_returns" in verdict.missing
    no_ts = tmp_path / "no_ts.csv"
    no_ts.write_text(
        "entry_id,facet,family,slice,window,value\nx,A,F1,s1,search,0.1\n",
        encoding="utf-8",
    )
    verdict = assess_pbo_feasibility(load_trial_ledger(no_ts))
    assert "slice_identity" not in verdict.missing
    assert "per_period_timestamps" in verdict.missing
