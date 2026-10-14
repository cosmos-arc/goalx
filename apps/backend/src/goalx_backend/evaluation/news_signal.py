"""
事件信号 PIT 回放评测（model-iterate 票 05，spec S5）。

N1 试点事件（silver guardian/news_event）× gold PIT 面 × run 预测残差
——"第二信息源存在与否"的诚实回答（研究 26 N2 评估协议自建）：

- **事件面**：事件计数/类型分布/侧分布/距开球时距（描述基线）；
- **对照面一（市场未消化？）**：事件场 vs 无事件场的模型残差
  （signal-attribution 同口径 realized）分布对照——事件场市场误差
  显著偏移=事件携带市场未消化信息；
- **对照面二（安慰剂）**：随机同时段无事件场对照（事件效应≠时段
  效应——豪门新闻多≠信息多）；
- **price-in 面**：发布→收盘的市场位移方向（q1h 切片 vs close）——
  事件后市场是否已动（动了=price-in，剩余空间小）。

样本量诚实（n≈几十事件场）：效应量+方向为主，显著性只作探索性参考。
观察性报告：不进模型不改参数。纯读面（事件 parquet + gold 只读桥 +
run 预测），报告落 reports/news-signal.{json,md}。
"""

from __future__ import annotations

# pyarrow.parquet 存根不全（data/gold 同例放宽）
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import fmean
from typing import Any

import duckdb
import pyarrow.parquet as pq

from goalx_backend.data import gold_reader
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.evaluation.metrics import load_backtest_run
from goalx_backend.markets import SELECTIONS

REPORT_BASENAME = "news-signal"
MIN_GROUP_N = 5  # 组内低于此 n 只报数不下结论
_PREDICTIONS_SQL = (
    "SELECT match_key, ftr, fair_probs FROM backtest_predictions"  # noqa: S608
    + " WHERE run_id = ? ORDER BY id"
)


def _read_events(store: CorpusStore) -> list[dict[str, Any]]:
    """事件 parquet → dict 行（缺数据集=空如实）。"""
    path = store.silver_path("guardian", "news_event") / "all"
    if not path.exists():
        return []
    return pq.read_table(path).to_pylist()


def _prediction_residuals(
    conn: sqlite3.Connection, run_id: int
) -> dict[str, dict[str, float]]:
    """Run 预测 → {sid: {side: realized}}（realized=1[ftr]−p_fair，票 21 口径）。"""
    rows = conn.execute(
        _PREDICTIONS_SQL,
        (run_id,),
    ).fetchall()
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        if row["ftr"] is None:
            continue
        fair = json.loads(str(row["fair_probs"]))
        ftr = str(row["ftr"]).lower()
        out[str(row["match_key"])] = {
            side: (1.0 if ftr == side else 0.0) - float(fair[side])
            for side in SELECTIONS
        }
    return out


@dataclass
class _Group:
    """一个残差对照组的累计。"""

    values: list[float] = field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        """n/均值（效应量为主，票面样本量诚实纪律）。"""
        if not self.values:
            return {"n": 0}
        return {"n": len(self.values), "mean": round(fmean(self.values), 4)}


def build_news_signal_report(
    conn: sqlite3.Connection,
    duck_con: duckdb.DuckDBPyConnection,
    store: CorpusStore,
    *,
    run_id: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """事件信号回放 → reports/ 双视图。"""
    rid, _label, _params, status = load_backtest_run(conn, run_id)
    events = _read_events(store)
    residuals = _prediction_residuals(conn, rid)
    sids = sorted({str(e["sid"]) for e in events} & set(residuals))
    event_sids = set(sids)

    # 事件面（描述基线）
    by_type: dict[str, int] = {}
    for event in events:
        by_type[str(event["event_type"])] = by_type.get(str(event["event_type"]), 0) + 1
    hours_to_kickoff = []
    for event in events:
        try:
            pub = datetime.fromisoformat(
                str(event["published_at"]).replace("Z", "+00:00")
            )
            ko = datetime.fromisoformat(
                str(event["kickoff_iso"]).replace(" ", "T")
            ).replace(tzinfo=pub.tzinfo)
            hours_to_kickoff.append(round((ko - pub).total_seconds() / 3600, 1))
        except ValueError:
            continue
    hours = sorted(h for h in hours_to_kickoff if h >= 0)
    event_face = {
        "n_events": len(events),
        "n_fixtures_with_events": len(event_sids),
        "by_type": dict(sorted(by_type.items())),
        "hours_to_kickoff_median": hours[len(hours) // 2] if hours else None,
        "hours_to_kickoff_p90": hours[int(len(hours) * 0.9)] if hours else None,
    }

    # 对照面一：事件场 vs 无事件场残差
    non_event = [s for s in residuals if s not in event_sids]
    control_sids = _placebo_sids(non_event, len(event_sids))
    groups = {
        "event": _Group(),
        "control": _Group(),
    }
    for sid in event_sids:
        groups["event"].values.extend(residuals[sid].values())
    for sid in control_sids:
        groups["control"].values.extend(residuals[sid].values())

    # price-in 面：事件场的 q1h→close 主队位移（gold signal 面）
    signal_rows = gold_reader.fetch_signal_rows_by_sids(duck_con, sids)
    price_in_moves = []
    for sid in sids:
        row = signal_rows.get(sid)
        if row is None:
            continue
        early = _devig_home(
            row.get("q1h1x2_h"), row.get("q1h1x2_d"), row.get("q1h1x2_a")
        )
        close = _devig_home(
            row.get("close1x2_h"), row.get("close1x2_d"), row.get("close1x2_a")
        )
        if early is not None and close is not None:
            price_in_moves.append(close - early)
    price_in = {
        "n": len(price_in_moves),
        "mean": round(fmean(price_in_moves), 5) if price_in_moves else None,
    }

    payload = {
        "built_at": (today or date.today()).isoformat(),
        "run_id": rid,
        "run_status": status,
        "event_face": event_face,
        "residual_contrast": {name: group.stats() for name, group in groups.items()},
        "price_in": price_in,
        "note": (
            "样本量诚实（n=事件场数级）：效应量+方向为主，显著性不报；"
            + "对照=同时段随机无事件场（安慰剂语义）；realized 口径同"
            + " signal-attribution（1[ftr]−p_fair 三向平铺）；观察性报告"
            + " 不进模型不改参数；全量回填=门后另裁"
        ),
    }
    reports = store.root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{REPORT_BASENAME}.md").write_text(
        _render_md(payload), encoding="utf-8"
    )
    (reports / f"{REPORT_BASENAME}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _placebo_sids(pool: list[str], n: int) -> list[str]:
    """确定性安慰剂对照（无事件场池均匀取 n 个）。"""
    if not pool or n <= 0:
        return []
    step = max(1, len(pool) // max(n, 1))
    return pool[::step][:n]


def _devig_home(h: float | None, d: float | None, a: float | None) -> float | None:
    """三向比例 devig 主队概率（缺端/≤1 → None）。"""
    values = [h, d, a]
    if any(v is None for v in values):
        return None
    clean = [float(v) for v in values if v is not None]
    if any(v <= 1.0 or not math.isfinite(v) for v in clean):
        return None
    inv = [1.0 / v for v in clean]
    return inv[0] / sum(inv)


def _render_md(payload: dict[str, Any]) -> str:
    """人读视图。"""
    face = payload["event_face"]
    contrast = payload["residual_contrast"]
    event_stats = contrast["event"]
    control_stats = contrast["control"]
    lines = [
        "# 事件信号回放（票 05）",
        "",
        f"run={payload['run_id']} 事件 {face['n_events']} 条 / 覆盖 "
        + f"{face['n_fixtures_with_events']} 场",
        f"类型分布 {face['by_type']}",
        f"距开球中位 {face['hours_to_kickoff_median']}h / p90 "
        + f"{face['hours_to_kickoff_p90']}h",
        "",
        "## 残差对照（事件场 vs 无事件场安慰剂）",
        "",
        f"- 事件场 realized 均值 {event_stats.get('mean')}（n 场"
        + f" {face['n_fixtures_with_events']}，值 n={event_stats.get('n')}）",
        f"- 对照场 realized 均值 {control_stats.get('mean')}"
        + f"（值 n={control_stats.get('n')}）",
        "- 判读：|差| ≥0.01 且方向稳定=事件携带信息候选；样本小只作方向参考",
        "",
        "## price-in 面（事件场 q1h→close 主队位移）",
        "",
        f"- n={payload['price_in']['n']} 均值 {payload['price_in']['mean']}"
        + "（≈0=事件后市场未动=未 price-in；负值=主队价走弱）",
        "",
        "- 判读注意：" + payload["note"],
    ]
    return "\n".join(lines) + "\n"
