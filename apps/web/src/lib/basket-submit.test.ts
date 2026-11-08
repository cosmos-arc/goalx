import { QueryClient } from "@tanstack/react-query";
import { expect, test } from "vitest";
import type { Bet } from "../api/goalx";
import { type SubmitLeg, submitBasketUnits } from "./basket-submit";

/**
 * 票 08 续作：共享提交环的单元划分政策单测（had/goals 三路的政策单源）。
 * 页面级行为由 fixtures/market-had/market-goals 页测钉死；此处直接钉环的
 * 四分支：冲突恢复键策略（整注全换新键/单关只换失败腿）/价变清篮+价格源
 * 失效/普通失败留篮（同键重试）/空单元直退。
 */

type Leg = SubmitLeg & { market: string };

function leg(n: number): Leg {
	return { fixture_id: n, request_id: `k${n}`, market: "ttg" };
}

function betOf(id: number): Bet {
	return {
		id,
		slip_id: null,
		mode: "paper",
		market_kind: "fixed",
		purchased: false,
		stake: 100,
		actual_stake: null,
		strategy_version: null,
		placed_at: null,
		locked_at: null,
		created_at: "2026-11-08T10:00:00+00:00",
		status: "open",
		payout: null,
		profit: null,
		settled_at: null,
		legs: [],
		review: null,
	} as unknown as Bet;
}

function conflictError() {
	return {
		detail: { message: "client_request_id 已绑定另一提交意图", existing_bet: { id: 80, mode: "paper", stake: 2 } },
	};
}

function priceChangeError() {
	return {
		detail: {
			message: "报价已变",
			current_quotes: [{ fixture_id: 2, selection_code: "2", server_odds: 4.1, snapshot_id: 147, observed_at: null }],
		},
	};
}

function harness() {
	const replaced: Leg[][] = [];
	const messages: string[] = [];
	let closed = 0;
	const invalidatedPrices: number[][] = [];
	const client = new QueryClient();
	return {
		replaced,
		messages,
		get closed() {
			return closed;
		},
		invalidatedPrices,
		client,
		async run(units: Leg[][], post: (unit: Leg[]) => Promise<Bet>, extra: Record<string, unknown> = {}) {
			await submitBasketUnits<Leg>({
				client,
				units,
				post,
				successNote: (created) => `success:${created.map((bet) => bet.id).join(",")}`,
				failureNote: (error, created) => `fail#${created.length}:${String(error)}`,
				invalidatePriceSource: (_client, fixtureIds) => {
					invalidatedPrices.push(fixtureIds);
				},
				replaceLegs: (next) => {
					replaced.push(next);
				},
				report: (message) => {
					messages.push(message);
				},
				closeBasket: () => {
					closed += 1;
				},
				...extra,
			});
		},
	};
}

test("整注（一单元）冲突：全腿换新键留篮；单关冲突只换失败腿、未提交腿原键不动", async () => {
	// 整注冲突：唯一单元=全部腿 → remaining 全部 fresh
	{
		const h = harness();
		await h.run([[leg(1), leg(2)]], async () => {
			throw conflictError();
		});
		expect(h.replaced).toHaveLength(1);
		expect(h.replaced[0]).toHaveLength(2);
		expect(h.replaced[0]?.map((l) => l.request_id).every((key) => !key.startsWith("k"))).toBe(true);
		expect(h.messages[0]).toContain("#80");
	}
	// 整注 unitLabel=""（review 修）：had 整注旧文案无「第 1 笔」前缀
	{
		const h = harness();
		await h.run(
			[[leg(1), leg(2)]],
			async () => {
				throw conflictError();
			},
			{ unitLabel: () => "" },
		);
		expect(h.messages[0]).not.toContain("第 1 笔");
		expect(h.messages[0]).toContain("原提交其实已成功");
	}
	// 单关冲突：单元1 失败换新键，单元2 未提交原键保留
	{
		const h = harness();
		await h.run([[leg(1)], [leg(2)], [leg(3)]], async (unit) =>
			unit[0]?.fixture_id === 2 ? Promise.reject(conflictError()) : Promise.resolve(betOf(unit[0]?.fixture_id ?? 0)),
		);
		expect(h.replaced).toHaveLength(1);
		const [freshLeg, untouched] = h.replaced[0] ?? [];
		expect(freshLeg?.request_id).not.toBe("k2"); // 冲突腿换新键
		expect(untouched?.request_id).toBe("k3"); // 未提交腿不动
		expect(h.messages[0]).toContain("此前已建 #1");
	}
});

test("价变 409：清篮+关抽屉+价格源失效带全部场次键；已成注账面保留提示", async () => {
	const h = harness();
	await h.run([[leg(1)], [leg(2)]], async (unit) =>
		unit[0]?.fixture_id === 2 ? Promise.reject(priceChangeError()) : Promise.resolve(betOf(1)),
	);
	expect(h.replaced).toEqual([[]]);
	expect(h.closed).toBe(1);
	expect(h.invalidatedPrices).toEqual([[1, 2]]); // 全部腿的场次，非仅失败腿
	expect(h.messages[0]).toContain("报价已变（2 → 4.10）");
	expect(h.messages[0]).toContain("已建 #1");
});

test("普通失败：失败单元起原键留篮（同键重试），成功前缀单元已清账", async () => {
	const h = harness();
	await h.run([[leg(1)], [leg(2)], [leg(3)]], async (unit) =>
		unit[0]?.fixture_id === 2 ? Promise.reject(new Error("boom")) : Promise.resolve(betOf(1)),
	);
	expect(h.replaced).toEqual([[leg(2), leg(3)]]); // 同键留篮
	expect(h.messages[0]).toBe("fail#1:Error: boom");
	expect(h.closed).toBe(0);
});

test("全单元成功：清篮+关抽屉+成功文案；空单元直退零副作用", async () => {
	{
		const h = harness();
		await h.run([[leg(1)], [leg(2)]], async (unit) => betOf(unit[0]?.fixture_id ?? 0));
		expect(h.replaced).toEqual([[]]);
		expect(h.closed).toBe(1);
		expect(h.messages).toEqual(["success:1,2"]);
	}
	{
		const h = harness();
		await h.run([[]], async () => betOf(1));
		expect(h.replaced).toHaveLength(0);
		expect(h.messages).toHaveLength(0);
		expect(h.closed).toBe(0);
	}
});
