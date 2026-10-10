"""
hhad/ttg 解锁判定报告（backtest-decade 票 15，spec S15）。

had-only 的来历：M2 实测 1X2→比分矩阵反推（goal_expectancy）在总进球
维度系统性欠分散（ttg 桶 4/5/6 隐含低于实际 15-40%），反推价制造假
edge。gold AH/OU 收盘族（11 家模板书商十年中位数）就绪后，本报告做
**数据对照**回答"真实线派生的市场侧是否可信、反推侧欠分散是否在十年
数据上复现"——不改动引擎下注面（解锁=用户裁决，门=报告+用户点头）。

方法（十年分 era×联赛）：

- **市场侧**（真实线）：AH/OU 收盘中位线 + 水位（马来/港式，正水位
  w → 欧赔 1+w）双向比例去水 → P(主赢盘)/P(大球)；只取无平局退款的
  干净线（AH 整数/半线中取半线，OU 取 x.0/x.5 半步长）。
- **反推侧**（旧路径）：era 正典收盘 1X2（复用 ``backtest.era_fair_probs``
  单一语义）→ ``market_implied_matrix`` 反推 (λh, λa) → 同线
  P(主赢盘)/P(大球)。
- **判据**：Brier 与校准误差（|隐含−实际|）市场侧 vs 反推侧逐面对照；
  ttg 桶分布反推隐含 vs 实际（v1 欠分散结论的十年复检）。
- 线约定（真树 2026-10-10 实证）：cid8 亚盘线正=主让（与 fdhist 相反），
  主赢盘 = 净胜球 − cid8线 > 0；反推矩阵侧等价 goal_line=−cid8线。

代称红线：输出一律 cid/角色代称。水位健全带 [0.5, 1.5]，带外行计入
``water_out_of_band`` 不进样本（真树 48,548 行中 25 行越带）。
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.leagues import FD_TO_LEAGUE
from goalx_backend.evaluation.backtest import era_fair_probs, market_implied_matrix
from goalx_backend.modelling.score_matrix import ScoreMatrix

REPORT_BASENAME = "unlock-hhad-ttg"
_HALF_STEP_EPS = 1e-9  # 半步长线判定容差（浮点线 x.0/x.5 vs .25/.75）
OU_LINE_MAX = 7.0  # OU 线上界：ttg 桶 7=7+ 并桶，≥7 的 over 概率不可分辨
WATER_SANITY_BAND = (0.5, 1.5)  # 马来/港式水位健全带（真树分布 1% 分位 0.80）


@dataclass
class _FaceStats:
    """一个市场面（ou/hhad）一个分层的样本累计。"""

    n: int = 0
    market_brier: float = 0.0
    model_brier: float = 0.0
    market_abs_err: float = 0.0
    model_abs_err: float = 0.0
    market_prob_sum: float = 0.0
    model_prob_sum: float = 0.0
    actual_sum: int = 0

    def add(self, market_p: float, model_p: float, actual: bool) -> None:
        outcome = 1.0 if actual else 0.0
        self.n += 1
        self.market_brier += (market_p - outcome) ** 2
        self.model_brier += (model_p - outcome) ** 2
        self.market_abs_err += abs(market_p - outcome)
        self.model_abs_err += abs(model_p - outcome)
        self.market_prob_sum += market_p
        self.model_prob_sum += model_p
        self.actual_sum += int(actual)

    def as_dict(self) -> dict[str, Any]:
        """汇总行（均值口径；n=0 时全 None 不给数）。"""
        if not self.n:
            return {"n": 0}
        return {
            "n": self.n,
            "actual_rate": round(self.actual_sum / self.n, 4),
            "market": {
                "mean_prob": round(self.market_prob_sum / self.n, 4),
                "brier": round(self.market_brier / self.n, 4),
                "mae": round(self.market_abs_err / self.n, 4),
            },
            "model_1x2_inverted": {
                "mean_prob": round(self.model_prob_sum / self.n, 4),
                "brier": round(self.model_brier / self.n, 4),
                "mae": round(self.model_abs_err / self.n, 4),
            },
        }


@dataclass
class UnlockReport:
    """解锁判定报告（机器可读 dict + reports/ 双视图）。"""

    built_at: str = ""
    n_matches: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    # faces[face][scope] → _FaceStats（scope = era / era:league）
    faces: dict[str, dict[str, _FaceStats]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    # ttg 桶：反推隐含分布 vs 实际分布（era 分层，v1 欠分散结论复检）
    ttg_implied: dict[str, dict[str, float]] = field(default_factory=dict)
    ttg_actual: dict[str, dict[str, float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """机器可读载荷（faces 逐层展开为 dict）。"""
        return {
            "built_at": self.built_at,
            "n_matches": self.n_matches,
            "skipped": self.skipped,
            "faces": {
                face: {scope: stats.as_dict() for scope, stats in scopes.items()}
                for face, scopes in self.faces.items()
            },
            "ttg_implied": self.ttg_implied,
            "ttg_actual": self.ttg_actual,
        }


def malay_decimal(water: float) -> float:
    """马来/港式正水位 → 欧赔（w=0.95 → 1.95）。"""
    return 1.0 + water


def two_way_prob(water_a: float, water_b: float) -> float:
    """双向比例去水（亚盘/大小水位两侧 → a 侧概率）。"""
    dec_a, dec_b = malay_decimal(water_a), malay_decimal(water_b)
    inv_a, inv_b = 1.0 / dec_a, 1.0 / dec_b
    return inv_a / (inv_a + inv_b)


def _clean_half_line(line: float | None) -> float | None:
    """半步长线（x.0/x.5）返回原值；四分之一线（.25/.75）返回 None。"""
    if line is None or not math.isfinite(line):
        return None
    return line if abs(line * 2 - round(line * 2)) < _HALF_STEP_EPS else None


def half_line_only(line: float | None) -> float | None:
    """仅半线（x.5，无平局退款）；整数线（有 push）与四分线返回 None。"""
    clean = _clean_half_line(line)
    if clean is None or clean == int(clean):
        return None
    return clean


def waters_sane(waters: tuple[float | None, ...]) -> bool:
    """水位三元组健全性（非空 + 有限 + 健全带内）。"""
    lo, hi = WATER_SANITY_BAND
    return all(w is not None and math.isfinite(w) and lo <= w <= hi for w in waters)


def _ttg_over_prob(ttg: dict[str, float], line: float) -> float:
    """矩阵 ttg 视图 → P(总进球 > line)（7 桶=7+ 并入）。"""
    return sum(p for bucket, p in ttg.items() if float(bucket) > line)


def build_unlock_report(
    store: CorpusStore,
    duck_con: duckdb.DuckDBPyConnection,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """
    全量对照（十年分 era×联赛）→ 报告 dict 并落语料树 reports/ 双视图。

    样本单位=场×面（OU/HHAH 各自独立入样）；只描述、不判定——解锁裁决
    字段留用户（spec S15 门=报告+用户点头）。
    """
    rows = gold_reader.fetch_market_face_rows(duck_con)
    report = UnlockReport(
        built_at=today.isoformat() if today else date.today().isoformat()
    )
    ttg_imp: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    ttg_act: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        _collect_row(row, report, ttg_imp, ttg_act)
    _normalize_ttg(report, ttg_imp, ttg_act)
    payload = report.as_dict()
    _write_report(store, payload)
    return payload


def _collect_row(
    row: dict[str, Any],
    report: UnlockReport,
    ttg_imp: dict[str, dict[str, float]],
    ttg_act: dict[str, dict[str, int]],
) -> None:
    """单场入样：反推矩阵 → ttg 桶累计 + OU/HHAH 双面对照样本。"""

    def skip(key: str) -> None:
        report.skipped[key] = report.skipped.get(key, 0) + 1

    report.n_matches += 1
    matrix, fail_key = _row_market_matrix(row)
    if matrix is None:
        skip(fail_key or "no_fair_baseline")
        return
    gh, ga = int(row["home_goals"]), int(row["away_goals"])
    league = FD_TO_LEAGUE.get(str(row["league"]), str(row["league"]))
    scopes = (str(row["era"]), f"{row['era']}:{league}")
    for scope in scopes:
        for bucket, p in matrix.ttg().items():
            ttg_imp[scope][bucket] += p
    # 实际分布按 era 主 scope 记（era:league 分层由 faces 承担）
    ttg_act[str(row["era"])][str(min(gh + ga, 7))] += 1
    for face in ("ou", "hhad"):
        skip_key = _add_face_sample(report, face, scopes, row, matrix, gh, ga)
        if skip_key:
            skip(skip_key)


def _normalize_ttg(
    report: UnlockReport,
    ttg_imp: dict[str, dict[str, float]],
    ttg_act: dict[str, dict[str, int]],
) -> None:
    """桶计数归一为分布（actual 只记 era 主 scope）。"""
    for era, act_counts in ttg_act.items():
        total = sum(act_counts.values())
        if total:
            report.ttg_actual[era] = {
                b: round(c / total, 4) for b, c in sorted(act_counts.items())
            }
    for scope, counts in ttg_imp.items():
        imp_total = sum(counts.values())
        if imp_total:
            report.ttg_implied[scope] = {
                b: round(p / imp_total, 4) for b, p in sorted(counts.items())
            }


def _write_report(store: CorpusStore, payload: dict[str, Any]) -> None:
    """reports/ 双视图（机器可读 JSON + 人读 MD；数据面资产不进 repo）。"""
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：faces 对照表 + ttg 桶复检表（只描述不判定）。"""
    lines = [
        "# hhad/ttg 解锁判定报告（票 15，数据对照）",
        "",
        "- 样本：{} 场（已赛+非行政判赛）；跳过 {}".format(
            payload["n_matches"], payload["skipped"]
        ),
        "- 判读方向：market=真实线水位去水；model_1x2_inverted=era 正典 1X2 反推。",
        "  市场侧 Brier/MAE 显著低于反推侧 ⇒ 真实线市场侧可信，解锁证据成立；",
        "  反推侧 ttg 桶隐含持续低于实际 ⇒ v1 欠分散结论在十年数据上复现。",
        "",
    ]
    for face in ("ou", "hhad"):
        lines.append(f"## {face} 面对照（Brier/MAE：市场 vs 反推）")
        lines.append("")
        lines.append(
            "| scope | n | 实际率 | 市场 Brier | 反推 Brier | 市场 MAE | 反推 MAE |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for scope, stats in sorted(payload["faces"].get(face, {}).items()):
            if stats.get("n", 0) == 0:
                continue
            m, inv = stats["market"], stats["model_1x2_inverted"]
            row_md = (
                f"| {scope} | {stats['n']} | {stats['actual_rate']:.4f}"
                f" | {m['brier']:.4f} | {inv['brier']:.4f}"
                f" | {m['mae']:.4f} | {inv['mae']:.4f} |"
            )
            lines.append(row_md)
        lines.append("")
    lines.append("## ttg 桶复检（反推隐含 vs 实际，era 分层）")
    lines.append("")
    for era, actual in sorted(payload["ttg_actual"].items()):
        implied = payload["ttg_implied"].get(era, {})
        lines.append(f"### {era}")
        lines.append("")
        lines.append("| 桶 | 反推隐含 | 实际 | 差（隐含−实际） |")
        lines.append("|---|---|---|---|")
        for bucket in sorted(actual):
            imp = implied.get(bucket)
            diff = f"{imp - actual[bucket]:+.4f}" if imp is not None else "—"
            imp_s = f"{imp:.4f}" if imp is not None else "—"
            lines.append(f"| {bucket} | {imp_s} | {actual[bucket]:.4f} | {diff} |")
        lines.append("")
    lines.append("<!-- 门=报告+用户点头：本报告不改动引擎下注面（had-only 维持） -->")
    return "\n".join(lines) + "\n"


def _row_market_matrix(
    row: dict[str, Any],
) -> tuple[ScoreMatrix | None, str | None]:
    """
    行 → (反推比分矩阵, 失败原因)。

    era 正典 fair → 反推；失败原因为 None=成功 / "no_fair_baseline" /
    "inversion_failed"（拟合误差超限）。
    """
    fair = era_fair_probs(
        str(row["era"]),
        (row["psc_home"], row["psc_draw"], row["psc_away"]),
        (row["avgc_home"], row["avgc_draw"], row["avgc_away"]),
        (row["close1x2_h"], row["close1x2_d"], row["close1x2_a"]),
        (row["close1x2_cons_h"], row["close1x2_cons_d"], row["close1x2_cons_a"]),
    )
    if fair is None:
        return None, "no_fair_baseline"
    matrix = market_implied_matrix(fair[0])
    if matrix is None:
        return None, "inversion_failed"
    return matrix, None


def _add_face_sample(
    report: UnlockReport,
    face: str,
    scopes: tuple[str, str],
    row: dict[str, Any],
    matrix: ScoreMatrix,
    gh: int,
    ga: int,
) -> str | None:
    """
    单场单面入样（ou：干净半步长线；hhad：仅半线；水位健全带内）。

    线约定（真树 2026-10-10 实证）：cid8 正=主让（与 fdhist 相反），主
    赢盘=净胜球−线>0；反推矩阵侧等价 goal_line=−cid8线。
    """
    if face == "ou":
        # 半线（x.5）限定：x.0 落线=退款面，错记 under 会偏置校准
        # （correctness F2，真树 9.9% 样本落线）；线 ≥7 超出 ttg 桶分辨率
        line = (
            half_line_only(row["ou_close_line_med"])
            if (
                row["ou_close_line_med"] is not None
                and float(row["ou_close_line_med"]) < OU_LINE_MAX
            )
            else None
        )
        waters = (row["ou_close_over_water_med"], row["ou_close_under_water_med"])
        side_a, side_b = "ou_close_over_water_med", "ou_close_under_water_med"
        skip_key = "ou_no_half_line"
    else:
        line = half_line_only(row["ah_close_line_med"])
        waters = (row["ah_close_home_water_med"], row["ah_close_away_water_med"])
        side_a, side_b = "ah_close_home_water_med", "ah_close_away_water_med"
        skip_key = "ah_no_half_line"
    if line is None or not waters_sane(waters):
        return skip_key
    if face == "ou":
        model_p = _ttg_over_prob(matrix.ttg(), line)
        actual = gh + ga > line
    else:
        model_p = matrix.cover_prob(-line)  # 半步长线：hhad() 的 int() 截断不适用
        actual = (gh - ga) - line > 0
    market_p = two_way_prob(float(row[side_a]), float(row[side_b]))
    for scope in scopes:
        st = report.faces[face].setdefault(scope, _FaceStats())
        st.add(market_p, model_p, actual)
    return None
