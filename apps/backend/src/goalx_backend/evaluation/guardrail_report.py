"""
既有护栏十年复验报告（backtest-decade 票 18，spec S18）。

护栏语义（backtest.py 模块 docstring）：模拟价天花板/市场概率下限拦的是
模型-市场**尾部分歧的噪声**（两头都无法分辨极低量级概率，产生的"EV"
是噪声）；反推拟合误差上限拦的是 1X2 反推失真的市场隐含矩阵。十年数据
（48k 场、分 era）上复验触发面与被拦结局——**只描述、只举证**，参数值
变更=用户点头（本报告不改任何护栏）。

方法（无需引擎实跑——护栏触发只依赖 fair 收盘概率，与 DC 模型无关）：

- 候选口径与引擎 `_candidates_for_match` 同：jc 价 = fair/(1−haircut)
  （缺省 −10%）；market_prob = 1/jc = fair_p/(1−haircut)。
- ``MIN_MARKET_PROB=0.02=1/50=1/max_sim_odds``：market_prob<0.02 ⇔
  jc>50 是倒数恒等式（任意 haircut 下两栏必然同触发），分别计数并
  以并集报告；阈值 fair_p < 0.02×(1−haircut)。
- 被拦结局：被拦 selection 的实际命中率 vs 其隐含概率（命中率≈隐含 =
  拦的是无信息尾部；命中率≫隐含 = 拦掉了真信号——举证给用户）。
- ``MARKET_MAX_GOAL_ERROR=0.02``：era 正典 fair → goal_expectancy 反推
  失败率（拟合误差超限即市场隐含矩阵不可用）。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.backtest import (
    DEFAULT_MAX_SIM_ODDS,
    MARKET_MAX_GOAL_ERROR,
    MIN_MARKET_PROB,
    era_fair_probs,
    market_implied_matrix,
)
from goalx_backend.markets import SELECTIONS

REPORT_BASENAME = "guardrail-decade"


@dataclass
class _GuardStats:
    """一个分层的护栏触发累计。"""

    n_selections: int = 0
    clipped_min_prob: int = 0
    clipped_max_odds: int = 0
    clipped_union: int = 0
    clipped_implied_sum: float = 0.0
    clipped_hit: int = 0

    def as_dict(self) -> dict[str, Any]:
        """汇总行（被拦结局=命中率 vs 隐含概率均值）。"""
        clipped = {
            "n": self.clipped_union,
            "rate": round(self.clipped_union / self.n_selections, 4)
            if self.n_selections
            else None,
        }
        if self.clipped_union:
            clipped |= {
                "implied_prob_mean": round(
                    self.clipped_implied_sum / self.clipped_union, 4
                ),
                "actual_hit_rate": round(self.clipped_hit / self.clipped_union, 4),
            }
        return {
            "n_selections": self.n_selections,
            "clipped_min_prob": self.clipped_min_prob,
            "clipped_max_odds": self.clipped_max_odds,
            "clipped_union": clipped,
        }


def build_guardrail_report(
    store: CorpusStore,
    duck_con: duckdb.DuckDBPyConnection,
    *,
    haircut: float = 0.10,
    today: date | None = None,
) -> dict[str, Any]:
    """触发面统计（十年分 era×联赛）→ 报告 dict 落 reports/ 双视图。"""
    rows = gold_reader.fetch_market_face_rows(duck_con)
    stats: dict[str, _GuardStats] = defaultdict(_GuardStats)
    inversion_failed = 0
    n_fair = 0
    source_tally: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for row in rows:
        fair = era_fair_probs(
            str(row["era"]),
            (row["psc_home"], row["psc_draw"], row["psc_away"]),
            (row["avgc_home"], row["avgc_draw"], row["avgc_away"]),
            (row["close1x2_h"], row["close1x2_d"], row["close1x2_a"]),
            (row["close1x2_cons_h"], row["close1x2_cons_d"], row["close1x2_cons_a"]),
        )
        if fair is None:
            continue
        n_fair += 1
        fair_probs, fair_source = fair
        era_scope = str(row["era"])
        scopes = (era_scope, "{}:{}".format(era_scope, row["league"]))
        source_tally[str(row["era"])][fair_source] += 1
        if market_implied_matrix(fair_probs) is None:
            inversion_failed += 1
        gh, ga = int(row["home_goals"]), int(row["away_goals"])
        outcome = {  # 该 selection 实际命中与否（H/D/A）
            "h": gh > ga,
            "d": gh == ga,
            "a": gh < ga,
        }
        for sel in SELECTIONS:
            fair_p = fair_probs[sel]
            jc_odds = (1.0 / fair_p) * (1.0 - haircut)
            market_prob = 1.0 / jc_odds
            hit_min = market_prob < MIN_MARKET_PROB
            hit_max = jc_odds > DEFAULT_MAX_SIM_ODDS
            for scope in scopes:
                st = stats[scope]
                st.n_selections += 1
                st.clipped_min_prob += int(hit_min)
                st.clipped_max_odds += int(hit_max)
                if hit_min or hit_max:
                    st.clipped_union += 1
                    st.clipped_implied_sum += market_prob
                    st.clipped_hit += int(outcome[sel])

    payload = {
        "built_at": today.isoformat() if today else date.today().isoformat(),
        "haircut": haircut,
        "guardrails": {
            "min_market_prob": MIN_MARKET_PROB,
            "max_sim_odds": DEFAULT_MAX_SIM_ODDS,
            "market_max_goal_error": MARKET_MAX_GOAL_ERROR,
        },
        "n_matches_with_fair": n_fair,
        "inversion_failed": {
            "n": inversion_failed,
            "rate": round(inversion_failed / n_fair, 6) if n_fair else None,
        },
        "fair_source_mix": {
            era: {k: int(v) for k, v in sorted(tally.items())}
            for era, tally in source_tally.items()
        },
        "faces": {scope: st.as_dict() for scope, st in sorted(stats.items())},
        "note": "只描述只举证，护栏参数不变更（变更=用户点头逐参数举证）",
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
    """人读视图：触发面 + 被拦结局对照（命中率 vs 隐含）。"""
    lines = [
        "# 护栏十年复验（票 18，只描述只举证）",
        "",
        "- 有基准场次 {}（haircut={}；护栏值 {}/{}）".format(
            payload["n_matches_with_fair"],
            payload["haircut"],
            payload["guardrails"]["min_market_prob"],
            payload["guardrails"]["max_sim_odds"],
        ),
        "- 反推拟合误差护栏：失败 {}/{}（rate={}）".format(
            payload["inversion_failed"]["n"],
            payload["n_matches_with_fair"],
            payload["inversion_failed"]["rate"],
        ),
        "",
        "| scope | selections | 触发率(并) | 被拦隐含均值 | 被拦实际命中 |",
        "|---|---|---|---|---|",
    ]
    for scope, face in payload["faces"].items():
        if ":" in scope:  # era 主层即可判读（league 层在 JSON）
            continue
        clipped = face["clipped_union"]
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                scope,
                face["n_selections"],
                clipped.get("rate", "—"),
                clipped.get("implied_prob_mean", "—"),
                clipped.get("actual_hit_rate", "—"),
            )
        )
    lines += [
        "",
        "<!-- 判读：被拦实际命中≈其隐含（低量级） ⇒ 拦的是噪声（护栏成立）；",
        "被拦命中≫隐含 ⇒ 护栏拦掉真信号——逐参数举证后由用户裁决 -->",
    ]
    return "\n".join(lines) + "\n"
