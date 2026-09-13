"""
演示/E2E 种子：动态时间的竞彩场次、销售状态与欧赔证据（票 36）。

只允许写隔离库（e2e/本地演示）；发现非 demo 采集数据即拒绝，避免把
伪造实采写进真实运行库（票 36 交付约束）。时间全部相对 now 动态生成，
保证 had 资格判定（新鲜度/停售/开赛）在种子后立刻可复现。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.fixtures import beijing_business_date
from goalx_backend.data.observations import sha256_hex
from goalx_backend.models import (
    MatchCodeInput,
    ObservationInput,
    ObservationPurpose,
    SaleStatusInput,
    SnapshotInput,
    Tier,
)

DEMO_PARSE_VERSION = "demo_seed_v1"
DEMO_SOURCE_TAG = "demo"


class DemoSeedRefused(ValueError):
    """目标库含非 demo 采集数据，拒绝写入伪造实采。"""


@dataclass(frozen=True)
class DemoMatch:
    """一场演示场次的种子参数。"""

    code: str
    league: str
    home: str
    away: str
    kickoff: str
    single: bool
    sale: str
    had: dict[str, float]
    eu: dict[str, dict[str, float]] | None = None
    event: str | None = None


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def ensure_demo_only_db(conn: sqlite3.Connection) -> None:
    """目标库只允许 demo 数据或空库（票 36：不写主库伪造实采）。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM match_codes
        WHERE kind = 'jingcai' AND COALESCE(source_match_id, '') != ?
        """,
        (DEMO_SOURCE_TAG,),
    ).fetchone()
    if row is not None and int(row["n"]) > 0:
        raise DemoSeedRefused("目标库已有非 demo 竞彩数据; seed-demo 只写隔离/演示库")


def seed_demo(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict[str, object]:
    """
    写入三场演示场次（相对 now 动态时间，幂等）：

    - 001 阿森纳 vs 切尔西：单固、在售、竞彩+欧赔证据新鲜 → 判定 valid；
    - 002 利物浦 vs 曼城：非单固（仅串关）、在售 → 串关可用，单关拒绝；
    - 003 拜仁 vs 多特：已停售 → 判定 rejected(sale_stopped)。
    """
    ensure_demo_only_db(conn)
    moment = now or datetime.now(UTC)
    observed = _iso(moment - timedelta(seconds=60))

    def kickoff(hours: float) -> str:
        return _iso(moment + timedelta(hours=hours))

    plan = [
        DemoMatch(
            code="周六001",
            league="英超",
            home="阿森纳",
            away="切尔西",
            kickoff=kickoff(2),
            single=True,
            sale="on_sale",
            had={"h": 6.5, "d": 5.0, "a": 1.3},
            eu={
                "pin": {"h": 6.0, "d": 5.0, "a": 1.30},
                "avg": {"h": 6.2, "d": 4.9, "a": 1.32},
                "bet365": {"h": 6.4, "d": 4.8, "a": 1.28},
            },
            event="demo-e1",
        ),
        DemoMatch(
            code="周六002",
            league="英超",
            home="利物浦",
            away="曼城",
            kickoff=kickoff(3),
            single=False,
            sale="on_sale",
            had={"h": 3.0, "d": 3.4, "a": 2.2},
            eu={
                "pin": {"h": 2.9, "d": 3.5, "a": 2.25},
                "avg": {"h": 3.0, "d": 3.4, "a": 2.2},
                "bet365": {"h": 3.1, "d": 3.3, "a": 2.15},
            },
            event="demo-e2",
        ),
        DemoMatch(
            code="周六003",
            league="德甲",
            home="拜仁",
            away="多特",
            kickoff=kickoff(4),
            single=True,
            sale="stopped",
            had={"h": 2.0, "d": 3.6, "a": 3.2},
            eu=None,
            event=None,
        ),
    ]

    fixture_ids: list[int] = []
    for match in plan:
        tier = Tier.TIER1 if match.league == "英超" else Tier.TIER2
        competition = fx_store.upsert_competition(
            conn, match.league, tier=tier, odds_api_sport_key="soccer_epl"
        )
        home = fx_store.upsert_team(conn, match.home)
        away = fx_store.upsert_team(conn, match.away)
        fixture = fx_store.upsert_fixture(conn, competition, match.kickoff, home, away)
        fixture_ids.append(fixture)
        fx_store.upsert_match_code(
            conn,
            MatchCodeInput(
                fixture_id=fixture,
                kind="jingcai",
                business_date=beijing_business_date(moment),
                code=match.code,
                source_match_id=DEMO_SOURCE_TAG,
                is_single=match.single,
            ),
        )
        evidence = json.dumps(
            {"code": match.code, "had": match.had, "sale": match.sale},
            ensure_ascii=False,
            sort_keys=True,
        )
        jc_obs = fx_store.record_quote_observation(
            conn,
            ObservationInput(
                source="sporttery",
                purpose=ObservationPurpose.LIVE,
                observed_at=observed,
                source_updated_at=observed,
                endpoint="demo://jingcai",
                parse_version=DEMO_PARSE_VERSION,
                raw_sha256=sha256_hex(evidence.encode("utf-8")),
                summary=f"demo {match.code}",
            ),
        )
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture,
                market_code=None,
                sale_state=match.sale,
                observed_at=observed,
                observation_id=jc_obs,
            ),
        )
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture,
                market_code="had",
                sale_state=match.sale,
                single_eligible=match.single if match.sale == "on_sale" else None,
                observed_at=observed,
                observation_id=jc_obs,
            ),
        )
        for sel, odds in match.had.items():
            fx_store.insert_odds_snapshot(
                conn,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code="had",
                    selection_code=sel,
                    source="sporttery",
                    odds=odds,
                    captured_at=observed,
                    observed_at=observed,
                    source_updated_at=observed,
                    observation_id=jc_obs,
                ),
            )
        if match.event is not None and match.eu:
            fx_store.set_odds_api_join(
                conn, fixture, match.event, "soccer_epl", "manual"
            )
            eu_evidence = json.dumps(match.eu, sort_keys=True)
            eu_obs = fx_store.record_quote_observation(
                conn,
                ObservationInput(
                    source="odds_api",
                    purpose=ObservationPurpose.LIVE,
                    observed_at=observed,
                    source_updated_at=observed,
                    endpoint="demo://odds",
                    parse_version=DEMO_PARSE_VERSION,
                    raw_sha256=sha256_hex(eu_evidence.encode("utf-8")),
                    summary=f"demo eu {match.code}",
                ),
            )
            for book, prices in match.eu.items():
                for sel, odds in prices.items():
                    fx_store.insert_odds_snapshot(
                        conn,
                        SnapshotInput(
                            fixture_id=fixture,
                            market_code="had",
                            selection_code=sel,
                            source=f"odds_api:{book}",
                            odds=odds,
                            captured_at=observed,
                            observed_at=observed,
                            source_updated_at=observed,
                            observation_id=eu_obs,
                        ),
                    )
    conn.commit()
    return {
        "business_date": beijing_business_date(moment),
        "fixture_ids": fixture_ids,
        "observed_at": observed,
    }
