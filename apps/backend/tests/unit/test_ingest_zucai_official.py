"""传统足彩官方采集测试（票 68）：解析纯函数 + MockTransport 入库闭环。

样本为 2026-09-26 凌晨实测响应裁剪（官方域，队名/联赛名按原样；奖级数值
照源）。端点模板走 Settings 缺省（官方 URL 非代称敏感）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from goalx_backend.config import Settings
from goalx_backend.data import pool as pool_store
from goalx_backend.data.ingest import zucai_official

# —— 实测样本裁剪 ————————————————————————————————————————————

_CURRENT_VALUE: dict[str, Any] = {
    "sfcMatch": {
        "lotteryDrawNum": "26133",
        "lotteryGameName": "胜负游戏",
        "lotterySaleEndtime": "2026-09-26 20:30:00",
        "lotteryDrawTime": "2026-09-28 14:00:00",
        "matchList": [
            {
                "matchNum": 1,
                "matchName": "国际赛",
                "masterTeamAllName": "中国",
                "guestTeamAllName": "新西兰",
                "infohubMatchId": 2041732,
                "startTime": "2026-09-27",
                "czScore": "",
            },
            {
                "matchNum": 2,
                "matchName": "英超",
                "masterTeamAllName": "阿森纳",
                "guestTeamAllName": "热刺",
                "infohubMatchId": 2041801,
                "startTime": "2026-09-28",
                "czScore": "",
            },
        ],
        "lastPoolDraw": {
            "lotteryDrawNum": "26131",
            "lotteryGameName": "胜负游戏",
            "lotteryDrawTime": "2026-09-22",
            "totalSaleAmount": "25,162,994",
            "poolBalanceAfterDraw": "0",
            "prizeLevelList": [
                {
                    "prizeLevel": "一等奖",
                    "stakeCount": "11",
                    "stakeAmount": "1,024,820",
                    "totalPrizeamount": "11,273,027.72",
                },
                {
                    "prizeLevel": "二等奖",
                    "stakeCount": "296",
                    "stakeAmount": "2,236",
                    "totalPrizeamount": "661,996.64",
                },
            ],
        },
        "lastPoolDrawRj": {
            "lotteryDrawNum": "26131",
            "lotteryGameName": "任九游戏",
            "totalSaleAmount": "20,386,268",
            "prizeLevelList": [
                {
                    "prizeLevel": "一等奖",
                    "stakeCount": "41",
                    "stakeAmount": "1,824",
                    "totalPrizeamount": "431,643.66",
                }
            ],
        },
    },
    "jqcMatch": {},
    "jqclist": [],
}

_DRAW_VALUE: dict[str, Any] = {
    "lotteryDrawNum": "26130",
    "lotteryGameName": "胜负游戏",
    "lotterySaleEndtime": "2026-09-19 21:00:00",
    "lotteryDrawTime": "2026-09-20",
    "totalSaleAmount": "18897366",
    "poolBalanceAfterdraw": "0",
    "totalSaleAmountRj": "20386268",
    "poolBalanceAfterdrawRj": "0",
    "lotteryDrawResult": "3 3 0 1 3 0 3 0 1 0 1 3 0 3",
    "matchList": [
        {
            "matchNum": 1,
            "matchName": "英超",
            "masterTeamAllName": "伊普斯维奇",
            "guestTeamAllName": "布伦特福德",
            "infohubMatchId": 0,
            "startTime": "2026-09-19",
            "czScore": "1:0",
        }
    ],
    "prizeLevelList": [
        {
            "prizeLevel": "一等奖",
            "stakeCount": "2",
            "stakeAmount": "445,579",
            "totalPrizeamount": "891,158.00",
        }
    ],
}


def _mock_client(
    *, jqc_on_sale: bool = False, draw_values: dict[str, dict[str, Any]] | None = None
) -> httpx.Client:
    """按端点分发的 MockTransport（当期 + 历史两族）。"""
    draws = draw_values or {
        "90|26130": _DRAW_VALUE,
        "90|26129": _DRAW_VALUE | {"lotteryDrawNum": "26129"},
    }
    current = json.loads(json.dumps(_CURRENT_VALUE))  # 深拷贝防串改
    if jqc_on_sale:
        current["jqcMatch"] = {
            "lotteryDrawNum": "26133",
            "lotteryGameName": "4场进球",
            "lotterySaleEndtime": "2026-09-26 20:30:00",
            "matchList": [
                {
                    "matchNum": 1,
                    "matchName": "英超",
                    "masterTeamAllName": "阿森纳",
                    "guestTeamAllName": "热刺",
                    "infohubMatchId": 2041801,
                    "startTime": "2026-09-28",
                }
            ],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("getFootBallMatchV1.qry"):
            return httpx.Response(200, json={"success": True, "value": current})
        if path.endswith("getFootBallDrawInfoByDrawNumV2.qry"):
            game = str(request.url.params["lotteryGameNum"])
            draw = str(request.url.params["lotteryDrawNum"])
            value = draws.get(f"{game}|{draw}")
            if value is None:
                return httpx.Response(200, json={"success": True, "value": {}})
            return httpx.Response(200, json={"success": True, "value": value})
        return httpx.Response(500, text="no-route")

    return httpx.Client(transport=httpx.MockTransport(handler))


# —— 解析纯函数 ———————————————————————————————————————————————


def test_parse_match_list_shapes() -> None:
    rows = zucai_official.parse_match_list(_CURRENT_VALUE["sfcMatch"]["matchList"])
    assert [r.match_seq for r in rows] == [1, 2]
    first = rows[0]
    assert first.source_match_id == "2041732"  # uniform 同族身份
    assert first.league == "国际赛"
    # 日期精度：CST 当日 00:00 → UTC 前一日 16:00
    assert first.kickoff_utc == "2026-09-26T16:00:00+00:00"


def test_money_and_cst_helpers() -> None:
    assert zucai_official._money("25,162,994") == 25162994.0
    assert zucai_official._money("") is None
    assert zucai_official._money(None) is None
    assert zucai_official._money("abc") is None
    assert zucai_official._cst_naive_to_utc("2026-09-26 20:30:00") is not None
    assert zucai_official._cst_naive_to_utc("2026-09-26") is not None
    assert zucai_official._cst_naive_to_utc("") is None


def test_parse_current_sfc_and_state_merge() -> None:
    periods = zucai_official.parse_current(_CURRENT_VALUE)
    onsale = next(p for p in periods if p.period_no == "26133")
    assert onsale.market == zucai_official.MARKET_SFC
    assert len(onsale.matches) == 2
    assert onsale.sales_deadline_utc is not None
    last = next(p for p in periods if p.period_no == "26131")
    assert last.prize_tiers is not None
    assert last.prize_tiers["sfc"]["total_sale"] == 25162994.0
    assert len(last.prize_tiers["sfc"]["tiers"]) == 2
    # 任9 面并入同期间 state
    assert last.prize_tiers["rj"]["total_sale"] == 20386268.0
    assert last.sales_amount == 25162994.0
    # jqc 未开售 → 无 goals4 期
    assert not [p for p in periods if p.market == zucai_official.MARKET_JQC]


def test_parse_draw_info_full_and_empty() -> None:
    period = zucai_official.parse_draw_info(zucai_official.MARKET_SFC, _DRAW_VALUE)
    assert period is not None
    assert period.period_no == "26130"
    assert period.published_at is not None
    assert period.prize_tiers is not None
    # V2 任9 金额面（无奖级）并档
    assert period.prize_tiers["rj_amounts"]["total_sale"] == 20386268.0
    assert zucai_official.parse_draw_info(zucai_official.MARKET_SFC, {}) is None


# —— 入库闭环 ————————————————————————————————————————————————


def test_sync_current_persists_and_keeps_deadline(db) -> None:
    stats = zucai_official.sync_current(db, Settings(), _mock_client())
    assert set(stats.period_nos) == {"26133", "26131"}
    assert stats.matches == 2
    # 在售期：期次+对阵+截止
    pid = pool_store.pool_period_id(db, "ttt14", "26133")
    assert pid is not None
    matches = pool_store.pool_matches_for_period(db, pid)
    assert len(matches) == 2
    assert matches[0]["source_match_id"] == "2041732"
    assert pool_store.pool_period_deadline(db, pid) is not None
    # 上期彩果：state 落 official tiers
    pid_last = pool_store.pool_period_id(db, "ttt14", "26131")
    state = pool_store.pool_state_for_period(db, pid_last)
    tiers = json.loads(str(state["prize_tiers"]))
    assert tiers["sfc"]["tiers"][0]["stake_amount"] == 1024820.0
    assert "rj" in tiers
    # 幂等重放：state 同值不炸，行数稳定
    zucai_official.sync_current(db, Settings(), _mock_client())
    assert len(pool_store.pool_matches_for_period(db, pid)) == 2


def test_sync_current_discovers_jqc_on_sale(db) -> None:
    zucai_official.sync_current(db, Settings(), _mock_client(jqc_on_sale=True))
    pid = pool_store.pool_period_id(db, "goals4", "26133")
    assert pid is not None
    assert len(pool_store.pool_matches_for_period(db, pid)) == 1


def test_backfill_draws_lands_official_state_and_skips(db) -> None:
    stats = zucai_official.backfill_draws(
        db,
        Settings(),
        _mock_client(),
        periods=3,
        start="26131",
        games=(zucai_official.GAME_SFC,),
        sleeper=0,
    )
    assert set(stats.period_nos) == {"26130", "26129"}
    assert (
        stats.pages == 3
    )  # start 已传 → 无 V1 起点；3 期 V2（26130/26129 有数据、26128 空）
    for period_no in ("26130", "26129"):
        pid = pool_store.pool_period_id(db, "ttt14", period_no)
        assert pid is not None
        state = pool_store.pool_state_for_period(db, pid)
        assert state is not None
        assert state["prize_tiers"] is not None
    # 幂等：已落官方面的期直接跳过（只花 V1 起点请求）
    stats2 = zucai_official.backfill_draws(
        db,
        Settings(),
        _mock_client(),
        periods=3,
        start="26131",
        games=(zucai_official.GAME_SFC,),
        sleeper=0,
    )
    assert stats2.pages == 1
    assert stats2.period_nos == []


def test_backfill_requires_referer_and_origin(db) -> None:
    """历史端点 CORS 闸：请求头必带 Referer(开奖页)+Origin（实测缺则空）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "value": {}})

    zucai_official.backfill_draws(
        db,
        Settings(),
        httpx.Client(transport=httpx.MockTransport(handler)),
        periods=1,
        start="26131",
        games=(zucai_official.GAME_SFC,),
        sleeper=0,
    )
    draw_requests = [r for r in seen if "DrawInfo" in str(r.url)]
    assert draw_requests
    for request in draw_requests:
        assert request.headers["Origin"] == "https://www.sporttery.cn"
        assert "kjgg" in request.headers["Referer"]
