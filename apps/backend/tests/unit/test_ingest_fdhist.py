"""football-data.co.uk 历史导入测试（票 21）。"""

from __future__ import annotations

from goalx_backend.config import Settings
from goalx_backend.ingest import fdhist
from goalx_backend.store import results as rs_store

CSV_TEXT = (
    "\ufeffDiv,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSCH,PSCD,PSCA,AvgCH,AvgCD,AvgCA\n"
)
CSV_ROWS = (
    "E0,16/08/2024,Man United,Fulham,1,0,H,2.05,3.60,4.10,2.10,3.50,4.00\n"
    "E0,17/08/2024,Chelsea,Man City,0,2,A,, ,,4.50,4.00,1.75\n"
    "E0,18/08/24,Liverpool,Ipswich,2,1,H,1.50,4.80,6.50,1.55,4.50,6.00\n"
    "E0,19/08/2024,,,,,,, ,,,,\n"
)


def test_parse_csv_cleans_and_maps_columns() -> None:
    rows, skipped = fdhist.parse_csv(CSV_TEXT + CSV_ROWS, "E0", "2425")
    assert skipped == 1  # 缺队伍/比分的空行
    assert len(rows) == 3
    first = rows[0]
    assert first["match_date"] == "2024-08-16"
    assert first["ftr"] == "H"
    assert first["psc_home"] == 2.05
    second = rows[1]
    assert second["psc_home"] is None  # Pinnacle 缺失
    assert second["avgc_away"] == 1.75  # AvgC 兜底列在
    third = rows[2]
    assert third["match_date"] == "2024-08-18"  # dd/mm/YY 两位年


def test_import_history_idempotent(db) -> None:
    settings = Settings()
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return CSV_TEXT + CSV_ROWS

    first = fdhist.import_history(
        db, settings, None, competitions=("E0",), seasons=("2425",), fetch=fetch
    )
    assert first.rows == 3
    assert first.skipped == 1
    assert calls == ["https://www.football-data.co.uk/mmz4281/2425/E0.csv"]
    stats = rs_store.hist_match_stats(db)
    assert stats["total"] == 3
    assert stats["psc_present"] == 2
    assert stats["avgc_present"] == 3
    second = fdhist.import_history(
        db, settings, None, competitions=("E0",), seasons=("2425",), fetch=fetch
    )
    assert second.rows == 3  # 幂等重跑（upsert 不翻倍）
    assert rs_store.hist_match_stats(db)["total"] == 3


def test_import_history_full_matrix_urls(db) -> None:
    urls: list[str] = []

    def fetch(url: str) -> str:
        urls.append(url)
        return CSV_TEXT + CSV_ROWS

    fdhist.import_history(db, Settings(), None, fetch=fetch)
    assert len(urls) == len(fdhist.FD_COMPETITIONS) * len(fdhist.SEASONS)
    assert "https://www.football-data.co.uk/mmz4281/2324/SP1.csv" in urls
