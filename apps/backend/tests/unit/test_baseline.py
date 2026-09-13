"""历史基准分期/来源对照测试（票 34 验收 5）。"""

from __future__ import annotations

import json

from goalx_backend import backtest as bt
from goalx_backend import baseline


def seed_hist(
    db, match_date: str, *, psc: bool = True, avgc: bool = True, n: int = 1
) -> None:
    max_id = int(
        db.execute("SELECT COALESCE(MAX(id), 0) AS m FROM hist_matches").fetchone()["m"]
    )
    for i in range(n):
        db.execute(
            "INSERT INTO hist_matches (competition, season, match_date, home_team,"
            " away_team, fthg, ftag, ftr, psc_home, psc_draw, psc_away,"
            " avgc_home, avgc_draw, avgc_away)"
            " VALUES ('E0','2526',?,?,'B', 2, 0, 'H', ?, ?, ?, ?, ?, ?)",
            (
                match_date,
                f"team{max_id + i}",  # 自然键含队名,批量种子各行唯一
                2.0 if psc else None,
                3.4 if psc else None,
                3.8 if psc else None,
                2.1 if avgc else None,
                3.5 if avgc else None,
                3.9 if avgc else None,
            ),
        )
    db.commit()


def test_baseline_quality_report_periods_and_diffs(db) -> None:
    seed_hist(db, "2025-06-01", n=3)  # 切期前,psc+avgc 齐
    seed_hist(db, "2025-08-01", psc=False, n=2)  # 切期后,只有 avgc(缺失 PSC)
    seed_hist(db, "2025-09-01", psc=False, avgc=False, n=1)  # 两无
    report = baseline.baseline_quality_report(db)
    assert report["review_date"] == "2025-07-23"
    before = report["periods"]["before_2025-07-23"]
    since = report["periods"]["since_2025-07-23"]
    assert before["n"] == 3
    assert before["n_psc"] == 3
    assert before["n_both"] == 3
    assert before["psc_missing_rate"] == 0.0
    assert before["mean_overround_psc"] > 0
    assert before["mean_shin_prob_abs_diff_psc_vs_avgc"] is not None
    assert since["n"] == 3
    assert since["n_psc"] == 0
    assert since["psc_missing_rate"] == 1.0
    assert since["n_avgc"] == 2
    assert since["n_neither"] == 1
    assert since["mean_overround_psc"] is None  # 无样本不给数
    assert "不判定真概率" in report["limitations"]


def test_run_baseline_comparison_creates_new_runs_and_keeps_old(db) -> None:
    db.execute(
        "INSERT INTO backtest_runs (label, params, status, created_at)"
        " VALUES ('old-run', '{}', 'done', '2026-09-01T00:00:00+00:00')"
    )
    db.commit()
    old_ids = {
        int(r["id"]) for r in db.execute("SELECT id FROM backtest_runs").fetchall()
    }
    results = baseline.run_baseline_comparison(db, bt.BacktestParams())
    labels = {
        str(r["label"]): dict(r)
        for r in db.execute(
            "SELECT label, params, status FROM backtest_runs"
        ).fetchall()
    }
    assert set(labels) == {"old-run", "baseline-psc", "baseline-avgc"}
    assert labels["old-run"]["status"] == "done"  # 旧 run 保留
    for source in ("psc", "avgc"):
        params = json.loads(labels[f"baseline-{source}"]["params"])
        assert params["fair_source"] == source
        assert params["price_model"] == "simulated_jc"  # 合成价标注
        assert "versions" in params
        assert results[source]["run_id"] not in old_ids


def test_fair_source_selection(db) -> None:
    db.execute(
        "INSERT INTO hist_matches (competition, season, match_date, home_team,"
        " away_team, fthg, ftag, ftr, psc_home, psc_draw, psc_away,"
        " avgc_home, avgc_draw, avgc_away)"
        " VALUES ('E0','2526','2025-09-01','A','B',2,0,'H',2.0,3.4,3.8,2.2,3.4,3.8)"
    )
    db.commit()
    row = db.execute("SELECT * FROM hist_matches").fetchone()
    auto = bt.fair_probs_from_close(row)
    assert auto is not None
    assert auto[1] == "psc"  # auto: PSC 优先
    only_avgc = bt.fair_probs_from_close(row, fair_source="avgc")
    assert only_avgc is not None
    assert only_avgc[1] == "avgc"
    assert only_avgc[0] != auto[0]  # 不同来源不同基准
    # PSC 缺失：强制 psc → 无基准；auto → AvgC 兜底
    db.execute("UPDATE hist_matches SET psc_home = NULL")
    db.commit()
    no_psc = db.execute("SELECT * FROM hist_matches").fetchone()
    assert bt.fair_probs_from_close(no_psc, fair_source="psc") is None
    fallback = bt.fair_probs_from_close(no_psc)
    assert fallback is not None
    assert fallback[1] == "avgc"
