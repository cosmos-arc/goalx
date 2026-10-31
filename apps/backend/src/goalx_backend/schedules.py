"""
Prefect 连续运行调度入口（票 37 协议 v1，用户已授权调度）。

专用 server + serve 两进程（``task serve-schedules`` 一条命令拉起）：

    nohup task serve-schedules >> .scratch/prefect-serve.log 2>&1 &

Prefect ≥3.7 的临时 server 不运行 scheduler（上游设计，``flow.serve`` 会警告
"Cannot schedule flows on an ephemeral server"），单进程 serve 的 schedule 永不
触发——票 37 day0 起连续 2 天 0 触发的根因。serve-schedules 会自动启动专用
server（PREFECT_API_URL=http://127.0.0.1:4200/api，scheduler 在其中运行），
server 存活独立于 serve 进程，serve 重启不影响已排程的 run。

节奏（Asia/Shanghai，协议 run-protocol-v1.md §3，启动后不改口径）：
- daily-capture 10:00/19:00（竞彩→预测）+ 12:30/16:30 可购窗拍
  （票 09）：开售后到停售前的 on_sale 观测，此前 L2 证据恒空
- eu-odds-closing 每 30 分钟：无窗口场次时自动零成本跳过（票 47 起兼作
  双锚采样的兜底拍）
- odds-anchor-dense 每 5 分钟（票 47）：停售决策锚 + 开球评估锚，零候选
  零请求
- draw-results-sync 18:00-05:59 每 30 分钟 + 08:00 补扫（双 deployment）：
  官方 uniform 赛果同步（票 44 切换，票 42 时代为源D；无待出赛果时零成本
  跳过；频率如有调整只改 cron）
- official-reconcile 08:30：赛果日终审计（票 44）——源D 页面 + openfootball
  双参照源对账，跟在官方同步 08:00 补扫落事实之后
- srcb-collect 10:40/22:40（票 49 采集先行）：源B变化时序低频回溯式攒语料
- srct-night 01:00（票 55 切片 13）：源T夜班 Phase1 回填——01:00-08:00 窗口
  内 ~8K 请求预算自动推进，连败 5 场熔断当晚收手，每夜摘要落语料树
  checkpoint 库（`goalx srct-night --list` 晨检）；Phase1 批次（三完整季+
  当季 ≈47K 请求）跑完后每夜零成本心跳
- srct-remat 08:10（票 11）：夜班收口后 silver 增量物化——odds 水位增量
  （触及分区重写，A 线参考链保鲜）、fixture/xg 门控全量；护栏命中自动
  全量兜底；零新工件秒级返回
- understat-sync 09:20：xG 特征同步（票 45）——1 首页 + 5 联赛 = 6 请求/日
  （票面上限 10；robots Disallow，个人研究低频使用），赶在 daily-capture 前
- clubelo-sync 09:10：Elo 评级日拍（票 74）——单请求全量快照，赶在
  daily-capture 前
- daily-wrap 23:30：结算批跑 + CLV 对账 + 只读账务核查
- pool-snapshot 10:20/16:20/22:20（票 43）：彩池期次/对阵/人气分布三拍

ponytail: 本机进程随睡眠暂停，睡过的窗口如实记漏跑（协议允许，
有分母）；要无人值守升级为 launchd/远端时换 deployment 即可，flow 不动。
"""

from __future__ import annotations

import argparse
import os
from typing import cast

from loguru import logger
from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Schedule

from goalx_backend import datasets
from goalx_backend.flows import (
    daily_capture_flow,
    daily_wrap_flow,
    draw_results_sync_flow,
    weekly_refresh_flow,
)


def build_deployments() -> dict[str, RunnerDeployment]:
    """
    全量 deployment 装配：组合面手写（票 09 后 6 面）+ 注册表推导面（14 面）=20 面。

    纯构造不副作用（serve 在 main）；规格断言测试打此处锁 serve 清单。
    """
    # to_deployment 经 async_dispatch 在同步路径返回 RunnerDeployment（stub 联合类型）
    daily = cast(
        RunnerDeployment,
        daily_capture_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="0 10,19 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 票 42：赛程集中在 18:00-次日 06:00，半小时一拍；08:00 补扫收尾晚场。
    # Prefect Schedule 单 deployment 只收一个 cron → 同 flow 双 deployment
    # （建议频率写入票 Answer 待追认）——双 cron 形状不进注册表，手写保留
    # 票 09 可购窗拍点（同 flow 第三/四拍）：开售 ~11:30 后到逐场停售之间
    # 12:30/16:30 两拍落 on_sale 观测——此前 10 点拍全未开售、19 点拍是
    # 唯一在售拍，L2 证据恒空（delay-audit 首跑实证）。每拍=计算器端点
    # 1 请求，预算可忽略。
    daily_window = cast(
        RunnerDeployment,
        daily_capture_flow.to_deployment(
            name="protocol-v1-window",
            schedule=Schedule(cron="30 12,16 * * *", timezone="Asia/Shanghai"),
        ),
    )
    draw_sync = cast(
        RunnerDeployment,
        draw_results_sync_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="*/30 0-5,18-23 * * *", timezone="Asia/Shanghai"),
        ),
    )
    draw_sync_sweep = cast(
        RunnerDeployment,
        draw_results_sync_flow.to_deployment(
            name="protocol-v1-sweep",
            schedule=Schedule(cron="0 8 * * *", timezone="Asia/Shanghai"),
        ),
    )
    wrap = cast(
        RunnerDeployment,
        daily_wrap_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="30 23 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 周刷新（票 54）：周一 06:10 fdhist 幂等重导（周末赛果）→ DC 周训练
    # （五大+N1）。此前训练无定拍——工件停在旧数据导致升班马 no_mapping
    # （2026-09-22 实证），定拍防再烂
    weekly = cast(
        RunnerDeployment,
        weekly_refresh_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="10 6 * * 1", timezone="Asia/Shanghai"),
        ),
    )
    deployments: dict[str, RunnerDeployment] = {
        "daily-capture": daily,
        "daily-capture-window": daily_window,
        "draw-results-sync": draw_sync,
        "draw-results-sweep": draw_sync_sweep,
        "weekly-refresh": weekly,
        "daily-wrap": wrap,
    }
    # 注册表面（票 03 全量 12 面：单 cron 定拍数据集，含两判死面）
    deployments.update(datasets.build_deployments())
    return deployments


def main(only: str | None = None) -> None:
    """
    单进程服务协议 v1 的定时 deployment（随票累加）。

    only=逗号分隔 deployment 键——停采期只服务指定面（票 63：源T 夜班
    先行恢复，竞彩/池侧复采另议后撤过滤恢复全量）；None=全量；空串/纯
    分隔符拒绝（fail-loud——静默全量会复活判死面，静默零面则空转常驻，
    2026-10-31 correctness F2/F3）。
    """
    deployments = build_deployments()
    # 未知键校验取 gate 摘除前的全量键集：缺省 --only 含 srct-night，先摘
    # 后校验会把缺省键判为未知——GOALX_SRCT_NIGHT_OFF 置位即崩、零面服务
    # （2026-10-31 correctness F1；gate 胜出=该面静默不选，摘除日志已响）。
    known = set(deployments)
    # 全程班模式闸门（2026-09-28）：手动连续窗期间摘除 srct-night 定拍触发，
    # 防双进程叠加；恢复=不带此环境变量重启栈。须在 selected 固化前摘除
    # （工作区原稿插在固化后属无效位，重建时修正）。
    if os.environ.get("GOALX_SRCT_NIGHT_OFF"):
        deployments.pop("srct-night", None)
        logger.info("GOALX_SRCT_NIGHT_OFF 置位：摘除 srct-night（手动连续窗防叠加）")
    selected = tuple(deployments.values())
    if only is not None:
        wanted = {name.strip() for name in only.split(",") if name.strip()}
        if not wanted:
            raise SystemExit("--only 为空串或纯分隔符：要全量请显式传 all")
        unknown = wanted - known
        if unknown:
            known_names = sorted(known)
            msg = f"--only 未知 deployment：{sorted(unknown)}（可用 {known_names}）"
            raise SystemExit(msg)
        selected = tuple(dep for key, dep in deployments.items() if key in wanted)
        logger.info("schedules --only：{}（其余停采面不注册）", sorted(wanted))
    serve(
        *selected,
    )


# 2026-09-25 恢复清单（用户裁决"相关采集任务一起调整"）：除两个判死面
# （eu-odds-closing 欧赔聚合 / srcb-collect 源B，国际赔率全走源T）外全量。
# 票 03 后全量推导：组合面手写 + 注册表 resume/dead 两推导，不再手抄行。
# 覆盖不变量在 test_datasets_registry.test_resume_derivation：装配面必须
# 恰归 RESUME ∪ DEAD（2026-10-31 daily-capture-window 漏进清单实证漏法——
# CLI 缺省即本清单，漏登记=面从未上线）。
RESUME_DEPLOYMENTS = (
    "daily-capture",
    "daily-capture-window",
    "draw-results-sync",
    "draw-results-sweep",
    "weekly-refresh",
    "daily-wrap",
    *datasets.resume_names(),
)
DEAD_DEPLOYMENTS = datasets.dead_names()


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI 解析器（缺陷现场抽出口——2026-10-31 票 09 漏登记正是缺省值无人钉）。"""
    parser = argparse.ArgumentParser(
        description="Prefect 调度入口（--only 停采期过滤）"
    )
    parser.add_argument(
        "--only",
        default=",".join(RESUME_DEPLOYMENTS),
        help=(
            "逗号分隔要服务的 deployment 键（缺省=2026-09-25 恢复清单"
            " RESUME_DEPLOYMENTS；全量传 all；判死面 eu-odds-closing/"
            "srcb-collect 不在恢复清单）"
        ),
    )
    return parser


if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    main(only=None if args.only == "all" else args.only)
