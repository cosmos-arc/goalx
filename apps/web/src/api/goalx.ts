import { client } from "./client";
import type { components } from "./generated/schema";

type Schemas = components["schemas"];
export type TodayFixture = Schemas["TodayFixtureView"];
export type OddsSnapshot = Schemas["OddsSnapshotView"];
export type Bet = Schemas["BetView"];
export type BetLeg = Schemas["BetLegView"];
export type Slip = Schemas["SlipView"];
export type DrawResultView = Schemas["DrawResultView"];
export type SettlementRun = Schemas["SettlementRunResponse"];
export type Bankroll = Schemas["BankrollResponse"];
export type BankrollEvent = Schemas["BankrollEventView"];
export type BetCreateInput = Schemas["BetCreate"];
export type SlipCreateInput = Schemas["SlipCreate"];
export type DrawResultImportInput = Schemas["DrawResultImport"];

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

export function fetchTodayFixtures(date?: string): Promise<TodayFixture[]> {
	return unwrap(
		client.GET("/api/v1/fixtures/today", {
			params: { query: { date: date ?? null } },
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

export function fetchSlips(): Promise<Slip[]> {
	return unwrap(client.GET("/api/v1/bet-slips"));
}

export function recordPurchase(payload: SlipCreateInput): Promise<Slip> {
	return unwrap(client.POST("/api/v1/bet-slips", { body: payload }));
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
