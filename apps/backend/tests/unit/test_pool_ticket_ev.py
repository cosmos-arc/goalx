"""彩池票级估值数值锚（报告审计票 08）。

返奖率整票只扣一次（既定模型）：ticketEV = ∏p × R/∏s − 1。
报告二数值判别样例钉死：四场各 p/s=1.25 → +58.69%（前端把含 R 的
场级"赔率"送进串关连乘会得 −56.42%，正负判反）。前端镜像
poolTicketAdvice 的同输入一致由 web 侧 lib/组件测试对同一组数字断言。
"""

from __future__ import annotations

import pytest

from goalx_backend.betting.pool_strategy import ticket_view
from goalx_backend.data.pool import PoolMatchView, PoolSelectionView


def _match(seq: int, prob: float, share: float) -> PoolMatchView:
    return PoolMatchView(
        match_seq=seq,
        league="测试",
        kickoff_utc="2026-11-06T10:00:00+00:00",
        home_team=f"主{seq}",
        away_team=f"客{seq}",
        selections=[
            PoolSelectionView(
                code="h", label="胜", prob=prob, prob_source="model", share=share
            ),
            PoolSelectionView(
                code="d", label="平", prob=0.2, prob_source="model", share=0.3
            ),
            PoolSelectionView(
                code="a", label="负", prob=0.3, prob_source="model", share=0.3
            ),
        ],
    )


def test_ticket_view_return_rate_applied_once() -> None:
    """报告二验收样例：四场各 p/s=1.25 → +58.69%（R 只扣一次）。"""
    views = [_match(seq, prob=0.5, share=0.4) for seq in range(1, 5)]  # p/s=1.25
    ticket = ticket_view(dict.fromkeys(range(1, 5), "h"), views)
    assert ticket.ev == pytest.approx(0.65 * 1.25**4 - 1, abs=1e-4)
    assert ticket.ev == pytest.approx(0.5869, abs=1e-4)


def test_ticket_view_nine_singles_mirror_frontend_fixture() -> None:
    """恰九场每场单选：与前端任9 页同输入一致（msw fixture 形态镜像）。

    前端 fixture 预选前 9 场：7 场 p/s=0.45/0.5、2 场（场 2/5）p/s=
    0.48/0.34 → EV = 0.65×∏(p/s) − 1 ≈ −0.3805。web 侧组件测试捕获
    stake-advice 请求体断言同一数值——两侧同输入同输出即验收②。
    """
    # 预选前 9 场：场 2/5 为冷门样本（p/s=0.48/0.34），其余 7 场 0.45/0.5
    cold = 0.48 / 0.34
    ratios = [0.9, cold, 0.9, 0.9, cold, 0.9, 0.9, 0.9, 0.9]
    views = [
        _match(seq, prob=ratio * 0.5, share=0.5) for seq, ratio in enumerate(ratios, 1)
    ]
    picks = dict.fromkeys(range(1, 10), "h")
    ticket = ticket_view(picks, views)
    expected = 0.65
    for ratio in ratios:
        expected *= ratio
    # ticket_view 输出按 4 位小数舍入：真值 −0.380366 → −0.3804
    assert ticket.ev == pytest.approx(expected - 1, abs=1e-4)
    assert ticket.ev == pytest.approx(-0.3804, abs=5e-5)
