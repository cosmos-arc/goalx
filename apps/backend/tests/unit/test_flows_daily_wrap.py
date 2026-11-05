"""票 02：daily-wrap 的 M3 独立异常边界——评估失败不拖后续对账审计。

裸编排体（``daily_wrap_flow.fn``，不拉 Prefect runtime）+ 全依赖替换：
控制流注入一次 M3 EOFError，断言返回体含 error 且后续四项
（冻结对账/CLV/账本回填/账务核查）确实执行。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from goalx_backend import flows


@dataclass
class _FakeClv:
    n: int = 0


def test_m3_failure_degrades_without_dragging_downstream(monkeypatch: Any) -> None:
    """注入一次 M3 读取异常 → 错误可见（返回体含 error）→ 后续四项确实执行。"""
    order: list[str] = []

    def step(name: str, value: Any) -> Any:
        order.append(name)
        return value

    @contextmanager
    def fake_conn() -> Iterator[None]:
        order.append("task_conn")
        yield None

    @contextmanager
    def fake_anchor() -> Iterator[None]:
        order.append("corpus_anchor")
        yield None

    def boom() -> dict[str, object]:
        order.append("m3")
        raise EOFError("corpus 并发读窗口（写完重读正常）")

    monkeypatch.setattr(
        flows, "settlement_flow", lambda: step("settlement", {"settled": 0})
    )
    monkeypatch.setattr(
        flows.tasks,
        "funnel_day",
        lambda: step("funnel", {"business_date": "2026-11-05"}),
    )
    monkeypatch.setattr(flows.tasks, "m3_evaluation", boom)
    monkeypatch.setattr(
        flows.tasks, "frozen_reconcile", lambda: step("frozen_recon", {"ok": True})
    )
    monkeypatch.setattr(flows.tasks, "task_conn", fake_conn)
    monkeypatch.setattr(flows.tasks, "corpus_anchor", fake_anchor)
    monkeypatch.setattr(
        flows.clv_mod,
        "reconcile_clv",
        lambda conn, duck_con=None: step("clv", _FakeClv(n=2)),
    )
    monkeypatch.setattr(
        flows.price_ledger,
        "backfill_missing_reference",
        lambda conn, anchor: step("ledger_backfill", 7),
    )
    monkeypatch.setattr(
        flows.price_ledger,
        "ledger_summary",
        lambda conn: step("ledger_summary", {"rows": 3}),
    )
    monkeypatch.setattr(
        flows, "audit_ledger", lambda conn: step("audit", {"manual_review": []})
    )
    monkeypatch.setattr(
        flows.frozen_mod,
        "frozen_summary",
        lambda conn, business_date: step("frozen_summary", {"date": business_date}),
    )

    result = flows.daily_wrap_flow.fn()

    # M3 失败可见：error + degraded 标记，不生成任何有效判词（失败≠通过）
    assert result["m3"] == {
        "error": "corpus 并发读窗口（写完重读正常）",
        "degraded": True,
    }
    # 后续四项确实执行，且都在 M3 之后（对账审计不被拖挂）
    assert order == [
        "settlement",
        "funnel",
        "m3",
        "frozen_recon",
        "task_conn",
        "corpus_anchor",
        "clv",
        "ledger_backfill",
        "ledger_summary",
        "audit",
        "frozen_summary",
    ]
    assert result["frozen_reconcile"] == {"ok": True}
    assert result["clv"] == {"n": 2}
    assert result["price_ledger"]["backfilled"] == 7
    assert result["audit_findings"] == 0
