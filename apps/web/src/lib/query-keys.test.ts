import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import {
	backtestRunsKey,
	bankrollKey,
	betsKey,
	costsKey,
	drawResultsKey,
	drawSyncKey,
	fixtureEvidenceKey,
	fixtureResearchKey,
	goalsMarketKey,
	invalidateBets,
	invalidatePoolViews,
	invalidateSettlementViews,
	poolEvidenceSummaryKey,
	poolPeriodDetailKey,
	poolPeriodsKey,
	poolSyncStatusKey,
	reviewQueueKey,
	slipsKey,
	stakeAdviceKey,
	todayFixturesKey,
	validationProgressKey,
} from "./query-keys";

// 失效辅助断言用：捕获 invalidateQueries 收到的键，不真跑缓存
function spyClient() {
	const client = new QueryClient();
	const spy = vi.spyOn(client, "invalidateQueries").mockReturnValue(Promise.resolve(true) as never);
	return { client, spy };
}

describe("query-keys 注册表（票 07）", () => {
	it("带参工厂：同参同键、异参异键", () => {
		expect(todayFixturesKey(1)).toEqual(["today-fixtures", 1]);
		expect(todayFixturesKey(3)).toEqual(["today-fixtures", 3]);
		expect(todayFixturesKey(1)).not.toEqual(todayFixturesKey(3));
		expect(goalsMarketKey(3)).toEqual(["goals-market", 3]);
		expect(fixtureEvidenceKey(7)).toEqual(["fixture-evidence", 7]);
		expect(fixtureEvidenceKey(null)).toEqual(["fixture-evidence", null]); // 禁用态休眠键与数值键不撞
		expect(fixtureResearchKey(7)).toEqual(["fixture-research", 7]);
		expect(poolEvidenceSummaryKey("2026-095")).toEqual(["pool-evidence-summary", "2026-095"]);
	});

	it("无参键常量钉字面量（散键复活时此测试红）", () => {
		expect(betsKey).toEqual(["bets"]);
		expect(backtestRunsKey).toEqual(["backtest-runs"]);
		expect(costsKey).toEqual(["costs"]);
		expect(reviewQueueKey).toEqual(["review-queue"]);
	});

	it("stakeAdviceKey：输入对象整进键——null 与数值不共享缓存（禁用态独立成键）", () => {
		expect(stakeAdviceKey({ mode: "paper", bankroll: 1000, ev: 0.02, odds: 1.85, cap: 0.05 })).toEqual([
			"stake-advice",
			{ mode: "paper", bankroll: 1000, ev: 0.02, odds: 1.85, cap: 0.05 },
		]);
		expect(stakeAdviceKey({ mode: "paper", bankroll: null, ev: null, odds: null, cap: 0.05 })).not.toEqual(
			stakeAdviceKey({ mode: "paper", bankroll: 0, ev: 0, odds: 0, cap: 0.05 }),
		);
	});

	it("poolPeriodDetailKey：无参 = 全期前缀，前缀是任意单期键的前缀（invalidate 派发所有期）", () => {
		const prefix = poolPeriodDetailKey();
		expect(prefix).toEqual(["pool-period-detail"]);
		expect(poolPeriodDetailKey("2026-095")).toEqual(["pool-period-detail", "2026-095"]);
		expect(poolPeriodDetailKey("2026-095").slice(0, prefix.length)).toEqual(prefix);
	});

	it("todayFixturesKey 双键合一：1 日与 3 日是两把键、与 goals 窗不撞", () => {
		expect(todayFixturesKey(3)).not.toEqual(goalsMarketKey(3));
	});

	it("invalidateBets：只失效 bets 单键", () => {
		const { client, spy } = spyClient();
		invalidateBets(client);
		expect(spy).toHaveBeenCalledTimes(1);
		expect(spy).toHaveBeenCalledWith({ queryKey: betsKey });
	});

	it("invalidateSettlementViews：bets 页 refresh() 既有六键清单原样迁移", () => {
		const { client, spy } = spyClient();
		invalidateSettlementViews(client);
		const keys = spy.mock.calls.map((call) => call[0]?.queryKey);
		expect(keys).toEqual([betsKey, drawResultsKey, drawSyncKey, bankrollKey, validationProgressKey, slipsKey]);
	});

	it("invalidatePoolViews：池同步三键（含全期前缀）原样迁移", () => {
		const { client, spy } = spyClient();
		invalidatePoolViews(client);
		const keys = spy.mock.calls.map((call) => call[0]?.queryKey);
		expect(keys).toEqual([poolPeriodsKey, poolPeriodDetailKey(), poolSyncStatusKey]);
	});
});
