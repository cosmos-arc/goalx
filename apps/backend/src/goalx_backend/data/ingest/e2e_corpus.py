"""
e2e 主链标准语料夹具（boundary-hardening 票 06）。

四场制式令 e2e 真正经过 corpus 主链（源T 页→bronze→silver 物化→
corpus.duckdb→sid 映射→欧共识→报价资格→纸面锁定），替代演示数据降级
路径；单测与 playwright 共用同一生成器（CLI ``e2e-corpus``）。

- S1 主链正例（sid 链接 + 三书 corpus 价）：轨迹含同发布时刻不同记录、
  A→B→A 回摆、晚到行（发布早观测晚——admission 以 observed_at 为准）；
  JC 价新鲜 → 今日页共识 + 纸面锁定 + 价变 409 全链可跑。
- S2 改期：corpus 日页摄于改期前（开球=今晚），运行面 fixture 已改明
  晚——sid 链接按 id 不按日，共识仍挂上。
- S3 缺源：无 sid 无任何欧赔（JC 有价）→ 欧共识面诚实为空。
- S4 两代数据源：无 sid，legacy odds_snapshots 欧赔行 → legacy 读径与
  S1 的 corpus 读径同页共存。

页时间相对 ``base``（北京钟面分钟粒度，与真实源同精度）；collect 单拍
浅深（日页+1x2 轨迹）。重跑语义：语料树侧幂等（ingest/silver 重建）；
运行面侧为**增量再 seed**——kickoff=当次 base+3h，重跑新建当班四场、
旧场留库成历史行（不清理）；e2e 每轮由 global-setup 清库重建，单测用
tmp 目录，二者均一轮一库。
cid/书名全假占位（代称红线，同 test_ingest_srct_odds 先例）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, mapping
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds
from goalx_backend.db import connect
from goalx_backend.models import MatchCodeInput, SaleStatusInput, SnapshotInput, Tier

BEIJING = ZoneInfo("Asia/Shanghai")
# sid 域（源T）与书商 cid 全假占位
SID_MAIN = "91001"
SID_MOVED = "91002"
JC_HAD = {"h": 2.05, "d": 3.40, "a": 3.60}
LEGACY_BOOKS = (
    "odds_api:testa",
    "odds_api:testb",
)  # legacy 读径只认 odds_api:% 命名空间(代称红线:占位名)
LEGACY_HAD = {"h": 2.10, "d": 3.30, "a": 3.55}
MANIFEST_NAME = "e2e-corpus-manifest.json"
# 运行面竞彩行来源 tag（隔离守卫的合法痕迹集=demo + 本前缀）
FIXTURE_SOURCE_PREFIX = "e2e-corpus-"


def require_isolated_paths(settings: Settings) -> None:
    """
    裸跑守卫（correctness F1）：db_path/corpus_root 任一为缺省值即拒。

    空主库过得了 ``ensure_isolated_db``（无真实行）,但缺省 corpus_root
    就是真实语料树 ~/goalx-data——夹具页/物化会污染生产数据。要求两个
    路径都显式指向隔离位置（env 注入）。
    """
    # model_construct 跳过 env/.env 加载,拿到纯字段缺省值——对比基准不能
    # 随进程 env 漂移(否则显式注入的隔离路径会被误判成缺省)
    defaults = Settings.model_construct()
    if settings.db_path == defaults.db_path:
        raise RuntimeError(
            "e2e-corpus 拒绝缺省 db_path(主库): 须显式 GOALX_DB_PATH 指向隔离库"
        )
    if settings.corpus_root == defaults.corpus_root:
        raise RuntimeError(
            "e2e-corpus 拒绝缺省 corpus_root(真实语料树): 须显式 GOALX_CORPUS_ROOT"
        )


def ensure_isolated_db(conn: sqlite3.Connection) -> None:
    """
    同 seed-demo 红线（票 36）：只写隔离/夹具库，拒绝主库伪造实采。

    合法痕迹=demo tag 或本夹具前缀；出现其他来源（真实采集,含 NULL
    来源旧行——COALESCE 口径与 demo 守卫同严）即拒绝。
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM match_codes
        WHERE kind = 'jingcai'
          AND COALESCE(source_match_id, '') != 'demo'
          AND COALESCE(source_match_id, '') NOT LIKE ?
        """,
        (f"{FIXTURE_SOURCE_PREFIX}%",),
    ).fetchone()
    if row is not None and int(row["n"]) > 0:
        raise RuntimeError("e2e-corpus 只写隔离/夹具库: 目标库已有真实竞彩数据")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _day_page(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    """
    日页：行=(联赛, label, 主队, 客队, sid)，比分列形态同既有合成页。

    行内标签必须紧凑单行（解析器按行正则,显式 ``+`` 拼接避免 pyright
    隐式串接禁令——拆行会破坏行形态,票 06 实测)。
    """
    trs = "".join(
        "<tr height=18 align=center>"
        + f"<td><span>{league}</span></td><td>{label}</td><td class=style1>完</td>"
        + f"<td align=right>{home}</td>"
        + "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        + f"<td align=left>{away}</td>"
        + f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
        for league, label, home, away, sid in rows
    )
    return f"<html><body><table>{trs}</table></body></html>".encode("gb18030")


def _game_row(
    cid: str, gid: str, name_en: str, name_zh: str, kickoff_bj: datetime
) -> str:
    """1x2 game 行（形态对齐 test_ingest_srct_odds 合成页）。"""
    ko = f"{kickoff_bj:%Y,%-m-%-d,%-H,%-M,0,00}"
    return (
        f"{cid}|{gid}|{name_en}|1.85|3.60|4.20|55|27|18|1.85|3.60|4.20|55|27|18|"
        f"0.92|0.90|0.90|{ko}|{name_zh}|0|0|0.85|0.90|1.05"
    )


def _detail_row(gid: str, h: str, d: str, a: str, published_bj: datetime) -> str:
    when = published_bj.strftime("%m-%d %H:%M")
    year = published_bj.strftime("%Y")
    return f"{gid}^{h}|{d}|{a}|{when}|0.90|0.90|0.90|{year};"


def _odds_js(games: list[str], details: list[str]) -> bytes:
    return (
        'var matchname_cn="英超";\ngame=Array(\n"'
        + '",\n"'.join(games)
        + '");\ngameDetail=Array(\n'
        + ",\n".join(f'"{d}"' for d in details)
        + ");\n"
    ).encode()


def _routes(kickoff_bj: datetime, base_bj: datetime) -> dict[str, httpx.Response]:
    """浅深路由：日页一页两场 + 每场一 odds 页（浅深不触其余端点）。"""

    def minutes_ago(mins: int) -> datetime:
        return base_bj - timedelta(minutes=mins)

    label = f"{kickoff_bj:%-d日%H:%M}"
    day_page = _day_page(
        [
            ("英超", label, "主队甲", "客队乙", SID_MAIN),
            ("英超", label, "主队丙", "客队丁", SID_MOVED),
        ]
    )
    # S1 三书：sharp（富轨迹）/lottery（单行）/late（晚到行,发布 m-5）
    main_games = [
        _game_row("9001", "719001", "TestSharp", "测试甲", kickoff_bj),
        _game_row("1129", "711129", "TestLottery", "测试官", kickoff_bj),
        _game_row("7007", "717007", "TestLate", "测试迟", kickoff_bj),
    ]
    sharp_details = [
        # 新→旧严格降序：最新 m-2（临场价,与 JC 价同窗）；同分钟异价对 m-4
        # （同发布时刻不同记录）；A→B→A 回摆段按时间旧→新读 m-10(A)→
        # m-8(B)→m-6(A)
        _detail_row("719001", "1.75", "3.70", "4.40", minutes_ago(2)),
        _detail_row("719001", "1.80", "3.65", "4.30", minutes_ago(4)),
        _detail_row("719001", "1.78", "3.68", "4.35", minutes_ago(4)),
        _detail_row("719001", "1.76", "3.72", "4.45", minutes_ago(6)),
        _detail_row("719001", "1.82", "3.62", "4.25", minutes_ago(8)),
        _detail_row("719001", "1.76", "3.72", "4.45", minutes_ago(10)),
    ]
    lottery_details = [
        _detail_row("711129", "2.02", "3.30", "3.75", minutes_ago(2)),
    ]
    late_details = [
        # 晚到书：发布 m-5，本拍才被观测（observed=collect 时刻）
        _detail_row("717007", "1.95", "3.45", "4.00", minutes_ago(5)),
    ]
    main_odds = _odds_js(main_games, sharp_details + lottery_details + late_details)
    moved_odds = _odds_js(
        [_game_row("9001", "719002", "TestSharp", "测试甲", kickoff_bj)],
        [_detail_row("719002", "1.90", "3.50", "4.05", minutes_ago(2))],
    )
    return {
        f"day:{kickoff_bj:%Y%m%d}": httpx.Response(200, content=day_page),
        f"odds:{SID_MAIN}": httpx.Response(200, content=main_odds),
        f"odds:{SID_MOVED}": httpx.Response(200, content=moved_odds),
    }


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/over/"):
            key = f"day:{path.removeprefix('/over/').removesuffix('.htm')}"
        else:
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        if key not in routes:
            return httpx.Response(500, text="boom")
        return routes[key]

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings_with_endpoints(settings: Settings) -> Settings:
    """夹具端点模板（.test 占位域，同 test_ingest_srct_odds 先例）。"""
    return settings.model_copy(
        update={
            "srct_day_url": "https://srct.test/over/{date}.htm",
            "srct_odds_url": "https://srct.test/odds/{sid}.js",
            "srct_odds_referer": "https://srct.test/oddslist/{sid}.htm",
            "srct_asianodds_url": "https://srct.test/asian/{sid}",
            "srct_overdown_url": "https://srct.test/overdown/{sid}",
            "srct_detail_url": "https://srct.test/detail/{sid}cn.htm",
            "srct_analysis_url": "https://srct.test/analysis/{sid}cn.htm",
            "srct_stats_url": "https://srct.test/shijian/{sid}.htm",
        }
    )


def build_corpus(settings: Settings, *, base: datetime | None = None) -> dict[str, Any]:
    """
    生成 corpus 侧：浅深单拍 → bookmakers+odds silver → duckdb 视图。

    幂等（ingest 与 silver 重建均幂等）；返回 corpus manifest 片段。
    """
    moment = base or datetime.now(UTC)
    base_bj = moment.astimezone(BEIJING)
    kickoff_bj = base_bj + timedelta(hours=3)
    gen_settings = _settings_with_endpoints(settings)
    store = CorpusStore(gen_settings.corpus_root)
    try:
        srct.collect_day(
            store,
            gen_settings,
            _client(_routes(kickoff_bj, base_bj)),
            date=f"{kickoff_bj:%Y-%m-%d}",
            sleeper=lambda _s: None,
            depth=srct.DEPTH_SHALLOW,
        )
        srct_odds.build_bookmakers(store)
        srct_odds.build_odds_change_events(store)
        corpus_duckdb.build_corpus_duckdb(store)
    finally:
        store.close()
    return {
        "corpus_root": str(gen_settings.corpus_root),
        "day": f"{kickoff_bj:%Y-%m-%d}",
        "sids": [SID_MAIN, SID_MOVED],
        "kickoff_utc": _iso(kickoff_bj),
        "base_utc": _iso(moment),
    }


def seed_running_db(
    settings: Settings, *, base: datetime | None = None
) -> dict[str, Any]:
    """
    运行面四场（JC 价 + 售卖态 + sid 链接 + legacy 欧赔）。

    S1 今晚开球（链接 91001）；S2 明晚开球（链接 91002，corpus 日页摄
    于改期前）；S3 今晚（无链接无欧赔）；S4 今晚（无链接，legacy 双书）。
    """
    moment = base or datetime.now(UTC)
    base_bj = moment.astimezone(BEIJING)
    kickoff_bj = base_bj + timedelta(hours=3)
    # JC 价观察偏移取小值:报价资格 freshness=300s,e2e 在生成后数分钟内跑
    observed = _iso(moment - timedelta(seconds=15))
    conn = connect(settings.db_path)
    fixtures_meta: list[dict[str, Any]] = []
    try:
        competition = fx_store.upsert_competition(
            conn, "英超", tier=Tier.TIER1, odds_api_sport_key="soccer_epl"
        )
        spec = [
            ("001", "主队甲", "客队乙", kickoff_bj, SID_MAIN, "mainline"),
            (
                "002",
                "主队丙",
                "客队丁",
                kickoff_bj + timedelta(days=1),
                SID_MOVED,
                "rescheduled",
            ),
            ("003", "主队戊", "客队己", kickoff_bj, None, "no_source"),
            ("004", "主队庚", "客队辛", kickoff_bj, None, "legacy_eu"),
        ]
        for code, home, away, ko_bj, sid, scenario in spec:
            home_id = fx_store.upsert_team(conn, home)
            away_id = fx_store.upsert_team(conn, away)
            ko_utc = ko_bj.astimezone(UTC)
            fixture_id = fx_store.upsert_fixture(
                conn, competition, _iso(ko_utc), home_id, away_id
            )
            weekday = "一二三四五六日"[ko_bj.weekday()]
            full_code = f"周{weekday}{code}"
            fx_store.upsert_match_code(
                conn,
                MatchCodeInput(
                    fixture_id=fixture_id,
                    kind="jingcai",
                    business_date=f"{ko_bj:%Y-%m-%d}",
                    code=full_code,
                    source_match_id=f"e2e-corpus-{code}",
                    is_single=True,
                ),
            )
            for selection, odds in JC_HAD.items():
                fx_store.insert_odds_snapshot(
                    conn,
                    SnapshotInput(
                        fixture_id=fixture_id,
                        market_code="had",
                        selection_code=selection,
                        source="sporttery",
                        odds=odds,
                        captured_at=observed,
                        observed_at=observed,
                        source_updated_at=observed,
                    ),
                )
            fx_store.append_sale_status(
                conn,
                SaleStatusInput(
                    fixture_id=fixture_id,
                    market_code="had",
                    sale_state="on_sale",
                    single_eligible=True,
                    observed_at=observed,
                ),
            )
            if sid is not None:
                mapping.link_fixture_sid(conn, fixture_id, sid, method="manual")
            fixtures_meta.append(
                {
                    "code": full_code,
                    "fixture_id": fixture_id,
                    "sid": sid,
                    "kickoff_utc": _iso(ko_utc),
                    "scenario": scenario,
                }
            )
        # S4 legacy 欧赔（两代数据源：无 sid → 读径走 odds_snapshots）
        legacy = next(f for f in fixtures_meta if f["scenario"] == "legacy_eu")
        for book in LEGACY_BOOKS:
            for selection, odds in LEGACY_HAD.items():
                fx_store.insert_odds_snapshot(
                    conn,
                    SnapshotInput(
                        fixture_id=int(legacy["fixture_id"]),
                        market_code="had",
                        selection_code=selection,
                        source=book,
                        odds=odds,
                        captured_at=observed,
                        observed_at=observed,
                        source_updated_at=observed,
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return {
        "business_date_today": f"{kickoff_bj:%Y-%m-%d}",
        "fixtures": fixtures_meta,
        "jc_had": JC_HAD,
    }


def build(settings: Settings, *, base: datetime | None = None) -> dict[str, Any]:
    """全量生成（corpus + 运行面），manifest 落 db 同目录并返回。"""
    moment = base or datetime.now(UTC)
    manifest: dict[str, Any] = {**build_corpus(settings, base=moment)}
    manifest.update(seed_running_db(settings, base=moment))
    manifest["db_path"] = str(settings.db_path)
    manifest_path = Path(settings.db_path).parent / MANIFEST_NAME
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["manifest_path"] = str(manifest_path)
    return manifest
