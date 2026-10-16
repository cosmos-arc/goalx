"""NewsShock 抽取胶水（llm/guardian_extract）：输出预算接线护栏。

2026-10-16 探针教训（caliber-loop 票 07 扩样）：glm-5.3 混合推理档下
max_tokens=512 会让 thinking 吃光预算、可见输出为空——空返回桶富集
有事件文章，扩样产出 30 事件 vs v1 137。护栏钉死两件事：预算常量
给足 thinking 头空间，且常量真接线到 glm_chat（防回退硬编码）。
"""

from __future__ import annotations

from typing import Any

from goalx_backend.data.ingest.guardian_silver import Candidate, _Link
from goalx_backend.llm import guardian_extract as gx

# thinking 头空间下限（探针验证臂 2048 全解析；最长观测 1736）
MIN_COMPLETION_TOKENS = 2048


class _StubResult:
    content = '{"relevant": false, "events": []}'
    model = "stub-model"
    estimated_cny = 0.0


def _candidate() -> Candidate:
    link = _Link(
        sid="s1",
        kickoff_day="2024-03-10",
        kickoff_iso="2024-03-10T20:00:00",
        en_home="Arsenal",
        en_away="Chelsea",
    )
    return Candidate(
        article_sid="a1",
        published_at="2024-03-09T10:00:00Z",
        title="Arsenal team news",
        body="Saka is out injured.",
        link=link,
        side="home",
        pit_group="strict_pit",
    )


def test_output_budget_wired_and_sufficient(monkeypatch) -> None:
    """护栏：glm_chat 收到的 max_tokens=常量且 ≥ 探针验证臂 2048。"""
    captured: dict[str, Any] = {}

    def fake_chat(
        conn: Any,
        settings: Any,
        tier: Any,
        messages: Any,
        *,
        purpose: Any,
        max_tokens: int,
        temperature: float,
    ) -> _StubResult:
        captured["max_tokens"] = max_tokens
        return _StubResult()

    monkeypatch.setattr(gx, "glm_chat", fake_chat)
    extract = gx.make_extractor(conn=None, settings=object())  # type: ignore[arg-type]
    entry = extract(_candidate())
    assert entry["filtered"] is False
    assert entry["parsed"] is not None
    assert captured["max_tokens"] == gx.MAX_OUTPUT_TOKENS
    assert gx.MAX_OUTPUT_TOKENS >= MIN_COMPLETION_TOKENS
