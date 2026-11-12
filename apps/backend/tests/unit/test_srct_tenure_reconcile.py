"""两源对账纯核测试（票 09，spec-srct-cutover Testing Decisions）。

合成双源 fixture 四象限：同任两档日期 / 错位日期 / 单源缺 / 名字不可对齐
——断言 diff 分桶、人审清单形态、无分母不造率（ADR-0012）。桥与覆盖面用
小型合成俱乐部/队名集合。真实主机名/路径不出现（代称红线）。
"""

from __future__ import annotations

from goalx_backend.data import srct_tenure_reconcile as rec

# —— 合成双源 fixture ——

# 桥：club_tm_id 900 ↔ team_id 19；club 901 ↔ team 20
BRIDGE = {900: 19, 901: 20}

SRCT_ARSENAL = [
    # 同任两档：两端 ±0/±2 日
    rec.TenureInterval("Mikel Arteta", "2020-12-22", None),
    # 错位：start 差 40 日（TM 侧晚），end 差 90 日 → 人审清单
    rec.TenureInterval("Unai Emery", "2018-05-23", "2019-11-29"),
    # 单源缺：TM 侧没有的名字
    rec.TenureInterval("Arsene Wenger", "1996-10-01", "2018-05-13"),
]
TM_ARSENAL = [
    rec.TenureInterval("Mikel Arteta", "2020-12-20", None),
    # 同人 caretaker→manager 两段（TM 拆分形态）：与源T 单段比联合跨度
    rec.TenureInterval("Unai Emery", "2018-07-01", "2019-11-29"),
    rec.TenureInterval("Someone Else", "2015-01-01", "2015-06-30"),
]

SRCT_BRIGHTON = [
    # 名字域不同：Graham Potter 拉丁名一致但重音差异（René 类），规范化后可对齐
    rec.TenureInterval("Graham Potter", "2019-05-13", "2022-09-08"),
]
TM_BRIGHTON = [
    rec.TenureInterval("Graham Potter", "2019-05-13", "2022-09-13"),
]


def test_tenure_diff_buckets() -> None:
    report = rec.tenure_diff(
        {19: SRCT_ARSENAL, 20: SRCT_BRIGHTON},
        {900: TM_ARSENAL, 901: TM_BRIGHTON},
        BRIDGE,
    )
    assert report["clubs_compared"] == 2
    # 四桶：Arteta 两档吻合；Emery 错位（人审）；Wenger/Someone Else 单源缺
    assert report["agree_intervals"] == 2  # Arteta + Potter（end 5 日差 ≤ 容差）
    assert report["misaligned_intervals"] == 1
    assert report["srct_only_intervals"] == 1  # Wenger
    assert report["tm_only_intervals"] == 1  # Someone Else
    # 名字不可对齐：Wenger（源T 侧）、Someone Else（TM 侧）→ 2 人
    assert report["name_unmatched_coaches"] == 2
    # 无分母不造率：有分母的率才有值
    assert report["interval_agreement_rate"] == 2 / 3
    assert (
        report["coach_name_match_rate"] == 3 / 5
    )  # matched 3（Arteta/Emery/Potter）of 5
    # Emery：start TM-源T = 2018-07-01 − 2018-05-23 = +39 日 → 人审（>30）
    review = report["review_list"]
    assert len(review) == 1
    entry = review[0]
    assert entry["club_tm_id"] == 900
    assert entry["team_id"] == 19
    assert entry["coach"] == "Unai Emery"
    assert entry["start_delta_days"] == 39
    assert entry["srct_date_from"] == "2018-05-23"
    assert entry["tm_date_from"] == "2018-07-01"
    # delta 分布：start Potter 0 / Arteta +2 / Emery +39；end Arteta 双开无样本
    assert report["delta_distribution"]["start"]["exact"] == 1
    assert report["delta_distribution"]["start"]["within_7"] == 1
    assert report["delta_distribution"]["start"]["within_90"] == 1
    assert report["delta_distribution"]["end"]["exact"] == 1  # Emery end 同日
    assert report["delta_distribution"]["end"]["within_7"] == 1  # Potter +5


def test_tenure_diff_no_denominator_no_rate() -> None:
    """单源缺为主、无匹配对 → 率为 None（不造率）。"""
    report = rec.tenure_diff(
        {19: [rec.TenureInterval("Arsene Wenger", "1996-10-01", "2018-05-13")]},
        {900: [rec.TenureInterval("Someone Else", "2015-01-01", "2015-06-30")]},
        BRIDGE,
    )
    assert report["agree_intervals"] == 0
    assert report["misaligned_intervals"] == 0
    assert report["interval_agreement_rate"] is None
    assert report["review_list"] == []


def test_tenure_diff_open_end_semantics() -> None:
    """一端开一端闭 = 端点错位（错位桶）；双开 = 端点一致。"""
    report = rec.tenure_diff(
        {
            19: [
                rec.TenureInterval("Coach A", "2020-01-01", None),
                rec.TenureInterval("Coach B", "2021-01-01", None),
            ]
        },
        {
            900: [
                rec.TenureInterval("Coach A", "2020-01-01", "2020-12-31"),
                rec.TenureInterval("Coach B", "2021-01-03", None),
            ]
        },
        BRIDGE,
    )
    assert report["agree_intervals"] == 1  # B：start 2 日差 + 双开
    assert report["misaligned_intervals"] == 1  # A：TM 已收口而源T 开着


def test_tenure_diff_tm_spell_split_union() -> None:
    """TM caretaker→manager 拆两段、源T 单段：比联合跨度，不误报 tm_only。"""
    report = rec.tenure_diff(
        {
            19: [
                rec.TenureInterval("Unai Emery", "2018-05-23", "2019-11-29"),
            ]
        },
        {
            900: [
                rec.TenureInterval("Unai Emery", "2018-05-25", "2019-02-28"),
                rec.TenureInterval("Unai Emery", "2019-03-01", "2019-11-29"),
            ]
        },
        BRIDGE,
    )
    assert report["agree_intervals"] == 1
    assert report["tm_only_intervals"] == 0
    assert report["misaligned_intervals"] == 0


def test_normalize_coach_name() -> None:
    assert rec.normalize_coach_name("René Wagner") == rec.normalize_coach_name(
        "Rene  WAGNER"
    )
    assert rec.normalize_coach_name("Mikel Arteta") == "mikel arteta"
    # 中文分隔符差异（枚举观测 vs 档案页）
    assert rec.normalize_coach_name("鲁本·阿莫林") == rec.normalize_coach_name(
        "鲁本.阿莫林"
    )


# —— 枚举完整性（spec user story 12）———


def test_enumeration_gap() -> None:
    observations = [
        (19, "2025-2026", "鲁本·阿莫林"),
        (19, "2025-2026", "鲁本.阿莫林"),  # 同人异写 → 规范化后不算缺
        (19, "2025-2026", "埃里克·滕哈赫"),
        (20, "2025-2026", "法比安·许尔泽勒"),
        (19, "2024-2025", "埃里克·滕哈赫"),
    ]
    corpus = {19: {"鲁本·阿莫林", "埃里克·滕哈赫"}, 20: {"法比安·许尔泽勒"}}
    report = rec.enumeration_gap(observations, corpus)
    assert report["cells_total"] == 3  # (19,25-26)/(20,25-26)/(19,24-25)
    assert report["cells_with_gap"] == 0
    assert report["distinct_missing_coaches"] == 0

    corpus_missing = {19: {"埃里克·滕哈赫"}, 20: set()}
    gap = rec.enumeration_gap(observations, corpus_missing)
    assert gap["cells_with_gap"] == 2
    assert gap["distinct_missing_coaches"] == 2
    cells = {(c["team_id"], c["season"]): c["missing"] for c in gap["gap_cells"]}
    assert cells[(19, "2025-2026")] == ["鲁本·阿莫林"]
    assert cells[(20, "2025-2026")] == ["法比安·许尔泽勒"]
    assert gap["gap_rate"] == 2 / 3


def test_enumeration_gap_empty_observations() -> None:
    report = rec.enumeration_gap([], {19: {"x"}})
    assert report["cells_total"] == 0
    assert report["gap_rate"] is None  # 无分母不造率


# —— 俱乐部桥（词汇 + 结构双路）———


def test_lexical_club_bridge() -> None:
    srct_teams = {
        19: ("阿森纳", "阿仙奴", "Arsenal"),
        18: ("西布罗姆维奇", "西布朗", "West Bromwich(WBA)"),
    }
    tm_names = {900: "Arsenal", 901: "West Bromwich Albion", 902: "FC Bayern Munich"}
    bridge, unmatched = rec.lexical_club_bridge(tm_names, srct_teams)
    assert bridge == {900: 19}
    assert unmatched == ["FC Bayern Munich", "West Bromwich Albion"]  # 名序


def test_structural_club_bridge_and_assembly() -> None:
    """结构链（linked 场投票）+ 词法桥合流；双路冲突不硬配（弃 + 计数）。"""
    srct_teams = {
        19: ("阿森纳", "阿仙奴", "Arsenal"),
        20: ("布莱顿", "白禮頓", "Brighton"),
    }
    # linked 场观察：源T名 → fd team 投票
    link_rows = [("阿森纳", 101), ("阿森纳", 101), ("布莱顿", 102)]
    # fd team → TM 别名名 → club_tm_id
    fd_aliases = {101: {"Arsenal"}, 102: {"Brighton & Hove Albion"}}
    tm_name_to_id = {"Arsenal": 900, "Brighton & Hove Albion": 901}
    structural, collisions = rec.structural_club_bridge(
        link_rows, srct_teams, fd_aliases, tm_name_to_id
    )
    assert structural == {900: 19, 901: 20}
    assert collisions["name_votes_dropped"] == 0
    # 合流：词法与结构一致 → keep；仅一路 → keep + 注记；冲突 → 弃
    lexical = {900: 19, 901: 20}
    merged = rec.assemble_club_bridge(lexical, structural)
    assert merged["bridge"] == {900: 19, 901: 20}
    assert merged["both_routes"] == 2
    assert merged["route_conflicts"] == 0

    merged_conflict = rec.assemble_club_bridge({900: 20}, structural)
    assert 900 not in merged_conflict["bridge"]
    assert merged_conflict["route_conflicts"] == 1


def test_structural_bridge_name_collision_drops() -> None:
    """一源T名投到多 fd team → 弃（不硬配）。"""
    srct_teams = {19: ("阿森纳", "阿仙奴", "Arsenal")}
    structural, collisions = rec.structural_club_bridge(
        [("阿森纳", 101), ("阿森纳", 102)],
        srct_teams,
        {101: {"Arsenal"}, 102: {"Arsenal"}},
        {"Arsenal": 900},
    )
    assert structural == {}
    assert collisions["name_votes_dropped"] == 1


# —— facet 俱乐部覆盖（喂票 10 迁移决策）———


def test_facet_club_coverage() -> None:
    srct_teams = {
        19: ("阿森纳", "阿仙奴", "Arsenal"),
        18: ("西布罗姆维奇", "西布朗", "West Bromwich(WBA)"),
    }
    facet_rows = [
        ("英超", 900, "Arsenal"),  # clean 命中
        ("英冠", 901, "West Bromwich Albion"),  # alias-work（词元重叠）
        ("英冠", 902, "Luton Town"),  # 语料内 GB 但名字零交集 → not at all
        ("德甲", 903, "FC Bayern Munich"),  # 非语料范围（GB 语料外）
    ]
    report = rec.facet_club_coverage(facet_rows, srct_teams)
    assert report["facet_clubs"] == 4
    assert report["gb_clubs"] == 3
    assert report["gb_mapped_clean"] == 1
    assert report["gb_alias_work"] == ["West Bromwich Albion"]
    assert report["gb_unmapped"] == ["Luton Town"]
    assert report["non_gb_clubs"] == 1


# —— 整装壳（合成主库 + 合成语料 + 合成 duckdb）———


def _seed_synthetic_world(tmp_path):  # type: ignore[no-untyped-def]
    """合成三源：主库文件（TM 面+facet+别名+已链场）+ 语料树 + duckdb 视图。"""

    import duckdb

    from goalx_backend import db
    from goalx_backend.data.corpus_store import CorpusStore
    from goalx_backend.data.ingest import srct, srct_tenure

    db_path = tmp_path / "synthetic.db"
    conn = db.connect(str(db_path))
    db.migrate(conn)
    conn.execute("DELETE FROM team_aliases")
    conn.execute(
        "INSERT INTO teams (id, canonical_name, created_at) VALUES"
        " (101, '阿森纳', '2026-11-12T00:00:00+00:00'),"
        " (102, '他队', '2026-11-12T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO team_aliases (team_id, source, alias)"
        " VALUES (101, 'transfermarkt', 'Arsenal')"
    )
    conn.execute(
        "INSERT INTO tm_club_seasons (league_tm_code, season, club_tm_id, club_name,"
        " first_seen_at, observed_at) VALUES ('GB1', '2025-2026', 900, 'Arsenal',"
        " '2026-11-12T00:00:00+00:00', '2026-11-12T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO tm_coach_tenures (club_tm_id, club_name, role, coach_tm_id,"
        " coach_name, date_from, date_to, games, first_seen_at, observed_at,"
        " parse_version) VALUES (900, 'Arsenal', 'manager', 1, 'Mikel Arteta',"
        " '2020-12-20', NULL, 200, '2026-11-12T00:00:00+00:00',"
        " '2026-11-12T00:00:00+00:00', 'tm_coach_v1')"
    )
    conn.execute(
        "INSERT INTO formation_family_facet (season, league_fd, league_zh, team_id,"
        " club_tm_id, club_name, coach_name, coach_role, family, n_matches, n_known,"
        " first_match_date, last_match_date, rebuilt_at)"
        " VALUES ('2025-26', 'E0', '英超', NULL, 900, 'Arsenal', 'Mikel Arteta',"
        " 'manager', '4-3-3', 10, 8, '2025-08-01', '2025-10-01',"
        " '2026-11-12T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO competitions (id, name, tier, created_at) VALUES"
        " (1, 'Premier League', 'tier1', '2026-11-12T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO fixtures (competition_id, kickoff_utc, home_team_id,"
        " away_team_id) VALUES (1, '2025-08-01T15:00:00+00:00', 101, 102)"
    )
    conn.execute(
        "INSERT INTO source_match_links (fixture_id, source, source_match_id,"
        " method, status, mapped_at) VALUES (1, 'srct', 'sid1', 'primary_key',"
        " 'linked', '2026-11-12T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    store = CorpusStore(tmp_path / "corpus")
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_CAREER_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.COACH_CAREER_DATASET,
                "sid": "10138",
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.COACH_CAREER_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": {
                    "once_team": "",
                    "now_team": [],
                    "transfers": [],
                    "tenures": [
                        {
                            "start": "2020-12-22",
                            "end": None,
                            "team_id": 19,
                            "name_cn": "阿森纳",
                            "name_tw": "阿仙奴",
                            "name_en": "Arsenal",
                            "matches": 100,
                            "wins": 60,
                            "draws": 20,
                            "losses": 20,
                        }
                    ],
                    "last_update": "2026-11-12 09:21:11",
                },
            }
        ],
    )
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.COACH_PAGE_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.COACH_PAGE_DATASET,
                "sid": "10138@19",
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.COACH_PAGE_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": {
                    "name_cn": "米克尔·阿尔特塔",
                    "name_tw": "阿仙奴",
                    "name_en": "Mikel Arteta",
                    "career_cid": "10138",
                    "career_version": "2026111209",
                },
            }
        ],
    )
    store.append_bronze(
        srct.SRCT_PROVIDER,
        srct_tenure.LEAGUE_SEASON_DATASET,
        [
            {
                "provider": srct.SRCT_PROVIDER,
                "dataset": srct_tenure.LEAGUE_SEASON_DATASET,
                "sid": "2025-2026-s36",
                "fetched_at": "2026-11-12T01:30:00+00:00",
                "parser_version": srct_tenure.TENURE_BRONZE_VERSIONS[
                    srct_tenure.LEAGUE_SEASON_DATASET
                ],
                "raw_sha": "deadbeef",
                "payload": {
                    "league": {
                        "league_id": 36,
                        "name_cn": "英格兰超级联赛",
                        "name_tw": "英格蘭超級聯賽",
                        "name_en": "English Premier League",
                        "season": "2025-2026",
                    },
                    "teams": [
                        {
                            "team_id": 19,
                            "name_cn": "阿森纳",
                            "name_tw": "阿仙奴",
                            "name_en": "Arsenal",
                        }
                    ],
                },
            }
        ],
    )

    duck = duckdb.connect(":memory:")
    duck.execute(f"ATTACH '{db_path.as_posix()}' AS goalx (TYPE sqlite, READ_ONLY)")
    duck.execute(
        """
        CREATE VIEW match_detail AS SELECT * FROM (VALUES
          ('sid1', TIMESTAMPTZ '2025-08-01 15:00:00+00', '阿森纳', '他队',
           '米克尔·阿尔特塔', NULL)
        ) AS t(sid, kickoff, home, away, home_coach, away_coach)
        """
    )
    return str(db_path), store, duck


def test_reconcile_report_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """整装壳：四面齐出（桥双路一致、diff 吻合、枚举满格、facet clean）。"""
    import sqlite3

    db_path, store, duck = _seed_synthetic_world(tmp_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report = rec.reconcile_report(conn, store, duck)
    bridge = report["club_bridge"]
    assert bridge["tm_gb_clubs"] == 1
    assert bridge["srct_gb_teams"] == 1
    assert bridge["bridged"] == 1
    assert bridge["both_routes"] == 1  # 词法+结构同指 900↔19
    diff = report["tenure_diff"]
    assert diff["clubs_compared"] == 1
    assert diff["agree_intervals"] == 1  # start 2 日差、双开 → 吻合
    assert diff["misaligned_intervals"] == 0
    enum = report["enumeration"]
    assert enum["corpus_available"] is True
    assert enum["cells_with_gap"] == 0
    coverage = report["facet_coverage"]
    assert coverage["gb_clubs"] == 1
    assert coverage["gb_mapped_clean"] == 1
    assert report["snapshot_gap"]["srct_teams_without_tm"] == 0
    conn.close()
    duck.close()
    store.close()


def test_reconcile_report_degrades_without_corpus(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """duckdb 缺席：结构路零桥、观测面 corpus_available=False（不炸不发请求）。"""
    import sqlite3

    db_path, store, _duck = _seed_synthetic_world(tmp_path)
    _duck.close()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report = rec.reconcile_report(conn, store, None)
    assert report["enumeration"]["corpus_available"] is False
    assert report["enumeration"]["cells_total"] == 0
    assert report["club_bridge"]["only_structural"] == 0  # 结构路缺席
    assert report["club_bridge"]["bridged"] >= 0  # 词法路仍在
    conn.close()
    store.close()
