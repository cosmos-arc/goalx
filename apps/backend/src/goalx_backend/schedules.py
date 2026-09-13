"""
Prefect 连续运行调度入口（票 37 协议 v1，用户已授权调度）。

单进程 ``serve``，无需 Prefect server/worker：

    uv run --no-sync python -m goalx_backend.schedules   # 或 task serve-schedules

节奏（Asia/Shanghai，协议 run-protocol-v1.md §3，启动后不改口径）：
- daily-capture 10:00/19:00：竞彩→预测→范围内欧赔（2 credits/次）
- eu-odds-closing 每 30 分钟：无窗口场次时自动零成本跳过
- daily-wrap 23:30：结算批跑 + CLV 对账 + 只读账务核查

ponytail: 本机 serve 进程随睡眠暂停，睡过的窗口如实记漏跑（协议允许，
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
    eu_odds_closing_flow,
)


def main() -> None:
    """单进程服务协议 v1 的三个定时 deployment。"""
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
    wrap = cast(
        RunnerDeployment,
        daily_wrap_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="30 23 * * *", timezone="Asia/Shanghai"),
        ),
    )
    serve(daily, closing, wrap)


if __name__ == "__main__":
    main()
