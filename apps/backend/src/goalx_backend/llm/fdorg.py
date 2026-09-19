"""
football-data.org 免费档客户端（票 09）：12 项赛事 standings 读取。

免费 12 项：五大 + 欧冠/葡超/荷甲/英冠/巴甲 + 世界杯/欧洲杯
（2026-09 查证，票 03 免费组合）。10 次/分钟限速——调用方控制频率。

场次级挂接（情报落库到 fixture）在票 10 scout 输入面定义装配；
本模块只负责抓取与解析。无 key 时调用方应零成本跳过（诚实降级）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import httpx

from goalx_backend.config import Settings

# 免费档 12 项赛事代码（competition code → 名称）
FREE_COMPETITIONS: dict[str, str] = {
    "PL": "英超",
    "BL1": "德甲",
    "SA": "意甲",
    "PD": "西甲",
    "FL1": "法甲",
    "DED": "荷甲",
    "EDC": "英冠",
    "PDP": "葡超",
    "BSA": "巴甲",
    "CL": "欧冠",
    "WC": "世界杯",
    "EC": "欧洲杯",
}


@dataclass(frozen=True)
class StandingRow:
    """一行积分榜（form 为联盟近 5 场 W/D/L 串）。"""

    position: int
    team: str
    points: int
    played: int
    goals_for: int
    goals_against: int
    form: str | None


class FdorgKeyMissing(RuntimeError):
    """FDORG_API_KEY 未配置——调用方应跳过而非报错中断。"""


def fetch_standings(
    client: httpx.Client, settings: Settings, competition_code: str
) -> list[StandingRow]:
    """拉取一项赛事当前积分榜（免费档端点 /v4/competitions/{code}/standings）。"""
    if not settings.fdorg_api_key:
        raise FdorgKeyMissing("FDORG_API_KEY 未配置（免费注册，见票 09）")
    response = client.get(
        f"{settings.fdorg_base_url.rstrip('/')}/v4/competitions/{competition_code}/standings",
        headers={"X-Auth-Token": settings.fdorg_api_key},
        timeout=25.0,
    )
    response.raise_for_status()
    return parse_standings(response.json())


def _as_int(value: object) -> int | None:
    """宽松取整数（int/float/str 数字；其余 None → 调用方按坏行跳过）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_standings(payload: dict[str, object]) -> list[StandingRow]:
    """v4 standings 响应 → 行列表（type=TOTAL 常规赛季首表；异常空列表）。"""
    standings = payload.get("standings")
    if not isinstance(standings, list):
        return []
    rows: list[StandingRow] = []
    for table in cast(list[dict[str, object]], standings):
        # 欧冠等赛事含分组表——只取常规赛季总表（小组赛信息量低）
        if table.get("type") != "TOTAL" or table.get("group") != "Regular Season":
            continue
        entries = table.get("table")
        if not isinstance(entries, list):
            continue
        for entry in cast(list[dict[str, object]], entries):
            team_raw = entry.get("team")
            team_name: str | None = None
            if isinstance(team_raw, dict):
                name = cast(dict[str, object], team_raw).get("name")
                team_name = name if isinstance(name, str) else None
            position = _as_int(entry.get("position"))
            points = _as_int(entry.get("points"))
            played = _as_int(entry.get("playedGames"))
            goals_for = _as_int(entry.get("goalsFor"))
            goals_against = _as_int(entry.get("goalsAgainst"))
            form = entry.get("form")
            if (
                team_name is None
                or position is None
                or points is None
                or played is None
                or goals_for is None
                or goals_against is None
            ):
                continue  # 坏行跳过，宁缺勿错
            rows.append(
                StandingRow(
                    position=position,
                    team=team_name,
                    points=points,
                    played=played,
                    goals_for=goals_for,
                    goals_against=goals_against,
                    form=form if isinstance(form, str) else None,
                )
            )
        break
    return rows
