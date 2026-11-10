"""票 02：daily-wrap 的 M3 独立异常边界——评估失败不拖后续对账审计。

裸编排体（``daily_wrap_flow.fn``，不拉 Prefect runtime）+ 全依赖替换：
控制流注入一次 M3 EOFError，断言返回体含 error 且后续四项
（冻结对账/CLV/账本回填/账务核查）确实执行。autoresearch 票 01/04 追加
探索轨日扫步（wrap 尾）与周一周报门（funnel 业务日历判）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from goalx_backend import flows


@dataclass
class _FakeClv:
    n: int = 0


def _patch_all(monkeypatch: Any, business_date: str) -> list[str]:
    """替换 daily-wrap 全依赖并记录执行序（返回 order 供断言）。"""
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
        lambda: step("funnel", {"business_date": business_date}),
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
        flows.probes,
        "backfill_bet_clv",
        lambda conn, anchor: step("probe_clv", {"filled": 0}),
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
    monkeypatch.setattr(
        flows.tasks,
        "edge_scan_day",
        lambda: step("edge_scan", {"business_date": business_date}),
    )
    monkeypatch.setattr(
        flows.tasks,
        "edge_digest_week",
        lambda: step("edge_digest", {"week": "ok"}),
    )
    monkeypatch.setattr(
        flows.tasks,
        "news_pit_beat",
        lambda: step("news_pit", {"articles": 3}),
    )
    return order


def test_m3_failure_degrades_without_dragging_downstream(monkeypatch: Any) -> None:
    """注入一次 M3 读取异常 → 错误可见（返回体含 error）→ 后续步确实执行。"""
    order = _patch_all(monkeypatch, "2026-11-05")  # 周四：无周报
    result = flows.daily_wrap_flow.fn()

    # M3 失败可见：error + degraded 标记，不生成任何有效判词（失败≠通过）
    assert result["m3"] == {
        "error": "corpus 并发读窗口（写完重读正常）",
        "degraded": True,
    }
    # 后续各步确实执行，且都在 M3 之后（对账审计不被拖挂）；探索轨日扫
    # 挂 wrap 尾部（结算后才有"昨日已结算场"）；新闻 PIT 夜拍（票 17）挂
    # 日扫后——当晚新闻纪元随日终收口落库
    assert order == [
        "settlement",
        "funnel",
        "m3",
        "frozen_recon",
        "task_conn",
        "corpus_anchor",
        "clv",
        "probe_clv",
        "ledger_backfill",
        "ledger_summary",
        "audit",
        "frozen_summary",
        "edge_scan",
        "news_pit",
    ]
    assert result["frozen_reconcile"] == {"ok": True}
    assert result["clv"] == {"n": 2}
    assert result["probe_clv"] == {"filled": 0}
    assert result["price_ledger"]["backfilled"] == 7
    assert result["audit_findings"] == 0
    assert result["edge_scan"] == {"business_date": "2026-11-05"}
    assert result["news_pit"] == {"articles": 3}
    # 非周一：周报不跑（funnel 业务日为周四）
    assert result["edge_digest"] is None


def test_monday_runs_weekly_digest(monkeypatch: Any) -> None:
    """周一（funnel 业务日）→ 日扫后加跑周报 digest（夜拍殿后）。"""
    order = _patch_all(monkeypatch, "2026-11-09")  # 周一
    result = flows.daily_wrap_flow.fn()
    assert order[-2:] == ["edge_digest", "news_pit"]
    assert result["edge_digest"] == {"week": "ok"}


def test_edge_scan_failure_degrades_without_dragging_return(monkeypatch: Any) -> None:
    """探索轨日扫异常 → 返回体含 error+degraded（失败≠通过，不炸 wrap）。"""
    order = _patch_all(monkeypatch, "2026-11-05")

    def boom_scan() -> dict[str, object]:
        order.append("edge_scan")
        raise RuntimeError("扫描面断线")

    monkeypatch.setattr(flows.tasks, "edge_scan_day", boom_scan)
    result = flows.daily_wrap_flow.fn()
    assert result["edge_scan"] == {"error": "扫描面断线", "degraded": True}
    assert result["frozen_summary"] == {"date": "2026-11-05"}


def test_news_pit_beat_failure_degrades_isolated(monkeypatch: Any) -> None:
    """新闻 PIT 夜拍（票 17）异常 → 返回体含 error+degraded，不炸 wrap 本体。

    红线：拍点失败不阻塞夜班主链（结算/对账/日扫全部照跑——每晚不开=
    永久缺口，失败夜的可见性由 degraded 标记+晨检缺夜面兜住）。
    """
    order = _patch_all(monkeypatch, "2026-11-05")

    def boom_beat() -> dict[str, object]:
        order.append("news_pit")
        raise httpx.ConnectError("卫报直连断线")

    monkeypatch.setattr(flows.tasks, "news_pit_beat", boom_beat)
    result = flows.daily_wrap_flow.fn()
    assert result["news_pit"] == {
        "error": "卫报直连断线",
        "degraded": True,
    }
    # wrap 本体（结算/对账/日扫）全部执行完毕
    assert order[-2:] == ["edge_scan", "news_pit"]
    assert result["frozen_summary"] == {"date": "2026-11-05"}
    assert result["edge_scan"] == {"business_date": "2026-11-05"}
