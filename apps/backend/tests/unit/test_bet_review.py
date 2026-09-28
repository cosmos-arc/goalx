"""前瞻纳入/排除 5 分类的领域 seam 测试（票 08；自 test_api.py 三条断言复制扩全矩阵）。

分类规则正典在 evaluation/bet_review.py（票 36 规则，票 08 自 api/bets.py
下沉）：excluded_unlocked / unknown / live_separate / excluded_post_kickoff /
missing_closing / included。test_api.py 的三条端点级断言是 HTTP 面抽查，
本文件直接打领域函数钉全矩阵与判定链顺序。

造态口径：能用领域 API（create_bet_with_legs/record_purchase）就用；
slip_id 置 NULL 的历史形态无领域 API 可产（locked_at 源自 list_bets 的
LEFT JOIN bet_slips），SQL 直改是诚实造态。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from goalx_backend.betting.bets import BetDraft, create_bet_with_legs, record_purchase
from goalx_backend.betting.store import list_bets
from goalx_backend.evaluation.bet_review import review_views
from goalx_backend.models import BetMode, LegInput

NOW = datetime.now(UTC)


def _seed_fixtures(db: sqlite3.Connection) -> None:
    """一组队伍/赛事/两场：fixture 1 未开赛（26h 后）、fixture 2 已开赛（1h 前）。"""
    db.execute(
        "INSERT INTO competitions (id, name, created_at) VALUES (1, '英超', ?)",
        (NOW.isoformat(),),
    )
    db.execute(
        "INSERT INTO teams (id, canonical_name, created_at) VALUES"
        " (1, '阿森纳', ?), (2, '切尔西', ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    for fid, kickoff in ((1, NOW + timedelta(hours=26)), (2, NOW - timedelta(hours=1))):
        db.execute(
            "INSERT INTO fixtures (id, competition_id, kickoff_utc, home_team_id,"
            " away_team_id) VALUES (?, 1, ?, 1, 2)",
            (fid, kickoff.isoformat()),
        )


def _create_bet(
    db: sqlite3.Connection, fixture_id: int, mode: BetMode = BetMode.PAPER
) -> int:
    return create_bet_with_legs(
        db,
        BetDraft(
            mode=mode,
            stake=2.0,
            legs=[
                LegInput(
                    fixture_id=fixture_id,
                    market_code="had",
                    selection_code="h",
                    locked_odds=6.5,
                )
            ],
        ),
    )


def _seed_closing(db: sqlite3.Connection, bet_id: int, fixture_id: int) -> None:
    db.execute(
        "INSERT INTO clv_records (bet_id, fixture_id, market_code, selection_code,"
        " taken_odds, close_prob, clv_prob, close_source, computed_at)"
        " VALUES (?, ?, 'had', 'h', 6.5, 0.15, 0.01, 'odds_api_closing', ?)",
        (bet_id, fixture_id, NOW.isoformat()),
    )


def test_forward_classification_matrix(db: sqlite3.Connection) -> None:
    """六态全矩阵 + 判定链顺序：unlocked → unknown → live → post_kickoff → closing。"""
    _seed_fixtures(db)

    # 1. 未购买 → excluded_unlocked（test_api:598 断言复制）
    unlocked = _create_bet(db, fixture_id=1)

    # 2. 已购买但无锁定时点（slip 缺失的历史形态）→ unknown；locked_pre_kickoff None
    unknown = _create_bet(db, fixture_id=1)
    record_purchase(db, [unknown])
    db.execute("UPDATE bets SET slip_id = NULL WHERE id = ?", (unknown,))

    # 3. live 单独分组——挂在已开赛场次上，钉住 live 判定先于 post_kickoff
    #    （test_api:912 断言复制扩全：那边挂未来场，钉不到这一对）
    live_post = _create_bet(db, fixture_id=2, mode=BetMode.LIVE)
    record_purchase(db, [live_post])

    # 4. 已购买 paper、锁定晚于开赛 → excluded_post_kickoff，旗 = False
    post = _create_bet(db, fixture_id=2)
    record_purchase(db, [post])

    # 5. 赛前锁定但无 closing 记录 → missing_closing（test_api:725 断言复制）
    missing = _create_bet(db, fixture_id=1)
    record_purchase(db, [missing])

    # 6. 赛前锁定 + closing 完整 → included，两旗均 True
    included = _create_bet(db, fixture_id=1)
    record_purchase(db, [included])
    _seed_closing(db, included, fixture_id=1)

    views = review_views(db, list_bets(db))

    assert views[unlocked].forward == "excluded_unlocked"
    assert views[unknown].forward == "unknown"
    assert views[unknown].locked_pre_kickoff is None
    assert views[unknown].closing_present is False  # None 只在无腿时出现（FK 下不可能）
    assert views[live_post].forward == "live_separate"
    assert views[live_post].locked_pre_kickoff is False  # live 分组但事实旗照记
    assert views[post].forward == "excluded_post_kickoff"
    assert views[post].locked_pre_kickoff is False
    assert views[missing].forward == "missing_closing"
    assert views[missing].closing_present is False
    assert views[included].forward == "included"
    assert views[included].locked_pre_kickoff is True
    assert views[included].closing_present is True


def test_closing_counts_legs_not_bets(db: sqlite3.Connection) -> None:
    """closing 完备性按腿数对账：两腿只有一条 closing 记录仍缺。"""
    _seed_fixtures(db)
    bet_id = create_bet_with_legs(
        db,
        BetDraft(
            mode=BetMode.PAPER,
            stake=4.0,
            legs=[
                LegInput(
                    fixture_id=1, market_code="had", selection_code="h", locked_odds=6.5
                ),
                LegInput(
                    fixture_id=2, market_code="had", selection_code="d", locked_odds=3.4
                ),
            ],
        ),
    )
    record_purchase(db, [bet_id])
    _seed_closing(db, bet_id, fixture_id=1)  # 只补第一腿

    # 腿 2 已开赛 → 整注先折在 post_kickoff 分支（两腿混合时短板先短路）
    assert review_views(db, list_bets(db))[bet_id].forward == "excluded_post_kickoff"

    # 腿 2 推迟到未来后再看 closing 腿数口径：一腿记录仍缺，两腿齐才 included
    db.execute(
        "UPDATE fixtures SET kickoff_utc = ? WHERE id = 2",
        ((NOW + timedelta(hours=30)).isoformat(),),
    )
    views = review_views(db, list_bets(db))
    assert views[bet_id].forward == "missing_closing"
    assert views[bet_id].closing_present is False
    _seed_closing(db, bet_id, fixture_id=2)
    assert review_views(db, list_bets(db))[bet_id].forward == "included"
