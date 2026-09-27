"""自算 Elo 测试（票 78：PIT 折叠、双名空间桥、RPS 报告、幂等物化）。"""

from __future__ import annotations

from datetime import date

import duckdb
import pyarrow.parquet as pq

from goalx_backend.data import results as rs_store
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import elo_silver
from goalx_backend.modelling import elo

D = elo.EloMatch


def duck_universe(
    rows: list[tuple[str, str, str, str, int, int, int]],
) -> duckdb.DuckDBPyConnection:
    """内存 duckdb 假 fixture_universe：(sid, league, kickoff, home, away, hg, ag)。"""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE fixture_universe"
        " (sid VARCHAR, league VARCHAR, kickoff TIMESTAMP, home VARCHAR,"
        " away VARCHAR, home_goals SMALLINT, away_goals SMALLINT)"
    )
    for sid, league, kickoff, home, away, hg, ag in rows:
        con.execute(
            "INSERT INTO fixture_universe VALUES (?, ?, ?::TIMESTAMP, ?, ?, ?, ?)",
            [sid, league, kickoff, home, away, hg, ag],
        )
    return con


# --- 纯函数层 ---


def test_expected_score_and_k_factor() -> None:
    """期望分公式（等值 0.5 + HFA 偏移）；K 随净胜球放大。"""
    assert elo.expected_score(1500, 1500, 0) == 0.5
    assert elo.expected_score(1500, 1500, 80) == elo.expected_score(1580, 1500, 0)
    assert elo.k_factor(0) == 20.0
    assert elo.k_factor(3) > elo.k_factor(1) > elo.k_factor(0)


def test_one_x_two_probs_sums_one() -> None:
    """1X2 近似：和为 1、均势平局最高、极端概率不越界。"""
    probs = elo.one_x_two_probs(0.5)
    assert abs(sum(probs) - 1.0) < 1e-12
    # 平局概率在均势处取峰（对 e 的曲线峰值）
    assert probs[1] > elo.one_x_two_probs(0.75)[1]
    extreme = elo.one_x_two_probs(0.99)
    assert all(0.0 <= p <= 1.0 for p in extreme)
    assert abs(sum(extreme) - 1.0) < 1e-12


def test_compute_elo_pit_and_upset_direction() -> None:
    """PIT：后场改动不影响前场 pre；爆冷方向正确（弱胜强→弱升强降）。"""
    base = [
        D("强队", "弱旅A", 3, 0, date(2025, 8, 1)),
        D("强队", "弱旅B", 2, 0, date(2025, 8, 2)),
    ]
    pres = elo.compute_elo(base)
    assert (pres[0].elo_home_pre, pres[0].elo_away_pre) == (1500.0, 1500.0)
    # 追加更晚的比赛，前两场 pre 不变（PIT 由排序折叠保证）
    extended = [*base, D("弱旅A", "强队", 1, 0, date(2025, 8, 3), seq="x")]
    pres_ext = elo.compute_elo(extended)
    assert pres_ext[0] == pres[0]
    assert pres_ext[1] == pres[1]
    # 爆冷场：弱旅A 赛前低于强队，赛后评级上跳
    upset_pre = pres_ext[2]
    assert upset_pre.elo_home_pre < upset_pre.elo_away_pre
    after = elo.ratings_asof(extended, date(2025, 8, 4))
    assert after["弱旅A"] > upset_pre.elo_home_pre
    assert after["强队"] < upset_pre.elo_away_pre


def test_ratings_asof_excludes_same_day_and_later() -> None:
    """as-of 查询面：同日与之后的比赛不计（供 asof 当日赛前使用）。"""
    matches = [
        D("甲", "乙", 1, 0, date(2025, 8, 1), seq="1"),
        D("丙", "丁", 0, 3, date(2025, 8, 1), seq="2"),
        D("甲", "丙", 2, 2, date(2025, 8, 2), seq="3"),
    ]
    assert elo.ratings_asof(matches, date(2025, 8, 1)) == {}  # 同日不计
    day1 = elo.ratings_asof(matches, date(2025, 8, 2))  # 8/1 两场计入
    assert set(day1) == {"甲", "乙", "丙", "丁"}
    day2 = elo.ratings_asof(matches, date(2025, 8, 3))  # 再计 8/2 场
    assert day2["甲"] != day1["甲"]


# --- 桥 ---


def _bridge_row(league: str, day: str, home: str, away: str, hg: int, ag: int):
    return elo.BridgeRow(
        league=league,
        day=date.fromisoformat(day),
        home=home,
        away=away,
        home_goals=hg,
        away_goals=ag,
    )


def test_bridge_unanimous_and_conflict_skipped() -> None:
    """一致多数票映射成立；同名分歧跳过；同日同比分并列键整组不配。"""
    hist = [
        _bridge_row("英超", "2025-08-01", "Man City", "Rivals", 3, 0),
        _bridge_row("英超", "2025-08-09", "Man City", "Other", 2, 1),
        _bridge_row("英超", "2025-08-17", "Man City", "Third", 1, 0),
        # 分歧名：同 fd 名映射两个中文名
        _bridge_row("英超", "2025-08-23", "Spurs", "X", 1, 0),
        _bridge_row("英超", "2025-08-30", "Spurs", "Y", 2, 0),
        _bridge_row("英超", "2025-09-06", "Spurs", "Z", 1, 1),
    ]
    corpus = [
        _bridge_row("英超", "2025-08-01", "曼城", "对手一", 3, 0),
        _bridge_row("英超", "2025-08-09", "曼城", "对手二", 2, 1),
        _bridge_row("英超", "2025-08-17", "曼城", "对手三", 1, 0),
        _bridge_row("英超", "2025-08-23", "热刺", "甲", 1, 0),
        _bridge_row("英超", "2025-08-30", "托特纳姆", "乙", 2, 0),
        _bridge_row("英超", "2025-09-06", "热刺", "丙", 1, 1),
    ]
    mapping_names, report = elo.build_name_bridge(hist, corpus, min_votes=3)
    assert mapping_names["Man City"] == "曼城"
    assert "Spurs" not in mapping_names  # 分歧跳过
    assert report["mapped_names"] == 1
    assert report["skipped_conflict"] == 1


def test_bridge_duplicate_score_key_skipped() -> None:
    """同联赛同日同比分两场 → 键不唯一，整组跳过（禁自信合并）。"""
    hist = [
        _bridge_row("英超", "2025-08-01", "A1", "B1", 1, 0),
        _bridge_row("英超", "2025-08-01", "A2", "B2", 1, 0),
    ] * 3
    corpus = [
        _bridge_row("英超", "2025-08-01", "甲一", "乙一", 1, 0),
        _bridge_row("英超", "2025-08-01", "甲二", "乙二", 1, 0),
    ] * 3
    mapping_names, report = elo.build_name_bridge(hist, corpus, min_votes=3)
    assert mapping_names == {}
    assert report["matched_keys"] == 0


def test_transfer_state_deterministic() -> None:
    """era 搬运：一对一转移；多源同名取评级最高（平局按名字序）。"""
    state = {"Man City": 1900.0, "Man Utd": 1600.0}
    bridge = {"Man City": "曼城", "Man Utd": "曼城"}
    moved = elo.transfer_state(state, bridge)
    assert moved == {"曼城": 1900.0}
    assert elo.transfer_state({"X": 1700.0}, {}) == {}


# --- 物化（ingest 编排） ---


def _seed_hist(db, rows: list[tuple[str, str, str, str, int, int]]) -> None:
    payloads = [
        {
            "competition": comp,
            "season": "2425",
            "match_date": day,
            "home_team": home,
            "away_team": away,
            "fthg": hg,
            "ftag": ag,
            "ftr": "H" if hg > ag else ("D" if hg == ag else "A"),
            "psc_home": 2.0,
            "psc_draw": 3.0,
            "psc_away": 3.0,
            "avgc_home": 2.0,
            "avgc_draw": 3.0,
            "avgc_away": 3.0,
        }
        for comp, day, home, away, hg, ag in rows
    ]
    rs_store.upsert_hist_matches(db, payloads)
    db.commit()


def test_build_elo_self_end_to_end(db, tmp_path) -> None:
    """热身→桥→折叠→物化：暖机值经桥传进语料名空间；幂等字节级一致。"""
    # 热身（era 边界 2025-06-01 前）：Man City 连胜
    warmup = [
        ("E0", f"2024-{m:02d}-01", "Man City", "Warmup Opp", 4, 0) for m in range(1, 9)
    ]
    # 重叠期：同键（联赛+日+比分）唯一配对 ×3 次投票
    overlap = [
        ("E0", "2025-06-01", "Man City", "Opp1", 2, 0),
        ("E0", "2025-06-08", "Man City", "Opp2", 3, 1),
        ("E0", "2025-06-15", "Man City", "Opp3", 2, 0),
    ]
    _seed_hist(db, warmup + overlap)
    store = CorpusStore(tmp_path / "corpus")
    store.ensure_tree()
    duck_con = duck_universe(
        [
            ("s1", "英超", "2025-06-01 20:00:00", "曼城", "对手一", 2, 0),
            ("s2", "英超", "2025-06-08 20:00:00", "曼城", "对手二", 3, 1),
            ("s3", "英超", "2025-06-15 21:00:00", "曼城", "对手三", 2, 0),
            ("s4", "英超", "2025-06-20 20:00:00", "曼城", "新军", 1, 1),
            ("s5", "英超", "2025-06-20 20:00:00", "新军", "对手一", 0, 2),
        ]
    )
    report = elo_silver.build_elo_self(store, db, duck_con)
    assert report.degraded is None
    assert report.bridge["mapped_names"] >= 1
    assert report.written == 5
    assert report.season_rps[0].matches == 5
    # 暖机传导：首场语料场的曼城赛前值显著高于基准（热身搬运成立）
    table = pq.read_table(
        store.root / "silver" / "elo" / "elo_self" / "all" / "data.parquet"
    )
    rows = {r["sid"]: r for r in table.to_pylist()}
    assert rows["s1"]["elo_home_pre"] > 1600.0
    assert rows["s1"]["version"] == elo.ELO_VERSION
    assert rows["s4"]["elo_away_pre"] == 1500.0  # 新军首见=基准
    # 幂等：重算 → parquet 字节级一致
    first = (
        store.root / "silver" / "elo" / "elo_self" / "all" / "data.parquet"
    ).read_bytes()
    elo_silver.build_elo_self(store, db, duck_con)
    second = (
        store.root / "silver" / "elo" / "elo_self" / "all" / "data.parquet"
    ).read_bytes()
    assert first == second
    store.close()


def test_build_degraded_on_empty_universe(db, tmp_path) -> None:
    """universe 空 → 只落 meta（degraded 注记），不写数据文件。"""
    _seed_hist(db, [("E0", "2024-08-01", "A", "B", 1, 0)])
    store = CorpusStore(tmp_path / "corpus")
    store.ensure_tree()
    empty = duck_universe([])
    report = elo_silver.build_elo_self(store, db, empty)
    assert report.degraded is not None
    meta = (store.root / "silver" / "elo" / "elo_self" / "_meta.json").read_text()
    assert "degraded" in meta
    store.close()
