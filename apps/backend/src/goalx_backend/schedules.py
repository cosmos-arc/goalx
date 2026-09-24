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
- understat-sync 09:20：xG 特征同步（票 45）——1 首页 + 5 联赛 = 6 请求/日
  （票面上限 10；robots Disallow，个人研究低频使用），赶在 daily-capture 前
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
    odds_anchor_dense_flow,
    official_reconcile_flow,
    pool_snapshot_flow,
    scout_line_flow,
    srcb_collect_flow,
    srct_night_flow,
    understat_sync_flow,
    weekly_refresh_flow,
)


def main() -> None:
    """单进程服务协议 v1 的全部定时 deployment（随票累加）。"""
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
    # 赛果日终审计（票 44）：跟在 08:00 官方补扫之后半小时——事实先落、
    # 参照源再比对；uniform 无待出赛果零请求，openfootball 对窗口内场次照跑
    official_reconcile = cast(
        RunnerDeployment,
        official_reconcile_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="30 8 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 双锚临场采样（票 47 修正设计）：*/5 拍，两锚零候选零请求；停售探测
    # 候选 = 开球 ≤3h 或北京 ≥19 点的次日内场次（凌晨场前夜墙钟停售形态）
    anchor_dense = cast(
        RunnerDeployment,
        odds_anchor_dense_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="*/5 * * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 源B变化时序（票 49 采集先行）：每日两拍回溯式（赶在 daily-capture 后，
    # 彩池期次已同步出新场次 mid）；36h 未开赛窗 × 17 pid，0.5s 限速
    srcb_collect_deploy = cast(
        RunnerDeployment,
        srcb_collect_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="40 10,22 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # 源T夜班（票 55 切片 13）：家宽低峰窗口开工，~8K 请求/日预算推进
    # Phase1 回填；窗口 01:00-08:00 = 7h ≈ 8.4K 请求（3s 均值抖动），预算
    # 与窗口天然咬合；跑完 Phase1 后每夜零请求心跳（断点续传零重抓）
    srct_night_deploy = cast(
        RunnerDeployment,
        srct_night_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="0 1 * * *", timezone="Asia/Shanghai"),
        ),
    )
    # xG 特征（票 45）：每日一拍足够（赛中 5-10 分钟级更新，我们只吃赛后
    # 累计）；09:20 赶在 daily-capture 10:00 前，当日预测决策时点最新鲜
    understat = cast(
        RunnerDeployment,
        understat_sync_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="20 9 * * *", timezone="Asia/Shanghai"),
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
    # scout（票 10）：读已存证情报出三项概率，跟情报采集后 10 分钟
    scout = cast(
        RunnerDeployment,
        scout_line_flow.to_deployment(
            name="protocol-v1",
            schedule=Schedule(cron="50 10,22 * * *", timezone="Asia/Shanghai"),
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
    serve(
        daily,
        closing,
        draw_sync,
        draw_sync_sweep,
        official_reconcile,
        understat,
        anchor_dense,
        srcb_collect_deploy,
        srct_night_deploy,
        weekly,
        wrap,
        pool,
        intel,
        scout,
    )


if __name__ == "__main__":
    main()
