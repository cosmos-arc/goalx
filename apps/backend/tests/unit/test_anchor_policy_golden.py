"""票 06 F0：锚行 ≤cutoff 政策护栏——金样等价测试 + 常量单点。

tie-break 不变量（published DESC NULLS LAST, observed DESC, source_order DESC；
cutoff 含端；NULL 三元组剔除）存在多份实现，本文件在共享 fixtures 上互钉：
1. srct_asof Python 镜像 ≡ quote_evidence SQL（ROW_NUMBER 读桥）——含并列/NULL/
   cutoff 边界；
2. srct_anchor_triplets_at_cutoffs（arg_max）与 gold._anchor_slices 同规则
   （key=(published_ms, source_order)，NULL published 行永不入选）。
行为零变化：任何既有不一致都应让本测试红并停下报告，不静默改任何一边。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import duckdb
import pytest

from goalx_backend.data import corpus_duckdb
from goalx_backend.data import quote_evidence as qe
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.gold import _anchor_slices
from goalx_backend.data.ingest import srct, srct_asof, srct_odds

SID = "95001"
KICKOFF = datetime(2026, 11, 5, 19, 0, tzinfo=UTC)
_ROW_A = "2.10|3.30|3.40|11-04 12:00|0.9|0.9|0.9|2026"
_ROW_B = "3.50|3.60|1.90|11-04 11:30|0.9|0.9|0.9|2026"


def _build(tmp_path: Path, envelopes: list[dict]) -> tuple[CorpusStore, str]:
    """攒 bronze → silver → duck 库（test_quote_asof_bronze 同款建具）。"""
    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(srct.SRCT_PROVIDER, srct.ODDS_DATASET, envelopes)
    srct_odds.build_odds_change_events(
        store,
        meta={SID: {"sid": SID, "kickoff": KICKOFF, "league": "英超"}},
    )
    path = corpus_duckdb.build_corpus_duckdb(store)
    return store, str(path)


def _envelope(fetched_at: str, book_a_rows: list[str] | None = None) -> dict:
    """9501/9502 双书 odds_1x2d 信封（书 A 轨迹可多行）。"""
    a_rows = ";".join(book_a_rows or [_ROW_A])
    return {
        "provider": srct.SRCT_PROVIDER,
        "dataset": srct.ODDS_DATASET,
        "sid": SID,
        "fetched_at": fetched_at,
        "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        "raw_sha": "0" * 64,
        "payload": {
            "meta": {},
            "game": [
                "9501|800001|TestSharpA|2.10|3.30|3.40|",
                "9502|800002|TestSharpB|3.50|3.60|1.90|",
            ],
            "game_detail": [
                f"800001^{a_rows};",
                f"800002^{_ROW_B};",
            ],
        },
    }


# 锚书商（cid177）轨迹：同分钟并列两行（值异→保留，source_order 决胜）、
# NULL published 一行、更晚一行。
_ANCHOR_TIE = "2.50|3.20|3.10|11-04 12:00|0.9|0.9|0.9|2026"
_ANCHOR_TIE2 = "2.40|3.25|3.20|11-04 12:00|0.9|0.9|0.9|2026"
_ANCHOR_NULLPUB = "9.99|9.99|9.99||0.9|0.9|0.9|2026"
_ANCHOR_LATE = "2.30|3.30|3.30|11-04 13:50|0.9|0.9|0.9|2026"
# 次日行：位于 kickoff−24h（11-05 03:00 北京）与 −1h（11-06 02:00 北京）之间
# ——令 gold 的 q1h/q24h 两切点取到不同行，等价测试脱离平凡区
_ANCHOR_NEXTDAY = "2.20|3.10|3.20|11-05 11:00|0.9|0.9|0.9|2026"

TIE_MS = int(datetime(2026, 11, 4, 4, 0, tzinfo=UTC).timestamp() * 1000)  # 12:00 北京
LATE_MS = int(datetime(2026, 11, 4, 5, 50, tzinfo=UTC).timestamp() * 1000)  # 13:50 北京


def _row(
    book: str = "177",
    pub_ms: int | None = TIE_MS,
    obs_ms: int = 1_000,
    order: int = 0,
    home: float = 2.5,
) -> dict[str, object]:
    return {
        "bookmaker_id": book,
        "published_at": pub_ms,
        "observed_at": obs_ms,
        "source_order": order,
        "odds_home": home,
        "odds_draw": 3.2,
        "odds_away": 3.1,
    }


def _mirror(rows: list[dict[str, object]]):
    from goalx_backend.data.ingest.srct_asof import _books_from_event_rows

    return _books_from_event_rows(rows, None)


def test_mirror_tie_broken_by_source_order_desc() -> None:
    low = _row(order=0, home=2.5)
    high = _row(order=1, home=2.4)
    # 传入序刻意倒置——决胜只看 source_order
    assert _mirror([high, low])["177"][0] == pytest.approx(2.4)


def test_mirror_tie_broken_by_observed_desc() -> None:
    """ROW_NUMBER 中键：同 published 且同 source_order 不可能（源页行序唯一），
    observed 层与 arg_max 族的关键差 = ROW_NUMBER 含 observed DESC。"""
    early = _row(pub_ms=TIE_MS, obs_ms=1_000, order=0, home=2.5)
    late = _row(pub_ms=TIE_MS, obs_ms=2_000, order=0, home=2.4)
    assert _mirror([early, late])["177"][0] == pytest.approx(2.4)


def test_mirror_null_published_ranks_last_but_selectable() -> None:
    got = _mirror([_row(pub_ms=None, home=9.99)])
    assert got["177"][0] == pytest.approx(9.99)  # 仅此一行时可选（captured=obs 兜底）
    mixed = _mirror([_row(pub_ms=None, home=9.99), _row(pub_ms=TIE_MS, home=2.5)])
    assert mixed["177"][0] == pytest.approx(2.5)  # 有 published 行必胜


def test_mirror_null_observed_or_null_odds_rows_skipped() -> None:
    assert _mirror([cast("dict[str, object]", {**_row(), "observed_at": None})]) == {}
    assert _mirror([cast("dict[str, object]", {**_row(), "odds_home": None})]) == {}


def _anchor_envelope(fetched_at: str) -> dict:
    trajectory = ";".join(
        [_ANCHOR_TIE, _ANCHOR_TIE2, _ANCHOR_NULLPUB, _ANCHOR_LATE, _ANCHOR_NEXTDAY]
    )
    return {
        "provider": srct.SRCT_PROVIDER,
        "dataset": srct.ODDS_DATASET,
        "sid": SID,
        "fetched_at": fetched_at,
        "parser_version": srct.BRONZE_VERSIONS[srct.ODDS_DATASET],
        "raw_sha": "0" * 64,
        "payload": {
            "meta": {},
            "game": [
                "9501|800001|TestSharpA|2.10|3.30|3.40|",
                "177|800177|Anchor|2.50|3.20|3.10|",
            ],
            "game_detail": [
                f"800001^{_ANCHOR_TIE};",
                f"800177^{trajectory};",
            ],
        },
    }


@pytest.fixture
def duck_path(tmp_path: Path) -> str:
    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct.ODDS_DATASET,
        [_anchor_envelope("2026-11-05T10:00:00+00:00")],
    )
    srct_odds.build_odds_change_events(
        store,
        meta={SID: {"sid": SID, "kickoff": KICKOFF, "league": "英超"}},
    )
    # fixture_universe 单行播种（kickoff=站点北京墙钟 naive——gold CTE
    # 以 AT TIME ZONE 'Asia/Shanghai' 还原）；免整链采集建库
    from goalx_backend.data import silver
    from goalx_backend.data.fixtures import CST
    from goalx_backend.data.ingest import srct_silver

    part = srct_silver._fixture_root(store) / "season=2026" / "competition=英超"
    silver.write_partition(
        part,
        [
            {
                "sid": SID,
                "league": "英超",
                "kickoff": KICKOFF.astimezone(CST).replace(tzinfo=None),
                "home": "home",
                "away": "away",
                "home_goals": None,
                "away_goals": None,
                "stage": "league",
                "day": "2026-11-05",
            }
        ],
        srct_silver._FIXTURE_SCHEMA,
    )
    path = corpus_duckdb.build_corpus_duckdb(store)
    store.close()
    return path


def test_anchor_triplets_tie_null_and_cutoff_boundaries(duck_path: str) -> None:
    con = duckdb.connect(duck_path, read_only=True)
    try:
        cuts = (
            datetime(2026, 11, 4, 4, 0, tzinfo=UTC),  # 恰等并列分钟（含端）
            datetime(2026, 11, 4, 5, 0, tzinfo=UTC),  # 并列后、晚行前
            datetime(2026, 11, 4, 3, 0, tzinfo=UTC),  # 全部 published 之前 → NULL
        )
        got = qe.srct_anchor_triplets_at_cutoffs(con, [(SID, cuts)])
    finally:
        con.close()
    assert got == [
        [
            (2.4, 3.25, 3.2),  # 并列分钟：source_order 高者胜（TIE2）
            (2.4, 3.25, 3.2),  # 晚行未到，仍并列胜者
            None,  # NULL published 行 arg_max 永不入选
        ]
    ]


def test_gold_slices_agree_with_triplets_across_regimes(duck_path: str) -> None:
    """q1h/q24h 两切点落在轨迹不同侧（脱离平凡区）仍逐值一致。"""
    con = duckdb.connect(duck_path, read_only=True)
    try:
        slices = _anchor_slices(con)
        cut_1h = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)  # kickoff−1h
        cut_24h = datetime(2026, 11, 4, 19, 0, tzinfo=UTC)  # kickoff−24h
        triplets = qe.srct_anchor_triplets_at_cutoffs(con, [(SID, (cut_1h, cut_24h))])
    finally:
        con.close()
    features = slices[SID]["features"]
    # −1h 切点取次日行；−24h 切点取 11-04 最晚行——两切点不同行
    assert (features["q1h1x2_h"], features["q1h1x2_d"], features["q1h1x2_a"]) == (
        2.2,
        3.1,
        3.2,
    )
    assert (
        features["q24h1x2_h"],
        features["q24h1x2_d"],
        features["q24h1x2_a"],
    ) == (
        2.3,
        3.3,
        3.3,
    )
    assert triplets[0] == [(2.2, 3.1, 3.2), (2.3, 3.3, 3.3)]  # gold ≡ triplets


def test_replay_bridge_equivalence_with_ties(tmp_path: Path) -> None:
    """青铜重放（Python 镜像）≡ silver 读桥（ROW_NUMBER SQL）——含并列用例。

    同一 tie 轨迹（9501 书）在两个实现里必须选出同一行：05:31 时点
    重放路径选 source_order 高者，与单信封期 silver 读数逐值一致。
    """
    tie_traj = [_ANCHOR_TIE, _ANCHOR_TIE2, _ANCHOR_NULLPUB]
    e1 = _envelope("2026-11-04T05:30:00+00:00", tie_traj)
    e2 = _envelope("2026-11-04T06:00:00+00:00", [*tie_traj, _ANCHOR_LATE])

    # 单信封库：silver 读桥直接回答 05:31
    store1, path1 = _build(tmp_path / "one", [e1])
    con1 = duckdb.connect(path1, read_only=True)
    try:
        silver_at_531 = qe.srct_snapshot_rows_asof(
            con1, SID, "2026-11-04T05:31:00+00:00"
        )
    finally:
        con1.close()
        store1.close()

    # 双信封库：silver 整体后移 → 青铜重放路径接管
    store2, path2 = _build(tmp_path / "two", [e1, e2])
    con2 = duckdb.connect(path2, read_only=True)
    try:
        replayed = srct_asof.asof_rows(store2, con2, SID, "2026-11-04T05:31:00+00:00")
        assert replayed is not None
        bridge_late = qe.srct_snapshot_rows_asof(con2, SID, "2026-11-04T06:05:00+00:00")
    finally:
        con2.close()
        store2.close()

    assert replayed == silver_at_531  # 镜像 ≡ SQL（含并列/NULL 行为）
    # 06:05 时点两库同取最晚行（NULL published 与并列行皆让位）
    late_home = [
        r
        for r in bridge_late
        if r["source"] == "srct:1x2:9501" and r["selection_code"] == "h"
    ]
    assert len(late_home) == 1
    assert late_home[0]["odds"] == pytest.approx(2.3)
