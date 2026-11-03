import { renderHook } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { __resetBasketForTests, basketActions, newRequestId, requestIdForLegs, useBasket } from "./basket-store";

/** 票 06 全站篮草稿单源：持久化往返/损坏恢复/幂等键组合。 */

const LEG = {
	fixture_id: 1,
	match_code: "周六001",
	home_team: "阿森纳",
	away_team: "切尔西",
	kickoff_utc: "2026-11-04T19:00:00+00:00",
	selection: "h" as const,
	odds: 2.35,
	single_eligible: true,
	request_id: "leg-1",
};

beforeEach(() => {
	localStorage.clear();
	__resetBasketForTests();
});

test("actions persist to localStorage and notify subscribers", () => {
	basketActions.setHadLegs([LEG]);
	basketActions.setStake("55");
	const stored = JSON.parse(localStorage.getItem("goalx:basket") ?? "{}");
	expect(stored.hadLegs).toHaveLength(1);
	expect(stored.stake).toBe("55");
	const { result } = renderHook(() => useBasket());
	expect(result.current.hadLegs[0]?.request_id).toBe("leg-1");
	expect(result.current.stake).toBe("55");
});

test("fresh module load restores persisted draft (reload semantics)", async () => {
	basketActions.setHadLegs([LEG]);
	basketActions.setGoalsLegs([]);
	vi.resetModules();
	const fresh = await import("./basket-store");
	const { result } = renderHook(() => fresh.useBasket());
	expect(result.current.hadLegs).toHaveLength(1);
	expect(result.current.hadLegs[0]?.kickoff_utc).toBe(LEG.kickoff_utc);
});

test("corrupt or shapeless storage recovers to empty basket", async () => {
	localStorage.setItem("goalx:basket", "{not json");
	vi.resetModules();
	const fresh = await import("./basket-store");
	const { result } = renderHook(() => fresh.useBasket());
	expect(result.current.hadLegs).toHaveLength(0);
	// 形状不对（无 request_id 的旧形状腿）宽容过滤
	localStorage.setItem("goalx:basket", JSON.stringify({ hadLegs: [{ fixture_id: 1 }], mode: "live" }));
	vi.resetModules();
	const fresh2 = await import("./basket-store");
	const { result: result2 } = renderHook(() => fresh2.useBasket());
	expect(result2.current.hadLegs).toHaveLength(0);
	expect(result2.current.mode).toBe("live"); // 标量字段照常恢复
});

test("requestIdForLegs: order-independent composite; empty = null (旧行为)", () => {
	const a = newRequestId();
	const b = newRequestId();
	expect(requestIdForLegs([{ request_id: a }, { request_id: b }])).toBe(
		requestIdForLegs([{ request_id: b }, { request_id: a }]),
	);
	expect(requestIdForLegs([{ request_id: a }])).toBe(a);
	expect(requestIdForLegs([])).toBeNull();
});
