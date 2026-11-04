"""corpus 主链 e2e 夹具测试（boundary-hardening 票 06）。

生成器单测=夹具自身的验收:corpus 物化可读、四场制式语义成立、
资格判定 valid、今日页共识面按 sid 链接分叉（corpus/legacy/空）。
playwright 侧跑同一生成器（CLI e2e-corpus）,口径单一。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from goalx_backend.config import Settings
from goalx_backend.data import corpus_duckdb, mapping, quote_evidence
from goalx_backend.data.ingest import e2e_corpus
from goalx_backend.data.today import build_today_view
from goalx_backend.db import connect, migrate


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（noop sleeper 下滑窗顶会忙转到真实时间翻窗）。"""
    from limits import RateLimitItemPerMinute

    from goalx_backend.data.ingest import srct

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


def _fixture_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
    )


@pytest.fixture
def built(tmp_path: Path) -> dict[str, object]:
    """真实 now 为 base:corpus 行 observed=collect 墙钟,须与 base 同拍
    (60min 观察龄门);断言全部结构性(场数/书数/场景),不依赖钟面值。"""
    conn = connect(tmp_path / "goalx.db")
    try:
        migrate(conn)
    finally:
        conn.close()
    settings = _fixture_settings(tmp_path)
    manifest = e2e_corpus.build(settings)
    return {"settings": settings, "manifest": manifest}


def test_corpus_silver_readback_via_duckdb(built) -> None:
    """物化链可读:corpus.duckdb 视图按 sid 出三书 as-of 最新三向。"""
    settings: Settings = built["settings"]  # type: ignore[assignment]
    manifest: dict = built["manifest"]  # type: ignore[assignment]
    base = manifest["base_utc"]
    with corpus_duckdb.reader(settings) as duck_con:
        assert duck_con is not None
        rows = quote_evidence.srct_snapshot_rows_asof(
            duck_con, e2e_corpus.SID_MAIN, base
        )
    books = {str(r["source"]) for r in rows}
    # 三书全部入场:sharp(富轨迹)/lottery/late(晚到行,observed≤as_of 准入)
    assert books == {"srct:1x2:9001", "srct:1x2:1129", "srct:1x2:7007"}


def test_running_db_scenarios_and_adjudication(built) -> None:
    """四场制式:S1 valid 三书;S2 改期仍挂共识;S3 无欧面;S4 legacy 双书。"""
    settings: Settings = built["settings"]  # type: ignore[assignment]
    manifest: dict = built["manifest"]  # type: ignore[assignment]
    by_scenario = {f["scenario"]: f for f in manifest["fixtures"]}
    conn = connect(settings.db_path)
    try:
        moment = manifest["base_utc"]
        with corpus_duckdb.reader(settings) as duck_con:
            # S1 主链:JC 新鲜+三书 corpus → valid 且单固
            s1 = int(by_scenario["mainline"]["fixture_id"])
            verdict = quote_evidence.adjudicate_had_quote(
                conn,
                s1,
                moment,
                duck_con=duck_con,
                sid=mapping.srct_sid_for_fixture(conn, s1),
            )
            assert verdict.status == "valid", verdict.reasons
            assert verdict.single_eligible is True
            assert verdict.eu_books == 3
            # S3 缺源:无 sid 无欧赔 → 无三向(books=0),但 JC 价仍可读
            s3 = int(by_scenario["no_source"]["fixture_id"])
            s3_verdict = quote_evidence.adjudicate_had_quote(conn, s3, moment)
            assert s3_verdict.eu_books == 0
    finally:
        conn.close()


def test_today_view_splits_corpus_and_legacy_faces(built) -> None:
    """今日页四行:S1 三书 corpus 共识;S2 改期单书;S3 空面;S4 legacy 两书。"""
    settings: Settings = built["settings"]  # type: ignore[assignment]
    manifest: dict = built["manifest"]  # type: ignore[assignment]
    by_scenario = {f["scenario"]: f for f in manifest["fixtures"]}
    conn = connect(settings.db_path)
    try:
        with corpus_duckdb.reader(settings) as duck_con:
            rows = build_today_view(
                conn, str(manifest["day"]), days=3, duck_con=duck_con
            )
    finally:
        conn.close()
    books_by_code = {
        r.match_code: r.books
        for r in rows  # type: ignore[attr-defined]
    }
    assert books_by_code[by_scenario["mainline"]["code"]] == 3
    assert books_by_code[by_scenario["rescheduled"]["code"]] == 1
    assert books_by_code[by_scenario["no_source"]["code"]] == 0
    assert books_by_code[by_scenario["legacy_eu"]["code"]] == 2
    # corpus 正例行的共识概率存在(非 legacy 行——S1 无任何 legacy 欧赔行)
    mainline = next(r for r in rows if r.match_code == by_scenario["mainline"]["code"])
    assert mainline.eu_prob is not None  # type: ignore[attr-defined]


def test_isolated_db_guard(tmp_path: Path) -> None:
    """红线:库中存在真实来源竞彩行时拒绝生成(NULL 旧行同拒,correctness F3)。"""
    from goalx_backend.data import fixtures as fx_store
    from goalx_backend.models import MatchCodeInput, Tier

    conn = connect(tmp_path / "guard.db")
    try:
        migrate(conn)
        e2e_corpus.ensure_isolated_db(conn)  # 空库通过
        competition = fx_store.upsert_competition(conn, "甲", tier=Tier.TIER1)
        home = fx_store.upsert_team(conn, "主A")
        away = fx_store.upsert_team(conn, "客B")
        fixture = fx_store.upsert_fixture(
            conn, competition, "2026-01-01T20:00:00+00:00", home, away
        )
        # NULL 来源旧行(真实采集早期)不逃守卫
        conn.execute(
            "INSERT INTO match_codes (fixture_id, kind, business_date, code,"
            " source_match_id, is_single)"
            " VALUES (?, 'jingcai', '2026-01-01', '周二001', NULL, 1)",
            (fixture,),
        )
        with pytest.raises(RuntimeError, match="真实竞彩数据"):
            e2e_corpus.ensure_isolated_db(conn)
        conn.execute("DELETE FROM match_codes")
        fx_store.upsert_match_code(
            conn,
            MatchCodeInput(
                fixture_id=fixture,
                kind="jingcai",
                business_date="2026-01-01",
                code="周二001",
                source_match_id="real-source",
                is_single=True,
            ),
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="真实竞彩数据"):
            e2e_corpus.ensure_isolated_db(conn)
    finally:
        conn.close()


def test_require_isolated_paths_guard(tmp_path: Path) -> None:
    """裸跑守卫:缺省 db_path/corpus_root(主库/真实语料树)拒绝,显式隔离路径放行。

    构造一律 model_construct（跳过 env/.env，纯字段缺省）——与守卫自身
    基线同法；conftest 的测试隔离 env 钉（票 10①）不参与本判定。"""
    from goalx_backend.config import Settings

    with pytest.raises(RuntimeError, match="db_path"):
        e2e_corpus.require_isolated_paths(Settings.model_construct())
    only_db = Settings.model_construct(db_path=tmp_path / "iso.db")  # corpus_root 缺省
    with pytest.raises(RuntimeError, match="corpus_root"):
        e2e_corpus.require_isolated_paths(only_db)
    isolated = Settings.model_construct(
        db_path=tmp_path / "iso.db", corpus_root=tmp_path / "tree"
    )
    e2e_corpus.require_isolated_paths(isolated)  # 双显式隔离路径通过
