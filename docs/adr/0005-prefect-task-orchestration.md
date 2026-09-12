# Prefect 作为统一任务编排

赔率快照、收盘抓取、scout 批量、模型周训练、复盘统计等定时/重试任务统一用 Prefect flows + deployments 管理（用户 2026-09-13 指定，与其其他项目选型一致）。FastAPI 不内嵌调度器（弃 APScheduler 方案）：进程职责分离——Web/API 与任务编排解耦，任务定义在代码里、由 Prefect 调度执行，错过窗口由 Prefect 重试/补跑策略覆盖。本地跑 Prefect server（或其云免费档）。
