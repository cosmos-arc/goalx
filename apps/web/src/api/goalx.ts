import { client } from "./client";
import type { components } from "./generated/schema";

type Schemas = components["schemas"];
export type TodayFixture = Schemas["TodayFixtureView"];
export type HadQuoteStatus = Schemas["HadQuoteStatus"];
export type FixtureResearch = Schemas["FixtureResearchView"];
export type BookQuote = Schemas["BookQuoteView"];
export type OddsSnapshot = Schemas["OddsSnapshotView"];
export type GoalsFixture = Schemas["GoalsFixtureView"];
export type GoalsMarketBlock = Schemas["GoalsMarketBlock"];
export type GoalsSelection = Schemas["GoalsSelectionView"];
export type Bet = Schemas["BetView"];
export type BetLeg = Schemas["BetLegView"];
export type BetReview = Schemas["BetReviewView"];
export type BetEvSnapshot = Schemas["BetEvSnapshotView"];
export type Slip = Schemas["SlipView"];
export type DrawResultView = Schemas["DrawResultView"];
export type DrawResultPreview = Schemas["DrawResultPreviewResponse"];
export type DrawSyncStatus = Schemas["DrawSyncStatusView"];
export type DrawSyncRun = Schemas["DrawSyncRunView"];
export type DrawSyncPendingItem = Schemas["DrawSyncPendingItem"];
export type SettlementRun = Schemas["SettlementRunResponse"];
export type Bankroll = Schemas["BankrollResponse"];
export type BankrollEvent = Schemas["BankrollEventView"];
export type DepositInput = Schemas["DepositPayload"];
export type DepositCreated = Schemas["DepositCreatedView"];
export type CostSummary = Schemas["CostSummaryView"];
export type BetCreateInput = Schemas["BetCreate"];
export type SlipCreateInput = Schemas["SlipCreate"];
export type StakeAdviceInput = Schemas["StakeAdviceRequest"];
export type StakeAdvice = Schemas["StakeSuggestion"];
export type DrawResultImportInput = Schemas["DrawResultImport"];
export type BacktestRun = Schemas["BacktestRunView"];
export type BacktestRunDetail = Schemas["BacktestRunDetailView"];
export type ValidationProgress = Schemas["ValidationProgressView"];

type FetchResult<T> = { data: T; error?: never } | { data?: never; error: unknown };

async function unwrap<T>(call: Promise<FetchResult<T>>): Promise<T> {
	const { data, error } = await call;
	if (error !== undefined) {
		throw error;
	}
	if (data === undefined) {
		throw new Error("empty response");
	}
	return data;
}

/** 场次列表（票 wb-01）：days>1 时返回 [date, date+days-1] 业务日窗口，行内带 business_date。 */
export function fetchTodayFixtures(date?: string, days?: number): Promise<TodayFixture[]> {
	return unwrap(
		client.GET("/api/v1/fixtures/today", {
			params: { query: { date: date ?? null, ...(days === undefined ? {} : { days }) } },
		}),
	);
}

/**
 * 进球玩法页读模型（票 wb-04）：ttg/crs 报价 + 矩阵推导概率 + 模型 EV。
 * EV 口径 = 模型概率 × 竞彩价 − 1（进球类无欧共识，与 had 页共识 EV 不同源）。
 */
export function fetchGoalsMarket(date?: string, days?: number): Promise<GoalsFixture[]> {
	return unwrap(
		client.GET("/api/v1/markets/goals", {
			params: { query: { date: date ?? null, ...(days === undefined ? {} : { days }) } },
		}),
	);
}

export function fetchFixtureOdds(fixtureId: number, market = "had"): Promise<OddsSnapshot[]> {
	return unwrap(
		client.GET("/api/v1/fixtures/{fixture_id}/odds", {
			params: { path: { fixture_id: fixtureId }, query: { market } },
		}),
	);
}

/** 单场研究视图（票 wb-02）：逐书赔率 + 去水共识 + 模型概率/EV + 资格判定。 */
export async function fetchFixtureResearch(fixtureId: number): Promise<FixtureResearch> {
	const { data, error, response } = await client.GET("/api/v1/fixtures/{fixture_id}/research", {
		params: { path: { fixture_id: fixtureId } },
	});
	if (error !== undefined || data === undefined) {
		// 404（不存在/旧后端）与后端不可用在页面分开表述——错误携带 HTTP 状态
		const failure = new Error(`fixture research request failed (${response.status})`) as Error & {
			status: number;
			payload: unknown;
		};
		failure.status = response.status;
		failure.payload = error;
		throw failure;
	}
	return data;
}

export function fetchBets(params?: { mode?: "paper" | "live"; only_open?: boolean }): Promise<Bet[]> {
	return unwrap(
		client.GET("/api/v1/bets", {
			params: { query: { mode: params?.mode ?? null, only_open: params?.only_open ?? false } },
		}),
	);
}

export function createBet(payload: BetCreateInput): Promise<Bet> {
	return unwrap(client.POST("/api/v1/bets", { body: payload }));
}

/**
 * 建议仓位（票 wb-06，只读）：EV≤0 → ¥0；paper 一律 flat（红线）；
 * live = ¼ fractional Kelly 截断单注 1–5%。串关传联合赔率/联合 EV（整注口径）。
 */
export function fetchStakeAdvice(payload: StakeAdviceInput): Promise<StakeAdvice> {
	return unwrap(client.POST("/api/v1/stake-advice", { body: payload }));
}

export function fetchSlips(): Promise<Slip[]> {
	return unwrap(client.GET("/api/v1/bet-slips"));
}

export function recordPurchase(payload: SlipCreateInput): Promise<Slip> {
	return unwrap(client.POST("/api/v1/bet-slips", { body: payload }));
}

export function previewDrawResults(payload: DrawResultImportInput): Promise<DrawResultPreview> {
	return unwrap(client.POST("/api/v1/draw-results/preview", { body: payload }));
}

export function fetchCostSummary(since?: string): Promise<CostSummary> {
	return unwrap(
		client.GET("/api/v1/costs/summary", {
			params: { query: since === undefined ? {} : { since } },
		}),
	);
}

export function importDrawResults(payload: DrawResultImportInput): Promise<{ imported: number }> {
	return unwrap(client.POST("/api/v1/draw-results", { body: payload }));
}

export function fetchDrawResults(fixtureId?: number): Promise<DrawResultView[]> {
	return unwrap(
		client.GET("/api/v1/draw-results", {
			params: { query: fixtureId === undefined ? {} : { fixture_id: fixtureId } },
		}),
	);
}

/** 赛果同步状态（票 42）：上次同步元信息 + 当前待出赛果数。 */
export function fetchDrawSyncStatus(): Promise<DrawSyncStatus> {
	return unwrap(client.GET("/api/v1/draw-sync/status"));
}

/** 主动触发一次赛果自动同步（返回触发后的最新状态）。 */
export function runDrawSync(): Promise<DrawSyncStatus> {
	return unwrap(client.POST("/api/v1/draw-sync/run"));
}

export function runSettlement(): Promise<SettlementRun> {
	return unwrap(client.POST("/api/v1/settlements/run"));
}

export function fetchBankroll(): Promise<Bankroll> {
	return unwrap(client.GET("/api/v1/bankroll"));
}

export function createDeposit(payload: DepositInput): Promise<DepositCreated> {
	return unwrap(client.POST("/api/v1/bankroll/deposits", { body: payload }));
}

export function fetchBacktestRuns(): Promise<BacktestRun[]> {
	return unwrap(client.GET("/api/v1/backtest/runs"));
}

export function fetchBacktestRun(runId: number): Promise<BacktestRunDetail> {
	return unwrap(
		client.GET("/api/v1/backtest/runs/{run_id}", {
			params: { path: { run_id: runId } },
		}),
	);
}

export function fetchValidationProgress(): Promise<ValidationProgress> {
	return unwrap(client.GET("/api/v1/validation/progress"));
}

export type PoolPeriod = Schemas["PoolPeriodView"];
export type PoolPeriodDetail = Schemas["PoolPeriodDetailView"];
export type PoolMatch = Schemas["PoolMatchView"];
export type PoolSelection = Schemas["PoolSelectionView"];
export type PoolSyncStatus = Schemas["PoolSyncStatusView"];
export type PoolStateImportInput = Schemas["PoolStateImportPayload"];

/**
 * 彩池期次列表（票 43）：期次/对阵/分布来自源B 同步；销量列仅 AI 代采可得。
 */
export function fetchPoolPeriods(marketCode?: string): Promise<PoolPeriod[]> {
	return unwrap(
		client.GET("/api/v1/pool/periods", {
			params: { query: { ...(marketCode === undefined ? {} : { market_code: marketCode }) } },
		}),
	);
}

/**
 * 彩池期次详情（票 43）：一场一选三向（概率/份额/估计派彩赔率/EV）。
 */
export function fetchPoolPeriodDetail(periodNo: string, marketCode?: string): Promise<PoolPeriodDetail> {
	return unwrap(
		client.GET("/api/v1/pool/periods/{period_no}", {
			params: {
				path: { period_no: periodNo },
				query: { ...(marketCode === undefined ? {} : { market_code: marketCode }) },
			},
		}),
	);
}

/** 彩池同步状态（票 43）：上次同步元信息与已采集期次数。 */
export function fetchPoolSyncStatus(): Promise<PoolSyncStatus> {
	return unwrap(client.GET("/api/v1/pool-sync/status"));
}

export type ColdVariantsInput = Schemas["ColdVariantsPayload"];
export type ColdVariants = Schemas["ColdVariantsView"];

/**
 * 搏冷变体生成（票 pool-v2/02）：基础票（缺省=各场最高概率）按 EV 增益
 * 贪心替换 1..N 处冷门；估值口径与期次详情一致。
 */
export function generateColdVariants(payload: ColdVariantsInput): Promise<ColdVariants> {
	return unwrap(client.POST("/api/v1/pool/cold-variants", { body: payload }));
}

/** 触发一次彩池同步（票 43：源B 期次/对阵/人气；幂等）。 */
export function runPoolSync(): Promise<PoolSyncStatus> {
	return unwrap(client.POST("/api/v1/pool-sync/run"));
}

/**
 * AI 代采导入（票 43 兜底层）：官方销量/滚存经代理结构化提交（幂等，source=agent）。
 */
export function importPoolState(payload: PoolStateImportInput): Promise<{ period_no: string; imported: boolean }> {
	return unwrap(client.POST("/api/v1/pool-states", { body: payload }));
}

export type PoolSlipCreateInput = Schemas["PoolSlipCreate"];

/**
 * 创建纸面池票（票 43 接线）：picks 笛卡尔积 materialize 为组合；
 * 奖池型只纸面（调研红线——真金须用户单独裁决）。
 */
export function createPoolSlip(payload: PoolSlipCreateInput): Promise<Slip> {
	return unwrap(client.POST("/api/v1/pool-slips", { body: payload }));
}
