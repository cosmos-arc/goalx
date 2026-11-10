"""E-06 FD 断点隔离重算（edge-audit-20261109 票 18）。

任务：剔除断点季 2526 重出收盘有效性主表（h/d/a 收盘隐含 vs 实际命中），
并确认早价前视教训段（EV_early>5% 组实得 +8.84%）的证据主体不在 2526。

口径回源（票 18 指令）：.scratch/goalx-quant/issues/54-drift-replay.md 及其
实现 evaluation/drift_replay.py——本脚本直接复用生产纯函数（票 02 已用独立
Shin/独立装载逐位复现坐实该实现），唯一自变量=赛季窗，保证新旧差只来自
剔段本身。

断点语义（票 15 research §二）：FD 断点 2025-07-23 落 2425/2526 休赛期，
隔离=整季剔除。漂移行要求 PSH+PSC 双全：2627 两列全缺本就不入行，故
E-06 的隔离窗=1920–2425（只额外剔 2526）。

数据面（只读）：repo data/goalx.db 的 hist_matches（sqlite mode=ro&immutable；
沙箱不允许 mode=ro 建 shm——immutable 假设由「全窗复算与 2026-09-22 归档
报告逐字节一致」的树漂门自证）。

输出：本目录 18-e06-isolation-output.json + stdout 人读表。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path("/Users/chevy/Desktop/code/goalx")
sys.path.insert(0, str(REPO / "apps" / "backend" / "src"))

from goalx_backend.data.ingest.fdhist import FD_COMPETITIONS, SEASONS  # noqa: E402
from goalx_backend.evaluation.drift_replay import (  # noqa: E402
    collect_drift_rows,
    drift_replay_report,
)

DB = REPO / "data" / "goalx.db"
ARCHIVED = REPO / ".scratch/goalx-quant/research/54-drift-report.json"
OUT = Path(__file__).resolve().parent / "18-e06-isolation-output.json"

# FD 断点季（票 15）：2526 起为断点后；2627 无 PSH/PSC 不入漂移行
BREAK_SEASONS = ("2526", "2627")
CLEAN_SEASONS = tuple(s for s in SEASONS if s not in BREAK_SEASONS)


def ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def deep_diff(a: object, b: object, path: str = "") -> list[str]:
    """逐路径差异清单（树漂门：期望为空）。"""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}/{k}: 仅一侧存在")
            else:
                out.extend(deep_diff(a[k], b[k], f"{path}/{k}"))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: 长度 {len(a)} vs {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(deep_diff(x, y, f"{path}[{i}]"))
        return out
    return [] if a == b else [f"{path}: {a!r} vs {b!r}"]


def selection_table(report: dict) -> dict[str, dict[str, float]]:
    return {
        sel: {
            "n": block["n"],
            "implied_pct": block["mean_p_close"] * 100,
            "actual_pct": block["realized_hit_rate"] * 100,
            "dev_pp": (block["realized_hit_rate"] - block["mean_p_close"]) * 100,
        }
        for sel, block in report["by_selection"].items()
    }


def main() -> None:
    conn = ro_conn()
    try:
        # ---- 树漂门：全窗复算 vs 2026-09-22 归档报告（逐字节一致才算过） ----
        full = drift_replay_report(
            conn, competitions=FD_COMPETITIONS, seasons=SEASONS
        )
        archived = json.loads(ARCHIVED.read_text())
        diffs = deep_diff(full, archived)
        print(f"[树漂门] 全窗复算 vs 归档 54-drift-report.json: "
              f"{'逐字节一致' if not diffs else f'{len(diffs)} 处差异'}")
        for d in diffs[:10]:
            print("  ", d)

        # ---- 新窗：剔除断点季（1920–2425） ----
        clean = drift_replay_report(
            conn, competitions=FD_COMPETITIONS, seasons=CLEAN_SEASONS
        )

        # ---- 主表：h/d/a 隐含 vs 实际（旧=全窗 新=剔除断点季） ----
        old_t, new_t = selection_table(full), selection_table(clean)
        print("\n[E-06 主表] 收盘隐含 vs 实际命中（旧=1920–2526 全窗 / 新=剔 2526）")
        print(f"{'向':>3} {'旧隐含%':>9} {'旧实际%':>9} {'旧偏差pp':>9}"
              f" {'新隐含%':>9} {'新实际%':>9} {'新偏差pp':>9} {'Δ偏差pp':>8}")
        for sel in ("h", "d", "a"):
            o, n = old_t[sel], new_t[sel]
            print(f"{sel:>3} {o['implied_pct']:>9.2f} {o['actual_pct']:>9.2f}"
                  f" {o['dev_pp']:>+9.2f} {n['implied_pct']:>9.2f}"
                  f" {n['actual_pct']:>9.2f} {n['dev_pp']:>+9.2f}"
                  f" {n['dev_pp'] - o['dev_pp']:>+8.2f}")
        print(f"观测数：旧 {full['overall']['n']:,} → 新 {clean['overall']['n']:,}"
              f"（剔 {full['overall']['n'] - clean['overall']['n']:,}"
              f" = 2526 段，占 {1 - clean['overall']['n'] / full['overall']['n']:.2%}）")

        # ---- 2526 段单独画像（陈旧 PSC 段自身的校准表现，诊断用） ----
        rows, _ = collect_drift_rows(
            conn, competitions=FD_COMPETITIONS, seasons=SEASONS
        )
        seg = [r for r in rows if r.season in BREAK_SEASONS]
        seg_stats = {}
        for sel in ("h", "d", "a"):
            g = [r for r in seg if r.selection == sel]
            seg_stats[sel] = {
                "n": len(g),
                "implied_pct": round(
                    sum(r.p_close for r in g) / len(g) * 100, 2
                ),
                "actual_pct": round(
                    sum(r.hit for r in g) / len(g) * 100, 2
                ),
            }
        print("\n[2526 段单独画像]（陈旧 PSC 段自身：隐含 vs 实际）")
        for sel, s in seg_stats.items():
            print(f"  {sel}: n={s['n']:,} 隐含 {s['implied_pct']:.2f}%"
                  f" 实际 {s['actual_pct']:.2f}%"
                  f" 偏差 {s['actual_pct'] - s['implied_pct']:+.2f}pp")

        # ---- 早价前视段（EV_early>5% 价值组）：证据主体是否在断点前 ----
        cohort = "ev_early_gt_0.05"
        old_c, new_c = full["value_cohorts"][cohort], clean["value_cohorts"][cohort]
        in_break = [r for r in rows
                    if r.season in BREAK_SEASONS and r.ev_early > 0.05]
        print(f"\n[早价前视段] EV_early>5% 组（账面实得 +8.84%）")
        print(f"  旧：n={old_c['n']:,} 实得 {old_c['roi_early_realized']:+.2%}"
              f" 命中 {old_c['realized_hit_rate']:.2%} vs p_close {old_c['mean_p_close']:.2%}")
        print(f"  新：n={new_c['n']:,} 实得 {new_c['roi_early_realized']:+.2%}"
              f" 命中 {new_c['realized_hit_rate']:.2%} vs p_close {new_c['mean_p_close']:.2%}")
        print(f"  2526 段在该组的占比：{len(in_break):,}/{old_c['n']:,}"
              f" = {len(in_break) / old_c['n']:.2%}")
        by_season_cohort = {
            season: sum(1 for r in rows
                        if r.season == season and r.ev_early > 0.05)
            for season in sorted({r.season for r in rows})
        }
        print("  组成分季 n:", " ".join(f"{s}:{n}" for s, n in by_season_cohort.items()))

        OUT.write_text(
            json.dumps(
                {
                    "params": {
                        "db": str(DB),
                        "archived_report": str(ARCHIVED),
                        "full_seasons": list(SEASONS),
                        "clean_seasons": list(CLEAN_SEASONS),
                        "break_seasons": list(BREAK_SEASONS),
                    },
                    "tree_drift_diffs": diffs,
                    "coverage_full": full["coverage"],
                    "coverage_clean": clean["coverage"],
                    "main_table_old": old_t,
                    "main_table_new": new_t,
                    "break_segment_profile": seg_stats,
                    "value_cohort_5pct_old": old_c,
                    "value_cohort_5pct_new": new_c,
                    "value_cohort_5pct_n_by_season": by_season_cohort,
                    "value_cohort_5pct_n_in_break": len(in_break),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        print(f"\n输出 → {OUT}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
