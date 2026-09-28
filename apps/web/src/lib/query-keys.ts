import type { QueryClient } from "@tanstack/react-query";

/**
 * server-state 缓存注册表（票 07）：全站 queryKey + 失效清单的唯一住所。
 *
 * 工厂 = 纯函数 + 常量（不建类不建 context，YAGNI）；失效辅助只包住既有
 * 清单，不发明新失效语义。新增查询必须先在此登记键再消费——散键会让跨页
 * 共享缓存与失效清单悄悄漂移（/fixtures/today 曾被 ["today"] 与
 * ["fixtures-window", 3] 两把键各缓存同一端点，就是这个漂移的实证）。
 * staleTime 不在此设值：全站默认 0（每次挂载重拍）是用户 2026-09-28 裁定，
 * 单用户工作台重拍代价可忽略、赔率新鲜优先。
 */

// ---- 无参键（单例读模型）----

export const betsKey = ["bets"] as const;
export const drawResultsKey = ["draw-results"] as const;
export const drawSyncKey = ["draw-sync"] as const;
export const bankrollKey = ["bankroll"] as const;
export const validationProgressKey = ["validation-progress"] as const;
export const backtestRunsKey = ["backtest-runs"] as const;
export const slipsKey = ["slips"] as const;
export const costsKey = ["costs"] as const;
export const reviewQueueKey = ["review-queue"] as const;
export const poolPeriodsKey = ["pool-periods"] as const;
export const poolSyncStatusKey = ["pool-sync-status"] as const;

// ---- 带参键工厂 ----

/**
 * 场次对照表（GET /fixtures/today）按窗参统一键（票 07 双键合一）：
 * 单日页传 1（后端 days 缺省即 1，请求等价），3 日页传各自窗口常数。
 */
export const todayFixturesKey = (days: number) => ["today-fixtures", days] as const;

/** 进球玩法读模型（GET /markets/goals）按窗口缓存。 */
export const goalsMarketKey = (days: number) => ["goals-market", days] as const;

/** 单场证据链：null = 无活跃场次（禁用态休眠观察者，与任何数值键不撞）。 */
export const fixtureEvidenceKey = (fixtureId: number | null) => ["fixture-evidence", fixtureId] as const;

/** 单场研究视图。 */
export const fixtureResearchKey = (fixtureId: number) => ["fixture-research", fixtureId] as const;

/** 池期证据卡。 */
export const poolEvidenceSummaryKey = (periodNo: string) => ["pool-evidence-summary", periodNo] as const;

/** 单期池面详情；无参 = 全期前缀（invalidate 派发所有期）。 */
export const poolPeriodDetailKey = (periodNo?: string) =>
	periodNo === undefined ? (["pool-period-detail"] as const) : (["pool-period-detail", periodNo] as const);

/**
 * 仓位建议：输入对象整进键（react-query 对对象做确定性结构哈希，加字段
 * 自动进键——不手抄字段清单，避免请求体加字段时两种建议静默共享缓存）。
 */
export const stakeAdviceKey = (input: {
	mode: "paper" | "live";
	bankroll: number | null;
	ev: number | null;
	odds: number | null;
	cap: number;
}) => ["stake-advice", input] as const;

// ---- 失效辅助（只包住既有清单）----

/** 建议/投注一变即失效（had-basket 建票与 goals 建单关建议两处同清单）。 */
export function invalidateBets(queryClient: QueryClient): void {
	void queryClient.invalidateQueries({ queryKey: betsKey });
}

/** 开奖侧一动全刷：bets 页 refresh() 既有六键清单（票 07 原样迁移）。 */
export function invalidateSettlementViews(queryClient: QueryClient): void {
	void queryClient.invalidateQueries({ queryKey: betsKey });
	void queryClient.invalidateQueries({ queryKey: drawResultsKey });
	void queryClient.invalidateQueries({ queryKey: drawSyncKey });
	void queryClient.invalidateQueries({ queryKey: bankrollKey });
	void queryClient.invalidateQueries({ queryKey: validationProgressKey });
	void queryClient.invalidateQueries({ queryKey: slipsKey });
}

/** 池同步成功后三键全刷（market-pool syncMutation 既有清单）。 */
export function invalidatePoolViews(queryClient: QueryClient): void {
	void queryClient.invalidateQueries({ queryKey: poolPeriodsKey });
	void queryClient.invalidateQueries({ queryKey: poolPeriodDetailKey() });
	void queryClient.invalidateQueries({ queryKey: poolSyncStatusKey });
}
