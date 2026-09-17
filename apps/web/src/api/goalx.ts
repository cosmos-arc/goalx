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
export type Slip = Schemas["SlipView"];
export type DrawResultView = Schemas["DrawResultView"];
export type DrawResultPreview = Schemas["DrawResultPreviewResponse"];
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
