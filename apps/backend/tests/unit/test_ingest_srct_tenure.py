"""源T 任期/队籍语料采集测试（票 08）：真实捕获样本解析 + 三相位班闭环。

样本取自 2026-11-12 实测裁剪（首日三钉探针，research/08-*）：matchResult
逐季文件 s36 2024-2025、tdl 奈梅亨、教练档案页（迪克·施洛德）、教练履历
jsData（潘塔洛尼 166402）。端点模板一律 `.test` 占位域——真值只进本地
.env（代称红线）；档案页样本内站链已洗为 .test。
"""

from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_tenure


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶：闭环测试请求密集，防封语义另由请求计数断言钉住。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


# —— 真实捕获样本（裁剪贴源；结构完整、长文本段省略） ——

_SAMPLE_LEAGUE_DESC = "<desc 裁剪>"

SAMPLE_LEAGUE_SEASON = (
    "var arrLeague = [36,'英格兰超级联赛','英格蘭超級聯賽','English Premier League',"
    "'2024-2025','#FF3333','league_match/images/164577482086.png',38,38,'英超','英超',"
    f"'ENG PR','{_SAMPLE_LEAGUE_DESC}','38','1'];\n"
    "var arrTeam = [[19,'阿森纳','阿仙奴','Arsenal','','images/19/1kenbrx1hg1z.png',0],"
    "[20,'阿斯顿维拉','阿士東維拉','Aston Villa','','images/20/1kenbrwzj42k.png',0],"
    "[24,'切尔西','車路士','Chelsea','','images/24/1kenbrwz272p.png',0]];\n"
    "var scoreColor=0;\nvar scoreCountType=0;\n"
    "var totalScore = [[0,1,25,3,38,25,9,4,86,41,45,'65.8','23.7','10.5',2.26,1.08,84,0,'',0,0,2,1,2,1,''],"  # noqa: E501 实测样本整行
    "[0,2,19,6,38,20,14,4,69,34,35,'52.6','36.8','10.5',1.82,0.89,74,0,'',0,1,2,1,0,0,'']];\n"
    "var lastUpdateTime = '2026-06-29 15:28:52';\n"
)

SAMPLE_TEAM_DETAIL = (
    "var teamDetail = [239,'奈梅亨','NEC尼美根','NEC Nijmegen',"
    "'images/239/1m0khmz2qb6.png','尼梅根','尼梅根','Nijmegen',"
    "'德格菲特球场','德格菲特球場','Stadion de Goffert','12500','1900-11-15',"
    "'//www.club.test/','','Stadionplein 1, 6532 AJ NIJMEGEN','',25.7,58.23];\n"
    "var rearguard = [['824256','3','桑德勒','辛度爾','Philippe Sandler',0]];\n"
    "var vanguard = [];\nvar goalkeeper = [];\nvar midfielder = [];\n"
    # 钉①：coach 变量 id 不可信（557170 伪值），lineupDetail 主教练行才是真 id
    "var coach = [['557170','','迪克·施洛德','迪克·施洛德','Dick Schreuder',0]];\n"
    "var lineupDetail=[[220708,'','迪克·施洛德','迪克·施洛德','','1971-08-02','','',"
    "'主教练','荷兰','荷蘭','','','0','0','0','0','0'],"
    "[360054,'34 ','凯文·柯尔斯','凱文·柯爾斯','0','2006-01-04','190','70','左边锋',"
    "'荷兰','荷蘭','25','2027-06-30','7','2','8','0','0']];\n"
    "var teamLastUpdateTime = '2026-11-10 09:07:26';\n"
)

# 档案页样本（站链已洗 .test；保留简体名/英文名行 + 履历 jsData 引用 + title）
SAMPLE_COACH_PAGE = """<!DOCTYPE html>
<html xmlns="//www.w3.org/1999/xhtml">
<head>
    <title>迪克&#183;施洛德_迪克&#183;施洛德个人资料-新球体育-球探体育</title>
</head>
<body>
    <script src="/teamdetail-test/tdl239.js?version=2026111208"></script>
    <script src="/coachdata-test/coach220708.js?version=2026111208"></script>
    <table>
            <tr>
              <td><span class="STYLE13">简体名：</span></td>
              <td width="307" bgcolor="#fbfbfb">
                <strong>迪克&#183;施洛德</strong>&nbsp;<a>资料</a>
              </td>
            </tr>
            <tr>
              <td><span class="STYLE13">繁体名：</span></td>
              <td><span class="STYLE13"><strong>迪克&#183;施洛德</strong></span></td>
            </tr>
            <tr>
              <td><span class="STYLE13">英文名：</span></td>
              <td><span class="STYLE13"><strong>Dick Schreuder</strong></span></td>
            </tr>
    </table>
</body>
</html>
"""

SAMPLE_COACH_CAREER = (
    "var onceTeam = '图尔、阿雅克肖、洛里昂';\n"
    "var transferInfo = [['2008-2009','194','194','2009-02-23','2012-06-13','阿雅克肖',"
    "'阿些斯奧','阿雅克肖','阿些斯奧','',1,'Ajaccio','Ajaccio'],"
    "['2013-2014','194','2103','2013-07-01','2014-10-21','阿雅克肖','阿些斯奧','图尔',"
    "'杜亞斯','',1,'Ajaccio','Tours']];\n"
    "var playerIntroduce = '';\n"
    "var nowTeamInfo = [['尼斯','','','主教练','主教練','Coach','Nice']];\n"
    "var countSum = [[0,'全部^全部^ALL',751,278,204,269,0.37,2256,168,910,896,50.332,46.686,1.38,'']];\n"  # noqa: E501 实测样本整行
    "var coachCount = [['2009-02-23','2012-06-13',0,194,'阿雅克肖^阿些斯奧^Ajaccio',"
    "156,53,48,55,0.34,188,40,177,191,44.59,40.54,1.33],"
    "['2013-07-01','2014-10-21',0,2103,'图尔^杜亞斯^Tours',63,23,16,24,0.37,194,14,95,87,43.86,59.65,1.35]];\n"
    "var honorInfo = [['法国乙组联赛冠军^法國乙組聯賽冠軍','24/25']];\n"
    "var lastUpdateTime = '2026-11-12 07:46:33';\n"
)

# 第二档案页样本（锚点教练 96203：站链已洗 .test；引用自己的履历 jsData）
SAMPLE_COACH_PAGE_ALT = (
    SAMPLE_COACH_PAGE.replace(
        "迪克&#183;施洛德_迪克&#183;施洛德个人资料", "埃迪·豪_埃迪·豪个人资料"
    )
    .replace("迪克&#183;施洛德", "埃迪·豪")
    .replace("Dick Schreuder", "Eddie Howe")
    .replace("coach220708.js", "coach96203.js")
)


def _settings(corpus_root: Path) -> Settings:
    return Settings(
        corpus_root=corpus_root,
        srct_league_season_url="https://srct.test/matchres/{season}/s{key}.js",
        srct_team_detail_url="https://srct.test/teamdetail/tdl{id}.js",
        srct_coach_page_url="https://srct.test/coachpage/{team_id}/{cid}.html",
        srct_coach_career_url="https://srct.test/coachdata/coach{cid}.js",
    )


# —— 解析冒烟（真实捕获样本钉形态） ———


def test_parse_league_season_real_shape() -> None:
    payload = srct_tenure.parse_league_season(SAMPLE_LEAGUE_SEASON.encode("utf-8"))
    assert payload["league"] == {
        "league_id": 36,
        "name_cn": "英格兰超级联赛",
        "name_tw": "英格蘭超級聯賽",
        "name_en": "English Premier League",
        "season": "2024-2025",
    }
    teams = payload["teams"]
    assert [t["team_id"] for t in teams] == [19, 20, 24]
    assert teams[0] == {
        "team_id": 19,
        "name_cn": "阿森纳",
        "name_tw": "阿仙奴",
        "name_en": "Arsenal",
    }
    assert payload["table_rows"] == 2
    assert payload["last_update"] == "2026-06-29 15:28:52"


def test_parse_league_season_rejects_stub() -> None:
    """SubLeague 桩页（"页面不存在"HTML）必须判坏体，不进语料。"""
    stub = "<!DOCTYPE html><html><body>对不起！你查看的页面不存在</body></html>"
    with pytest.raises(srct.SrctContentError):
        srct_tenure.parse_league_season(stub.encode("utf-8"))


def test_parse_team_detail_coach_from_lineup_not_coach_var() -> None:
    """钉①裁定：教练真 id 取 lineupDetail 主教练行，coach 变量 id 永不采用。"""
    payload = srct_tenure.parse_team_detail(SAMPLE_TEAM_DETAIL.encode("utf-8"))
    assert payload["team"] == {
        "team_id": 239,
        "name_cn": "奈梅亨",
        "name_tw": "NEC尼美根",
        "name_en": "NEC Nijmegen",
    }
    assert payload["coach"]["coach_id"] == 220708
    assert payload["coach"]["name_cn"] == "迪克·施洛德"
    assert payload["coach"]["role"] == "主教练"
    # coach 变量只留姓名对账，不留 id
    assert payload["coach_var"] == {
        "name_cn": "迪克·施洛德",
        "name_tw": "迪克·施洛德",
        "name_en": "Dick Schreuder",
    }


def test_parse_coach_page_names_and_career_ref() -> None:
    """钉②裁定：档案页 = 中文名+英文名来源 + 履历 jsData 引用（含 version）。"""
    payload = srct_tenure.parse_coach_page(SAMPLE_COACH_PAGE.encode("utf-8"))
    assert payload["name_cn"] == "迪克·施洛德"
    assert payload["name_en"] == "Dick Schreuder"
    assert payload["career_cid"] == "220708"
    assert payload["career_version"] == "2026111208"


def test_parse_coach_career_tenures_real_shape() -> None:
    payload = srct_tenure.parse_coach_career(SAMPLE_COACH_CAREER.encode("utf-8"))
    assert payload["once_team"] == "图尔、阿雅克肖、洛里昂"
    tenures = payload["tenures"]
    assert len(tenures) == 2
    first = tenures[0]
    assert first["start"] == "2009-02-23"
    assert first["end"] == "2012-06-13"
    assert first["team_id"] == 194
    assert first["name_cn"] == "阿雅克肖"
    assert first["name_en"] == "Ajaccio"
    assert (first["matches"], first["wins"], first["draws"], first["losses"]) == (
        156,
        53,
        48,
        55,
    )
    transfers = payload["transfers"]
    assert transfers[1]["to_team_id"] == 2103
    assert transfers[1]["name_en_to"] == "Tours"
    assert payload["now_team"][0][0] == "尼斯"


def test_harvest_coach_anchors_from_raw_detail(tmp_path: Path) -> None:
    """钉③裁定：match_detail 锚点离线重放收获 (cid, team) 种子（零网络）。"""
    detail_dir = tmp_path / "raw" / "srct" / srct.DETAIL_DATASET
    detail_dir.mkdir(parents=True)
    page_a = (
        "<div class='homeN'><a href='//info.test/cn/team/Summary/19.html'>阿森纳</a>"
        "<a class='coach' href='//srct.test/coach/19/96203.html' "
        "title='埃梅里'>(主教练: 埃梅里)</a></div>"
    )
    page_b = (
        "<a class='coach' href='//srct.test/coach/999/11111.html' "
        "title='他队教练'>(主教练: 他队教练)</a>"
    )
    (detail_dir / "100.html.gz").write_bytes(gzip.compress(page_a.encode("utf-8")))
    (detail_dir / "101.html.gz").write_bytes(gzip.compress(page_b.encode("utf-8")))
    anchors = srct_tenure.harvest_coach_anchors(detail_dir)
    assert anchors["96203"]["teams"] == {"19"}
    assert anchors["96203"]["name"] == "埃梅里"
    assert anchors["11111"]["teams"] == {"999"}


# —— 三相位班闭环（MockTransport；sleeper 注入即时） ———


def _routes() -> dict[str, httpx.Response]:
    return {
        "/matchres/2015-2016/s36.js": httpx.Response(
            200, content=SAMPLE_LEAGUE_SEASON.encode("utf-8")
        ),
        "/teamdetail/tdl19.js": httpx.Response(
            200, content=SAMPLE_TEAM_DETAIL.encode("utf-8")
        ),
        "/teamdetail/tdl20.js": httpx.Response(
            200, content=SAMPLE_TEAM_DETAIL.encode("utf-8")
        ),
        "/teamdetail/tdl24.js": httpx.Response(
            200, content=SAMPLE_TEAM_DETAIL.encode("utf-8")
        ),
        "/coachpage/19/96203.html": httpx.Response(
            200, content=SAMPLE_COACH_PAGE_ALT.encode("utf-8")
        ),
        "/coachdata/coach96203.js": httpx.Response(
            200, content=SAMPLE_COACH_CAREER.encode("utf-8")
        ),
        "/coachpage/19/220708.html": httpx.Response(
            200, content=SAMPLE_COACH_PAGE.encode("utf-8")
        ),
        "/coachdata/coach220708.js": httpx.Response(
            200, content=SAMPLE_COACH_CAREER.encode("utf-8")
        ),
        "/coachdata/coach404.js": httpx.Response(404, text="missing"),
        "/coachpage/24/404.html": httpx.Response(404, text="missing"),
    }


def _client(
    seen: list[httpx.Request], routes: dict[str, httpx.Response]
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(500, text=f"unrouted {request.url.path}")
        return route

    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_detail_page(store: CorpusStore) -> None:
    page = (
        "<div class='homeN'>"
        "<a class='coach' href='//srct.test/coach/19/96203.html' "
        "title='埃梅里'>(主教练: 埃梅里)</a></div>"
    )
    store.ingest_raw(
        srct.SRCT_PROVIDER,
        srct.DETAIL_DATASET,
        "100",
        page.encode("utf-8"),
        ext=".html",
    )


_FIXED_NOW = datetime(2026, 11, 12, 1, 30, tzinfo=UTC)


def _run(
    tmp_path: Path,
    seen: list[httpx.Request],
    routes: dict[str, httpx.Response],
    **kwargs: Any,
) -> tuple[srct_tenure.TenureStats, CorpusStore]:
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("enforce_window", False)
    kwargs.setdefault("now_fn", lambda: _FIXED_NOW)
    store = CorpusStore(_settings(tmp_path / "corpus").corpus_root)
    _seed_detail_page(store)
    stats = srct_tenure.run_tenure_shift(
        store,
        _settings(tmp_path / "corpus"),
        _client(seen, routes),
        seasons=("2015-2016",),
        leagues=(("英超", "36"),),
        **kwargs,
    )
    return stats, store


def test_tenure_shift_three_phase_loop(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    stats, store = _run(tmp_path, seen, _routes())

    # 相位 A：1 个 matchResult 文件
    assert stats.league_new == 1
    # 相位 B：arrTeam 三队 tdl
    assert stats.team_new == 3
    # 相位 C：锚点教练 96203（GB 队 19）+ tdl 现任 220708 两步采集
    assert stats.coach_page_new == 2
    assert stats.career_new == 2
    assert stats.requests == 1 + 3 + 2 + 2

    # raw 落盘（四数据集，srct provider）
    assert store.has(srct.SRCT_PROVIDER, "league_season", "2015-2016-s36")
    assert store.has(srct.SRCT_PROVIDER, "team_detail", "19")
    assert store.has(srct.SRCT_PROVIDER, "coach_page", "96203@19")
    assert store.has(srct.SRCT_PROVIDER, "coach_career", "96203")

    # bronze 信封行 + 解析形态
    career_rows = [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, "coach_career")
        if row["sid"] == "96203"
    ]
    assert career_rows[0]["parser_version"] == "srct_coach_career_v1"
    page_rows = [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, "coach_page")
        if row["sid"] == "96203@19"
    ]
    assert page_rows[0]["parser_version"] == "srct_coach_page_v2"
    # 96203 的档案页样本=埃迪·豪（简体名行无 <span> 包裹的真实形态）
    assert page_rows[0]["payload"]["name_cn"] == "埃迪·豪"
    assert page_rows[0]["payload"]["name_en"] == "Eddie Howe"
    assert career_rows[0]["payload"]["tenures"][0]["team_id"] == 194
    team_rows = [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, "team_detail")
        if row["sid"] == "19"
    ]
    # raw 键=请求队 id；样本体是奈梅亨（team_id 239，教练 220708）
    assert team_rows[0]["payload"]["team"]["team_id"] == 239
    assert team_rows[0]["payload"]["coach"]["coach_id"] == 220708

    # 两步法：履历请求必须带档案页引用的原样 version 参数
    career_reqs = [
        r
        for r in seen
        if r.url.path in ("/coachdata/coach220708.js", "/coachdata/coach96203.js")
    ]
    assert len(career_reqs) == 2
    assert all(r.url.params.get("version") == "2026111208" for r in career_reqs)
    # 逐季/tdl 请求带日级 version（jsData 族缓存参数，缺则边缘偶发 404）
    league_req = next(r for r in seen if r.url.path == "/matchres/2015-2016/s36.js")
    assert league_req.url.params.get("version") == "20261112"
    tdl_req = next(r for r in seen if r.url.path == "/teamdetail/tdl19.js")
    assert tdl_req.url.params.get("version") == "20261112"


def test_tenure_shift_idempotent_second_run(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    _run(tmp_path, seen, _routes())
    seen.clear()
    stats, _store = _run(tmp_path, seen, _routes())
    assert stats.requests == 0
    assert stats.career_new == 0
    assert stats.team_new == 0


def test_tenure_shift_coach_not_found_tolerated(tmp_path: Path) -> None:
    """锚点指向已死 id（档案页 404）：计数留痕不炸班，raw 不占键。"""
    store_dir = tmp_path / "corpus2"
    detail_dir = store_dir / "raw" / "srct" / srct.DETAIL_DATASET
    detail_dir.mkdir(parents=True)
    page = (
        "<a class='coach' href='//srct.test/coach/24/404.html' "
        "title='死档'>(主教练: 死档)</a>"
    )
    (detail_dir / "200.html.gz").write_bytes(gzip.compress(page.encode("utf-8")))
    seen: list[httpx.Request] = []
    store = CorpusStore(store_dir)
    stats = srct_tenure.run_tenure_shift(
        store,
        _settings(store_dir),
        _client(seen, _routes()),
        seasons=("2015-2016",),
        leagues=(("英超", "36"),),
        sleeper=lambda _s: None,
        enforce_window=False,
    )
    assert stats.coaches_not_found == 1
    assert "404" in stats.failed["coach:404"]
    assert not store.has(srct.SRCT_PROVIDER, "coach_career", "404")


def test_tenure_shift_parser_bump_self_heals_from_raw(tmp_path: Path) -> None:
    """解析器升版：raw 在缓存、bronze 缺新版行 → 零网络重解析回补。"""
    seen: list[httpx.Request] = []
    _run(tmp_path, seen, _routes())
    seen.clear()
    bumped = {**srct_tenure.TENURE_BRONZE_VERSIONS, "coach_page": "srct_coach_page_v9"}
    monkey_version = bumped
    # 快照副本：finally 从 original 恢复——引用同 dict 会随 clear 一并清空，
    # TENURE_BRONZE_VERSIONS 被永久置空（xdist 同 worker 后续测试连锁 KeyError）
    original = dict(srct_tenure.TENURE_BRONZE_VERSIONS)
    srct_tenure.TENURE_BRONZE_VERSIONS.clear()
    srct_tenure.TENURE_BRONZE_VERSIONS.update(monkey_version)
    try:
        stats, store = _run(tmp_path, seen, _routes())
    finally:
        srct_tenure.TENURE_BRONZE_VERSIONS.clear()
        srct_tenure.TENURE_BRONZE_VERSIONS.update(original)
    assert stats.requests == 0  # 全缓存零网络
    assert stats.bronze_repaired == 2  # 两页 coach_page 重解析
    rows = [
        row
        for row in store.read_bronze(srct.SRCT_PROVIDER, "coach_page")
        if row["parser_version"] == "srct_coach_page_v9"
    ]
    assert {row["sid"] for row in rows} == {"96203@19", "220708@19"}
