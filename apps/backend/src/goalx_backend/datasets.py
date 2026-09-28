"""
定拍数据集注册表（deepen-20260928 票 02）：一份声明推导 cli/flow/deployment。

新增定拍数据集只摊两处：tasks.py 落任务实现 + 本模块加一条 DatasetSpec——
cli 子命令、Prefect flow、serve deployment、恢复清单（resume_names）、
（语料数据集的）checkpoint 表核对全部由声明推导。任务实现唯一住所仍是
tasks（ADR-0005）：本模块持引用不持实现。

首批（票 02）入册三个形态代表：guardian（纯 corpus 定拍）、pool-snapshot
（运行面）、srct-night（off-gate 闸门 + 丰富 CLI）；其余数据集手写 wiring
原样并存，票 03 全量搬迁后 RESUME 与 deployment 字典收口为纯推导。
checkpoint 表登记是规格核对（corpus_store 单处建表，记录表名交叉断言），
不是运行时推导——checkpoint 库 SQL 自持在 corpus_store（ADR-0008）。

ponytail: 记录形状从简（frozen dataclass 元组），不搞插件系统/装饰器注册
魔法；非同构 flow（daily-capture/daily-wrap/weekly-refresh 等编排型）不硬塞，
留在 flows.py 手写。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from loguru import logger
from prefect import flow
from prefect.deployments.runner import RunnerDeployment
from prefect.flows import Flow
from prefect.schedules import Schedule

from goalx_backend import tasks
from goalx_backend.data.ingest import srct

_DEPLOYMENT_NAME = "protocol-v1"
_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class CliArg:
    """一个 cli 子命令参数（add_argument 的声明式形状；覆盖现有用法，够用即止）。"""

    flag: str
    help: str
    type: type | None = None  # int 等（None=flag 型）
    default: object = None
    action: str | None = None  # "store_true" 时 default 恒 False

    @property
    def dest(self) -> str:
        """目标属性名（--request-cap → request_cap，同 argparse 官方规则）。"""
        return self.flag.lstrip("-").replace("-", "_")

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """按声明挂到子 parser（分支直传避开 add_argument 的联合重载）。"""
        if self.action is not None:
            parser.add_argument(self.flag, help=self.help, action=self.action)
        elif self.type is not None:
            parser.add_argument(
                self.flag, help=self.help, type=self.type, default=self.default
            )
        else:
            parser.add_argument(self.flag, help=self.help, default=self.default)


@dataclass(frozen=True)
class DatasetSpec:
    """一条定拍数据集声明：名字、cron、任务函数、cli 参数、checkpoint 表。"""

    name: str  # deployment 键 = flow 名（kebab-case）
    cron: str  # Asia/Shanghai
    task: Callable[..., dict[str, object]]  # 住所是 tasks；此处持引用不持实现
    cli_help: str
    flow_help: str
    cli_command: str | None = None  # 缺省 = name（pool 命令名历史上与键不同）
    cli_args: tuple[CliArg, ...] = ()
    cli_renames: tuple[tuple[str, str], ...] = ()  # cli dest → task 形参名（缺省同名）
    checkpoint_tables: tuple[str, ...] = ()  # 语料 checkpoint 表名（规格核对面）

    @property
    def command(self) -> str:
        """子命令名（cli 子命令与 dispatch 共用，勿再手抄 or 表达式）。"""
        return self.cli_command or self.name


_GUARDIAN = DatasetSpec(
    name="guardian-sync",
    cron="40 9 * * *",
    task=tasks.guardian_sync,
    cli_help="卫报新闻语料同步(票79;回填~2日@500/日,断点续跑幂等)",
    flow_help="卫报新闻语料同步（票 79）：回填翻页/日增量，断点续跑幂等。",
    cli_args=(
        CliArg(
            "--request-cap",
            help="单次请求上限(默认=当日剩余预算全部)",
            type=int,
        ),
    ),
    checkpoint_tables=("guardian_sync_state", "guardian_request_days"),
)

_POOL_SNAPSHOT = DatasetSpec(
    name="pool-snapshot",
    cron="20 10,16,22 * * *",
    task=tasks.pool_snapshot,
    cli_command="pool-sync",
    cli_help="手动拉一次彩池期次/对阵/人气分布(票 43)",
    flow_help="彩池同步（票 68 官方化）：体彩官方在售对阵+上期彩果（幂等）。",
)

_SRCT_NIGHT = DatasetSpec(
    name="srct-night",
    cron="0 1 * * *",
    task=tasks.srct_night_run,
    cli_help="源T夜班推进Phase1回填(票55切片13;预算/熔断/断点续传,摘要落库)",
    flow_help=(
        "源T夜班（票 55 切片 13）：Phase1 回填批自动推进。\n"
        "01:00 deployment 触发；窗口/预算/熔断在任务体内裁（窗口外零成本）。\n"
        "每夜摘要落语料树 checkpoint 库，`goalx srct-night --list` 晨检。"
    ),
    cli_args=(
        CliArg(
            "--request-cap",
            help=f"当夜请求预算上限(默认 {srct.NIGHT_REQUEST_CAP})",
            type=int,
            default=srct.NIGHT_REQUEST_CAP,
        ),
        CliArg(
            "--no-window",
            help="跳过 01:00-08:00 窗口判断(白天冒烟/手工回补用)",
            action="store_true",
        ),
        CliArg("--list", help="只读查最近夜班摘要(不发请求)", action="store_true"),
        CliArg("--limit", help="--list 行数(默认 20)", type=int, default=20),
    ),
    cli_renames=(("list", "list_mode"),),
    checkpoint_tables=("srct_night_summaries",),
)

#: 首批入册记录（票 02；03 全量搬迁时逐条追加）
DATASETS: tuple[DatasetSpec, ...] = (_GUARDIAN, _POOL_SNAPSHOT, _SRCT_NIGHT)


def resume_names() -> tuple[str, ...]:
    """恢复清单的注册表推导部分（票 03 全量后吞并 schedules 手写余量）。"""
    return tuple(spec.name for spec in DATASETS)


def build_flow(spec: DatasetSpec) -> Flow[[], dict[str, object]]:
    """同构 flow 壳工厂：调 tasks 函数、记日志、回 stats（非同构的留 flows.py）。"""

    def _recorded_flow() -> dict[str, object]:
        stats = spec.task()
        logger.info("{}: {}", spec.name, stats)
        return stats

    _recorded_flow.__doc__ = spec.flow_help  # @flow 取 docstring 作 description
    return flow(name=spec.name, log_prints=True)(_recorded_flow)


def build_deployments() -> dict[str, RunnerDeployment]:
    """注册表 → serve deployment（name=protocol-v1，与手写面同形）。"""
    return {
        spec.name: cast(
            RunnerDeployment,
            build_flow(spec).to_deployment(
                name=_DEPLOYMENT_NAME,
                schedule=Schedule(cron=spec.cron, timezone=_TIMEZONE),
            ),
        )
        for spec in DATASETS
    }


class _SubParserAdder(Protocol):
    """argparse 子命令注册面的结构类型（_SubParsersAction 私有不可注）。"""

    def add_parser(
        self,
        name: str,
        *,
        help: str | None = None,  # noqa: A002 与 argparse 同名 kwarg
    ) -> argparse.ArgumentParser: ...


def add_cli_subparsers(sub: _SubParserAdder) -> None:
    """注册表 → cli 子命令（help 文案与参数默认值逐字保留手写时代）。"""
    for spec in DATASETS:
        parser = sub.add_parser(spec.command, help=spec.cli_help)
        for arg in spec.cli_args:
            arg.add_to(parser)


def run_cli(spec: DatasetSpec, args: argparse.Namespace) -> None:
    """注册表 → cli handler：本子命令参数映射为 task 形参，载荷打印 JSON。"""
    renames = dict(spec.cli_renames)
    kwargs = {
        renames.get(arg.dest, arg.dest): getattr(args, arg.dest)
        for arg in spec.cli_args
    }
    payload = spec.task(**kwargs)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
