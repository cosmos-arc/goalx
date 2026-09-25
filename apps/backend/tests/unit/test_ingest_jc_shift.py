"""JC 当期实时拍测试（票 71）：发现解析 + 四类拍闭环 + 幂等自愈 + 同 deployment 集成。

样本为 2026-09-26 凌晨实测裁剪（calculator subMatchList 行形状、fixedBonus
oddsHistory 行形状照源；官方域队名按原样）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc, jc_shift, srct, srct_shift
from goalx_backend.rate_limit import default_limiter


@pytest.fixture(autouse=True)
def _wide_rate_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from limits import RateLimitItemPerMinute

    monkeypatch.setattr(srct, "REQUEST_WINDOW", RateLimitItemPerMinute(10**6))


# —— calculator 发现样本（subMatchList 行形状照实测裁剪）———

_CALC_SUB = {
    "matchId": 2041691,
    "matchDate": "2026-09-26",
    "matchTime": "02:45:00",
    "leagueAbbName": "欧国联",
    "homeTeamAbbName": "意大利",
    "awayTeamAbbName": "比利时",
    "matchNumStr": "周五008",
}
CALC_PAYLOAD: dict[str, Any] = {
    "errorCode": 0,
    "value": {
        "matchInfoList": [
            {"businessDate": "2026-09-25", "subMatchList": [_CALC_SUB]},
        ],
        "totalCount": 1,
    },
}


def _fixed_body(had_rows: list[dict[str, Any]]) -> bytes:
    value = {
        "isCancel": 0,
        "oddsHistory": {
            "matchId": 2041691,
            "homeTeamAbbName": "意大利",
            "awayTeamAbbName": "比利时",
            "leagueAbbName": "欧国联",
            "hadList": had_rows,
            "hhadList": [],
            "ttgList": [],
            "hafuList": [],
            "crsList": [],
        },
    }
    return json.dumps({"success": True, "value": value}, ensure_ascii=False).encode()


_HAD_T1 = {
    "h": "2.00",
    "d": "3.30",
    "a": "3.06",
    "updateDate": "2026-09-25",
    "updateTime": "18:17:02",
}
_HAD_T2 = {
    "h": "1.85",
    "d": "3.40",
    "a": "3.40",
    "updateDate": "2026-09-25",
    "updateTime": "20:00:00",
}


def _client(
    *,
    calc: dict[str, Any] | None = None,
    fixed_responses: list[bytes] | None = None,
    basid: bytes | None = None,
) -> httpx.Client:
    """三端点族 MockTransport：calculator + fixedBonus（可编程序列）+ BaSID。"""
    fixed = list(fixed_responses or [_fixed_body([_HAD_T1])])
    calc_payload = calc if calc is not None else CALC_PAYLOAD
    basid_body = basid or b'var Ba_Soccer="";'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("getMatchCalculatorV1.qry"):
            return httpx.Response(200, json=calc_payload)
        if path.endswith("getFixedBonusV1.qry"):
            if fixed:
                return httpx.Response(200, content=fixed.pop(0))
            last = fixed_responses[-1] if fixed_responses else _fixed_body([_HAD_T1])
            return httpx.Response(200, content=last)
        if "BaSID" in path:
            return httpx.Response(200, content=basid_body)
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        corpus_root=tmp_path / "corpus",
        srct_basid_url="https://srct.test/basid/BaSID.js",
        srct_detail_url="https://live.test/detail/{sid}cn.htm",
    )


NOW = datetime(2026, 9, 25, 17, 30, tzinfo=UTC)  # 26 日 01:30 京（欧国联 02:45 场前）


# —— 发现解析 ———————————————————————————————————————————————


def test_parse_on_sale_matches() -> None:
    on_sale = jc_shift.parse_on_sale_matches(CALC_PAYLOAD)
    assert list(on_sale) == ["2041691"]
    row = on_sale["2041691"]
    assert row.kickoff_utc == "2026-09-25T18:45:00+00:00"  # 02:45 京 → 前日 18:45 UTC
    assert (row.league, row.home, row.away) == ("欧国联", "意大利", "比利时")
    assert jc_shift.parse_on_sale_matches({"value": {}}) == {}


# —— 四类拍闭环 ——————————————————————————————————————————————


def test_open_beat_then_close_beat_and_keys(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    # 首拍（开球前 1.25h ≤ 2h 晚场临场窗）：开售拍先落
    stats = jc_shift.run_jc_beats(
        store, settings, _client(), now=NOW, sleeper=lambda _s: None
    )
    assert stats.discovered == 1
    assert stats.open_beats == 1
    assert store.has(jc.JC_PROVIDER, jc.SP_DATASET, "2041691@open")
    # 二拍（同窗）：临场拍补齐，bronze 行 sid=matchId
    stats2 = jc_shift.run_jc_beats(
        store,
        settings,
        _client(),
        now=NOW + timedelta(minutes=15),
        sleeper=lambda _s: None,
    )
    assert stats2.open_beats == 0
    assert stats2.close_beats == 1
    rows = store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET)
    assert {str(r["sid"]) for r in rows} == {"2041691"}
    ledger = store.jc_shift_matches()
    assert ledger["2041691"]["beats"] == 2
    # 三拍：全键在 → 只剩发现请求（拍零重抓）
    stats3 = jc_shift.run_jc_beats(
        store,
        settings,
        _client(),
        now=NOW + timedelta(minutes=20),
        sleeper=lambda _s: None,
    )
    assert stats3.requests == 1
    assert (stats3.open_beats, stats3.close_beats, stats3.finalized) == (0, 0, 0)


def test_self_heal_full_history_no_loss(tmp_path: Path) -> None:
    """幂等自愈（验收）：两拍间 SP 变化，第二拍拉全量历史零丢失。"""
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    client = _client(
        fixed_responses=[
            _fixed_body([_HAD_T1]),  # 第一拍：仅 T1
            _fixed_body([_HAD_T1, _HAD_T2]),  # 第二拍：全量（T1+T2）
        ]
    )
    jc_shift.run_jc_beats(store, settings, client, now=NOW, sleeper=lambda _s: None)
    jc_shift.run_jc_beats(
        store,
        settings,
        client,
        now=NOW + timedelta(minutes=15),
        sleeper=lambda _s: None,
    )
    rows = store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET)
    second = rows[-1]["payload"]["oddsHistory"]["hadList"]
    assert len(second) == 2  # T1 没丢——oddsHistory 全量返回=漏拍自愈


def test_finalize_after_kickoff_bare_key(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    client = _client()
    jc_shift.run_jc_beats(store, settings, client, now=NOW, sleeper=lambda _s: None)
    # 开球后 2h（过 30min 缓冲）：收口裸键 + finalized 旗
    after = NOW + timedelta(hours=2, minutes=30)
    jc_shift.run_jc_beats(store, settings, client, now=after, sleeper=lambda _s: None)
    # NOW 后 2.5h，discovery 里场次仍在 calculator（matchDate 已过但清单未裁）
    # → on-sale 循环 skip（已开球），收口循环补裸键
    stats2 = store.jc_shift_matches()
    assert stats2["2041691"]["finalized"] == 1
    assert store.has(jc.JC_PROVIDER, jc.SP_DATASET, "2041691")  # 裸键收口在
    # 再跑：finalized → 零请求零重复收口
    stats3 = jc_shift.run_jc_beats(
        store,
        settings,
        _client(),
        now=after + timedelta(minutes=30),
        sleeper=lambda _s: None,
    )
    assert stats3.requests == 1  # 仅发现
    assert stats3.finalized == 0


def test_discovery_failure_does_not_raise(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    stats = jc_shift.run_jc_beats(
        store,
        settings,
        httpx.Client(transport=httpx.MockTransport(boom)),
        now=NOW,
        sleeper=lambda _s: None,
    )
    assert "jc:discovery" in stats.failed
    assert stats.discovered == 0


# —— 同 deployment 集成（run_shift 内 JC 相位）——————————————————


def test_run_shift_absorbs_jc_phase(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    active = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)  # 18:30 京：活动窗
    stats = srct_shift.run_shift(
        store,
        settings,
        _client(basid=b'var Ba_Soccer="";'),
        now_fn=lambda: active,
        sleeper=lambda _s: None,
        jitter=None,
        rate_limiter=default_limiter(),
    )
    assert stats.stopped is None
    assert stats.jc_discovered == 1
    assert stats.jc_open_beats == 1
    assert stats.requests == 3  # BaSID 入口 + calculator 发现 + fixedBonus 开售拍
    assert store.has(jc.JC_PROVIDER, jc.SP_DATASET, "2041691@open")
