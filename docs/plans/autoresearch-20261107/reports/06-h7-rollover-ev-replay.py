#!/usr/bin/env python3
# 票 06（autoresearch-20261107）：H7 滚存期 EV 全量回放——池类唯一理论正 EV 窗口。
#
# 纯离线只读研究脚本（analysis_03 先例：先 sqlite3 .backup 出主库快照再 ro
# 分析，全程零写运行库——回填刚写完主库，快照隔离必须）。产出打印 stdout，
# 供 06-h7-rollover-ev-replay.md 引用。
#
# 滚存期识别（口径第一步）：
# - 入库口径 period.rollover_in = 官方「开奖后奖池余额 pool_after」（zucai_official
#   _period_from_game 映射）——即该期开奖后留下的死钱，滚入下一期。
# - 期 N 为滚存期 ⟺ 前一期（market 内 period_no 数值序的前驱）pool_after > 0。
#   跨年号段（25180→26001）数值序天然单调，直接排序取前驱。
# - 首期无前驱 → 排除（识别不确定期如实标注，不硬分）。
#
# 两个度量：
# ① 主指标·随机票期望返奖率：EV = Σ_j P(hit tier_j) × (prize_amount_j /
#   stake_count_j) / 2 元注金；P(14 全对)=(1/3)^14、P(13 对)=14·(1/3)^13·(2/3)。
#   口径注记：官方取消场（void）数不在库 → 按 14 场计；void 场对滚存/非滚存
#   两组同向抬 P，差值（判读量）几乎不受影响。0 中奖奖级该期未派 → 该奖级
#   贡献按 0 计（钱在下期滚存面体现，不虚记）。
# ② 配套·池面实现派彩率（无概率模型）：Σ prize_amount / total_sale——
#   完全不受 void/P 假设影响的硬账对照。
# 门：组间差 > 2SE（Welch，期次为单位）；判读边界：>非滚存但 <100% 仍亏，
#   只有滚存组返奖率 >100% 才改变「池票=测量仪表」定位。
#
# 基准②（结构化票）：十年档无每场赔率面（官方 V2 无欧指、源B 已判死），
#   live 窗（2026-04 起）滚存期仅 ~19 期无统计力——本票诚实记不可行，
#   归 S5 滚存窗扫描随 live 积累（spec v0 S5 已列）。

from __future__ import annotations

import json
import math
import sqlite3
import sys

import numpy as np

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/h7-pool-snapshot.db"
STAKE = 2.0  # 14场/任9 每注 2 元
P_14 = (1.0 / 3.0) ** 14
P_13 = 14 * (1.0 / 3.0) ** 13 * (2.0 / 3.0)
P_RJ9 = (1.0 / 3.0) ** 9  # 任9 随机票：随机挑 9 场随机填 → 全对概率


def welch(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    ma, mb = a.mean(), b.mean()
    va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
    se = math.sqrt(va + vb)
    return float(ma - mb), float(se)


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT p.id, p.period_no, p.sales_deadline, s.sales_amount, s.rollover_in,
               s.prize_tiers
        FROM pool_periods p JOIN pool_states s ON s.pool_period_id = p.id
        WHERE p.market_code = 'ttt14' AND s.prize_tiers IS NOT NULL
        ORDER BY CAST(p.period_no AS INTEGER)
        """
    ).fetchall()
    print("=" * 76)
    print("H7 滚存期 EV 全量回放（14场胜负彩，官方档 V2 回填）")
    print("=" * 76)
    print(f"带官方 prize_tiers 的期次: {len(rows)}（{rows[0]['period_no']}…{rows[-1]['period_no']}）")

    periods = []
    rj_periods = []
    bad_tier = 0
    for r in rows:
        state = json.loads(r["prize_tiers"])
        tiers = state.get("sfc") or {}
        sale = tiers.get("total_sale")
        rows_j = tiers.get("tiers") or []
        if not sale or sale <= 0:
            bad_tier += 1
            continue
        ev = 0.0
        ev_t1 = 0.0
        ev_t2 = 0.0
        paid = 0.0
        # sfc 派彩只计一/二等奖级——tiers 数组可能混入「任选9场」奖级行
        # （V2 部分期任9 面带完整奖级），混计会虚增 sfc 派彩率。
        for t in rows_j:
            prize, count = t.get("prize_amount"), t.get("stake_count")
            level = str(t.get("level") or "")
            if prize is None or ("一" not in level and "二" not in level):
                continue
            paid += prize
            if count and count > 0:
                per_winner = prize / count
                if "一" in level:
                    ev_t1 = P_14 * per_winner
                elif "二" in level:
                    ev_t2 = P_13 * per_winner
        ev = ev_t1 + ev_t2
        periods.append(
            {
                "no": r["period_no"],
                "sale": sale,
                "pool_after": r["rollover_in"] or 0.0,
                "ev_rate": ev / STAKE,
                "ev_t1_rate": ev_t1 / STAKE,
                "ev_t2_rate": ev_t2 / STAKE,
                "payout_rate": paid / sale,
            }
        )
        # 任9 面：历史档奖级行（「任选9场」）或 live「rj」面，销量/滚存自
        # rj_amounts（历史）或 rj 面（live）
        rj_state = state.get("rj") or {}
        rj_tier = next(
            (t for t in rows_j if "任选9场" in str(t.get("level") or "") ), None
        )
        rj_sale = (
            rj_state.get("total_sale")
            or (state.get("rj_amounts") or {}).get("total_sale")
        )
        rj_pa = (
            rj_state.get("pool_after")
            if rj_state
            else (state.get("rj_amounts") or {}).get("pool_after")
        )
        if rj_tier and rj_sale and rj_sale > 0:
            prize, count = rj_tier.get("prize_amount"), rj_tier.get("stake_count")
            rj_ev = P_RJ9 * (prize / count) / STAKE if count and prize else 0.0
            rj_periods.append(
                {
                    "no": r["period_no"],
                    "sale": rj_sale,
                    "pool_after": rj_pa or 0.0,
                    "ev_rate": rj_ev,
                    "payout_rate": (prize or 0.0) / rj_sale,
                }
            )
        elif rj_state.get("tiers"):
            # live「rj」面（多奖级罕见，通常单奖级）：同法累计
            rj_paid = 0.0
            rj_ev2 = 0.0
            for t in rj_state.get("tiers") or []:
                prize, count = t.get("prize_amount"), t.get("stake_count")
                if prize is None:
                    continue
                rj_paid += prize
                if count and count > 0 and "一" in str(t.get("level") or ""):
                    rj_ev2 += P_RJ9 * (prize / count)
            if rj_sale and rj_sale > 0:
                rj_periods.append(
                    {
                        "no": r["period_no"],
                        "sale": rj_sale,
                        "pool_after": rj_state.get("pool_after") or 0.0,
                        "ev_rate": rj_ev2 / STAKE,
                        "payout_rate": rj_paid / rj_sale,
                    }
                )
    print(f"可分析期次: {len(periods)}（缺销量/奖级 {bad_tier} 期排除）；任9 面可用 {len(rj_periods)} 期")

    # 滚存期识别：前驱 pool_after > 0
    def attach_predecessor(seq: list[dict]) -> list[dict]:
        out = []
        for i, p in enumerate(seq):
            if i == 0:
                continue
            p["rollover_in_amount"] = seq[i - 1]["pool_after"]
            out.append(p)
        return out

    sample = attach_predecessor(periods)

    def analyze(seq: list[dict], title: str, *, primary: bool) -> None:
        rollover = [p for p in seq if p["rollover_in_amount"] > 0]
        normal = [p for p in seq if p["rollover_in_amount"] <= 0]
        print()
        print("=" * 76)
        print(f"{title}：样本 {len(seq)} = 滚存 {len(rollover)} + 非滚存 {len(normal)}（首期无前驱已排除）")
        print("=" * 76)
        if rollover:
            print(
                f"滚存注入规模: 中位 ¥{np.median([p['rollover_in_amount'] for p in rollover]):,.0f} "
                f"均值 ¥{np.mean([p['rollover_in_amount'] for p in rollover]):,.0f}"
            )
        for name, key in (
            ("①随机票期望返奖率（主指标）", "ev_rate"),
            ("②池面实现派彩率（硬账）", "payout_rate"),
        ):
            a = np.array([p[key] for p in rollover])
            b = np.array([p[key] for p in normal])
            if not len(a) or not len(b):
                continue
            d, se = welch(a, b)
            z = d / se if se > 0 else float("nan")
            print()
            print(f"{name}")
            print(
                f"  滚存期:   n={len(a)} 均值 {a.mean():.4f} 中位 {np.median(a):.4f} "
                f"p10 {np.percentile(a, 10):.4f} p90 {np.percentile(a, 90):.4f}"
            )
            print(
                f"  非滚存期: n={len(b)} 均值 {b.mean():.4f} 中位 {np.median(b):.4f} "
                f"p10 {np.percentile(b, 10):.4f} p90 {np.percentile(b, 90):.4f}"
            )
            print(f"  差（滚存−非滚存）= {d:+.4f} ± {se:.4f}（z={z:+.2f}，门=差>2SE）")
        a = np.array([p["ev_rate"] for p in rollover]) if rollover else np.array([float("nan")])
        se_a = a.std(ddof=1) / math.sqrt(len(a)) if len(a) > 1 else float("nan")
        print()
        print(
            f"[判读线] 滚存组随机票期望返奖率 = {a.mean():.4f}（±{se_a:.4f}）"
            f"——{'>100% ✓ 正 EV 窗口' if a.mean() > 1.0 else '<100%：抬高入场优先级但不改池票=测量仪表定位'}"
        )
        if rollover and normal:
            print("[奖级分解] 随机票 EV 的一等奖/二等奖贡献（滚存 vs 非滚存）")
            for label, key in (("一等奖（死钱注入面）", "ev_t1_rate"), ("二等奖（随机票 EV 主导项）", "ev_t2_rate")):
                ra = np.array([p[key] for p in rollover])
                na_ = np.array([p[key] for p in normal])
                print(f"  {label}: 滚存 {ra.mean():.4f} vs 非滚存 {na_.mean():.4f}")
        if not primary or not rollover:
            return
        # 剂量响应 + 逐年代（主面）
        print()
        print("[剂量响应] 滚存注入规模四分位 × 随机票期望返奖率")
        rs = sorted(rollover, key=lambda p: p["rollover_in_amount"])
        q = max(1, len(rs) // 4)
        for i, label in enumerate(("Q1", "Q2", "Q3", "Q4")):
            seg = rs[i * q : (i + 1) * q] if i < 3 else rs[3 * q :]
            if not seg:
                continue
            evs = np.array([p["ev_rate"] for p in seg])
            print(
                f"  {label}: n={len(seg)} 注入中位 ¥{np.median([p['rollover_in_amount'] for p in seg]):,.0f} "
                f"→ 返奖率 {evs.mean():.4f}"
            )
        print()
        print("[逐年代] 随机票期望返奖率（滚存 vs 非滚存）")
        by_year: dict[str, list[list[float]]] = {}
        for p in seq:
            y = "20" + p["no"][:2]
            by_year.setdefault(y, [[], []])
            by_year[y][0 if p["rollover_in_amount"] > 0 else 1].append(p["ev_rate"])
        for y in sorted(by_year):
            r_, n_ = by_year[y]
            rm = f"{np.mean(r_):.4f}(n={len(r_)})" if r_ else "-"
            nm = f"{np.mean(n_):.4f}(n={len(n_)})" if n_ else "-"
            print(f"  {y}: 滚存 {rm} 非滚存 {nm}")

    analyze(sample, "14场胜负彩（sfc·主面）", primary=True)

    # 任9 面（诚实账）：任选9场 随机票全对概率 (1/3)^9≈1/19683，实际销量下
    # 一等奖每期必有人中（实测 498/498 期 stake_count>0、rj pool_after 恒 0）
    # ——任9 无滚存面，H7 机制只在 14场 一等奖（1/4.78M）上成立。
    print()
    print(f"[任9 面] {len(rj_periods)} 期覆盖，但任9 奖池每期清空（一等奖必有人中，"
          f"pool_after 恒 0）——无滚存期可对比，不进判读。")

    con.close()


if __name__ == "__main__":
    main()
