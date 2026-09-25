"""
竞彩官方 SP 历史采集（票 70，jc provider 地基）：getFixedBonusV1 垂直入列。

官方权威层（2026-09-25 用户裁决"所有竞彩数据以官方为准、单独落"）——
独立 provider ``jc`` 与 srct 书商语料层**物理隔离**（树路径/数据集零共享，
spec 69 story 11）。71（实时拍）/72（silver）/67（历史回填）共用本模块。

端点事实（collection-spec §六，2026-09-25 浏览器审计+实测）：

- ``webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry``
  （``clientCode=3001&matchId={mid}``；uniform 族同赛果端点头直通）——响应
  ``value``：``oddsHistory`` 含
  ``hadList/hhadList/ttgList``（消费链：HAD/HHAD/TTG）+ ``hafuList/crsList``
  （**留档不建消费链**，采是免费的）+ 队/联赛身份字段 + ``singleList``
  单关旗；另有 ``isCancel/matchResultList/sectionsNo999``。
- 逐笔行：``updateDate+updateTime``（北京钟面）+ 各玩法值组——had/hhad=
  ``h/d/a`` 三价（hhad 另带 ``goalLine`` 盘口），ttg=``s0..s7`` 八档长格式
  （``sNf`` 为封盘旗）。
- **oddsHistory 全量返回=幂等自愈**（漏拍重拉即补，71 实时拍的漏拍保障）。
- 空 ``oddsHistory={}`` = 该场无竞彩数据（合法空：非 JC 场/未开售历史）——
  raw 照落（枚举侧防重查），bronze 不写。
- matchId 来源：uniform 赛果按日反查（生产已有 fetch_uniform_results，
  67 的枚举通道）。

口径：raw=响应字节贴源（重放零损）；bronze=value 原样 payload（解析归
silver 层，票 72）；三时间定则——published_at（updateDate+updateTime
北京钟面）归 silver，采集只记 fetched_at。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import httpx

from goalx_backend.config import Settings
from goalx_backend.data.corpus_store import CorpusStore

JC_PROVIDER = "jc"  # 官方权威层身份（与 srct 书商层物理隔离）
SP_DATASET = "sp_history"  # 一场一响应全玩法（getFixedBonusV1）
BRONZE_VERSION = "jc_sp_v1"
JC_FIXED_BONUS_URL = (
    "https://webapi.sporttery.cn/gateway/uniform/football/getFixedBonusV1.qry"
)
# 消费链玩法（CRS/HAFU 留档不消费——采是免费的，用不用再裁）
PLAYTYPES = ("had", "hhad", "ttg")


class JcContentError(Exception):
    """内容判别失败（非 JSON/success=false）——确定性坏响应，不重试。"""


@dataclass
class JcCollectStats:
    """一次 JC 采集的统计（CLI/回填摘要口径）。"""

    match_ids: list[str] = field(default_factory=list)
    requests: int = 0
    raw_new: int = 0
    cached: int = 0
    bronze_new: int = 0  # oddsHistory 非空落 bronze 的场数
    empty: int = 0  # oddsHistory={} 合法空（raw 落盘防枚举重查）
    failed: dict[str, str] = field(default_factory=dict)
    rows: dict[str, int] = field(default_factory=dict)  # 各玩法逐笔行数

    def note_rows(self, odds_history: dict[str, Any]) -> None:
        """按玩法累计逐笔行数（CLI 报告口径）。"""
        for playtype in PLAYTYPES:
            rows = cast("list[object]", odds_history.get(f"{playtype}List") or [])
            self.rows[playtype] = self.rows.get(playtype, 0) + len(rows)


def fetch_fixed_bonus(client: httpx.Client, settings: Settings, match_id: str) -> bytes:
    """拉一场官方 SP 历史原始字节（uniform 族同头直通）。"""
    response = client.get(
        settings.jc_fixed_bonus_url or JC_FIXED_BONUS_URL,
        params={"clientCode": "3001", "matchId": match_id},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://www.sporttery.cn/",
            "Accept": "application/json, text/plain, */*",
        },
        timeout=25.0,
    )
    response.raise_for_status()
    return response.content


def parse_fixed_bonus(body: bytes) -> dict[str, Any]:
    """
    一场响应 → bronze payload（value 原样贴源）。

    空位约定：``odds_history`` 键必在（{} = 该场无 JC 数据，空≠无）；
    非 JSON/success=false 抛 JcContentError。
    """
    try:
        payload = cast("dict[str, Any]", json.loads(body))
    except ValueError as exc:
        msg = f"jc: 响应非 JSON（{exc}）"
        raise JcContentError(msg) from exc
    if not payload.get("success"):
        msg = f"jc: 端点返回失败: {payload.get('errorMessage')}"
        raise JcContentError(msg)
    value = cast("dict[str, Any]", payload.get("value") or {})
    if "oddsHistory" not in value:
        msg = "jc: 响应缺 oddsHistory（改版或坏响应）"
        raise JcContentError(msg)
    return value


def collect_match(
    store: CorpusStore,
    settings: Settings,
    client: httpx.Client,
    match_id: str,
    *,
    stats: JcCollectStats | None = None,
    ext: str = ".json",
    key: str | None = None,
) -> JcCollectStats:
    """
    一场闭环：拉取 → raw → bronze（oddsHistory 非空才写；行 sid=matchId）。

    raw 键缺省=matchId（收口/回填：一场一工件全量）；71 实时拍传拍键
    （``{mid}@open`` 等——同场多拍 append，bronze latest-per-sid 收敛）。
    幂等：键命中零重抓（oddsHistory 全量返回，无增量语义）。stats 注入
    累计（回填循环/拍循环共用）。
    """
    stats = stats if stats is not None else JcCollectStats()
    artifact_key = key if key is not None else match_id
    if match_id not in stats.match_ids:
        stats.match_ids.append(match_id)
    if store.has(JC_PROVIDER, SP_DATASET, artifact_key):
        stats.cached += 1
        return stats
    stats.requests += 1
    try:
        body = fetch_fixed_bonus(client, settings, match_id)
        value = parse_fixed_bonus(body)
    except (httpx.HTTPError, JcContentError) as exc:
        stats.failed[artifact_key] = f"{type(exc).__name__}: {exc}"[:120]
        return stats
    store.ingest_raw(JC_PROVIDER, SP_DATASET, artifact_key, body, ext=ext)
    stats.raw_new += 1
    odds_history = cast("dict[str, Any]", value.get("oddsHistory") or {})
    if odds_history:
        store.append_bronze(
            JC_PROVIDER,
            SP_DATASET,
            [
                {
                    "provider": JC_PROVIDER,
                    "dataset": SP_DATASET,
                    "sid": match_id,
                    "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "parser_version": BRONZE_VERSION,
                    "raw_sha": store.raw_sha(JC_PROVIDER, SP_DATASET, artifact_key),
                    "payload": value,
                }
            ],
        )
        stats.bronze_new += 1
        stats.note_rows(odds_history)
    else:
        stats.empty += 1
    return stats
