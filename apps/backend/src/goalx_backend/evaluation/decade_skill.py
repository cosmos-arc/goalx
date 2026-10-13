"""
十年分时代技能报告（backtest-decade 票 19，spec S19）。

回答主问题"数据质变后模型到底有没有 skill"：era（psc_proxy 2016→2023
vs trajectory 2023/24+）×联赛×赛季×玩法四轴，预测面与下注面分列——

- **预测面**（分母=run 预测集，非下注集）：RPS skill vs 同链公平基准
  （evaluate_predictions 复用，DM p 为探索性参考）；
- **下注面**（同 run 的 bets）：等权/加权 ROI 分列（票 34 口径，统计经
  metrics.flat_stake_stats 单一定义）、命中率、投入与盈利、t 统计量；
  玩法归属沿 compute_run_metrics 口径（串关进其每条腿的玩法，同玩法只
  计一次）；联赛轴用注行的 competition，赛季轴经腿→预测行 season 映射；
- **腿级概率面**（下注子集）：p_model vs 隐含 p=(1−haircut)/odds 的
  Brier skill。价由 (1/p_mkt)×(1−haircut) 构造 ⇒ 隐含概率代数还原即
  引擎市场侧概率（had=公平链、hhad/ttg=联合矩阵，票面不变量"市场基准
  =引擎市场侧同源"由此满足）；口径标"下注集"（选择有偏，不外推全集）；
- **EV 实现率**：Σ利润 / Σ(ev×stake)，账面 EV 是否兑现（era/玩法/overall）；
- **era 对照**：两时代预测 skill 与下注 ROI 的差 + 样本量（粗判）。

纯 sqlite 读面（v25 预测行自带 era/ftr，不回查 gold）；报告落语料树
reports/ MD+JSON（与判门同惯例）。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.leagues import ERA_PSC_PROXY, ERA_TRAJECTORY
from goalx_backend.evaluation.metrics import evaluate_predictions, flat_stake_stats

REPORT_BASENAME = "decade-skill"
ERA_ORDER = (ERA_PSC_PROXY, ERA_TRAJECTORY)
MIN_FACE_SAMPLES = 30  # 分层面低于此 n 只计数不下结论（探索纪律）
MIN_SEASON_TREND = 2  # 赛季走向句最少季数
_LATEST_DONE_SQL = (
    "SELECT id FROM backtest_runs WHERE status = 'done' ORDER BY id DESC LIMIT 1"
)


@dataclass
class _BetFace:
    """一个分层的下注面累计（收益统计经 flat_stake_stats 单一定义）。"""

    n: int = 0
    won: int = 0
    lost: int = 0
    void: int = 0
    staked: float = 0.0
    profit: float = 0.0
    pairs: list[tuple[float, float]] = field(default_factory=list)  # (profit, stake)

    def add(self, stake: float, profit: float, status: str) -> None:
        self.n += 1
        if status == "won":
            self.won += 1
        elif status == "lost":
            self.lost += 1
        else:
            self.void += 1
        self.staked += stake
        self.profit += profit
        self.pairs.append((profit, stake))

    def as_dict(self) -> dict[str, Any]:
        """下注面行：等权 roi / 加权 ROI 分列（票 34：不得混称）。"""
        stats = flat_stake_stats([p for p, _ in self.pairs], [s for _, s in self.pairs])
        return {
            "n": self.n,
            "won": self.won,
            "lost": self.lost,
            "void": self.void,
            "hit_rate": round(self.won / (self.won + self.lost), 4)
            if self.won + self.lost
            else None,
            "staked": round(self.staked, 2),
            "profit": round(self.profit, 2),
            "roi_equal": round(stats["roi"], 4),
            "roi_stake_weighted": round(stats["roi_stake_weighted"], 4),
            "t_stat": round(stats["t_stat"], 3),
        }


@dataclass
class _LegFace:
    """一个玩法的腿级概率面（下注子集；void 腿只计数不进损失）。"""

    n: int = 0
    void: int = 0
    brier_model_sum: float = 0.0
    brier_implied_sum: float = 0.0
    model_prob_sum: float = 0.0
    implied_sum: float = 0.0
    hit: int = 0

    def add(self, model_prob: float, implied: float, hit: bool, void: bool) -> None:
        self.n += 1
        if void:
            self.void += 1
            return
        outcome = 1.0 if hit else 0.0
        self.brier_model_sum += (model_prob - outcome) ** 2
        self.brier_implied_sum += (implied - outcome) ** 2
        self.model_prob_sum += model_prob
        self.implied_sum += implied
        self.hit += int(hit)

    def as_dict(self) -> dict[str, Any]:
        """腿面行：Brier skill（下注集口径）+ 校准快照。"""
        settled = self.n - self.void
        brier_m = self.brier_model_sum / settled if settled else None
        brier_i = self.brier_implied_sum / settled if settled else None
        skill = 1.0 - brier_m / brier_i if brier_m is not None and brier_i else None
        return {
            "n": self.n,
            "void": self.void,
            "brier_model": round(brier_m, 6) if brier_m is not None else None,
            "brier_implied": round(brier_i, 6) if brier_i is not None else None,
            "skill_brier": round(skill, 4) if skill is not None else None,
            "model_prob_mean": round(self.model_prob_sum / settled, 4)
            if settled
            else None,
            "implied_mean": round(self.implied_sum / settled, 4) if settled else None,
            "hit_rate": round(self.hit / settled, 4) if settled else None,
        }


def _load_run(
    conn: sqlite3.Connection, run_id: int | None
) -> tuple[int, str, dict[str, Any]]:
    """取目标 run（缺省=最新 done；显式 id 允许任意状态供中途检视）。"""
    if run_id is None:
        row = conn.execute(_LATEST_DONE_SQL).fetchone()
        if row is None:
            raise ValueError("没有可用的 done 回测 run（先跑 backtest）")
        rid = int(row["id"])
    else:
        rid = run_id
    meta = conn.execute(
        "SELECT label, params FROM backtest_runs WHERE id = ?", (rid,)
    ).fetchone()
    if meta is None:
        raise ValueError(f"run {rid} 不存在")
    params: dict[str, Any] = json.loads(str(meta["params"])) if meta["params"] else {}
    return rid, str(meta["label"]), params


def _single_or_bucket(values: set[str]) -> str:
    """腿集映射到单值；空=unknown（预测行缺 era/season），多值=mixed。"""
    if len(values) == 1:
        return next(iter(values))
    return "mixed" if values else "unknown"


def _prediction_faces(
    rows: list[sqlite3.Row],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, int]], int]:
    """预测面分层（overall / era / era:联赛 / era:赛季 / 赛季）+ 混布。"""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    source_mix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    era_missing = 0
    for row in rows:
        if row["ftr"] is None:
            continue  # v25 前旧 run 行：指标面如实跳过
        sample = {
            "had_probs": json.loads(str(row["had_probs"])),
            "fair_probs": json.loads(str(row["fair_probs"])),
            "ftr": str(row["ftr"]),
            "competition": str(row["competition"]),
            "season": str(row["season"]),
        }
        groups["overall"].append(sample)
        groups[f"season:{sample['season']}"].append(sample)
        era = row["era"]
        if era is None:
            era_missing += 1
            continue
        era = str(era)
        source_mix[era][str(row["fair_source"])] += 1
        groups[f"era:{era}"].append(sample)
        groups[f"era:{era}:lg:{sample['competition']}"].append(sample)
        groups[f"era:{era}:season:{sample['season']}"].append(sample)
    faces = {scope: evaluate_predictions(group) for scope, group in groups.items()}
    return (
        faces,
        {era: dict(t) for era, t in sorted(source_mix.items())},
        era_missing,
    )


def _bet_faces(
    bet_rows: list[sqlite3.Row],
    era_of: Callable[[str], str | None],
    season_of: Callable[[str], str | None],
    haircut: float,
) -> tuple[dict[str, _BetFace], dict[str, _LegFace], dict[str, dict[str, Any]]]:
    """下注面（era / era:联赛 / era:玩法 / 赛季）+ 腿面 + EV 实现率。"""
    bets: dict[str, _BetFace] = defaultdict(_BetFace)
    legs: dict[str, _LegFace] = defaultdict(_LegFace)
    ev_pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for bet in bet_rows:
        leg_rows = json.loads(str(bet["legs"]))
        detail = json.loads(str(bet["detail"])) if bet["detail"] else None
        if detail is None:
            raise ValueError(f"bet {bet['id']} 缺结算 detail（腿级面不可信）")
        leg_hits = detail["legs"]
        stake = float(bet["stake"])
        profit = float(bet["profit"])
        status = str(bet["status"])
        keys = [str(leg["match_key"]) for leg in leg_rows]
        era = _single_or_bucket({e for e in map(era_of, keys) if e is not None})
        season = _single_or_bucket({s for s in map(season_of, keys) if s is not None})
        competition = str(bet["competition"])
        markets = {str(leg["market_code"]) for leg in leg_rows}
        bets[f"era:{era}"].add(stake, profit, status)
        bets[f"era:{era}:lg:{competition}"].add(stake, profit, status)
        bets[f"season:{season}"].add(stake, profit, status)
        expected = float(bet["ev"]) * stake
        ev_pairs[era].append((expected, profit))
        ev_pairs["overall"].append((expected, profit))
        for market in markets:  # 同玩法串关只计一次（compute_run_metrics 口径）
            bets[f"era:{era}:mkt:{market}"].add(stake, profit, status)
            ev_pairs[f"mkt:{market}"].append((expected, profit))
        for leg, hit_info in zip(leg_rows, leg_hits, strict=True):
            legs[str(leg["market_code"])].add(
                float(leg["model_prob"]),
                (1.0 - haircut) / float(leg["odds"]),
                bool(hit_info["hit"]),
                bool(hit_info["void"]),
            )

    ev = {key: _ev_stats(pairs) for key, pairs in sorted(ev_pairs.items())}
    return dict(bets), dict(legs), ev


def _ev_stats(pairs: list[tuple[float, float]]) -> dict[str, Any]:
    """EV 实现率：Σ利润 / Σ账面期望利润（分母≤0 时如实置空）。"""
    expected = sum(e for e, _ in pairs)
    realized = sum(p for _, p in pairs)
    return {
        "n": len(pairs),
        "expected_profit": round(expected, 2),
        "realized_profit": round(realized, 2),
        "realization": round(realized / expected, 4) if expected > 0 else None,
    }


def _era_contrast(
    pred_faces: dict[str, dict[str, Any]], bet_faces: dict[str, _BetFace]
) -> dict[str, dict[str, Any]]:
    """两时代对照：预测 skill 差 + 下注 ROI 差（粗判，n 随行）。"""
    contrast: dict[str, dict[str, Any]] = {}
    for era in ERA_ORDER:
        pred = pred_faces.get(f"era:{era}", {})
        bet = bet_faces.get(f"era:{era}")
        skill = pred.get("skill_rps")
        contrast[era] = {
            "pred_n": pred.get("n", 0),
            "skill_rps": round(skill, 4) if isinstance(skill, float) else skill,
            "bet_n": bet.n if bet else 0,
            "bet_roi": bet.as_dict()["roi_stake_weighted"] if bet else None,
        }
    skills = [contrast[era]["skill_rps"] for era in ERA_ORDER]
    if all(isinstance(s, float | int) for s in skills):
        contrast["skill_gap"] = round(skills[1] - skills[0], 4)
    return contrast


def _axis_verdicts(payload: dict[str, Any]) -> list[str]:
    """结论句（阈值机械生成，n 不足只报数不下结论）。"""
    lines: list[str] = []
    for era in ERA_ORDER:
        face = payload["prediction_faces"].get(f"era:{era}")
        n = face["n"] if face else 0
        if face is None or n < MIN_FACE_SAMPLES:
            lines.append(f"- {era}: 预测面样本不足（n={n}）")
            continue
        skill = face["skill_rps"]
        verdict = (
            "有 skill（RPS 优于公平基准）"
            if skill >= 0
            else "无 skill（RPS 劣于公平基准）"
        )
        lines.append(
            f"- {era}: 预测 n={n} skill_rps={skill:+.4f}"
            + f"（DM p={face['dm_p']:.3f}，探索性）→ 模型{verdict}"
        )
    # 联赛轴：每 era 预测面 n 最大的前三联赛（完整面在 JSON）
    for era in ERA_ORDER:
        lg_faces = sorted(
            (
                (scope, face)
                for scope, face in payload["prediction_faces"].items()
                if scope.startswith(f"era:{era}:lg:")
                and face.get("n", 0) >= MIN_FACE_SAMPLES
            ),
            key=lambda kv: -kv[1]["n"],
        )[:3]
        for scope, face in lg_faces:
            skill = face["skill_rps"]
            league = scope.rsplit(":", 1)[-1]
            lines.append(
                f"- {era}:{league}: n={face['n']} skill_rps={skill:+.4f}"
                + ("（优于基准）" if skill >= 0 else "（劣于基准）")
            )
    # 赛季轴：首末赛季 skill 走向
    seasons = sorted(
        (scope, face)
        for scope, face in payload["prediction_faces"].items()
        if scope.startswith("season:") and face.get("n", 0) >= MIN_FACE_SAMPLES
    )
    if len(seasons) >= MIN_SEASON_TREND:
        first, last = seasons[0], seasons[-1]
        lines.append(
            "- 赛季走向: {} skill={:+.4f} → {} skill={:+.4f}".format(
                first[0].removeprefix("season:"),
                first[1]["skill_rps"],
                last[0].removeprefix("season:"),
                last[1]["skill_rps"],
            )
        )
    for era in ERA_ORDER:
        face = payload["betting_faces"].get(f"era:{era}")
        if face is None or face["n"] < MIN_FACE_SAMPLES:
            continue
        roi = face["roi_stake_weighted"]
        direction = "正收益" if roi is not None and roi > 0 else "负收益"
        lines.append(
            f"- {era}: 下注 n={face['n']}（staked={face['staked']:.0f}）"
            + f" ROI={roi:+.2%}（t={face['t_stat']}）→ {direction}"
        )
    for market, face in sorted(payload["leg_faces"].items()):
        skill = face.get("skill_brier")
        if skill is None or face["n"] - face["void"] < MIN_FACE_SAMPLES:
            continue
        verdict = "模型优于市场隐含" if skill > 0 else "模型不优于市场隐含"
        lines.append(
            f"- 玩法 {market}（下注集 n={face['n']}）：Brier skill={skill:+.4f}"
            + f"（校准 模型 {face['model_prob_mean']} vs 隐含 {face['implied_mean']}"
            + f" vs 实际 {face['hit_rate']}）→ {verdict}"
        )
    lines.append(
        "- 判读注意：腿级面在下注子集上（选择有偏）；era1 公平基准=PSC 收盘"
        + "代理（PSC≠轨迹末价），两时代 skill 差含基准口径差的混入。"
    )
    return lines


def build_decade_skill_report(
    conn: sqlite3.Connection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """十年技能报告：run 数据四轴分层 → reports/ 双视图。"""
    rid, label, params = _load_run(conn, run_id)
    haircut = float(params.get("haircut", 0.10))
    pred_rows = conn.execute(
        "SELECT * FROM backtest_predictions WHERE run_id = ? ORDER BY id",
        (rid,),
    ).fetchall()
    bet_rows = conn.execute(
        "SELECT * FROM backtest_bets WHERE run_id = ? ORDER BY id", (rid,)
    ).fetchall()
    era_of = {
        str(row["match_key"]): str(row["era"])
        for row in pred_rows
        if row["era"] is not None
    }
    season_of = {str(row["match_key"]): str(row["season"]) for row in pred_rows}

    pred_faces, source_mix, era_missing = _prediction_faces(pred_rows)
    bet_faces, leg_faces, ev_faces = _bet_faces(
        bet_rows, era_of.get, season_of.get, haircut
    )

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "label": label,
        "haircut": haircut,
        "markets": params.get("markets", []),
        "window": {"start": params.get("start"), "end": params.get("end")},
        "n_predictions": len(pred_rows),
        "n_era_missing_predictions": era_missing,
        "n_bets": len(bet_rows),
        "prediction_faces": pred_faces,
        "fair_source_mix": source_mix,
        "betting_faces": {
            scope: face.as_dict() for scope, face in sorted(bet_faces.items())
        },
        "leg_faces": {
            market: face.as_dict() for market, face in sorted(leg_faces.items())
        },
        "ev_realization": ev_faces,
        "era_contrast": _era_contrast(pred_faces, bet_faces),
    }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )
    return payload


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图：结论句先行，分层表随后（完整面在 JSON）。"""
    end = payload["window"]["end"] or "数据尾"
    lines = [
        "# 十年分时代技能报告（票 19）",
        "",
        f"run={payload['run_id']}（{payload['label']}）"
        + f" 窗口 {payload['window']['start']}→{end}"
        + f" haircut={payload['haircut']}",
        f"预测 {payload['n_predictions']} 场 / 下注 {payload['n_bets']} 注，玩法 "
        + ",".join(payload["markets"]),
        "",
        "## 结论",
        "",
        *_axis_verdicts(payload),
        "",
        "## era 对照",
        "",
        "| era | 预测 n | skill_rps | 下注 n | 下注 ROI |",
        "|---|---|---|---|---|",
    ]
    for era in ERA_ORDER:
        row = payload["era_contrast"][era]
        roi = row["bet_roi"]
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                era,
                row["pred_n"],
                row["skill_rps"],
                row["bet_n"],
                f"{roi:+.2%}" if roi is not None else "—",
            )
        )
    gap = payload["era_contrast"].get("skill_gap")
    if gap is not None:
        lines.append(f"\n两时代 skill 差（trajectory−psc_proxy）= {gap:+.4f}")
    lines += [
        "",
        "## 下注面（era / era×联赛 / era×玩法 / 赛季）",
        "",
        "| scope | n | won/lost/void | hit | staked | profit | ROI | t |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for scope, face in payload["betting_faces"].items():
        roi = face["roi_stake_weighted"]
        lines.append(
            "| {} | {} | {}/{}/{} | {} | {} | {} | {} | {} |".format(
                scope,
                face["n"],
                face["won"],
                face["lost"],
                face["void"],
                face["hit_rate"],
                face["staked"],
                face["profit"],
                f"{roi:+.2%}" if roi is not None else "—",
                face["t_stat"],
            )
        )
    lines += [
        "",
        "完整分层（era:联赛:赛季交叉 / 腿级概率面 / EV 实现率 / fair_source "
        + "混布）见 decade-skill.json。",
    ]
    return "\n".join(lines) + "\n"
