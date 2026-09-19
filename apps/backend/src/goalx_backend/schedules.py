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
- daily-capture 10:00/19:00：竞彩→预测→范围内欧赔（2 credits/次）
- eu-odds-closing 每 30 分钟：无窗口场次时自动零成本跳过
- draw-results-sync 18:00-05:59 每 30 分钟 + 08:00 补扫（双 deployment）：源D 结果页
  （票 42；无待出赛果时零成本跳过；频率待用户追认后如有调整只改 cron）
- daily-wrap 23:30：结算批跑 + CLV 对账 + 只读账务核查
- pool-snapshot 10:20/16:20/22:20（票 43）：彩池期次/对阵/人气分布三拍

ponytail: 本机进程随睡眠暂停，睡过的窗口如实记漏跑（协议允许，
有分母）；要无人值守升级为 launchd/远端时换 deployment 即可，flow 不动。
"""

from __future__ import annotations

from typing import cast

from prefect import serve
from prefect.deployments.runner import RunnerDeployment
from prefect.schedules import Schedule

from goalx_backend.flows import (
    daily_capture_flow,
    daily_wrap_flow,
    draw_results_sync_flow,
    eu_odds_closing_flow,
    intel_collect_flow,
    pool_snapshot_flow,
)


def main() -> None:
    """单进程服务协议 v1 的七个定时 deployment。"""
    # to_deployment 经 async_dispatch 在同步路径返回 RunnerDeployment（stub 联合类型）
    daily = cast(
        RunnerDeployment,
        daily_capture_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="0 10,19 * * *", timezone="Asia/Shanghai"),
        ),
    )
    closing = cast(
        RunnerDeployment,
        eu_odds_closing_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="*/30 * * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 票 42：赛程集中在 18:00-次日 06:00，半小时一拍；08:00 补扫收尾晚场。
    # Prefect Schedule 单 deployment 只收一个 cron → 同 flow 双 deployment
    # （建议频率写入票 Answer 待追认）
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
    # 彩池（票 43）：期次跨 2-3 天、分布随销售累积，每日三拍足够；
    # 官方销量无自动源（AI 代采层按需人工触发，不进调度）
    pool = cast(
        RunnerDeployment,
        pool_snapshot_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="20 10,16,22 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 情报（票 09）：跟在 10:20/22:20 彩池同步与 10:00 采集之后（幂等增量）
    intel = cast(
        RunnerDeployment,
        intel_collect_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="40 10,22 * * *", timezone="Asia/Shanghai"),
        ),
    )
    serve(daily, closing, draw_sync, draw_sync_sweep, wrap, pool, intel)


if __name__ == "__main__":
    main()
