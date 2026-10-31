"""源T silver 增量物化测试（票 11）。

合成语料（结构对齐实测裁剪样本，cid/书名全假——代称红线）经真实
collect_day 攒 raw+bronze，再走 run_remat 全链。核心断言面：

- 增量≡全量的字节级契约：增量触及分区重写后，随后的全量重建对**每个**
  分区产出逐字节相同的 parquet（增量只做全量切片，非合并旧面）；
- 幂等：零新工件秒级 skip、silver 树 mtime 零改写；未触及分区不动；
- 新日页解锁孤儿（_unknown→真实分区）成对重写；
- 护栏：无状态行/bronze 计数超水位加账 → 全量兜底；
- 拍键（sid@open）入账、水位推进只随整轮成功。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest

# 测试经 monkeypatch 放宽滑窗（同 odds 测试惯例；避免忙转到真实时间翻窗）
from goalx_backend.config import Settings
from goalx_backend.data import silver
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import srct, srct_odds, srct_remat, srct_silver


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """放宽滑窗硬顶（noop sleeper 下会忙转到真实时间翻窗）+ 余量探测
    钉高（票 13：护栏全量测试不受宿主机真实内存影响）。"""
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "_REQUEST_WINDOW", RateLimitItemPerMinute(10**6))
    monkeypatch.setattr(srct_remat, "_headroom_bytes", lambda: 1 << 40)


_ODDS_JS = (
    'var matchname_cn="英超";\n'
    'game=Array(\n"'
    "9001|700001|TestSharp|1.30|5.50|8.50|71|17|12|93|1.30|5.50|8.50|72|17|11|94|"
    '0.93|0.97|0.84|2025,10-1,19,0,0,00|测试甲*|1|0|0.85|0.95|1.11",\n"'
    "1129|700002|TestLottery|3.20|3.40|2.10|27|26|47|88|2.90|3.10|2.20|30|27|43|88|"
    '0.85|0.85|0.93|2025,10-1,19,0,0,00|测试官*|1|0|0.8|0.9|1.0");\n'
    'gameDetail=Array(\n"'
    "700001^1.40|5.00|8.00|10-01 13:00|0.92|0.90|0.90|2025;"
    '1.30|5.50|8.50|10-01 10:00|0.93|0.97|0.84|2025;",\n"'
    '700002^3.10|3.30|2.20|10-01 12:00|0.85|0.85|0.93|2025;");\n'
).encode()

# 拍快照变体：同 sid 后到轨迹（值组不同——ledger 后行胜出面）
_ODDS_JS_BEAT = (
    'var matchname_cn="英超";\n'
    'game=Array(\n"'
    "9001|700001|TestSharp|1.30|5.50|8.50|71|17|12|93|1.30|5.50|8.50|72|17|11|94|"
    '0.93|0.97|0.84|2025,10-1,19,0,0,00|测试甲*|1|0|0.85|0.95|1.11");\n'
    'gameDetail=Array(\n"'
    '700001^1.44|4.90|8.10|10-01 14:00|0.91|0.91|0.91|2025;");\n'
).encode()

ASIANODDS_HTML = (
    "<html><head><title>甲VS乙-亚指指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商8 封</td><td></td>"
    "<td>0.90</td><td>受让半球</td><td>0.95</td>"
    "<td>2.65</td><td>平手/半球</td><td>0.27</td>"
    "<td>0.80</td><td>受让平手/半球</td><td>1.05</td>"
    "<td><a href=/changeDetail/handicap.aspx?id=1&companyID=8>详</a></td>"
    "</tr></table></body></html>"
).encode()
OVERDOWN_HTML = (
    "<html><head><title>甲VS乙-大小指数-新球体育</title></head><body><table>"
    "<tr><td></td><td>书商1 封</td><td></td>"
    "<td>0.93</td><td>2.5/3</td><td>0.87</td>"
    "<td>1.25</td><td>2.5</td><td>0.50</td>"
    "<td>0.80</td><td>2.5</td><td>1.00</td>"
    "<td><a href=/changeDetail/overunder.aspx?id=1&companyID=1>详</a></td>"
    "</tr></table></body></html>"
).encode()
STATS_HTML = (
    '<html><body><script>var jsonData = {"techStat":{"itemList":['
    '{"home":{"value":0.71},"away":{"value":1.68},"name":"预期进球",'
    '"kind":"EXPECTED_GOALS"}]},"info":{}};</script></body></html>'
).encode()


def _day_page(sid: str, league: str, label: str) -> bytes:
    row = (
        "<tr height=18 align=center>"
        f"<td><span>{league}</span></td><td>{label}</td><td class=style1>完</td>"
        "<td align=right>主队甲</td>"
        "<td class=style1><font color=blue>1</font>-<font color=red>2</font></td>"
        "<td align=left>客队乙</td>"
        f"<td><a onclick='analysis({sid})'>析</a></td></tr>"
    )
    return f"<html><body><table>{row}</table></body></html>".encode("gb18030")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        db_path=tmp_path / "goalx.db",
        srct_day_url="https://srct.test/over/{date}.htm",
        srct_odds_url="https://srct.test/odds/{sid}.js",
        srct_odds_referer="https://srct.test/oddslist/{sid}.htm",
        srct_asianodds_url="https://srct.test/asian/{sid}",
        srct_overdown_url="https://srct.test/overdown/{sid}",
        srct_detail_url="https://srct.test/detail/{sid}cn.htm",
        srct_analysis_url="https://srct.test/analysis/{sid}cn.htm",
        srct_stats_url="https://srct.test/shijian/{sid}.htm",
    )


def _routes_for(day: str, sid: str, league: str) -> dict[str, httpx.Response]:
    label = f"{int(day[-2:])}日20:00"
    return {
        f"day:{day.replace('-', '')}": httpx.Response(
            200, content=_day_page(sid, league, label)
        ),
        f"odds:{sid}": httpx.Response(200, content=_ODDS_JS),
        f"ah:{sid}": httpx.Response(200, content=ASIANODDS_HTML),
        f"ou:{sid}": httpx.Response(200, content=OVERDOWN_HTML),
        f"stats:{sid}": httpx.Response(200, content=STATS_HTML),
    }


def _client(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/over/"):
            key = f"day:{path.removeprefix('/over/').removesuffix('.htm')}"
        elif path.startswith("/odds/"):
            key = f"odds:{path.removeprefix('/odds/').removesuffix('.js')}"
        elif path.startswith("/asian/"):
            key = f"ah:{path.removeprefix('/asian/')}"
        elif path.startswith("/overdown/"):
            key = f"ou:{path.removeprefix('/overdown/')}"
        else:
            key = f"stats:{path.removeprefix('/shijian/').removesuffix('.htm')}"
        return routes.get(key) or httpx.Response(500, text="boom")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _collect(
    store: CorpusStore,
    settings: Settings,
    routes: dict[str, httpx.Response],
    day: str,
) -> None:
    srct.collect_day(
        store, settings, _client(routes), date=day, sleeper=lambda _s: None
    )


def _odds_root(store: CorpusStore) -> Path:
    return store.silver_path(srct.SRCT_PROVIDER, srct_odds.ODDS_DATASET)


def _tree_face(store: CorpusStore) -> dict[str, tuple[str, float]]:
    """odds silver 树快照：分区文件 → (sha256, mtime)（幂等/触及面断言用）。"""
    face: dict[str, tuple[str, float]] = {}
    for part in sorted(_odds_root(store).glob("season=*/*/data.parquet")):
        rel = part.relative_to(_odds_root(store)).as_posix()
        face[rel] = (
            hashlib.sha256(part.read_bytes()).hexdigest(),
            part.stat().st_mtime_ns,
        )
    return face


def _tree_face_digests(store: CorpusStore) -> dict[str, str]:
    return {k: v[0] for k, v in _tree_face(store).items()}


def _rows_of(store: CorpusStore, season: str, league: str) -> list[dict[str, Any]]:
    part = (
        _odds_root(store)
        / f"season={season}"
        / f"competition={league}"
        / "data.parquet"
    )
    return pq.read_table(part).to_pylist()


def _seed_two_partitions(tmp_path: Path) -> tuple[CorpusStore, Settings]:
    """英超+西甲两分区（各一场，2025-26）+ 首轮全量 remat（seed 状态行）。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    routes: dict[str, httpx.Response] = {}
    routes.update(_routes_for("2025-10-01", "90001", "英超"))
    routes.update(_routes_for("2025-10-02", "90002", "西甲"))
    for day in ("2025-10-01", "2025-10-02"):
        _collect(store, settings, routes, day)
    report = srct_remat.run_remat(store)
    assert report.mode == "full"  # 无状态行=首跑全量兜底
    assert report.guard_reason
    assert not report.degraded
    return store, settings


def test_incremental_byte_equivalence_and_idempotent_rerun(
    tmp_path: Path,
) -> None:
    """增量触及分区重写 ≡ 全量切片（逐字节）；零新工件 skip 零改写。"""
    store, settings = _seed_two_partitions(tmp_path)
    try:
        # 幂等重跑：零新工件 → skip、树零改写（mtime 不变）
        face_before = _tree_face(store)
        report = srct_remat.run_remat(store)
        assert report.mode == "skip"
        assert report.fixture == {}
        assert report.xg == {}
        assert report.odds == {}
        assert _tree_face(store) == face_before

        # 新日页+新场（英超）→ 增量：英超分区重写、西甲分区 mtime 不动
        routes = _routes_for("2025-10-03", "90003", "英超")
        _collect(store, settings, routes, "2025-10-03")
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert not report.degraded
        assert report.affected_partitions == [
            "season=2025-26/competition=英超",
            "season=_unknown/competition=_unknown",  # 新日页解锁面恒入触及集
        ]
        face_after = _tree_face(store)
        changed = {k for k in face_after if face_after[k] != face_before[k]}
        assert changed == {"season=2025-26/competition=英超/data.parquet"}
        rows = _rows_of(store, "2025-26", "英超")
        assert {r["sid"] for r in rows} == {"90001", "90003"}

        # 契约核心：随后全量重建，每分区逐字节等于增量产出（含未触及分区）
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == {k: v[0] for k, v in face_after.items()}
    finally:
        store.close()


def test_beat_key_rewrites_latest_trajectory(tmp_path: Path) -> None:
    """拍键（sid@open）剥后缀归 sid：后到轨迹入账（ledger 后行胜出）。"""
    store, _ = _seed_two_partitions(tmp_path)
    try:
        # 当期班拍形状：raw 键=拍键、bronze 行 sid=裸 sid、fetched 晚于夜班
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            "90002@open",
            _ODDS_JS_BEAT,
            ext=".js",
        )
        payload = srct.parse_odds_page(_ODDS_JS_BEAT)
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90002",
                    "fetched_at": "2025-10-02T09:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "a" * 64,
                    "payload": payload,
                }
            ],
        )
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert report.affected_partitions == ["season=2025-26/competition=西甲"]
        rows = _rows_of(store, "2025-26", "西甲")
        prices = {
            (r["odds_home"], r["odds_draw"])
            for r in rows
            if r["bookmaker_id"] == "srct:1x2:9001"
        }
        assert (1.44, 4.9) in prices  # 后到拍轨迹值组入账
        # 全量等价（逐字节）：随后全量重建各分区 digest 与增量产出相等
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()


def test_new_day_page_unlocks_orphan_partition(tmp_path: Path) -> None:
    """孤儿 sid（无日页→_unknown）随新日页迁入真实分区，两面成对重写。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        # 先采西甲正场（有日页）；孤儿 90004 无日页（拍键直写 odds+bronze）
        routes = _routes_for("2025-10-01", "90001", "西甲")
        _collect(store, settings, routes, "2025-10-01")
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            "90004@open",
            _ODDS_JS,
            ext=".js",
        )
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90004",
                    "fetched_at": "2025-10-02T01:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "b" * 64,
                    "payload": srct.parse_odds_page(_ODDS_JS),
                }
            ],
        )
        srct_remat.run_remat(store)  # 首跑全量：90004 落 _unknown
        assert {r["sid"] for r in _rows_of(store, "_unknown", "_unknown")} == {"90004"}

        # 日页到位（lag-2 路径）→ 增量迁出
        routes = _routes_for("2025-10-02", "90004", "西甲")
        _collect(store, settings, routes, "2025-10-02")
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert "season=_unknown/competition=_unknown" in report.affected_partitions
        assert "season=2025-26/competition=西甲" in report.affected_partitions
        # 触及集内零行分区定向清理：_unknown 目录消失（全量语义切片）
        assert not (
            _odds_root(store) / "season=_unknown" / "competition=_unknown"
        ).exists()
        assert {r["sid"] for r in _rows_of(store, "2025-26", "西甲")} == {
            "90001",
            "90004",
        }
        # 迁出后全量等价（逐字节）
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()


def test_guard_full_defers_on_low_headroom(tmp_path: Path) -> None:
    """票 13：无人值守护栏全量余量不足 → deferred 显式降级，零写入零水位。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    routes = _routes_for("2025-10-01", "90001", "英超")
    _collect(store, settings, routes, "2025-10-01")
    try:
        report = srct_remat.run_remat(store, headroom_fn=lambda: 0)
        assert report.mode == "deferred"
        assert report.degraded == ["full_deferred_low_headroom: 0.0GiB"]
        assert report.fixture == {}  # 早退于任何阶段写之前
        assert store.remat_state() is None  # 水位不推
    finally:
        store.close()


def test_explicit_full_ignores_low_headroom(tmp_path: Path) -> None:
    """票 13：`--full` 显式人值守不拦——低余量照走全量。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    routes = _routes_for("2025-10-01", "90001", "英超")
    _collect(store, settings, routes, "2025-10-01")
    try:
        report = srct_remat.run_remat(store, full=True, headroom_fn=lambda: 0)
        assert report.mode == "full"
        assert "full_deferred_low_headroom" not in report.degraded
        assert store.remat_state() is not None  # 跑完推水位
    finally:
        store.close()


def test_guard_full_proceeds_with_headroom(tmp_path: Path) -> None:
    """票 13：护栏全量余量充足照跑（真实路径不因探测良性降级）。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    routes = _routes_for("2025-10-01", "90001", "英超")
    _collect(store, settings, routes, "2025-10-01")
    try:
        report = srct_remat.run_remat(store, headroom_fn=lambda: 1 << 40)
        assert report.mode == "full"
        assert report.degraded == []
    finally:
        store.close()


def test_headroom_probe_failure_fails_open(tmp_path: Path) -> None:
    """correctness F1：探测自身异常不许成为无人值守新死因——fail-open 照走全量。"""

    def _boom() -> int:
        raise OSError("probe unavailable")

    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    routes = _routes_for("2025-10-01", "90001", "英超")
    _collect(store, settings, routes, "2025-10-01")
    try:
        report = srct_remat.run_remat(store, headroom_fn=_boom)
        assert report.mode == "full"
        assert report.degraded == []
    finally:
        store.close()


def test_count_guard_falls_back_full(tmp_path: Path) -> None:
    """无 raw 佐证的 bronze 新 sid（中断回补形状）→ 计数护栏 → 全量兜底。"""
    store, _settings_unused = _seed_two_partitions(tmp_path)
    try:
        state = store.remat_state()
        assert state is not None
        before = store.bronze_sid_count(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        )
        # 直写 bronze（无 ingest_raw）：计数+1、水位加账 0
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90099",
                    "fetched_at": "2025-10-03T00:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "c" * 64,
                    "payload": srct.parse_odds_page(_ODDS_JS),
                }
            ],
        )
        assert (
            store.bronze_sid_count(
                srct.SRCT_PROVIDER,
                srct.ODDS_DATASET,
                srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
            )
            == before + 1
        )
        report = srct_remat.run_remat(store)
        assert report.mode == "full"
        assert "超新工件" in report.guard_reason
        assert not report.degraded
        # 全量面 _meta.json 在位、状态推进
        assert (_odds_root(store) / "_meta.json").exists()
        assert store.remat_state() is not None
    finally:
        store.close()


def test_filtered_ledger_matches_full_restriction(tmp_path: Path) -> None:
    """前缀扫描过滤账本 ≡ 全量账本限wanted（含拍键多行 sid 的后行胜出）。"""
    store, _ = _seed_two_partitions(tmp_path)
    try:
        version = srct.BRONZE_VERSIONS[srct.ODDS_DATASET]
        full = silver.latest_bronze_ledger(
            store, srct.SRCT_PROVIDER, srct.ODDS_DATASET, version
        )
        wanted = {"90002"}
        filtered = silver.latest_bronze_ledger_filtered(
            store, srct.SRCT_PROVIDER, srct.ODDS_DATASET, version, wanted
        )
        assert filtered == {sid: full[sid] for sid in wanted}
        assert filtered  # 非空：语义真是"限制"而非"清空"
    finally:
        store.close()


def test_relisted_sid_migration_covers_old_partition(tmp_path: Path) -> None:
    """同 sid 被更晚日页改挂另一真实分区（重挂改联赛）：迁移出分区并入触及集
    （correctness-review F1）——旧分区不得残留该 sid，树与全量逐字节一致。"""
    store, settings = _seed_two_partitions(tmp_path)
    try:
        # 更晚日页重挂 90002（原西甲）到英超（无新 odds raw，纯日页事件）
        routes = _routes_for("2025-10-03", "90002", "英超")
        _collect(store, settings, routes, "2025-10-03")
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert "season=2025-26/competition=西甲" in report.affected_partitions
        assert "season=2025-26/competition=英超" in report.affected_partitions
        # 迁出：西甲分区被清空收走（drained，全量语义同）；英超收编两场
        assert not (_odds_root(store) / "season=2025-26" / "competition=西甲").exists()
        assert {r["sid"] for r in _rows_of(store, "2025-26", "英超")} == {
            "90001",
            "90002",
        }
        # 全量等价（逐字节）——契约核心
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()


def test_prune_failure_degrades_not_raises(tmp_path: Path) -> None:
    """定向清零遇目录内杂物（子目录）记 degraded 不上抛（correctness-review F3）。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        routes = _routes_for("2025-10-01", "90001", "西甲")
        _collect(store, settings, routes, "2025-10-01")
        store.ingest_raw(
            srct.SRCT_PROVIDER, srct.ODDS_DATASET, "90004@open", _ODDS_JS, ext=".js"
        )
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90004",
                    "fetched_at": "2025-10-02T01:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "d" * 64,
                    "payload": srct.parse_odds_page(_ODDS_JS),
                }
            ],
        )
        srct_remat.run_remat(store)  # 首跑全量：90004 落 _unknown
        # _unknown 内预置子目录（外部杂物形状）→ 90004 日页到位迁出时清理会炸
        stale_dir = (
            _odds_root(store) / "season=_unknown" / "competition=_unknown" / "junk"
        )
        stale_dir.mkdir()
        routes = _routes_for("2025-10-02", "90004", "西甲")
        _collect(store, settings, routes, "2025-10-02")
        report = srct_remat.run_remat(store)
        assert report.mode == "degraded"
        assert any(d.startswith("odds_incremental") for d in report.degraded)
        assert store.remat_state() is not None  # 旧状态仍在（水位未推进）
    finally:
        store.close()


def test_night_repaired_window_uses_ended_at(tmp_path: Path) -> None:
    """跨过上次 remat 的夜班（started<updated<ended）回补也入窗（F4）。"""
    store, _ = _seed_two_partitions(tmp_path)
    try:
        state = store.remat_state()
        assert state is not None
        # 夜班 started 在状态刷新前、ended 在后（overrun 过 08:10 形状）
        store.record_night_summary(
            {
                "night_date": "2025-10-02",
                "started_at": "2025-10-02T00:30:00+00:00",
                "ended_at": "2099-01-01T00:30:00+00:00",
                "stop_reason": "done",
                "dates_attempted": 1,
                "dates_done": 1,
                "dates_not_found": 0,
                "pending_before": 0,
                "pending_after": 0,
                "requests": 1,
                "raw_new": 0,
                "parsed_ok": 1,
                "bronze_repaired": 2,
                "xg_matches": 0,
                "parse_failed_count": 0,
                "failed_count": 0,
                "failed_json": "{}",
                "budget_cap": 8000,
            }
        )
        report = srct_remat.run_remat(store)
        assert report.mode == "full"
        assert report.guard_reason == "夜班摘要报 bronze_repaired"
    finally:
        store.close()


def test_fixture_and_xg_gated_by_watermark(tmp_path: Path) -> None:
    """fixture/xg 门控：day_page/match_stats 有新工件才重建（全量路径）。"""
    store, settings = _seed_two_partitions(tmp_path)
    try:
        fixture_root = store.silver_path(
            srct.SRCT_PROVIDER, srct_silver.FIXTURE_DATASET
        )
        xg_meta = fixture_root.parent / srct_silver.XG_DATASET / "_meta.json"
        fixture_meta = fixture_root / "_meta.json"
        fixture_built = fixture_meta.read_text()
        xg_built = xg_meta.read_text()
        # 零新工件：两者都不重建（_meta 不改写）
        srct_remat.run_remat(store)
        assert fixture_meta.read_text() == fixture_built
        assert xg_meta.read_text() == xg_built
        # 新日页：fixture 重建（built_at 变化）+ odds 增量；该日 match_stats
        # 端点随 collect 采到新工件 → xg 门控开、同样重建
        routes = _routes_for("2025-10-04", "90005", "英超")
        _collect(store, settings, routes, "2025-10-04")
        report = srct_remat.run_remat(store)
        assert report.fixture  # 全量报告在
        assert report.xg
        assert report.mode == "incremental"
        assert fixture_meta.read_text() != fixture_built
        assert xg_meta.read_text() != xg_built
    finally:
        store.close()


# --- odds-read-switch 票 03：当期班兜底锚定（在售孤儿当日进真实分区） ---


def _seed_orphan_odds(store: CorpusStore, sid: str) -> None:
    """拍键直写 odds+bronze（无日页）：在售场当期班形态。"""
    store.ingest_raw(
        srct.SRCT_PROVIDER, srct.ODDS_DATASET, f"{sid}@open", _ODDS_JS, ext=".js"
    )
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct.ODDS_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct.ODDS_DATASET,
                "sid": sid,
                "fetched_at": "2025-10-01T02:00:00+00:00",
                "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                "raw_sha": "c" * 64,
                "payload": srct.parse_odds_page(_ODDS_JS),
            }
        ],
    )


def test_shift_match_anchors_orphan_first_run(tmp_path: Path) -> None:
    """当期班建档 sid 无日页：remat 直接落真实分区、kickoff 列非空。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        _seed_orphan_odds(store, "90010")
        store.upsert_shift_match(
            {
                "sid": "90010",
                "league": "德乙",
                "home": "测试主",
                "away": "测试客",
                "kickoff_utc": "2025-10-01T12:00:00+00:00",  # 北京 20:00
                "in_scope": 1,
                "beats": 1,
                "last_beat_at": "2025-10-01T02:00:00+00:00",
            }
        )
        report = srct_remat.run_remat(store)  # 首跑全量（无状态行）
        assert report.odds["orphan_sids"] == 0  # 当期班兜底锚定生效
        assert report.odds["partition_keys"] == ["2025-26/德乙"]
        rows = _rows_of(store, "2025-26", "德乙")
        assert {r["sid"] for r in rows} == {"90010"}
        assert all(r["kickoff"] is not None for r in rows)
        assert not (_odds_root(store) / "season=_unknown").exists()
        # 全量重建（无 meta 注入）同出处 → 字节级一致
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()


def test_shift_anchor_migration_rewrites_unknown_on_beat(tmp_path: Path) -> None:
    """增量面：孤儿先落 _unknown，当期班建档后新拍触发成对迁出。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        routes = _routes_for("2025-10-01", "90001", "西甲")
        _collect(store, settings, routes, "2025-10-01")
        _seed_orphan_odds(store, "90011")
        srct_remat.run_remat(store)  # 90011 无档 → _unknown
        assert {r["sid"] for r in _rows_of(store, "_unknown", "_unknown")} == {"90011"}

        # 当期班建档 + 新一拍 odds 工件 → 增量迁出（含 _unknown 清驻留）
        store.upsert_shift_match(
            {
                "sid": "90011",
                "league": "德乙",
                "home": "测试主",
                "away": "测试客",
                "kickoff_utc": "2025-10-02T18:30:00+00:00",
                "in_scope": 1,
                "beats": 1,
                "last_beat_at": "2025-10-02T03:00:00+00:00",
            }
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            "90011@2025-10-02@open",  # 拍键含日+slot（daily_key 形态，键唯一）
            _ODDS_JS_BEAT,
            ext=".js",
        )
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90011",
                    "fetched_at": "2025-10-02T03:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "d" * 64,
                    "payload": srct.parse_odds_page(_ODDS_JS_BEAT),
                }
            ],
        )
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert "season=_unknown/competition=_unknown" in report.affected_partitions
        assert "season=2025-26/competition=德乙" in report.affected_partitions
        assert {r["sid"] for r in _rows_of(store, "2025-26", "德乙")} == {"90011"}
        assert not (_odds_root(store) / "season=_unknown").exists()
    finally:
        store.close()


def test_day_page_takes_over_live_anchor_partition(tmp_path: Path) -> None:
    """live 锚→日页接管：日页联赛/开球面接管后 live 旧分区成对清驻留。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        # 当期班档期面：90012 挂德乙（开球 10-02 18:30Z）
        _seed_orphan_odds(store, "90012")
        store.upsert_shift_match(
            {
                "sid": "90012",
                "league": "德乙",
                "home": "测试主",
                "away": "测试客",
                "kickoff_utc": "2025-10-02T18:30:00+00:00",
                "in_scope": 1,
                "beats": 1,
                "last_beat_at": "2025-10-02T02:00:00+00:00",
            }
        )
        srct_remat.run_remat(store)  # live 锚定 → 2025-26/德乙
        assert {r["sid"] for r in _rows_of(store, "2025-26", "德乙")} == {"90012"}

        # 次日日页到位（西甲面，票 03 接管语义）→ 增量迁出含 live 旧分区
        routes = _routes_for("2025-10-02", "90012", "西甲")
        _collect(store, settings, routes, "2025-10-02")
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert "season=2025-26/competition=德乙" in report.affected_partitions
        assert "season=2025-26/competition=西甲" in report.affected_partitions
        assert {r["sid"] for r in _rows_of(store, "2025-26", "西甲")} == {"90012"}
        assert not (_odds_root(store) / "season=2025-26" / "competition=德乙").exists()
        # 接管后全量等价（逐字节）
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()


def test_remat_lock_serializes_concurrent_runs(tmp_path: Path) -> None:
    """F1 回归：互斥锁被占零动作早退；过期锁可夺。"""
    from datetime import UTC, datetime, timedelta

    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        routes = _routes_for("2025-10-01", "90001", "西甲")
        _collect(store, settings, routes, "2025-10-01")
        store._checkpoint().execute(
            "INSERT INTO srct_remat_lock (id, holder_pid, started_at)"
            " VALUES (1, 999999, ?)",
            (datetime.now(UTC).isoformat(timespec="seconds"),),
        )
        store._checkpoint().commit()
        report = srct_remat.run_remat(store)
        assert report.busy.startswith("pid=999999")
        assert report.mode == "skip"
        assert not _odds_root(store).exists()  # 零动作
        # 过期锁（>6h）→ 夺锁执行
        stale = (datetime.now(UTC) - timedelta(hours=7)).isoformat(timespec="seconds")
        store._checkpoint().execute(
            "UPDATE srct_remat_lock SET started_at = ?", (stale,)
        )
        store._checkpoint().commit()
        report2 = srct_remat.run_remat(store)
        assert report2.busy == ""
        assert report2.mode == "full"
        store._checkpoint().execute(
            "DELETE FROM srct_remat_lock"
        )  # 清锁模拟整轮正常路径已释放
        store._checkpoint().commit()
    finally:
        store.close()


def test_live_anchor_drift_migrates_old_partition(tmp_path: Path) -> None:
    """F2 回归：当期班行跨拍改档（league 空→德乙）旧分区成对清驻留。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    try:
        _seed_orphan_odds(store, "90013")
        store.upsert_shift_match(
            {
                "sid": "90013",
                "league": "",  # 开售拍未带档期面 → 兜底 "live"
                "home": "测试主",
                "away": "测试客",
                "kickoff_utc": "2025-10-02T18:30:00+00:00",
                "in_scope": 1,
                "beats": 1,
                "last_beat_at": "2025-10-02T02:00:00+00:00",
            }
        )
        srct_remat.run_remat(store)
        assert {r["sid"] for r in _rows_of(store, "2025-26", "live")} == {"90013"}

        # 次拍档期面补齐 + 新工件 → sidecar 差分并入旧 live 分区
        store.upsert_shift_match(
            {
                "sid": "90013",
                "league": "德乙",
                "home": "测试主",
                "away": "测试客",
                "kickoff_utc": "2025-10-02T18:30:00+00:00",
                "in_scope": 1,
                "beats": 2,
                "last_beat_at": "2025-10-02T03:00:00+00:00",
            }
        )
        store.ingest_raw(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            "90013@2025-10-02@open",
            _ODDS_JS_BEAT,
            ext=".js",
        )
        store.append_bronze(
            srct.SRCT_PROVIDER,
            srct.ODDS_DATASET,
            [
                {
                    "provider": srct.SRCT_PROVIDER,
                    "dataset": srct.ODDS_DATASET,
                    "sid": "90013",
                    "fetched_at": "2025-10-02T03:00:00+00:00",
                    "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
                    "raw_sha": "e" * 64,
                    "payload": srct.parse_odds_page(_ODDS_JS_BEAT),
                }
            ],
        )
        report = srct_remat.run_remat(store)
        assert report.mode == "incremental"
        assert "season=2025-26/competition=live" in report.affected_partitions
        assert "season=2025-26/competition=德乙" in report.affected_partitions
        assert not (_odds_root(store) / "season=2025-26" / "competition=live").exists()
        assert {r["sid"] for r in _rows_of(store, "2025-26", "德乙")} == {"90013"}
        face = _tree_face_digests(store)
        srct_odds.build_odds_change_events(store)
        assert _tree_face_digests(store) == face
    finally:
        store.close()
