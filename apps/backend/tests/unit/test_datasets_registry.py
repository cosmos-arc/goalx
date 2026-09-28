"""数据集注册表规格断言（票 02 建，票 03 扩至全量）：声明与推导面钉死一致。

机器可校验契约：DATASETS 声明是唯一事实源（先改声明、再改码）——本文件把
全部定拍数据集的 deployment 名/cron、cli dispatch→同一 tasks 函数、
RESUME/DEAD 推导、checkpoint 表核对钉进字面量，任一漂移即红。新数据集
入列（或撤采翻 resume）必须同步改本文件——这就是"改表"动作的测试面。

钉死时点：票 03 全量 = 12 条注册（含判死面 eu-odds-closing/srcb-collect
resume=False）+ 手写 5 面（daily-capture、draw-results 双 cron、weekly、
daily-wrap）；serve 清单金标 = 手写 wiring 时代末态（17 deployment，
名与 cron 逐一相等）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from goalx_backend import cli, datasets, schedules, tasks
from goalx_backend.data import corpus_store
from goalx_backend.data.ingest import srct, srct_shift

_RECORD_NAMES = (
    "guardian-sync",
    "pool-snapshot",
    "srct-night",
    "official-reconcile",
    "understat-sync",
    "clubelo-sync",
    "odds-anchor-dense",
    "srcb-collect",
    "eu-odds-closing",
    "intel-collect",
    "scout-line",
    "srct-shift",
)


def test_registry_pinned_to_full_batch() -> None:
    """全量 12 记录钉死：task 同一性（ADR-0005）、cron、cli 命令面。"""
    by_name = {spec.name: spec for spec in datasets.DATASETS}
    assert tuple(by_name) == _RECORD_NAMES
    # dispatch 与定拍命中同一 tasks 函数（一份实现，两个 adapter）
    assert by_name["guardian-sync"].task is tasks.guardian_sync
    assert by_name["pool-snapshot"].task is tasks.pool_snapshot
    assert by_name["srct-night"].task is tasks.srct_night_run
    assert by_name["official-reconcile"].task is tasks.official_results_reconcile
    assert by_name["understat-sync"].task is tasks.understat_sync
    assert by_name["clubelo-sync"].task is tasks.clubelo_sync
    assert by_name["odds-anchor-dense"].task is tasks.odds_anchor_dense
    assert by_name["srcb-collect"].task is tasks.srcb_collect
    assert by_name["eu-odds-closing"].task is tasks.eu_odds_closing
    assert by_name["intel-collect"].task is tasks.intel_collection
    assert by_name["scout-line"].task is tasks.scout_line
    assert by_name["srct-shift"].task is tasks.srct_shift_run
    assert by_name["official-reconcile"].cron == "30 8 * * *"
    assert by_name["understat-sync"].cron == "20 9 * * *"
    assert by_name["clubelo-sync"].cron == "10 9 * * *"
    assert by_name["odds-anchor-dense"].cron == "*/5 * * * *"
    assert by_name["srcb-collect"].cron == "40 10,22 * * *"
    assert by_name["eu-odds-closing"].cron == "*/30 * * * *"
    assert by_name["intel-collect"].cron == "40 10,22 * * *"
    assert by_name["scout-line"].cron == "50 10,22 * * *"
    assert by_name["srct-shift"].cron == "*/30 8-23,0 * * *"
    # cli 面：命令名逐字保留（pool/closing 历史上命令名 ≠ deployment 键）；
    # cli=False 五面不派生 cli（official-reconcile 手写 handler 保留）
    assert [spec.command for spec in datasets.DATASETS if spec.cli] == [
        "guardian-sync",
        "pool-sync",
        "srct-night",
        "understat-sync",
        "clubelo-sync",
        "closing-snapshot",
        "srct-shift",
    ]
    assert by_name["eu-odds-closing"].cli_command == "closing-snapshot"
    assert {name for name in _RECORD_NAMES if not by_name[name].cli} == {
        "official-reconcile",
        "odds-anchor-dense",
        "srcb-collect",
        "intel-collect",
        "scout-line",
    }
    # 判死面 resume=False，其余 True
    assert datasets.dead_names() == ("srcb-collect", "eu-odds-closing")


def test_schedules_serve_list_pinned_golden() -> None:
    """票 03 不变量：17 deployment 名→cron 与手写时代逐一相等（键序为新序，
    组合面 5 个在前、注册面 12 个在后——serve 独立注册各面，键序非契约）。"""
    deps = schedules.build_deployments()
    assert tuple(deps) == (
        "daily-capture",
        "draw-results-sync",
        "draw-results-sweep",
        "weekly-refresh",
        "daily-wrap",
        *_RECORD_NAMES,
    )
    crons = {}
    for name, dep in deps.items():
        schedules_on_dep = dep.schedules
        assert len(schedules_on_dep) == 1
        assert dep.name == "protocol-v1" or name == "draw-results-sweep"
        assert schedules_on_dep[0].schedule.timezone == "Asia/Shanghai"
        crons[name] = schedules_on_dep[0].schedule.cron
    assert crons == {
        "daily-capture": "0 10,19 * * *",
        "eu-odds-closing": "*/30 * * * *",
        "draw-results-sync": "*/30 0-5,18-23 * * *",
        "draw-results-sweep": "0 8 * * *",
        "official-reconcile": "30 8 * * *",
        "understat-sync": "20 9 * * *",
        "clubelo-sync": "10 9 * * *",
        "guardian-sync": "40 9 * * *",
        "odds-anchor-dense": "*/5 * * * *",
        "srcb-collect": "40 10,22 * * *",
        "srct-night": "0 1 * * *",
        "srct-shift": "*/30 8-23,0 * * *",
        "weekly-refresh": "10 6 * * 1",
        "daily-wrap": "30 23 * * *",
        "pool-snapshot": "20 10,16,22 * * *",
        "intel-collect": "40 10,22 * * *",
        "scout-line": "50 10,22 * * *",
    }


def test_derived_deployments_flow_names_match_records() -> None:
    """推导面：deployment 名 protocol-v1、flow 名=记录名（注册数据集部分）。"""
    deps = schedules.build_deployments()
    for spec in datasets.DATASETS:
        dep = deps[spec.name]
        assert dep.name == "protocol-v1"
        assert dep.flow_name == spec.name


def test_resume_derivation() -> None:
    """RESUME = 组合面手写 5 + 注册表推导；判死面推导自注册表。"""
    assert set(schedules.RESUME_DEPLOYMENTS) == {
        "daily-capture",
        "draw-results-sync",
        "draw-results-sweep",
        "weekly-refresh",
        "daily-wrap",
        *_RECORD_NAMES,
    } - set(datasets.dead_names())
    assert set(datasets.resume_names()).isdisjoint(datasets.dead_names())
    assert datasets.dead_names() == schedules.DEAD_DEPLOYMENTS
    assert set(schedules.DEAD_DEPLOYMENTS).isdisjoint(schedules.RESUME_DEPLOYMENTS)


def test_cli_surface_parses_with_handwritten_defaults() -> None:
    """cli 子命令由注册表推导：参数默认值/形状与手写时代逐字一致。"""
    parser = cli.build_parser()
    args = parser.parse_args(["srct-night"])
    assert args.request_cap == srct.NIGHT_REQUEST_CAP
    assert args.no_window is False
    assert args.list is False
    assert args.limit == 20
    args = parser.parse_args(["srct-shift"])
    assert args.request_cap == srct_shift.SHIFT_REQUEST_CAP
    assert args.no_window is False
    args = parser.parse_args(["understat-sync", "--seasons", "2021", "2022"])
    assert args.seasons == ["2021", "2022"]
    assert args.leagues == []
    args = parser.parse_args(["clubelo-sync"])
    assert args.backfill is False
    args = parser.parse_args(["closing-snapshot"])
    assert args.command == "closing-snapshot"


def test_run_cli_maps_args_to_task_kwargs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_cli：参数 → task 形参（重命名 + nargs→tuple/None 归一），载荷打印 JSON。"""
    captured: dict[str, object] = {}

    def stub(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    night = next(spec for spec in datasets.DATASETS if spec.name == "srct-night")
    datasets.run_cli(
        replace(night, task=stub),
        cli.build_parser().parse_args(
            ["srct-night", "--request-cap", "7", "--list", "--limit", "5"]
        ),
    )
    assert captured == {
        "request_cap": 7,
        "no_window": False,
        "list_mode": True,
        "limit": 5,
    }

    understat = next(
        spec for spec in datasets.DATASETS if spec.name == "understat-sync"
    )
    captured.clear()
    datasets.run_cli(
        replace(understat, task=stub),
        cli.build_parser().parse_args(["understat-sync", "--leagues", "rfpl"]),
    )
    # seasons 缺省 None（当前季）；leagues 空→None、传值→tuple（旧 handler 口径）
    assert captured == {"seasons": None, "leagues": ("rfpl",)}
    captured.clear()
    datasets.run_cli(
        replace(understat, task=stub),
        cli.build_parser().parse_args(["understat-sync", "--seasons", "2021"]),
    )
    assert captured == {"seasons": ("2021",), "leagues": None}
    assert capsys.readouterr().out.count('"ok": true') == 3


def test_checkpoint_tables_registered() -> None:
    """记录的 checkpoint 表名 ⊆ corpus_store 单处登记面（规格核对，非运行时推导）。"""
    registered = corpus_store.checkpoint_table_names()
    for spec in datasets.DATASETS:
        assert set(spec.checkpoint_tables) <= set(registered), spec.name
    assert "guardian_sync_state" in registered
    assert "guardian_request_days" in registered
    assert "srct_night_summaries" in registered
    assert "srct_shift_matches" in registered
