"""
定拍数据集注册表（票 02 建，票 03 全量）：一份声明推导 cli/flow/deployment。

新增定拍数据集只摊两处：tasks.py 落任务实现 + 本模块加一条 DatasetSpec——
cli 子命令、Prefect flow、serve deployment、恢复/判死清单（resume_names/
dead_names）、（语料数据集的）checkpoint 表核对全部由声明推导。任务实现
唯一住所仍是 tasks（ADR-0005）：本模块持引用不持实现。

vision-build 票 03 后注册面 = 全部单 cron 定拍数据集（19 条，含两个
判死面 resume=False）；
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

_TM_COACH = DatasetSpec(
    name="tm-coach-sync",
    cron="20 3 * * 1",
    task=tasks.tm_coach_sync,
    cli_help="TM教练任职周拍(autoresearch票18;--seasons回填2016起十年窗)",
    flow_help=(
        "TM 教练任职周拍（autoresearch 票 18）：当前季 11 联赛保鲜 +\n"
        "换帅事件/样本派生面重建。十年回填 = --seasons 2016..2026（一次性\n"
        "运维步，约 11×N 联赛页 + 逐俱乐部 2 页，2s 礼貌间距）。"
    ),
    cli_args=(
        CliArg(
            "--seasons",
            help="回填赛季起始年(如 2016 2017 …,默认当前季)",
            nargs="*",
            transform=_tuple_or_none,
        ),
        CliArg(
            "--leagues",
            help="fd 联赛码(默认 CorpusScope 11 联赛)",
            nargs="*",
            default=[],
            transform=_tuple_or_none,
        ),
    ),
)

_ODDS_ANCHOR = DatasetSpec(
    name="odds-anchor-dense",
    cron="*/5 * * * *",
    task=tasks.odds_anchor_dense,
    cli_help="停售加密探测+开售轮询(票 47/04)",
    flow_help=(
        "停售加密探测+开售锚轮询（票 47/04；feed-cutover 票 01 拔"
        " odds_api 锚拉取腿、候选面切广义已链）。"
    ),
    cli=False,  # 纯 schedule 面，无 cli 命令
)

_TRACK2 = DatasetSpec(
    name="track2-snapshots",
    cron="*/10 * * * *",
    task=tasks.track2_snapshots,
    cli_help="轨二拍点采样(票05)",
    flow_help="轨二拍点采样（票 05）：开球前 24h/12h 两源快照，零候选零请求。",
    cli=False,
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
    cli_help=(
        "源T当期班四类拍+赛前拍相位(票65/14;在售清单发现+开售/每日/临场拍"
        "+JC赛前detail快照,拍键幂等)"
    ),
    flow_help=(
        "源T 当期班（票 65）：*/30 拍，竞彩在售场四类拍决策。\n\n"
        "夜窗让位零成本；拍键幂等（漏拍重试自愈）。赛前拍相位（票 14）："
        "JC 板上场 −150/−90/−60/−30min detail 快照挂主班循环后（E-15 前向"
        "真值；主库候选解析降级不炸拍班）。"
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

_SRCT_REMAT = DatasetSpec(
    name="srct-remat",
    cron="10 8 * * *",
    task=tasks.srct_remat_run,
    cli_help="源T silver增量物化(票11;水位+触及分区重写,零新工件秒级返回)",
    flow_help=(
        "源T silver 增量物化（票 11）：夜班收口后 08:10 定拍，A 线参考链"
        "（fixture_universe + odds_change_event）保鲜。\n\n"
        "odds 水位增量（触及分区重写，字节级=全量切片）；fixture/xg 门控"
        "全量；护栏命中（首跑/计数不平/版本漂移/夜班回补）自动全量兜底。"
        "降级不打断，水位仅整轮成功推进。"
    ),
    cli_args=(
        CliArg(
            "--full",
            help="强制全量(维护口径:bookmaker+odds 全量+fixture+xg)",
            action="store_true",
        ),
    ),
    checkpoint_tables=("srct_remat_state",),
)

_GOLD_REBUILD = DatasetSpec(
    name="gold-rebuild",
    cron="40 8 * * *",
    task=tasks.gold_rebuild,
    cli_help="gold日频重算(misprice票03;market_quote保鲜+gold全量,晨检面兜底)",
    flow_help=(
        "gold 日频重算（misprice 票 03①）：market_quote 保鲜 + gold 全量。\n\n"
        "remat 08:10 只物化 odds/fixture/xg——market_quote 曾停摆月余无红面"
        "（ou_line_drift 对 10 月全缺，票 03④）。银段先重物化 market_quote"
        "再重建 gold，08:40 定拍赶在 09:05 晨检 gold 新鲜度面前。"
    ),
    cli=False,  # 手动路径走既有 gold-build/srct-market 命令（分步可控）
)

_ANCHOR_SENTINEL_CAPTURE = DatasetSpec(
    name="anchor-sentinel-capture",
    cron="*/30 * * * *",
    task=tasks.anchor_sentinel_capture,
    cli_help="断点哨兵采集(propline:pinnacle窄化,票34;零候选零请求空转)",
    flow_help=(
        "断点哨兵采集面（票 34，设计稿 13-sentinel-design）：仅 pinnacle\n"
        "（服务端 bookmakers 过滤）仅活跃 sport（已链 36h ∪ JC FD 待链\n"
        "7d，零请求选择）——~50-200 请求/日，护栏 900 原样复用。滚中行\n"
        "只入观察不入对账；本机睡眠漏拍按 ponytail 纪律如实记（覆盖轴\n"
        "显形，不静默）。"
    ),
    cli=False,  # 纯 schedule 面；手动冒烟走 propline-capture
)

_ANCHOR_SENTINEL_WEEKLY = DatasetSpec(
    name="anchor-sentinel-weekly",
    cron="50 8 * * 1",
    task=tasks.anchor_sentinel_weekly,
    cli_command="anchor-sentinel",
    cli_help="断点哨兵周对账重算(票34;缺省=最近完整ISO周,幂等追加留痕)",
    flow_help=(
        "断点哨兵对账面（票 34）：周一 08:50（gold-rebuild 08:40 后、\n"
        "晨检 09:05 前）srct（cid177 第一锚，只读）× propline:pinnacle\n"
        "（第二源）三轴周级对账+留痕+周报 md。alert=留痕+晨检红面，\n"
        "不自动处置（不换轨/不补采/不触发付费兜底）。"
    ),
    cli_args=(
        CliArg("--iso-year", help="目标 ISO 年(缺省=最近完整周)", type=int),
        CliArg("--iso-week", help="目标 ISO 周(缺省=最近完整周)", type=int),
    ),
)

_WEEKLY_AGENT = DatasetSpec(
    name="weekly-agent",
    cron="0 6 * * 1",
    task=tasks.weekly_agent_batch,
    cli_help="周批agent循环(vision票03;读台账/注册表/教训库→LLM提案≤5条+对抗质疑;空转守卫)",
    flow_help=(
        "周批 agent 循环（vision-build 票 03）：周一 06:00（TM 周拍后/\n"
        "晨检前），agent=心跳内函数非长驻。收缩 ≥L2 只留心跳；台账/教训库\n"
        "无新料只报心跳不烧 LLM；真跑=读面（edge_findings 全量含阴性+注册表\n"
        "+教训注入+周报 digest）→ LLM 提案（≤5 条，引用 id 必带，管线层\n"
        "拒收缺引用/伪新颖）→ 每条 fresh-context 对抗质疑附反驳 → 提案落\n"
        "语料树 reports/proposals/（append-only）→ 花费落 llm_proposal_costs。\n"
        "人审注册：goalx proposal-register <file> --proposal-id … --h-id …"
    ),
)

#: 注册面（vision-build 票 03 全量）：单 cron 定拍数据集；
#: 组合面/双 cron 另见 flows/schedules
DATASETS: tuple[DatasetSpec, ...] = (
    _GUARDIAN,
    _POOL_SNAPSHOT,
    _SRCT_NIGHT,
    _OFFICIAL_RECONCILE,
    _UNDERSTAT,
    _CLUBELO,
    _TM_COACH,
    _ODDS_ANCHOR,
    _TRACK2,
    _SRCB,
    _EU_CLOSING,
    _INTEL,
    _SCOUT,
    _SRCT_SHIFT,
    _SRCT_REMAT,
    _GOLD_REBUILD,
    _ANCHOR_SENTINEL_CAPTURE,
    _ANCHOR_SENTINEL_WEEKLY,
    _WEEKLY_AGENT,
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
