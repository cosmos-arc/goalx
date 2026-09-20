"""十年语料票 46 测试：完整性报表 + openfootball 对 fdhist 交叉验证。"""

from __future__ import annotations

import sqlite3

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import results as rs_store
from goalx_backend.data.ingest import openfootball as of
from goalx_backend.evaluation.corpus import completeness_report


def _seed_hist(db: sqlite3.Connection) -> None:
    rows = [
        # E0 2425：两行（一行缺 PSC）
        {
            "competition": "E0",
            "season": "2425",
            "match_date": "2025-05-10",
            "home_team": "Man United",
            "away_team": "Chelsea",
            "fthg": 2,
            "ftag": 1,
            "ftr": "H",
            "psc_home": 2.1,
            "psc_draw": 3.4,
            "psc_away": 3.2,
            "avgc_home": 2.05,
            "avgc_draw": 3.5,
            "avgc_away": 3.3,
        },
        {
            "competition": "E0",
            "season": "2425",
            "match_date": "2025-05-11",
            "home_team": "Arsenal",
            "away_team": "Everton",
            "fthg": 0,
            "ftag": 0,
            "ftr": "D",
            "psc_home": None,
            "psc_draw": None,
            "psc_away": None,
            "avgc_home": 1.5,
            "avgc_draw": 4.0,
            "avgc_away": 6.0,
        },
        # E1 2425：一行（交叉验证用）
        {
            "competition": "E1",
            "season": "2425",
            "match_date": "2025-04-19",
            "home_team": "Leeds",
            "away_team": "Stoke City",
            "fthg": 3,
            "ftag": 1,
            "ftr": "H",
            "psc_home": 1.9,
            "psc_draw": 3.6,
            "psc_away": 4.0,
            "avgc_home": 1.95,
            "avgc_draw": 3.5,
            "avgc_away": 3.9,
        },
    ]
    assert rs_store.upsert_hist_matches(db, rows) == 3


def test_completeness_report_rows_and_gaps(db) -> None:
    _seed_hist(db)
    report = completeness_report(
        db, competitions=("E0", "E1"), seasons=("2324", "2425")
    )
    assert report["total_rows"] == 3
    per = report["per_competition"]
    assert per["E0"]["2324"] == {"rows": 0}  # 缺季显式列出
    assert per["E0"]["2425"]["rows"] == 2
    assert per["E0"]["2425"]["psc_missing"] == 1
    assert per["E0"]["2425"]["avgc_missing"] == 0
    assert per["E1"]["2425"]["rows"] == 1


def _of_client(files: dict[str, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        name = f"{parts[-2]}/{parts[-1].removesuffix('.json')}"
        if name not in files:
            return httpx.Response(404, text="no file")
        return httpx.Response(200, json=files[name])

    return httpx.Client(transport=httpx.MockTransport(handler))


OF_E0 = {
    "name": "EPL 2024/25",
    "matches": [
        # 与 hist 同比分（跨日差一天：fd 05-10 vs of 05-11——±1 兜底）
        {
            "date": "2025-05-11",
            "team1": "Manchester United FC",
            "team2": "Chelsea FC",
            "score": [2, 1],
        },
        # 比分不一致
        {
            "date": "2025-05-11",
            "team1": "Arsenal FC",
            "team2": "Everton FC",
            "score": [1, 0],
        },
        # openfootball 有、fdhist 无
        {
            "date": "2025-05-11",
            "team1": "Nottingham Forest FC",
            "team2": "West Ham FC",
            "score": [0, 2],
        },
    ],
}


def test_cross_check_consistent_mismatch_and_gaps(db) -> None:
    _seed_hist(db)
    with _of_client({"2024-25/en.1": OF_E0}) as client:
        report = of.cross_check_fdhist(
            db,
            Settings(openfootball_base_url="https://of.test"),
            client,
            seasons=("2425",),
            leagues={"E0": "en.1"},
        )
    entry = report["E0"]["2425"]
    assert entry.compared == 2
    assert entry.consistent == 1
    assert entry.openfootball_only == 1
    assert entry.fdhist_only == 0
    assert entry.score_mismatches == [
        {
            "date": "2025-05-11",
            "fixture": "Arsenal FC v Everton FC",
            "openfootball": "1:0",
            "fdhist": "0:0",
        }
    ]
    # dict 化（CLI 输出形态）
    as_dict = of.cross_check_dict(report)["E0"]["2425"]
    assert as_dict["consistent"] == 1
    assert "score_mismatches" in as_dict


def test_cross_check_404_marks_not_available(db) -> None:
    with _of_client({}) as client:
        report = of.cross_check_fdhist(
            db,
            Settings(openfootball_base_url="https://of.test"),
            client,
            seasons=("1617",),
            leagues={"E0": "en.1"},
        )
    entry = report["E0"]["1617"]
    assert entry.not_available is True
    assert entry.compared == 0
    assert of.cross_check_dict(report)["E0"]["1617"]["not_available"] is True
