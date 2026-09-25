"""竞彩官方 SP 历史采集测试（票 70）：解析纯函数 + 采集闭环（jc provider）。

样本为 2026-09-26 凌晨实测响应裁剪（官方域；队名/联赛名按原样）。fixture
值照源（hhad goalLine/ttg 八档/封盘旗/留档玩法）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc

# —— 实测样本裁剪（matchId 2041656 中国 vs 马尔代夫 + 老年份分布形态）———

_HHAD_ROW = {
    "h": "2.30",
    "d": "4.80",
    "a": "2.06",
    "goalLine": "-4",
    "updateDate": "2026-09-23",
    "updateTime": "09:17:34",
    "hf": "0",
    "df": "0",
    "af": "0",
}
_HAD_ROW = {
    "h": "2.05",
    "d": "3.20",
    "a": "3.05",
    "updateDate": "2026-09-23",
    "updateTime": "09:17:34",
}
_TTG_ROW = {
    "s0": "100.0",
    "s1": "2.6",
    "s2": "2.7",
    "s3": "5.65",
    "s4": "4.60",
    "s5": "4.75",
    "s6": "5.80",
    "s7": "3.95",
    "s0f": "0",
    "s1f": "0",
    "s2f": "0",
    "s3f": "0",
    "s4f": "0",
    "s5f": "0",
    "s6f": "0",
    "s7f": "0",
    "goalLine": "",
    "updateDate": "2026-09-23",
    "updateTime": "09:17:34",
}
_CRS_ROW = {
    "s05s00": "6.90",
    "s-1sd": "800.0",
    "updateDate": "2026-09-23",
    "updateTime": "09:17:34",
}

SAMPLE_VALUE: dict[str, Any] = {
    "isCancel": 0,
    "sectionsNo999": "3:0",
    "matchResultList": [{"had": "h", "half": "h"}],
    "oddsHistory": {
        "matchId": 2041656,
        "homeTeamAbbName": "中国",
        "awayTeamAbbName": "马尔代夫",
        "leagueAbbName": "国际赛",
        "hadList": [_HAD_ROW],
        "hhadList": [_HHAD_ROW],
        "ttgList": [_TTG_ROW],
        "hafuList": [],
        "crsList": [_CRS_ROW],  # 留档不消费（解析不进消费链，bronze 原样带）
        "singleList": [{"single": 1, "poolCode": "HHAD"}],
    },
}
SAMPLE_BODY = json.dumps(
    {"success": True, "errorCode": "0", "value": SAMPLE_VALUE}, ensure_ascii=False
).encode()
EMPTY_BODY = json.dumps(
    {"success": True, "value": {"oddsHistory": {}, "isCancel": 0}}
).encode()


def _settings(tmp_path: Path) -> Settings:
    return Settings(corpus_root=tmp_path / "corpus")


def _client(routes: dict[str, httpx.Response] | None = None) -> httpx.Client:
    """MockTransport：按 matchId 参数分发（缺省=样本场）。"""
    default = httpx.Response(200, content=SAMPLE_BODY)

    def handler(request: httpx.Request) -> httpx.Response:
        mid = str(request.url.params.get("matchId") or "")
        if routes and mid in routes:
            return routes[mid]
        return default

    return httpx.Client(transport=httpx.MockTransport(handler))


# —— 解析纯函数 ———————————————————————————————————————————————


def test_parse_fixed_bonus_shapes() -> None:
    value = jc.parse_fixed_bonus(SAMPLE_BODY)
    oh = value["oddsHistory"]
    assert oh["matchId"] == 2041656
    # hhad 逐笔 goalLine；had 行不带盘口语义
    assert oh["hhadList"][0]["goalLine"] == "-4"
    assert oh["hadList"][0].get("goalLine") in (None, "")
    # ttg 八档长格式（s0~s7 + 封盘旗）
    assert all(f"s{i}" in oh["ttgList"][0] for i in range(8))
    assert all(f"s{i}f" in oh["ttgList"][0] for i in range(8))
    # 留档玩法原样在（不建消费链=PLAYTYPES 不含）
    assert oh["crsList"]
    assert jc.PLAYTYPES == ("had", "hhad", "ttg")


def test_parse_fixed_bonus_empty_is_legal() -> None:
    value = jc.parse_fixed_bonus(EMPTY_BODY)
    assert value["oddsHistory"] == {}  # 空≠无：非 JC 场合法空


def test_parse_fixed_bonus_bad_shapes() -> None:
    with pytest.raises(jc.JcContentError):
        jc.parse_fixed_bonus(b"")  # 非 JSON（空参实测形态）
    with pytest.raises(jc.JcContentError):
        jc.parse_fixed_bonus(
            json.dumps({"success": False, "errorMessage": "x"}).encode()
        )
    with pytest.raises(jc.JcContentError):
        jc.parse_fixed_bonus(
            json.dumps({"success": True, "value": {"isCancel": 0}}).encode()
        )


# —— 采集闭环 ————————————————————————————————————————————————


def test_collect_match_lands_raw_and_bronze(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    stats = jc.collect_match(store, settings, _client(), "2041656")
    store.close()
    assert stats.raw_new == 1
    assert stats.bronze_new == 1
    assert stats.rows == {"had": 1, "hhad": 1, "ttg": 1}
    # jc provider 物理隔离：树路径 raw/jc/sp_history/，与 srct 零共享
    assert (settings.corpus_root / "raw/jc/sp_history/2041656.json.gz").exists()
    rows = store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET)
    assert len(rows) == 1
    assert rows[0]["sid"] == "2041656"
    assert rows[0]["parser_version"] == jc.BRONZE_VERSION
    assert rows[0]["payload"]["oddsHistory"]["hhadList"][0]["goalLine"] == "-4"


def test_collect_match_cached_no_refetch(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    client = _client()
    stats = jc.collect_match(store, settings, client, "2041656")
    stats2 = jc.collect_match(store, settings, client, "2041656")
    store.close()
    assert stats.requests == 1
    assert stats2.cached == 1
    assert stats2.requests == 0
    assert len(store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET)) == 1


def test_collect_match_empty_lands_raw_only(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    client = _client(routes={"1": httpx.Response(200, content=EMPTY_BODY)})
    stats = jc.collect_match(store, settings, client, "1")
    store.close()
    assert stats.empty == 1
    assert stats.raw_new == 1
    assert store.read_bronze(jc.JC_PROVIDER, jc.SP_DATASET) == []


def test_collect_match_failure_counted_no_raw(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    client = _client(routes={"9": httpx.Response(500, text="boom")})
    stats = jc.collect_match(store, settings, client, "9")
    store.close()
    assert stats.failed["9"]
    assert not store.has(jc.JC_PROVIDER, jc.SP_DATASET, "9")  # 失败无键=可重试
