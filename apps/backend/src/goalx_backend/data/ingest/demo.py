"""
演示/E2E 种子：动态时间的竞彩场次、销售状态与欧赔证据（票 36）。

只允许写隔离库（e2e/本地演示）；发现非 demo 采集数据即拒绝，避免把
伪造实采写进真实运行库（票 36 交付约束）。时间全部相对 now 动态生成，
保证 had 资格判定（新鲜度/停售/开赛）在种子后立刻可复现。

票 wb-04：加进球类（ttg/crs）演示数据——003 停售仍给盘口（禁用路径）；
002 附 demo Forecast（λ 1.4/1.3 矩阵，ttg s2 价 4.50 → 模型 EV 为正，
驱动进球页组合非空路径）；001 无 Forecast（保留研究页"暂无模型预测"
演示口径不受影响）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from goalx_backend.data import fixtures as fx_store
from goalx_backend.data.fixtures import beijing_business_date
from goalx_backend.data.observations import sha256_hex
from goalx_backend.modelling.forecast import content_hash, insert_forecast
from goalx_backend.modelling.score_matrix import matrix_from_lambdas
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
    # 进球类（票 wb-04）：ttg/crs 盘口、单固资格与 demo Forecast λ
    ttg: dict[str, float] | None = None
    crs: dict[str, float] | None = None
    goals_single: bool | None = None
    forecast_lambdas: tuple[float, float] | None = None


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


def _seed_goals_market(
    conn: sqlite3.Connection,
    fixture: int,
    match: DemoMatch,
    observed: str,
    jc_obs: int,
) -> None:
    """进球类（ttg/crs）销售状态与盘口快照（票 wb-04；盘口缺省跳过）。"""
    for market_code, prices in (("ttg", match.ttg), ("crs", match.crs)):
        if prices is None:
            continue
        fx_store.append_sale_status(
            conn,
            SaleStatusInput(
                fixture_id=fixture,
                market_code=market_code,
                sale_state=match.sale,
                single_eligible=(
                    match.goals_single if match.sale == "on_sale" else None
                ),
                observed_at=observed,
                observation_id=jc_obs,
            ),
        )
        for sel, odds in prices.items():
            fx_store.insert_odds_snapshot(
                conn,
                SnapshotInput(
                    fixture_id=fixture,
                    market_code=market_code,
                    selection_code=sel,
                    source="sporttery",
                    odds=odds,
                    captured_at=observed,
                    observed_at=observed,
                    source_updated_at=observed,
                    observation_id=jc_obs,
                ),
            )


def _seed_demo_forecast(
    conn: sqlite3.Connection, fixture: int, match: DemoMatch, observed: str
) -> None:
    """Demo Forecast（票 wb-04）：固定 λ 的矩阵，进球页模型 EV 路径用。"""
    if match.forecast_lambdas is None:
        return
    lam_home, lam_away = match.forecast_lambdas
    matrix = matrix_from_lambdas(lam_home, lam_away)
    payload: dict[str, object] = {
        "matrix": [list(row) for row in matrix],
        "lambda_home": lam_home,
        "lambda_away": lam_away,
        "rho": 0.0,
        "model_as_of": observed,
        "had_ci": None,
        "ci_nominal_level": None,
        "ci_effective_samples": 0,
    }
    insert_forecast(
        conn,
        fixture_id=fixture,
        track="ml",
        model_version="dc-demo",
        content_hash=content_hash(payload),
        payload=payload,
    )


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
            ttg={
                "0": 10.5,
                "1": 3.9,
                "2": 3.05,
                "3": 3.5,
                "4": 5.2,
                "5": 9.8,
                "6": 17.5,
                "7": 34.0,
            },
            crs={
                "0:0": 12.5,
                "0:1": 7.0,
                "0:2": 8.0,
                "0:3": 15.0,
                "0:4": 40.0,
                "0:5": 100.0,
                "1:0": 13.0,
                "1:1": 7.5,
                "1:2": 8.5,
                "1:3": 16.0,
                "1:4": 42.0,
                "1:5": 110.0,
                "2:0": 26.0,
                "2:1": 15.5,
                "2:2": 17.0,
                "2:3": 32.0,
                "2:4": 80.0,
                "2:5": 200.0,
                "3:0": 65.0,
                "3:1": 40.0,
                "3:2": 45.0,
                "3:3": 85.0,
                "4:0": 150.0,
                "4:1": 95.0,
                "4:2": 110.0,
                "5:0": 400.0,
                "5:1": 250.0,
                "5:2": 300.0,
                "h_other": 120.0,
                "d_other": 400.0,
                "a_other": 60.0,
            },
            goals_single=True,
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
            # ttg s2 价 4.50：模型 P(2 球)=0.245 → EV≈+10.3%（组合非空路径）；
            # 其余档与 crs 全网格按 ~25% 水位 → 模型 EV 全负（诚实多数态）
            ttg={
                "0": 11.0,
                "1": 4.1,
                "2": 4.5,
                "3": 3.4,
                "4": 5.0,
                "5": 9.0,
                "6": 20.0,
                "7": 35.0,
            },
            crs={
                "0:0": 11.0,
                "0:1": 8.5,
                "0:2": 13.0,
                "0:3": 30.0,
                "0:4": 90.0,
                "0:5": 250.0,
                "1:0": 8.0,
                "1:1": 6.0,
                "1:2": 9.5,
                "1:3": 22.0,
                "1:4": 65.0,
                "1:5": 220.0,
                "2:0": 11.5,
                "2:1": 8.75,
                "2:2": 13.5,
                "2:3": 31.0,
                "2:4": 95.0,
                "2:5": 250.0,
                "3:0": 24.5,
                "3:1": 19.0,
                "3:2": 29.0,
                "3:3": 65.0,
                "4:0": 70.0,
                "4:1": 54.0,
                "4:2": 82.0,
                "5:0": 250.0,
                "5:1": 190.0,
                "5:2": 290.0,
                "h_other": 90.0,
                "d_other": 500.0,
                "a_other": 100.0,
            },
            goals_single=True,
            forecast_lambdas=(1.4, 1.3),
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
            ttg={
                "0": 10.0,
                "1": 3.7,
                "2": 3.0,
                "3": 3.5,
                "4": 5.3,
                "5": 10.0,
                "6": 18.0,
                "7": 33.0,
            },
            crs={
                "0:0": 11.5,
                "0:1": 10.0,
                "0:2": 18.0,
                "0:3": 42.0,
                "0:4": 120.0,
                "0:5": 320.0,
                "1:0": 6.5,
                "1:1": 6.0,
                "1:2": 10.5,
                "1:3": 25.0,
                "1:4": 70.0,
                "1:5": 200.0,
                "2:0": 8.5,
                "2:1": 7.5,
                "2:2": 13.5,
                "2:3": 32.0,
                "2:4": 90.0,
                "2:5": 250.0,
                "3:0": 18.0,
                "3:1": 16.0,
                "3:2": 28.0,
                "3:3": 65.0,
                "4:0": 50.0,
                "4:1": 45.0,
                "4:2": 80.0,
                "5:0": 140.0,
                "5:1": 125.0,
                "5:2": 220.0,
                "h_other": 75.0,
                "d_other": 420.0,
                "a_other": 90.0,
            },
            goals_single=True,
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
        _seed_goals_market(conn, fixture, match, observed, jc_obs)
        _seed_demo_forecast(conn, fixture, match, observed)
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
    pool_period_id = _seed_demo_pool(conn, moment, observed)
    return {
        "business_date": beijing_business_date(moment),
        "fixture_ids": fixture_ids,
        "observed_at": observed,
        "pool_period_id": pool_period_id,
    }


# 演示彩池期次（票 43）：3 场与竞彩演示场次同队同窗（模型概率映射可命中），
# 其余 11 场为合成对阵（期次结构/分布展示）。销量不造——留 AI 代采待命态。
_DEMO_POOL_MATCHES: list[tuple[str, str, str, float, float, float]] = [
    ("英超", "阿森纳", "切尔西", 6.2, 4.9, 1.32),
    ("英超", "利物浦", "曼城", 2.6, 3.5, 2.55),
    ("德甲", "拜仁", "多特", 1.95, 3.7, 3.6),
    ("英超", "维拉", "热刺", 2.35, 3.45, 2.85),
    ("英超", "纽卡斯尔", "西汉姆", 1.75, 3.8, 4.3),
    ("西甲", "巴萨", "塞维利亚", 1.45, 4.6, 6.5),
    ("西甲", "马竞技", "贝蒂斯", 1.6, 3.7, 5.4),
    ("意甲", "国米", "拉齐奥", 1.55, 4.0, 5.5),
    ("意甲", "尤文", "佛罗伦萨", 1.7, 3.6, 5.0),
    ("意甲", "AC米兰", "罗马", 2.05, 3.4, 3.5),
    ("法甲", "日尔曼", "里昂", 1.35, 5.0, 8.0),
    ("法甲", "摩纳哥", "马赛", 2.3, 3.3, 3.0),
    ("葡超", "本菲卡", "波尔图", 2.15, 3.3, 3.3),
    ("荷甲", "阿贾克斯", "埃因霍温", 2.5, 3.6, 2.5),
]


def _demo_pool_shares(odds: tuple[float, float, float]) -> dict[str, float]:
    """演示分布：欧赔反比近似 + 主队偏置/平局低注（公众分布代理典型形态）。"""
    inv = [1.0 / o for o in odds]
    total = sum(inv)
    shares = [v / total for v in inv]
    shares[0] *= 1.15
    shares[1] *= 0.9
    norm = sum(shares)
    return {"3": shares[0] / norm, "1": shares[1] / norm, "0": shares[2] / norm}


def _seed_demo_pool(conn: sqlite3.Connection, moment: datetime, observed: str) -> int:
    """写入一个演示彩池期次（14 场 + 分布快照；幂等刷新当前态）。"""
    from goalx_backend.data import pool as pool_store  # noqa: PLC0415

    period_no = "26999"
    deadline = (moment + timedelta(hours=28)).isoformat(timespec="seconds")
    pool_period_id = pool_store.upsert_pool_period(conn, "ttt14", period_no, deadline)
    # 前 3 场与竞彩演示场次同队同窗（+2/+3/+4h），模型概率映射可命中
    kickoff_hours = [2.0, 3.0, 4.0] + [30.0 + 2.0 * seq for seq in range(11)]
    matches: list[pool_store.PoolMatchInput] = []
    share_rows: list[pool_store.PoolShareInput] = []
    for seq, ((league, home, away, h, d, a), hours) in enumerate(
        zip(_DEMO_POOL_MATCHES, kickoff_hours, strict=True), start=1
    ):
        odds = (float(h), float(d), float(a))
        kickoff = (moment + timedelta(hours=hours)).isoformat(timespec="seconds")
        matches.append(
            pool_store.PoolMatchInput(
                match_seq=seq,
                source_match_id=None,
                league=league,
                kickoff_utc=kickoff,
                home_team=home,
                away_team=away,
                euro_odds=odds,
            )
        )
        share_rows.append(
            pool_store.PoolShareInput(
                match_seq=seq,
                shares=_demo_pool_shares(odds),
                votes=None,
            )
        )
    pool_store.replace_pool_matches(conn, pool_period_id, matches)
    # 同 observed 幂等：先清该时点 demo 快照再插（种子重放场景）
    conn.execute(
        "DELETE FROM public_shares WHERE pool_period_id = ? AND source = ?"  # noqa: S608 常量拼接
        + " AND captured_at = ?",
        (pool_period_id, DEMO_SOURCE_TAG, observed),
    )
    pool_store.insert_public_shares(
        conn,
        pool_period_id,
        share_rows,
        origin="estimated",
        source=DEMO_SOURCE_TAG,
        captured_at=observed,
    )
    conn.commit()
    return pool_period_id
