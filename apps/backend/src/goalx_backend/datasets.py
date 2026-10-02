"""
定拍数据集注册表（票 02 建，票 03 全量）：一份声明推导 cli/flow/deployment。

新增定拍数据集只摊两处：tasks.py 落任务实现 + 本模块加一条 DatasetSpec——
cli 子命令、Prefect flow、serve deployment、恢复/判死清单（resume_names/
dead_names）、（语料数据集的）checkpoint 表核对全部由声明推导。任务实现
唯一住所仍是 tasks（ADR-0005）：本模块持引用不持实现。

票 03 后注册面 = 全部单 cron 定拍数据集（12 条，含两个判死面 resume=False）；
留在 flows.py 手写的非同构形状：组合面（daily-capture/weekly-refresh/
daily-wrap 多 flow 编排）与 draw-results-sync（单 flow 双 deployment 双 cron）。
cli=False 的记录不派生 cli（official-reconcile 的手写 handler 带 pending_manual
告警走查，纯 schedule 面无命令）；checkpoint 表登记是规格核对（corpus_store
单处建表，记录表名交叉断言），不是运行时推导——checkpoint 库 SQL 自持在
corpus_store（ADR-0008）。

ponytail: 记录形状从简（frozen dataclass 元组），不搞插件系统/装饰器注册
魔法；非同构 flow 不硬塞，留在 flows.py 手写。
"""

from __future__ import annotations

import argparse
import json
import re
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
from goalx_backend.data.ingest import srct, srct_shift

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
    nargs: str | None = None  # "*" 等（understat 回填参数形态）
    transform: Callable[[object], object] | None = None  # 解析值 → task 形参值

    def __post_init__(self) -> None:
        """守互斥：nargs 分支不传 type（组合会让 type 被静默丢弃）。"""
        if self.nargs is not None and self.type is not None:
            msg = f"CliArg {self.flag}: nargs 与 type 互斥（transform 做值归一）"
            raise ValueError(msg)

    @property
    def dest(self) -> str:
        """目标属性名（--request-cap → request_cap，同 argparse 官方规则）。"""
        return self.flag.lstrip("-").replace("-", "_")

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """按声明挂到子 parser（分支直传避开 add_argument 的联合重载）。"""
        if self.action is not None:
            parser.add_argument(self.flag, help=self.help, action=self.action)
        elif self.nargs is not None:
            parser.add_argument(
                self.flag, help=self.help, nargs=self.nargs, default=self.default
            )
        elif self.type is not None:
            parser.add_argument(
                self.flag, help=self.help, type=self.type, default=self.default
            )
        else:
            parser.add_argument(self.flag, help=self.help, default=self.default)


def _tuple_or_none(value: object) -> tuple[str, ...] | None:
    """Nargs 列表 → tuple；空/None 归一 None（understat seasons/leagues 形态）。"""
    if not value:
        return None
    return tuple(str(item) for item in value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class DatasetSpec:
    """一条定拍数据集声明：名字、cron、任务函数、cli 参数、checkpoint 表。"""

    name: str  # deployment 键 = flow 名（kebab-case）
    cron: str  # Asia/Shanghai
    task: Callable[..., dict[str, object]]  # 住所是 tasks；此处持引用不持实现
    cli_help: str
    flow_help: str
    cli_command: str | None = None  # 缺省 = name（pool/closing 命令名历史上与键不同）
    cli_args: tuple[CliArg, ...] = ()
    cli_renames: tuple[tuple[str, str], ...] = ()  # cli dest → task 形参名（缺省同名）
    checkpoint_tables: tuple[str, ...] = ()  # 语料 checkpoint 表名（规格核对面）
    cli: bool = True  # False = 不派生 cli（纯 schedule 面/手写 handler 保留）
    resume: bool = True  # False = 判死面（不进恢复清单，serve 仍注册可显式 --only）

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

_OFFICIAL_RECONCILE = DatasetSpec(
    name="official-reconcile",
    cron="30 8 * * *",
    task=tasks.official_results_reconcile,
    cli_help="赛果日终审计+清单报告(源D+openfootball,票 44)",
    flow_help="赛果日终审计（票 44）：源D 页面 + openfootball 双参照源，不落事实。",
    cli=False,  # 手写 _cmd_official_reconcile 带 pending_manual 告警走查，保留
)

_UNDERSTAT = DatasetSpec(
    name="understat-sync",
    cron="20 9 * * *",
    task=tasks.understat_sync,
    cli_help="Understat xG 特征同步(票 45;默认当前季)",
    flow_help="Understat xG 特征同步（票 45）：五大当前季，幂等。",
    cli_args=(
        CliArg(
            "--seasons",
            help="回填赛季起始年(如 2021 2022 …,默认当前季)",
            nargs="*",
            transform=_tuple_or_none,
        ),
        CliArg(
            "--leagues",
            help="understat slug(默认五大;俄超按需传 rfpl)",
            nargs="*",
            default=[],
            transform=_tuple_or_none,
        ),
    ),
)

_CLUBELO = DatasetSpec(
    name="clubelo-sync",
    cron="10 9 * * *",
    task=tasks.clubelo_sync,
    cli_help="clubelo Elo 评级日拍(票 74;--backfill 全历史回填)",
    flow_help="Clubelo Elo 评级日拍（票 74）：当日全量快照单请求，幂等。",
    cli_args=(
        CliArg(
            "--backfill",
            help="当日快照后逐队拉全历史区间(一次性,约 600 请求)",
            action="store_true",
        ),
    ),
)

_ODDS_ANCHOR = DatasetSpec(
    name="odds-anchor-dense",
    cron="*/5 * * * *",
    task=tasks.odds_anchor_dense,
    cli_help="双锚临场采样(票 47)",
    flow_help="双锚临场采样（票 47）：*/5 拍，零候选零请求；30 分钟 closing 循环兜底。",
    cli=False,  # 纯 schedule 面，无 cli 命令
)

_SRCB = DatasetSpec(
    name="srcb-collect",
    cron="40 10,22 * * *",
    task=tasks.srcb_collect,
    cli_help="源B变化时序采集(票 49)",
    flow_help="源B变化时序采集（票 49 采集先行）：低频回溯式攒语料。",
    cli=False,
    resume=False,  # 判死面：三源停采留备用（2026-09-26 终局）
)

_EU_CLOSING = DatasetSpec(
    name="eu-odds-closing",
    cron="*/30 * * * *",
    task=tasks.eu_odds_closing,
    cli_command="closing-snapshot",
    cli_help="收盘窗口尽力快照(票 32)",
    flow_help="收盘窗口快照（票 32/35）：与常规采集共享月预算（同一记账路径）。",
    resume=False,  # 判死面：国际赔率全走源T（2026-09-25 裁决）
)

_INTEL = DatasetSpec(
    name="intel-collect",
    cron="40 10,22 * * *",
    task=tasks.intel_collection,
    cli_help="情报采集(票 09)",
    flow_help="情报采集（票 09）：当期彩池场次内部推导情报（幂等，零外部请求）。",
    cli=False,
)

_SCOUT = DatasetSpec(
    name="scout-line",
    cron="50 10,22 * * *",
    task=tasks.scout_line,
    cli_help="Scout 线(票 10)",
    flow_help="Scout 线（票 10）：读已存证情报出三项概率（跟 intel-collect 后）。",
    cli=False,
)

_SRCT_SHIFT = DatasetSpec(
    name="srct-shift",
    cron="*/30 8-23,0 * * *",
    task=tasks.srct_shift_run,
    cli_help="源T当期班四类拍(票65;在售清单发现+开售/每日/临场拍,拍键幂等)",
    flow_help=(
        "源T 当期班（票 65）：*/30 拍，竞彩在售场四类拍决策。\n\n"
        "夜窗让位零成本；拍键幂等（漏拍重试自愈）。"
    ),
    cli_args=(
        CliArg(
            "--request-cap",
            help=f"当次运行请求预算上限(默认 {srct_shift.SHIFT_REQUEST_CAP})",
            type=int,
            default=srct_shift.SHIFT_REQUEST_CAP,
        ),
        CliArg(
            "--no-window",
            help="跳过 01:00-08:00 夜窗让位判断(冒烟/手工回补用)",
            action="store_true",
        ),
    ),
    checkpoint_tables=("srct_shift_matches",),
)

#: 注册面（票 03 全量）：全部单 cron 定拍数据集；组合面/双 cron 面另见 flows/schedules
DATASETS: tuple[DatasetSpec, ...] = (
    _GUARDIAN,
    _POOL_SNAPSHOT,
    _SRCT_NIGHT,
    _OFFICIAL_RECONCILE,
    _UNDERSTAT,
    _CLUBELO,
    _ODDS_ANCHOR,
    _SRCB,
    _EU_CLOSING,
    _INTEL,
    _SCOUT,
    _SRCT_SHIFT,
)


def resume_names() -> tuple[str, ...]:
    """恢复清单推导（resume=True 记录）。"""
    return tuple(spec.name for spec in DATASETS if spec.resume)


def dead_names() -> tuple[str, ...]:
    """判死面例外表推导（serve 仍注册、恢复清单排除；显式 --only 可用）。"""
    return tuple(spec.name for spec in DATASETS if not spec.resume)


def _flow_attr(name: str) -> str:
    """数据集名 → 模块级 flow 属性名（非法字符转下划线）。"""
    return "_" + re.sub(r"[^0-9a-zA-Z_]", "_", name) + "_flow"


def build_flow(spec: DatasetSpec) -> Flow[[], dict[str, object]]:
    """
    同构 flow 壳工厂：调 tasks 函数、记日志、回 stats（非同构的留 flows.py）。

    __qualname__ 指向模块级物化属性（_REGISTRY_FLOWS）——Runner 按入口点
    字符串起子进程 import，闭包 qualname 不可导入=必炸（2026-10-01 实证：
    注册面 deployment 空转 ~24h，根因即此）。
    """

    def _recorded_flow() -> dict[str, object]:
        stats = spec.task()
        logger.info("{}: {}", spec.name, stats)
        return stats

    _recorded_flow.__doc__ = spec.flow_help  # @flow 取 docstring 作 description
    _recorded_flow.__qualname__ = _flow_attr(spec.name)
    return flow(name=spec.name, log_prints=True)(_recorded_flow)


# 模块级物化：子进程入口点 import 的就是这些属性（单一物化点）
_REGISTRY_FLOWS: dict[str, Flow[[], dict[str, object]]] = {
    spec.name: build_flow(spec) for spec in DATASETS
}
for _name, _built in _REGISTRY_FLOWS.items():
    globals()[_flow_attr(_name)] = _built


def build_deployments() -> dict[str, RunnerDeployment]:
    """注册表 → serve deployment（name=protocol-v1，与手写面同形）。"""
    return {
        spec.name: cast(
            RunnerDeployment,
            _REGISTRY_FLOWS[spec.name].to_deployment(
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


def cli_specs() -> tuple[DatasetSpec, ...]:
    """派生 cli 的记录（cli=False 的纯 schedule 面/手写 handler 面单列）。"""
    return tuple(spec for spec in DATASETS if spec.cli)


def add_cli_subparsers(sub: _SubParserAdder) -> None:
    """注册表 → cli 子命令（help 文案与参数默认值逐字保留手写时代）。"""
    for spec in cli_specs():
        parser = sub.add_parser(spec.command, help=spec.cli_help)
        for arg in spec.cli_args:
            arg.add_to(parser)


def run_cli(spec: DatasetSpec, args: argparse.Namespace) -> None:
    """注册表 → cli handler：本子命令参数映射为 task 形参，载荷打印 JSON。"""
    renames = dict(spec.cli_renames)
    kwargs: dict[str, object] = {}
    for arg in spec.cli_args:
        value = getattr(args, arg.dest)
        kwargs[renames.get(arg.dest, arg.dest)] = (
            arg.transform(value) if arg.transform is not None else value
        )
    payload = spec.task(**kwargs)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
