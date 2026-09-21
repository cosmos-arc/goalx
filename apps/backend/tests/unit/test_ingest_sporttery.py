"""sporttery 竞彩采集测试（解析纯函数 + 入库幂等；票 35 证据/状态/改期）。"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data import fixtures as fx_store
from goalx_backend.data import observations
from goalx_backend.data.ingest import sporttery
from goalx_backend.models import ObservationInput, ObservationPurpose

SAMPLE = {
    "errorCode": "0",
    "value": {
        "matchInfoList": [
            {
                "businessDate": "2026-09-12",
                "subMatchList": [
                    {
                        "matchId": 2041430,
                        "matchNumStr": "周六026",
                        "leagueAbbName": "荷甲",
                        "leagueAllName": "荷兰甲级联赛",
                        "homeTeamAllName": "福图纳锡塔德",
                        "awayTeamAllName": "阿贾克斯",
                        "matchDate": "2026-09-13",
                        "matchTime": "02:00:00",
                        "bettingSingle": 0,
                        "had": {
                            "a": "1.28",
                            "d": "5.10",
                            "h": "6.60",
                            "updateDate": "2026-09-12",
                            "updateTime": "22:29:36",
                        },
                        "hhad": {
                            "a": "1.83",
                            "d": "4.10",
                            "h": "2.95",
                            "goalLine": "+1",
                            "goalLineValue": "+1.00",
                            "updateDate": "2026-09-12",
                            "updateTime": "22:29:27",
                        },
                        "crs": {
                            "s00s00": "32.00",
                            "s01s00": "26.00",
                            "s02s01": "9.00",
                            "s1sh": "40.00",
                            "s1sd": "18.00",
                            "s1sa": "6.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "21:10:00",
                        },
                        "ttg": {
                            "s0": "32.00",
                            "s3": "3.70",
                            "s7": "11.50",
                            "updateDate": "2026-09-12",
                            "updateTime": "17:56:39",
                        },
                        "hafu": {
                            "aa": "1.75",
                            "hh": "12.50",
                            "dh": "18.00",
                            "updateDate": "2026-09-12",
                            "updateTime": "19:49:59",
                        },
                    }
                ],
            }
        ]
    },
}


def test_parse_matches_full_shape() -> None:
    matches = sporttery.parse_matches(SAMPLE)
    assert len(matches) == 1
    m = matches[0]
    assert m.source_match_id == "2041430"
    assert m.code == "周六026"
    assert m.kickoff_utc == "2026-09-12T18:00:00+00:00"  # CST 02:00 → UTC 前一日 18:00
    assert m.is_single is False


def test_parse_market_selections() -> None:
    m = sporttery.parse_matches(SAMPLE)[0]
    by_code = {q.market_code: q for q in m.markets}
    assert by_code["had"].prices == {"h": 6.60, "d": 5.10, "a": 1.28}
    assert by_code["hhad"].goal_line == "+1"
    assert by_code["crs"].prices["0:0"] == 32.0
    assert by_code["crs"].prices["2:1"] == 9.0
    assert by_code["crs"].prices["h_other"] == 40.0
    assert by_code["ttg"].prices["3"] == 3.70
    assert by_code["hafu"].prices["aa"] == 1.75
    # 调盘时点随市场各自保留（CST→UTC）
    assert by_code["had"].captured_at == "2026-09-12T14:29:36+00:00"
    assert by_code["ttg"].captured_at == "2026-09-12T09:56:39+00:00"


def test_store_matches_idempotent(db) -> None:
    matches = sporttery.parse_matches(SAMPLE)
    first = sporttery.store_matches(db, matches)
    second = sporttery.store_matches(db, matches)
    assert first.matches == 1
    assert first.snapshots > 0
    assert second.snapshots == 0  # append-only 去重
    assert second.duplicate_snapshots == first.snapshots
    rows = fx_store.fixtures_for_business_date(db, "2026-09-12")
    assert len(rows) == 1
    assert rows[0]["competition_tier"] == "tier2"  # 荷甲
    history = fx_store.odds_history(db, int(rows[0]["id"]), "had")
    assert len(history) == 3  # h/d/a 各一条，重跑不增


def test_fetch_uses_referer_and_retries_payload(db) -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        assert "poolCode" in str(request.url)
        return httpx.Response(200, json=SAMPLE)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = sporttery.fetch_calculator_payload(Settings(), client)
    assert payload["errorCode"] == "0"
    assert seen_headers["referer"] == "https://www.sporttery.cn/"


def test_fetch_raises_on_error_code() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errorCode": "E0001"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="E0001"):
        sporttery.fetch_calculator_payload(Settings(), client)


# --- 票 35：观测证据 / 销售状态 / 改期 ---


def test_capture_records_observation_and_reparse(tmp_path, db) -> None:
    """原始响应可重解析、重复观测可审计（票 35 验收 2）。"""
    client = httpx.Client(
        transport=httpx.MockTransport(
            handler=lambda _: httpx.Response(200, json=SAMPLE)
        )
    )
    fixed = datetime(2026, 9, 12, 14, 30, tzinfo=UTC)
    stats = sporttery.capture_jingcai(
        db, Settings(), client, raw_root=tmp_path, now=fixed
    )
    assert stats.observation_id is not None
    obs = db.execute(
        "SELECT * FROM quote_observations WHERE id = ?", (stats.observation_id,)
    ).fetchone()
    assert obs["observed_at"] == "2026-09-12T14:30:00+00:00"
    assert obs["source"] == "sporttery"
    assert obs["raw_ref"] is not None
    # 快照携带证据列：源时间=调盘时间，观测时间=固定时钟
    snap = db.execute(
        "SELECT * FROM odds_snapshots WHERE market_code='had' ORDER BY id LIMIT 1"
    ).fetchone()
    assert snap["observed_at"] == "2026-09-12T14:30:00+00:00"
    assert snap["source_updated_at"] == "2026-09-12T14:29:36+00:00"
    assert snap["observation_id"] == stats.observation_id
    # 原始响应可重解析出同一批比赛
    raw = observations.read_raw(tmp_path, str(obs["raw_ref"]))
    reparsed = sporttery.parse_matches(json.loads(raw))
    assert [m.source_match_id for m in reparsed] == ["2041430"]
    # 重复观测：快照去重，但观测行 +1（再次观测有证据）
    second = sporttery.capture_jingcai(
        db,
        Settings(),
        client,
        raw_root=tmp_path,
        now=datetime(2026, 9, 12, 14, 40, tzinfo=UTC),
    )
    assert second.snapshots == 0
    assert second.duplicate_snapshots == stats.snapshots
    count = db.execute("SELECT COUNT(*) AS n FROM quote_observations").fetchone()["n"]
    assert count == 2
    # 密钥/请求参数不落证据文件
    assert "apiKey" not in raw.decode()


def test_capture_parses_sale_status_and_single(tmp_path, db) -> None:
    payload = json.loads(json.dumps(SAMPLE))
    payload["value"]["matchInfoList"][0]["subMatchList"][0]["sellStatus"] = "0"
    payload["value"]["matchInfoList"][0]["subMatchList"][0]["had"]["single"] = "1"
    client = httpx.Client(
        transport=httpx.MockTransport(
            handler=lambda _: httpx.Response(200, json=payload)
        )
    )
    sporttery.capture_jingcai(
        db,
        Settings(),
        client,
        raw_root=tmp_path,
        now=datetime(2026, 9, 12, 14, 30, tzinfo=UTC),
    )
    fixture = db.execute("SELECT id FROM fixtures").fetchone()["id"]
    had = db.execute(
        "SELECT * FROM sale_statuses WHERE fixture_id=? AND market_code='had'",
        (fixture,),
    ).fetchone()
    assert had["sale_state"] == "on_sale"
    assert had["single_eligible"] == 1
    # 未知 sellStatus 值 → unknown，不伪造
    payload["value"]["matchInfoList"][0]["subMatchList"][0]["sellStatus"] = "9"
    payload["value"]["matchInfoList"][0]["subMatchList"][0]["had"].pop("single")
    sporttery.capture_jingcai(
        db,
        Settings(),
        client,
        raw_root=tmp_path,
        now=datetime(2026, 9, 12, 14, 35, tzinfo=UTC),
    )
    latest = db.execute(
        "SELECT * FROM sale_statuses WHERE fixture_id=? AND market_code='had'"
        " ORDER BY id DESC LIMIT 1",
        (fixture,),
    ).fetchone()
    assert latest["sale_state"] == "unknown"
    # had 块 single 未知，但比赛级 bettingSingle=0 → 该场无单关（False，非未知）
    assert latest["single_eligible"] == 0


def test_single_eligibility_match_level_veto() -> None:
    match = sporttery.parse_matches(SAMPLE)[0]
    assert match.had_single_eligible() is False  # bettingSingle=0 → 无单关
    sample_single = json.loads(json.dumps(SAMPLE))
    sample_single["value"]["matchInfoList"][0]["subMatchList"][0]["bettingSingle"] = 1
    match2 = sporttery.parse_matches(sample_single)[0]
    assert match2.had_single_eligible() is None  # 比赛级开放但 had 块未知


def test_reschedule_updates_kickoff_not_new_fixture(db) -> None:
    """改期不新造比赛：同 matchId 的新开球时间更新原 fixture（票 35 验收 3）。"""
    first = json.loads(json.dumps(SAMPLE))
    sporttery.store_matches(db, sporttery.parse_matches(first))
    fixture_id = db.execute("SELECT id FROM fixtures").fetchone()["id"]
    moved = json.loads(json.dumps(SAMPLE))
    sub = moved["value"]["matchInfoList"][0]["subMatchList"][0]
    sub["matchDate"] = "2026-09-14"
    sub["matchTime"] = "02:00:00"
    sporttery.store_matches(db, sporttery.parse_matches(moved))
    rows = db.execute("SELECT * FROM fixtures").fetchall()
    assert len(rows) == 1  # 同一 fixture
    assert rows[0]["id"] == fixture_id
    assert rows[0]["kickoff_utc"] == "2026-09-13T18:00:00+00:00"


# --- 票 wb-04：进球类（ttg/crs）单固资格（poolList 池级 single）---


def _pool_list_payload(*, ttg_single: int, crs_single: int) -> dict:
    """带 poolList 的载荷（实测形状：market 块恒缺 single，池级 single 为 int）。"""
    payload = json.loads(json.dumps(SAMPLE))
    sub = payload["value"]["matchInfoList"][0]["subMatchList"][0]
    sub["poolList"] = [
        {"poolCode": "HAD", "single": 1},
        {"poolCode": "TTG", "single": ttg_single},
        {"poolCode": "CRS", "single": crs_single},
        {"poolCode": "HHAD", "single": 0},
    ]
    return payload


def test_goals_single_eligible_from_pool_list() -> None:
    match = sporttery.parse_matches(_pool_list_payload(ttg_single=1, crs_single=0))[0]
    assert match.goals_single_eligible("ttg") is True
    assert match.goals_single_eligible("crs") is False


def test_goals_single_eligible_market_block_wins_then_veto() -> None:
    payload = _pool_list_payload(ttg_single=0, crs_single=1)
    sub = payload["value"]["matchInfoList"][0]["subMatchList"][0]
    # 市场块 single 优先（防御路径：实测恒缺，出现时按票 35 同规则采信）
    sub["ttg"]["single"] = "1"
    match = sporttery.parse_matches(payload)[0]
    assert match.goals_single_eligible("ttg") is True
    # poolList 缺该池 + 比赛级 bettingSingle=0 → False；开放 → None（未知）
    no_pool = json.loads(json.dumps(SAMPLE))
    match2 = sporttery.parse_matches(no_pool)[0]
    assert match2.goals_single_eligible("ttg") is False
    no_pool["value"]["matchInfoList"][0]["subMatchList"][0]["bettingSingle"] = 1
    match3 = sporttery.parse_matches(no_pool)[0]
    assert match3.goals_single_eligible("crs") is None
    with pytest.raises(ValueError, match="非进球类玩法"):
        match3.goals_single_eligible("had")


def test_store_matches_records_goals_sale_status(db) -> None:
    """ttg/crs 销售状态行随快照落库（票 wb-04），had 行同链读 poolList（票 38）。"""
    match = sporttery.parse_matches(_pool_list_payload(ttg_single=1, crs_single=0))[0]
    sporttery.store_matches(db, [match])
    fixture = db.execute("SELECT id FROM fixtures").fetchone()["id"]
    rows = db.execute(
        "SELECT * FROM sale_statuses WHERE fixture_id=? ORDER BY market_code",
        (fixture,),
    ).fetchall()
    by_market = {row["market_code"]: row for row in rows}
    assert by_market[None]["sale_state"] == "on_sale"  # 比赛级
    assert "had" in by_market  # had 行保留（poolList HAD single=1，票 38 修正口径）
    assert by_market["had"]["single_eligible"] == 1
    assert by_market["ttg"]["market_code"] == "ttg"
    assert by_market["ttg"]["single_eligible"] == 1
    assert by_market["crs"]["single_eligible"] == 0
    assert by_market["ttg"]["sale_state"] == "on_sale"
    # hhad/hafu 不落市场级行（票据面范围之外，保持原行为）
    assert set(by_market) == {None, "had", "ttg", "crs"}


# --- 票 38：had 单固误记修正（poolList 池级 single）---


def test_had_single_eligible_from_pool_list() -> None:
    """误记形状（实证）：市场块缺 single + bettingSingle=0 + poolList had=1。"""
    match = sporttery.parse_matches(_pool_list_payload(ttg_single=1, crs_single=0))[0]
    assert match.is_single is False  # 比赛级关单关
    assert match.had_single_eligible() is True  # poolList 池级 → 单固
    off = json.loads(json.dumps(_pool_list_payload(ttg_single=1, crs_single=0)))
    for pool in off["value"]["matchInfoList"][0]["subMatchList"][0]["poolList"]:
        if pool["poolCode"] == "HAD":
            pool["single"] = 0
    assert sporttery.parse_matches(off)[0].had_single_eligible() is False


def test_had_single_eligible_pool_list_wins_on_conflict() -> None:
    """票 38 人裁决项：市场块字段与 poolList 冲突时以 poolList 为准。"""
    payload = _pool_list_payload(ttg_single=1, crs_single=0)
    sub = payload["value"]["matchInfoList"][0]["subMatchList"][0]
    sub["had"]["single"] = "0"  # 防御路径出现且与池级冲突（实证未观测）
    match = sporttery.parse_matches(payload)[0]
    assert match.had_single_eligible() is True  # poolList 裁决优先
    # poolList 缺 had 池时市场块仍采信（票 35 防御路径保留）
    no_had_pool = json.loads(json.dumps(payload))
    pools = no_had_pool["value"]["matchInfoList"][0]["subMatchList"][0]["poolList"]
    pools[:] = [pool for pool in pools if pool["poolCode"] != "HAD"]
    match2 = sporttery.parse_matches(no_had_pool)[0]
    assert match2.had_single_eligible() is False  # 市场块 single="0"


def test_had_single_eligible_legacy_and_unknown_pool_value() -> None:
    """旧数据兼容：无 poolList 时票 35 口径逐分支持保留。"""
    # 无 poolList + 市场块 single（票 35 主路径）
    legacy = json.loads(json.dumps(SAMPLE))
    legacy["value"]["matchInfoList"][0]["subMatchList"][0]["had"]["single"] = "1"
    assert sporttery.parse_matches(legacy)[0].had_single_eligible() is True
    # 无 poolList + 市场块缺 + bettingSingle=0 → False（票 35 否决）
    assert sporttery.parse_matches(SAMPLE)[0].had_single_eligible() is False
    # poolList 列了 had 池但 single 值未知 → None（保守未知，不被比赛级否决翻转）
    unknown = json.loads(json.dumps(_pool_list_payload(ttg_single=1, crs_single=0)))
    for pool in unknown["value"]["matchInfoList"][0]["subMatchList"][0]["poolList"]:
        if pool["poolCode"] == "HAD":
            pool["single"] = "x"
    assert sporttery.parse_matches(unknown)[0].had_single_eligible() is None


def _seed_observation(
    db, tmp_path, *, payload: dict, raw_ref: bool = True
) -> tuple[int, str]:
    """
    手工落一条观测证据（含/不含原始文件），返回 ``(observation_id, observed_at)``。

    重解析测试用它搭 v2 底座：observed_at 固定，v2 行随后由
    ``store_matches`` 以"旧解析（忽略 poolList）"写入——与 sale_statuses
    的 append-only 触发器相容，且忠实复现 v2 采集路径。
    """
    raw = json.dumps(payload).encode()
    sha, ref = (
        observations.save_raw(tmp_path, "sporttery", raw)
        if raw_ref
        else (observations.sha256_hex(raw), None)
    )
    observed = "2026-09-12T14:30:00+00:00"
    observation_id = fx_store.record_quote_observation(
        db,
        ObservationInput(
            source="sporttery",
            purpose=ObservationPurpose.LIVE,
            observed_at=observed,
            endpoint="getMatchCalculatorV1.qry",
            parse_version="sporttery_calculator_v2",
            raw_sha256=sha,
            raw_ref=ref,
            summary="matches=1",
        ),
    )
    return observation_id, observed


def _drop_pool_list(payload: dict) -> dict:
    stripped = json.loads(json.dumps(payload))
    sub = stripped["value"]["matchInfoList"][0]["subMatchList"][0]
    sub.pop("poolList", None)
    return stripped


def test_reprocess_corrects_misrecord_and_is_idempotent(db, tmp_path) -> None:
    """存量修正：重解析原始证据追加修正行（旧行保留），重复执行零增量。"""
    payload = _pool_list_payload(ttg_single=1, crs_single=0)
    observation_id, observed = _seed_observation(db, tmp_path, payload=payload)
    # 模拟 v2 采集：旧解析不读 poolList → bettingSingle=0 否决 → had 误记 0
    v2_matches = sporttery.parse_matches(_drop_pool_list(payload))
    assert v2_matches[0].had_single_eligible() is False
    sporttery.store_matches(
        db, v2_matches, observed_at=observed, observation_id=observation_id
    )
    fixture = db.execute("SELECT id FROM fixtures").fetchone()["id"]
    misrecord = fx_store.latest_sale_status_asof(
        db, fixture, "had", "2026-09-12T14:31:00+00:00"
    )
    assert misrecord is not None
    assert misrecord["single_eligible"] == 0
    sale_rows_before = db.execute("SELECT COUNT(*) AS n FROM sale_statuses").fetchone()[
        "n"
    ]

    first = sporttery.reprocess_observations(db, tmp_path)
    assert first.observations == 1
    assert first.reparsed == 1
    assert first.snapshots == 0  # 快照 append-only 判重（v2 行已写入同内容）
    assert first.duplicate_snapshots > 0
    # 修正行追加：同证据身份（observation_id/observed_at）+ single=1，
    # 旧误记行保留（append-only 审计痕迹，sale_statuses 禁 UPDATE）
    had_rows = db.execute(
        "SELECT single_eligible FROM sale_statuses"
        " WHERE fixture_id=? AND market_code='had' ORDER BY id",
        (fixture,),
    ).fetchall()
    assert [row["single_eligible"] for row in had_rows] == [0, 1]
    corrected = fx_store.latest_sale_status_asof(
        db, fixture, "had", "2026-09-12T14:31:00+00:00"
    )
    assert corrected is not None
    assert corrected["single_eligible"] == 1
    # as-of 早于首个观测 → 无行（不倒填时间线）
    assert (
        fx_store.latest_sale_status_asof(
            db, fixture, "had", "2026-09-12T10:00:00+00:00"
        )
        is None
    )

    # 第一次重解析的增量 = 2：had 误记修正行（0→1）+ ttg 行（v2 模拟缺
    # poolList 时被比赛级否决成 False，重解析按池级回到 1；真实 v2 库无
    # ttg 市场行，重解析只补新行）。比赛级/crs 行同内容被证据身份判重。
    sale_rows_mid = db.execute("SELECT COUNT(*) AS n FROM sale_statuses").fetchone()[
        "n"
    ]
    assert sale_rows_mid == sale_rows_before + 2

    # 幂等：再跑一遍零增量
    second = sporttery.reprocess_observations(db, tmp_path)
    assert second.reparsed == 1
    assert second.snapshots == 0
    sale_rows_after = db.execute("SELECT COUNT(*) AS n FROM sale_statuses").fetchone()[
        "n"
    ]
    assert sale_rows_after == sale_rows_mid


def test_reprocess_skips_observations_without_raw(db, tmp_path) -> None:
    """raw_ref 为空（只记哈希）的观测跳过，不入库不报错。"""
    payload = _pool_list_payload(ttg_single=1, crs_single=0)
    _seed_observation(db, tmp_path, payload=payload, raw_ref=False)
    stats = sporttery.reprocess_observations(db, tmp_path)
    assert stats.observations == 1
    assert stats.reparsed == 0
    assert stats.skipped_no_raw == 1
    assert stats.matches == 0
    assert db.execute("SELECT COUNT(*) AS n FROM fixtures").fetchone()["n"] == 0


def test_reprocess_fails_closed_on_hash_mismatch(db, tmp_path) -> None:
    """证据哈希不符 = 损坏：立即失败，不产生部分修正。"""
    payload = _pool_list_payload(ttg_single=1, crs_single=0)
    observation_id, _ = _seed_observation(db, tmp_path, payload=payload)
    obs = db.execute(
        "SELECT raw_ref FROM quote_observations WHERE id = ?", (observation_id,)
    ).fetchone()
    (tmp_path / str(obs["raw_ref"])).write_bytes(gzip.compress(b"tampered"))
    with pytest.raises(ValueError, match="hash mismatch"):
        sporttery.reprocess_observations(db, tmp_path)


# --- 票 47：停售加密探测（决策锚触发器） ---

EMPTY_PAYDAY = {"errorCode": "0", "value": {"matchInfoList": []}}


def _seed_joined_sample(db, tmp_path) -> int:
    """入库 SAMPLE 场次（on_sale，固定早期时钟保时间线单调）并完成欧赔 join。"""
    client = httpx.Client(
        transport=httpx.MockTransport(
            handler=lambda _: httpx.Response(200, json=SAMPLE)
        )
    )
    sporttery.capture_jingcai(
        db,
        Settings(),
        client,
        raw_root=tmp_path,
        now=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
    )
    row = fx_store.find_fixture_by_source_match(db, "jingcai", "2041430")
    assert row is not None
    fx_store.set_odds_api_join(
        db, int(row["id"]), "evt-2041430", "soccer_netherlands_eredivisie", "manual"
    )
    return int(row["id"])


def _probe_client(payload: dict, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_probe_no_candidates_makes_no_request(db, tmp_path) -> None:
    calls: list[str] = []
    probe = sporttery.probe_sale_stops(
        db, Settings(), _probe_client(SAMPLE, calls), raw_root=tmp_path
    )
    assert probe.candidates == 0
    assert probe.stopped == []
    assert calls == []


def test_probe_disappearance_marks_stopped_once(db, tmp_path) -> None:
    fixture_id = _seed_joined_sample(db, tmp_path)
    now = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)  # SAMPLE 开球前 90 分钟
    calls: list[str] = []
    probe = sporttery.probe_sale_stops(
        db, Settings(), _probe_client(EMPTY_PAYDAY, calls), raw_root=tmp_path, now=now
    )
    assert probe.candidates == 1
    assert [int(r["id"]) for r in probe.stopped] == [fixture_id]
    assert calls  # 全链路采集发生
    latest = db.execute(
        "SELECT sale_state, observed_at FROM sale_statuses WHERE fixture_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (fixture_id,),
    ).fetchone()
    assert latest["sale_state"] == "stopped"
    assert latest["observed_at"] == "2026-09-12T16:30:00+00:00"
    # 幂等：已 stopped 后不再是候选 → 零请求
    calls2: list[str] = []
    again = sporttery.probe_sale_stops(
        db,
        Settings(),
        _probe_client(EMPTY_PAYDAY, calls2),
        raw_root=tmp_path,
        now=datetime(2026, 9, 12, 16, 40, tzinfo=UTC),
    )
    assert again.candidates == 0
    assert again.stopped == []
    assert calls2 == []


def test_probe_explicit_sell_status_stopped(db, tmp_path) -> None:
    _seed_joined_sample(db, tmp_path)
    stopped_payload = json.loads(json.dumps(SAMPLE))
    stopped_payload["value"]["matchInfoList"][0]["subMatchList"][0]["sellStatus"] = "1"
    now = datetime(2026, 9, 12, 17, 0, tzinfo=UTC)
    probe = sporttery.probe_sale_stops(
        db, Settings(), _probe_client(stopped_payload, []), raw_root=tmp_path, now=now
    )
    assert len(probe.stopped) == 1


def test_probe_on_sale_no_anchor(db, tmp_path) -> None:
    _seed_joined_sample(db, tmp_path)
    now = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)
    probe = sporttery.probe_sale_stops(
        db, Settings(), _probe_client(SAMPLE, []), raw_root=tmp_path, now=now
    )
    assert probe.candidates == 1
    assert probe.stopped == []


def test_probe_unknown_state_not_marked_stopped(db, tmp_path) -> None:
    """从未观测在售（unknown）的场次消失不冒充停售迁移（评审修正）。"""
    # 直接种子：有 fixture/join/match_code 但无 sale_statuses（unknown 态）
    competition = fx_store.upsert_competition(
        db, "荷甲", odds_api_sport_key="soccer_netherlands_eredivisie"
    )
    home = fx_store.upsert_team(db, "主队U")
    away = fx_store.upsert_team(db, "客队U")
    fixture_id = fx_store.upsert_fixture(
        db, competition, "2026-09-12T18:00:00+00:00", home, away
    )
    from goalx_backend.models import MatchCodeInput

    fx_store.upsert_match_code(
        db,
        MatchCodeInput(
            fixture_id=fixture_id,
            kind="jingcai",
            business_date="2026-09-12",
            code="周六0U",
            source_match_id="2041430",
        ),
    )
    fx_store.set_odds_api_join(
        db, fixture_id, "evt-u", "soccer_netherlands_eredivisie", "manual"
    )
    db.commit()
    now = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)
    probe = sporttery.probe_sale_stops(
        db, Settings(), _probe_client(EMPTY_PAYDAY, []), raw_root=tmp_path, now=now
    )
    assert probe.candidates >= 1
    assert probe.stopped == []
    assert db.execute("SELECT COUNT(*) AS n FROM sale_statuses").fetchone()["n"] == 0


def test_probe_window_two_regimes(db, tmp_path) -> None:
    """候选两形态：临场 ≤3h 恒探测；北京 ≥19 点探测 16h 内（凌晨场前夜墙钟）。"""
    _seed_joined_sample(db, tmp_path)  # 开球 2026-09-12T18:00Z
    cases = (
        (datetime(2026, 9, 12, 16, 30, tzinfo=UTC), SAMPLE, 1),  # 临场 90 分 → 候选
        (datetime(2026, 9, 12, 12, 0, tzinfo=UTC), SAMPLE, 1),  # 北京 20 点、−6h → 候选
        (
            datetime(2026, 9, 12, 2, 0, tzinfo=UTC),
            EMPTY_PAYDAY,
            0,
        ),  # 北京 10 点 → 非候选
    )
    results = [
        sporttery.probe_sale_stops(
            db, Settings(), _probe_client(payload, []), raw_root=tmp_path, now=now
        ).candidates
        for now, payload, _ in cases
    ]
    assert results == [want for _, _, want in cases]
