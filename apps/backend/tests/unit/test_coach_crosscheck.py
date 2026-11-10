"""srct 教练名交叉核验测试（autoresearch 票 18；只读、诚实小样本）。

语义：结构性一致——TM 换帅边界前后，源T 侧该队教练名（中文）是否也换人。
中英文教练名无映射，故只核「名有没有换」不核「名是谁」；窗口与覆盖缺失
如实计数（skipped），不猜。
"""

from __future__ import annotations

from goalx_backend.data import coach_crosscheck


def _obs(day: str, coach: str | None) -> tuple[str, str | None]:
    return (day, coach)


def test_agreement_counts_name_change_across_boundary() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        )
    ]
    by_team = {
        1: [
            _obs("2024-11-10", "阿尔特塔前前任"),
            _obs("2024-11-24", "前任中文"),
            _obs("2024-12-03", "新任中文"),
            _obs("2024-12-15", "新任中文"),
        ]
    }
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["eligible"] == 1
    assert report["agree"] == 1
    assert report["disagree"] == 0
    assert report["agreement_rate"] == 1.0


def test_disagree_when_srct_name_unchanged() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        )
    ]
    by_team = {1: [_obs("2024-11-20", "同一名"), _obs("2024-12-05", "同一名")]}
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["disagree"] == 1
    assert report["agreement_rate"] == 0.0


def test_skipped_when_no_coverage_on_either_side() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        ),
        coach_crosscheck.EventLite(
            team_id=2, club_name="Chelsea", event_date="2024-12-01"
        ),
    ]
    by_team = {
        1: [_obs("2024-11-20", "X")],  # 只有前无后
        2: [_obs("2024-12-20", "Y")],  # 只有后无前
    }
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["eligible"] == 0
    assert report["skipped_no_coverage"] == 2
    assert report["agreement_rate"] is None  # 无分母不造率


def test_window_bounds_respected() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        )
    ]
    by_team = {
        1: [
            _obs("2024-09-01", "太早之前"),  # 窗外（>45d）不用
            _obs("2024-11-25", None),  # 窗内但无教练名 → 跳过继续找
            _obs("2024-11-20", "前任"),
            _obs("2025-02-01", "太晚之后"),  # 窗外不用
            _obs("2024-12-20", "新任"),
        ]
    }
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["agree"] == 1
    assert report["eligible"] == 1


def test_event_day_itself_is_after_side() -> None:
    """事件日=新帅首战日：当日观察属后侧（不属前侧）。"""
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        )
    ]
    by_team = {1: [_obs("2024-11-28", "前任"), _obs("2024-12-01", "新任")]}
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["agree"] == 1


def test_events_without_team_id_are_excluded() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=None, club_name="未解析FC", event_date="2024-12-01"
        )
    ]
    report = coach_crosscheck.agreement_from_rows(events, {}, window_days=45)
    assert report["events"] == 1
    assert report["unresolved_team"] == 1
    assert report["eligible"] == 0


def test_unsorted_input_is_handled() -> None:
    events = [
        coach_crosscheck.EventLite(
            team_id=1, club_name="Arsenal", event_date="2024-12-01"
        )
    ]
    by_team = {
        1: [_obs("2024-12-03", "新任"), _obs("2024-11-01", "前任")]  # 乱序给入
    }
    report = coach_crosscheck.agreement_from_rows(events, by_team, window_days=45)
    assert report["agree"] == 1
