"""JC 历史回填编排测试（票 67）：日账防重扫 + 预算触顶收口。

样本行形状照 2026-09-26 实测裁剪（uniform 按日反查 + fixedBonus）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore
from goalx_backend.data.ingest import jc_backfill, srct

UNIFORM_BODY = json.dumps(
    {
        "success": True,
        "value": {
            "matchResult": [
                {"matchId": 700001, "matchDate": "2026-09-22"},
                {"matchId": 700002, "matchDate": "2026-09-22"},
            ],
            "pages": 1,
        },
    }
).encode()
FIXED_BODY = json.dumps(
    {
        "success": True,
        "value": {
            "oddsHistory": {
                "matchId": 700001,
                "hadList": [
                    {
                        "h": "2.0",
                        "d": "3.0",
                        "a": "3.5",
                        "updateDate": "2026-09-22",
                        "updateTime": "10:00:00",
                    }
                ],
                "hhadList": [],
                "ttgList": [],
            }
        },
    }
).encode()


def _client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if "getUniformMatchResultV1" in request.url.path:
            return httpx.Response(200, content=UNIFORM_BODY)
        if "getFixedBonusV1" in request.url.path:
            return httpx.Response(200, content=FIXED_BODY)
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _settings(tmp_path: Any) -> Settings:
    return Settings(corpus_root=tmp_path / "corpus")


def _run(
    tmp_path: Any, **kwargs: Any
) -> tuple[jc_backfill.JcBackfillStats, CorpusStore, srct.NightBudget]:
    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    kwargs.setdefault("sleeper", lambda _s: None)
    kwargs.setdefault("sleep_seconds", 0)
    budget = kwargs.pop("budget", srct.NightBudget(request_cap=100))
    stats = jc_backfill.backfill_range(
        store,
        settings,
        _client(),
        date_to="2026-09-22",
        date_from="2026-09-22",
        budget=budget,
        **kwargs,
    )
    return stats, store, budget


def test_day_ledger_records_and_skips_on_rerun(tmp_path: Any) -> None:
    """采齐日入账；重跑零请求零重采（2026-09-26 夜窗实跑暴露的缺口）。"""
    stats1, store, budget1 = _run(tmp_path)
    assert stats1.matches_collected == 2
    assert stats1.days_done == 0  # 采齐但当日含采集 → days_done 只计跳过日
    assert store.jc_backfill_days("done") == {"2026-09-22"}
    stats2, _, budget2 = _run(tmp_path)
    assert stats2.days_done == 1  # 日账命中跳过
    assert budget2.requests == 0  # 计数权威口径在预算对象
    assert stats2.matches_collected == 0
    assert budget1.requests > 0


def test_budget_stop_returns_not_raises(tmp_path: Any) -> None:
    """预算触顶=正常返回（stopped=budget），进度已落库（CLI/夜班统一接）。"""
    stats, store, budget = _run(tmp_path, budget=srct.NightBudget(request_cap=3))
    assert stats.stopped == "budget"
    assert budget.requests == 3
    assert store.jc_backfill_days("done") == set()  # 半途日不入账


# —— 票 03 缺账：uniform 行缺 matchId 不得零痕迹 ——
def test_rows_without_matchid_counted(tmp_path: Any) -> None:
    """行在而 matchId 缺（形状漂移候选）：计数进 stats，日账判定行为不变。"""
    body = json.dumps(
        {
            "success": True,
            "value": {
                "matchResult": [
                    {"matchId": 700001, "matchDate": "2026-09-22"},
                    {"matchId": None, "matchDate": "2026-09-22"},  # 形状漂移行
                    {"matchDate": "2026-09-22"},  # 键缺失行
                ],
                "pages": 1,
            },
        }
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if "getUniformMatchResultV1" in request.url.path:
            return httpx.Response(200, content=body)
        if "getFixedBonusV1" in request.url.path:
            return httpx.Response(200, content=FIXED_BODY)
        return httpx.Response(500, text="no-route")

    settings = _settings(tmp_path)
    store = CorpusStore(settings.corpus_root)
    mids, dropped = jc_backfill.enumerate_day_mids(
        httpx.Client(transport=httpx.MockTransport(handler)), settings, "2026-09-22"
    )
    assert mids == ["700001"]
    assert dropped == 2
    stats = jc_backfill.backfill_range(
        store,
        settings,
        httpx.Client(transport=httpx.MockTransport(handler)),
        date_to="2026-09-22",
        date_from="2026-09-22",
        budget=srct.NightBudget(request_cap=100),
        sleeper=lambda _s: None,
        sleep_seconds=0,
    )
    assert stats.rows_without_matchid == 2
    # 行为不变：唯一可采 mid 照采，日账照走
    assert stats.mids_seen == 1
    assert store.jc_backfill_days("done") == {"2026-09-22"}
