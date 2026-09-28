"""数据集注册表规格断言（deepen-20260928 票 02）：声明与推导面钉死一致。

机器可校验契约：DATASETS 声明是唯一事实源（先改声明、再改码）——本文件把
首批三数据集的 deployment 名/cron、cli dispatch→同一 tasks 函数、RESUME
推导、checkpoint 表核对钉进字面量，任一漂移即红。票 03 全量搬迁时逐批
扩本文件（新记录入列 = "改表"动作）。

钉死时点：票 02 首批 = guardian-sync / pool-snapshot / srct-night；
serve 清单金标 = 手写 wiring 时代末态（17 deployment，名与 cron 逐一相等）。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from goalx_backend import cli, datasets, schedules, tasks
from goalx_backend.data import corpus_store
from goalx_backend.data.ingest import srct


def test_registry_pinned_to_first_batch() -> None:
    """首批三记录逐字段钉死：task 同一性（ADR-0005）、cron、cli 命令名。"""
    by_name = {spec.name: spec for spec in datasets.DATASETS}
    assert set(by_name) == {"guardian-sync", "pool-snapshot", "srct-night"}
    # dispatch 与定拍命中同一 tasks 函数（一份实现，两个 adapter）
    assert by_name["guardian-sync"].task is tasks.guardian_sync
    assert by_name["pool-snapshot"].task is tasks.pool_snapshot
    assert by_name["srct-night"].task is tasks.srct_night_run
    assert by_name["guardian-sync"].cron == "40 9 * * *"
    assert by_name["pool-snapshot"].cron == "20 10,16,22 * * *"
    assert by_name["srct-night"].cron == "0 1 * * *"
    # pool 历史上 cli 命令名 ≠ deployment 键，逐字保留
    assert by_name["pool-snapshot"].cli_command == "pool-sync"
    assert [spec.command for spec in datasets.DATASETS] == [
        "guardian-sync",
        "pool-sync",
        "srct-night",
    ]


def test_schedules_serve_list_pinned_golden() -> None:
    """票 02 不变量：17 deployment 名→cron 与手写时代逐一相等（键序为新序，
    推导面三数据集排尾——serve 对各 deployment 独立注册，键序非契约）。"""
    deps = schedules.build_deployments()
    assert tuple(deps) == (
        "daily-capture",
        "eu-odds-closing",
        "draw-results-sync",
        "draw-results-sweep",
        "official-reconcile",
        "understat-sync",
        "clubelo-sync",
        "odds-anchor-dense",
        "srcb-collect",
        "srct-shift",
        "weekly-refresh",
        "daily-wrap",
        "intel-collect",
        "scout-line",
        "guardian-sync",
        "pool-snapshot",
        "srct-night",
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
    """RESUME = 手写余量 + 注册表推导；两判死面不在。"""
    assert set(schedules.RESUME_DEPLOYMENTS) == {
        "daily-capture",
        "draw-results-sync",
        "draw-results-sweep",
        "official-reconcile",
        "srct-night",
        "srct-shift",
        "understat-sync",
        "clubelo-sync",
        "guardian-sync",
        "odds-anchor-dense",
        "weekly-refresh",
        "daily-wrap",
        "pool-snapshot",
        "intel-collect",
        "scout-line",
    }
    assert set(datasets.resume_names()) <= set(schedules.RESUME_DEPLOYMENTS)
    # 判死面例外表显式列出，且与恢复清单不相交
    assert schedules.DEAD_DEPLOYMENTS == ("eu-odds-closing", "srcb-collect")
    assert set(schedules.DEAD_DEPLOYMENTS).isdisjoint(schedules.RESUME_DEPLOYMENTS)


def test_cli_surface_parses_with_handwritten_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli 子命令由注册表推导：参数默认值/形状与手写时代逐字一致。"""
    parser = cli.build_parser()
    args = parser.parse_args(["srct-night"])
    assert args.request_cap == srct.NIGHT_REQUEST_CAP
    assert args.no_window is False
    assert args.list is False
    assert args.limit == 20
    args = parser.parse_args(["guardian-sync", "--request-cap", "3"])
    assert args.request_cap == 3
    args = parser.parse_args(["pool-sync"])
    assert args.command == "pool-sync"


def test_run_cli_maps_args_to_task_kwargs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """run_cli：本子命令参数 → task 形参（含 list→list_mode 重命名），载荷打印 JSON。"""
    captured: dict[str, object] = {}

    def stub(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True}

    night = next(spec for spec in datasets.DATASETS if spec.name == "srct-night")
    spec = replace(night, task=stub)
    args = cli.build_parser().parse_args(
        ["srct-night", "--request-cap", "7", "--list", "--limit", "5"]
    )
    datasets.run_cli(spec, args)
    assert captured == {
        "request_cap": 7,
        "no_window": False,
        "list_mode": True,
        "limit": 5,
    }
    assert capsys.readouterr().out.strip() == '{\n  "ok": true\n}'


def test_checkpoint_tables_registered() -> None:
    """记录的 checkpoint 表名 ⊆ corpus_store 单处登记面（规格核对，非运行时推导）。"""
    registered = corpus_store.checkpoint_table_names()
    for spec in datasets.DATASETS:
        assert set(spec.checkpoint_tables) <= set(registered), spec.name
    assert "guardian_sync_state" in registered
    assert "guardian_request_days" in registered
    assert "srct_night_summaries" in registered
