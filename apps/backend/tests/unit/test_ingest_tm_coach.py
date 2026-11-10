"""TM 教练任职层采集测试（autoresearch 票 18）。

fixture 为 2026-11-10 实拍页的裁剪（页结构漂移时重拍替换）：
- tm_league_pl_2023.html.txt：英超 2023-24 赛季页俱乐部表（20 队）；
- tm_staff_arsenal_manager.html.txt：阿森纳 Manager 沿革（18 任）；
- tm_staff_arsenal_caretaker.html.txt：阿森纳 Caretaker 沿革（6 段）。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from limits import RateLimitItemPerMinute

from goalx_backend.config import Settings
from goalx_backend.data.ingest import tm_coach

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_clubs_extracts_league_membership() -> None:
    clubs = tm_coach.parse_clubs(_fixture("tm_league_pl_2023.html.txt"))
    ids = {c.club_tm_id for c in clubs}
    assert len(ids) == 20  # 英超 2023-24 全 20 队
    by_id = {c.club_tm_id: c for c in clubs}
    assert by_id[11].club_name == "Arsenal FC"
    assert by_id[281].club_name == "Manchester City"
    assert by_id[11].slug == "arsenal-fc" or by_id[11].slug == "fc-arsenal"


def test_parse_staff_history_manager_rows() -> None:
    tenures = tm_coach.parse_staff_history(
        _fixture("tm_staff_arsenal_manager.html.txt"),
        role="manager",
        club_tm_id=11,
        club_name="Arsenal FC",
    )
    by_coach = {t.coach_name: t for t in tenures}
    arteta = by_coach["Mikel Arteta"]
    assert arteta.coach_tm_id == 47620
    assert arteta.date_from == "2019-12-22"
    assert arteta.date_to is None  # 现任（页面上 End of time in post 空）
    assert arteta.games == 369
    # 多国籍行（Bruce Rioch：Scotland+England 双旗）不错位
    rioch = by_coach["Bruce Rioch"]
    assert rioch.date_from == "1995-07-01"
    assert rioch.date_to == "1996-06-30"
    assert rioch.games == 47
    wenger = by_coach["Arsène Wenger"]
    assert wenger.date_from == "1996-10-01"
    assert wenger.date_to == "2018-06-30"


def test_parse_staff_history_caretaker_rows() -> None:
    tenures = tm_coach.parse_staff_history(
        _fixture("tm_staff_arsenal_caretaker.html.txt"),
        role="caretaker",
        club_tm_id=11,
        club_name="Arsenal FC",
    )
    by_coach = {t.coach_name: t for t in tenures}
    ljungberg = by_coach["Freddie Ljungberg"]
    assert ljungberg.role == "caretaker"
    assert ljungberg.date_from == "2019-11-29"
    assert ljungberg.date_to == "2019-12-21"


def test_upsert_tenures_idempotent_and_extends(db) -> None:
    tenures = tm_coach.parse_staff_history(
        _fixture("tm_staff_arsenal_manager.html.txt"),
        role="manager",
        club_tm_id=11,
        club_name="Arsenal FC",
    )
    first = tm_coach.upsert_tenures(
        db, tenures, observed_at="2026-11-10T00:00:00+00:00"
    )
    second = tm_coach.upsert_tenures(
        db, tenures, observed_at="2026-11-11T00:00:00+00:00"
    )
    assert first == second  # 重跑写入计数稳定（upsert 冲突更新，不翻倍）
    total = db.execute("SELECT COUNT(*) AS n FROM tm_coach_tenures").fetchone()
    assert total["n"] == len(tenures)
    # date_to 现态更新：Arteta 离任后重见同键行 → 右端从 NULL 变日期
    from goalx_backend.data.ingest.tm_coach import TmTenure

    tm_coach.upsert_tenures(
        db,
        [
            TmTenure(
                club_tm_id=11,
                club_name="Arsenal FC",
                role="manager",
                coach_tm_id=47620,
                coach_name="Mikel Arteta",
                date_from="2019-12-22",
                date_to="2026-12-01",
                games=400,
            )
        ],
        observed_at="2026-12-02T00:00:00+00:00",
    )
    row = db.execute(
        "SELECT date_to FROM tm_coach_tenures WHERE coach_name = 'Mikel Arteta'"
    ).fetchone()
    assert row["date_to"] == "2026-12-01"


def _league_response(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "wettbewerb/GB1" in url and "saison_id=2016" in url:
        return httpx.Response(200, text=_fixture("tm_league_pl_2023.html.txt"))
    raise httpx.ConnectError("boom")


def _staff_response(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "verein/281" in url:
        raise httpx.ConnectError("boom")  # 曼城拉取失败（跳过不中断）
    if "personalie_id=1&" in url or url.endswith("personalie_id=1"):
        return httpx.Response(200, text=_fixture("tm_staff_arsenal_manager.html.txt"))
    return httpx.Response(200, text=_fixture("tm_staff_arsenal_caretaker.html.txt"))


def test_sync_tm_coach_end_to_end(db, monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if "/startseite/wettbewerb/" in url:
            return _league_response(request)
        return _staff_response(request)

    # 41 请求 > 30/min 顶帽：no-op sleeper 下真滑窗会空转等真实时钟——
    # 测试放宽顶帽（间距语义在别处断言），只测采集/落库行为
    monkeypatch.setattr(tm_coach, "REQUEST_WINDOW", RateLimitItemPerMinute(10_000))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = tm_coach.sync_tm_coach(
        db,
        Settings(),
        client,
        leagues=("E0",),
        seasons=("2016",),
        sleeper=lambda _s: None,
        now=datetime(2026, 11, 10, tzinfo=UTC),
    )
    assert stats.clubs == 20
    assert any("verein/11" in u for u in calls)
    assert stats.failed_clubs == ["Manchester City"]
    # 覆盖行：联赛×赛季 covered，20 队
    cov = db.execute(
        """
        SELECT match_count, coverage_status FROM source_coverage
        WHERE source = 'transfermarkt' AND league_key = 'GB1'
          AND coverage_date = '2016'
        """
    ).fetchone()
    assert cov["match_count"] == 20
    assert cov["coverage_status"] == "covered"
    # 任职行落库：19 家成功 × (18 manager + 6 caretaker)，曼城失败跳过
    n = db.execute("SELECT COUNT(*) AS n FROM tm_coach_tenures").fetchone()
    assert n["n"] == 19 * 25
    # 俱乐部×赛季成员面
    n = db.execute("SELECT COUNT(*) AS n FROM tm_club_seasons").fetchone()
    assert n["n"] == 20
    # run 行 append-only
    runs = db.execute("SELECT COUNT(*) AS n FROM tm_coach_sync_runs").fetchone()
    assert runs["n"] == 1
    # 幂等重跑：任职/成员行数不变，run 行 +1
    stats2 = tm_coach.sync_tm_coach(
        db,
        Settings(),
        client,
        leagues=("E0",),
        seasons=("2016",),
        sleeper=lambda _s: None,
        now=datetime(2026, 11, 11, tzinfo=UTC),
    )
    assert stats2.tenure_rows == stats.tenure_rows
    n = db.execute("SELECT COUNT(*) AS n FROM tm_coach_tenures").fetchone()
    assert n["n"] == 19 * 25
    runs = db.execute("SELECT COUNT(*) AS n FROM tm_coach_sync_runs").fetchone()
    assert runs["n"] == 2


def test_sync_tm_coach_league_fetch_failed(db) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("site down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stats = tm_coach.sync_tm_coach(
        db,
        Settings(),
        client,
        leagues=("E0",),
        seasons=("2016",),
        sleeper=lambda _s: None,
        now=datetime(2026, 11, 10, tzinfo=UTC),
    )
    assert stats.leagues[0]["status"] == "fetch_failed"
    cov = db.execute(
        "SELECT coverage_status FROM source_coverage WHERE source = 'transfermarkt'"
    ).fetchone()
    assert cov["coverage_status"] == "fetch_failed"
    assert stats.clubs == 0


def test_season_start_year_rolls_in_july() -> None:
    assert tm_coach.season_start_year(date(2026, 6, 30)) == "2025"
    assert tm_coach.season_start_year(date(2026, 7, 1)) == "2026"


def test_default_leagues_cover_corpus_scope() -> None:
    # 11 联赛映射（CorpusScope fdhist 重叠全集），TM 侧代码真实存在
    assert tm_coach.TM_LEAGUES["E0"] == "GB1"
    assert tm_coach.TM_LEAGUES["E1"] == "GB2"
    assert tm_coach.TM_LEAGUES["SC0"] == "SC1"
    assert len(tm_coach.TM_LEAGUES) == 11


def test_backfill_seasons_range() -> None:
    seasons = tm_coach.backfill_seasons(today=date(2026, 11, 10))
    assert seasons[0] == "2016"
    assert seasons[-1] == "2026"
    assert len(seasons) == 11


def test_parse_clubs_tolerates_empty_page(db) -> None:
    assert tm_coach.parse_clubs("<html><body>维护中</body></html>") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("22/12/2019", "2019-12-22"),
        ("", None),
        ("garbage", None),
        ("99/99/2020", None),  # 格式合但假日期——不得产 "2020-99-99" 毒化比较
    ],
)
def test_iso_date_conversion(raw: str, expected: str | None) -> None:
    assert tm_coach._tm_date_to_iso(raw) == expected


def test_rate_window_is_polite() -> None:
    # 礼貌红线：顶帽 ≤30/min 且固定间距 ≥1s（间距主导、窗口只加顶）
    assert tm_coach.REQUEST_WINDOW.amount <= 30
    assert tm_coach.REQUEST_GAP_SECONDS >= 1.0
    assert re.fullmatch(r"\d+", str(tm_coach.REQUEST_WINDOW.amount))
