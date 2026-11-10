"""E-22 FD 断点隔离重算（edge-audit-20261109 票 18）。

任务：剔除断点季 2526/2627 重出冷门十年主读数——max fair/份额（账面 1.4916）
与分季分布，附实测校准、早期镜、固定赔率镜（翻案探针）同窗对照。

口径回源（票 18 指令）：.scratch/goalx-quant/issues/51-pool-v2-out-of-sample.md
及其实现 evaluation/pool_replay.py——本脚本直接复用生产纯函数（票 02 已独立
复算逐位坐实该实现：23,169/0/1.4916），唯一自变量=赛季窗。

断点语义（票 15）：隔离=整季剔除 2526/2627。psc 分期（结构性证据）中 2526
段 fair=陈旧 PSC（该段读数无效）；2627 无 Pinnacle 列本就只入 avgc 退化分期
（EV≡−35% 不作证据）——剔后新窗 psc 分期=1920–2425。

数据面（只读）：repo data/goalx.db 的 hist_matches（mode=ro&immutable，树漂
门=全窗复算与归档 51-pool-replay-report.json 逐字节一致）。

输出：本目录 18-e22-isolation-output.json + stdout 人读表。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO = Path("/Users/chevy/Desktop/code/goalx")
sys.path.insert(0, str(REPO / "apps" / "backend" / "src"))

from goalx_backend.data.ingest.fdhist import FD_COMPETITIONS, SEASONS  # noqa: E402
from goalx_backend.evaluation.pool_replay import (  # noqa: E402
    collect_candidates,
    pool_replay_report,
)

DB = REPO / "data" / "goalx.db"
ARCHIVED = REPO / ".scratch/goalx-quant/research/51-pool-replay-report.json"
OUT = Path(__file__).resolve().parent / "18-e22-isolation-output.json"

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


def stage_summary(stats: dict) -> str:
    pct = stats["fair_over_share_percentiles"]
    return (
        f"候选 {stats['candidates']:,} 正EV {stats['pool_ev_positive']}"
        f" max {pct['max']:.4f} p50 {pct['p50']:.4f} p99 {pct['p99']:.4f}"
        f"（盈亏线 {stats['pool_breakeven_ratio']:.4f}）"
    )


def main() -> None:
    conn = ro_conn()
    try:
        # ---- 树漂门：全窗复算 vs 归档报告 ----
        full = pool_replay_report(
            conn, competitions=FD_COMPETITIONS, seasons=SEASONS
        )
        archived = json.loads(ARCHIVED.read_text())
        diffs = deep_diff(full, archived)
        print(f"[树漂门] 全窗复算 vs 归档 51-pool-replay-report.json: "
              f"{'逐字节一致' if not diffs else f'{len(diffs)} 处差异'}")
        for d in diffs[:10]:
            print("  ", d)

        clean = pool_replay_report(
            conn, competitions=FD_COMPETITIONS, seasons=CLEAN_SEASONS
        )

        # ---- psc 分期主读数（结构性证据） ----
        print("\n[E-22 psc 分期] max fair/份额 与分布（旧=1920–2526 / 新=剔 2526/2627）")
        print("  旧:", stage_summary(full["overall"]["psc"]))
        print("  新:", stage_summary(clean["overall"]["psc"]))
        o_cal = full["realized_calibration"]["psc"]["pooled"]
        n_cal = clean["realized_calibration"]["psc"]["pooled"]
        print(f"  实测校准（旧）：命中 {o_cal['hit_rate']:.4%} ≈ fair {o_cal['mean_fair']:.4%}"
              f" ≈ 份额 {o_cal['mean_share']:.4%}（n={o_cal['n']:,}）")
        print(f"  实测校准（新）：命中 {n_cal['hit_rate']:.4%} ≈ fair {n_cal['mean_fair']:.4%}"
              f" ≈ 份额 {n_cal['mean_share']:.4%}（n={n_cal['n']:,}）")
        o_fx, n_fx = full["overall"]["psc"], clean["overall"]["psc"]
        print(f"  固定赔率镜正 EV（翻案探针）：旧 {o_fx['fx_ev_positive']}"
              f"（{o_fx['fx_ev_positive_rate']:.4%}，p99 {o_fx['fx_ev_percentiles']['p99']:+.4f}）"
              f" → 新 {n_fx['fx_ev_positive']}"
              f"（{n_fx['fx_ev_positive_rate']:.4%}，p99 {n_fx['fx_ev_percentiles']['p99']:+.4f}）")
        o_e = full["early_lens"]["psc"]
        n_e = clean["early_lens"]["psc"]
        print(f"  早期镜：旧 正EV {o_e['early_ev_positive']}/存活 {o_e['persisted_to_close']}"
              f" → 新 {n_e['early_ev_positive']}/{n_e['persisted_to_close']}")

        # ---- 分季分布（psc 分期） ----
        print("\n[分季分布] psc 分期逐季 max fair/份额（候选数）")
        print(f"{'季':>6} {'旧max':>8} {'新max':>8} {'旧候选':>7} {'新候选':>7}")
        for season in sorted(set(full["by_season"]) | set(clean["by_season"])):
            o = full["by_season"].get(season, {}).get("psc")
            n = clean["by_season"].get(season, {}).get("psc")
            o_s = f"{o['fair_over_share_percentiles']['max']:.4f}" if o else "—"
            n_s = f"{n['fair_over_share_percentiles']['max']:.4f}" if n else "—"
            o_n = f"{o['candidates']:,}" if o else "—"
            n_n = f"{n['candidates']:,}" if n else "—"
            print(f"{season:>6} {o_s:>8} {n_s:>8} {o_n:>7} {n_n:>7}")

        # ---- avgc 退化分期：断点后段是否全部随之消失 ----
        print("\n[avgc 退化分期]（fair=份额同源，EV≡−35%，仅标注不作证据）")
        print("  旧:", {s: v.get("avgc", {}).get("candidates", 0)
                       for s, v in full["by_season"].items()
                       if "avgc" in v})
        print("  新（剔 2526/2627 后残余）:",
              {s: v.get("avgc", {}).get("candidates", 0)
               for s, v in clean["by_season"].items() if "avgc" in v}
              or "无——退化分期完全由断点后季构成")

        # ---- max 行定位（旧/新同源） ----
        candidates, _ = collect_candidates(
            conn, competitions=FD_COMPETITIONS, seasons=SEASONS
        )
        psc_c = [c for c in candidates if c.stage == "psc"]
        top_old = max(psc_c, key=lambda c: c.fair / c.share)
        psc_new = [c for c in psc_c if c.season not in BREAK_SEASONS]
        top_new = max(psc_new, key=lambda c: c.fair / c.share)
        for tag, c in (("旧", top_old), ("新", top_new)):
            print(f"\n[max 行·{tag}] {c.season} {c.competition} {c.home_team}"
                  f" 选向 {c.selection}：fair {c.fair:.4f} / 份额 {c.share:.4f}"
                  f" = {c.fair / c.share:.4f}（彩池 EV {c.pool_ev:+.4%}，命中 {c.hit}）")
        seg = [c for c in psc_c if c.season in BREAK_SEASONS]
        print(f"\n[断点段画像] 2526 psc 候选 {len(seg):,}"
              f"（占 psc 候选 {len(seg) / len(psc_c):.2%}），段内 max"
              f" {max(c.fair / c.share for c in seg):.4f}")

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
                    "psc_overall_old": full["overall"]["psc"],
                    "psc_overall_new": clean["overall"]["psc"],
                    "calibration_psc_old": full["realized_calibration"]["psc"],
                    "calibration_psc_new": clean["realized_calibration"]["psc"],
                    "early_lens_psc_old": full["early_lens"]["psc"],
                    "early_lens_psc_new": clean["early_lens"]["psc"],
                    "by_season_psc_old": {
                        s: v["psc"] for s, v in full["by_season"].items()
                        if "psc" in v
                    },
                    "by_season_psc_new": {
                        s: v["psc"] for s, v in clean["by_season"].items()
                        if "psc" in v
                    },
                    "avgc_by_season_old": {
                        s: v["avgc"] for s, v in full["by_season"].items()
                        if "avgc" in v
                    },
                    "avgc_by_season_new": {
                        s: v["avgc"] for s, v in clean["by_season"].items()
                        if "avgc" in v
                    },
                    "max_row_old": {
                        "season": top_old.season,
                        "competition": top_old.competition,
                        "home_team": top_old.home_team,
                        "selection": top_old.selection,
                        "fair": top_old.fair,
                        "share": top_old.share,
                        "ratio": top_old.fair / top_old.share,
                        "pool_ev": top_old.pool_ev,
                    },
                    "max_row_new": {
                        "season": top_new.season,
                        "competition": top_new.competition,
                        "home_team": top_new.home_team,
                        "selection": top_new.selection,
                        "fair": top_new.fair,
                        "share": top_new.share,
                        "ratio": top_new.fair / top_new.share,
                        "pool_ev": top_new.pool_ev,
                    },
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
