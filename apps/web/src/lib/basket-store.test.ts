import { act, renderHook } from "@testing-library/react";
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

test("入篮概率快照随腿持久化；旧形状腿（无快照）照常恢复（票 06）", async () => {
	const withSnapshot = {
		...LEG,
		probability: {
			value: 0.53,
			source: "research:consensus" as const,
			at: "2026-11-04T10:00:00+00:00",
		},
	};
	basketActions.setHadLegs([withSnapshot]);
	vi.resetModules();
	const fresh = await import("./basket-store");
	const { result } = renderHook(() => fresh.useBasket());
	expect(result.current.hadLegs[0]?.probability).toEqual(withSnapshot.probability);

	// 畸形快照（correctness F4 同类 + F2 概率区间）：腿保留，快照剥为
	// null=依据缺失不炸渲染；越界概率（53）同剥
	localStorage.setItem(
		"goalx:basket",
		JSON.stringify({
			hadLegs: [
				{ ...LEG, request_id: "bad-snap", probability: { value: "0.5" } },
				{
					...LEG,
					fixture_id: 8,
					request_id: "out-of-range",
					probability: { value: 53, source: "research:consensus" as const, at: null },
				},
			],
			stake: "100",
			mode: "paper",
		}),
	);
	vi.resetModules();
	const freshBad = await import("./basket-store");
	const { result: resultBad } = renderHook(() => freshBad.useBasket());
	expect(resultBad.current.hadLegs).toHaveLength(2);
	expect(resultBad.current.hadLegs.every((leg) => leg.probability === null)).toBe(true);

	// 修复前入库的旧篮（无 probability 字段）：不丢腿，快照 undefined=依据缺失
	basketActions.setHadLegs([{ ...LEG, request_id: "legacy-leg" }]);
	localStorage.setItem(
		"goalx:basket",
		JSON.stringify({ hadLegs: [{ ...LEG, request_id: "legacy-leg" }], stake: "100", mode: "paper" }),
	);
	vi.resetModules();
	const fresh2 = await import("./basket-store");
	const { result: result2 } = renderHook(() => fresh2.useBasket());
	expect(result2.current.hadLegs).toHaveLength(1);
	expect(result2.current.hadLegs[0]?.probability).toBeUndefined();
});

// ---- 票 03：hadPending 全局冻结门 ----

test("setHadLegs 飞行中被门拦下，replaceHadLegs 自写放行，标志不落盘", () => {
	const { result } = renderHook(() => useBasket());
	act(() => {
		basketActions.setHadLegs([LEG]);
		basketActions.beginHadFlight();
		// 编辑入口（pick/换选/移除/带入）静默不动
		basketActions.setHadLegs([]);
	});
	expect(result.current.hadLegs).toHaveLength(1);
	expect(result.current.hadPending).toBe(true);
	act(() => {
		// 提交路径自写（清篮/冲突恢复腿）不受门
		basketActions.replaceHadLegs([]);
	});
	expect(result.current.hadLegs).toHaveLength(0);
	// 冻结标志不持久化（刷新即解冻的存储侧依据）
	const persisted = JSON.parse(globalThis.localStorage.getItem("goalx:basket") ?? "{}");
	expect(persisted.hadPending ?? false).toBe(false);
	act(() => {
		// 解冻后编辑写入恢复
		basketActions.endHadFlight();
		basketActions.setHadLegs([LEG]);
	});
	expect(result.current.hadLegs).toHaveLength(1);
});

test("重叠提交计数制冻结：首个 settle 不解冻仍飞行的第二个（correctness F1）", () => {
	const { result } = renderHook(() => useBasket());
	act(() => {
		basketActions.setHadLegs([LEG]);
		basketActions.beginHadFlight(); // 提交 A 起飞
		basketActions.beginHadFlight(); // 提交 B 重叠起飞
	});
	expect(result.current.hadPending).toBe(true);
	act(() => {
		basketActions.setHadLegs([]); // 仍冻结
	});
	expect(result.current.hadLegs).toHaveLength(1);
	act(() => {
		basketActions.endHadFlight(); // A settle：仍剩 B 在飞
	});
	expect(result.current.hadPending).toBe(true);
	act(() => {
		basketActions.setHadLegs([]);
	});
	expect(result.current.hadLegs).toHaveLength(1); // 冻结未解
	act(() => {
		basketActions.endHadFlight(); // B settle：全落
	});
	expect(result.current.hadPending).toBe(false);
	act(() => {
		basketActions.setHadLegs([]);
	});
	expect(result.current.hadLegs).toHaveLength(0);
});

// ---- 票 04：goalsPending 冻结门（与 had 各自独立）----

test("共享提交参数（金额/模式/策略版本）任一飞行中都冻结，settle 后恢复（票 01 F1）", () => {
	const { result } = renderHook(() => useBasket());
	act(() => {
		basketActions.beginHadFlight();
		basketActions.setStake("999");
		basketActions.setMode("live");
		basketActions.setStrategyVersion("drift");
	});
	// had 飞行中从任何页改共享参数：静默不动（防失败重提口径漂移）
	expect(result.current.stake).toBe("100");
	expect(result.current.mode).toBe("paper");
	expect(result.current.strategyVersion).toBe("");
	act(() => {
		basketActions.endHadFlight();
		basketActions.beginGoalsFlight();
		basketActions.setStake("999");
		basketActions.setMode("live");
	});
	// goals 飞行同拦（对称面）
	expect(result.current.stake).toBe("100");
	expect(result.current.mode).toBe("paper");
	act(() => {
		basketActions.endGoalsFlight();
		basketActions.setStake("150");
	});
	expect(result.current.stake).toBe("150"); // 解冻后可改
});

test("setGoalsLegs 飞行中被门拦下，replaceGoalsLegs 自写放行，与 had 门互不干扰", () => {
	const goalsLeg = {
		fixture_id: 9,
		market: "ttg",
		selection: "2",
		odds: 3.1,
		request_id: "g1",
		kickoff_utc: "2026-11-07T11:00:00Z",
	} as never;
	const { result } = renderHook(() => useBasket());
	act(() => {
		basketActions.setGoalsLegs([goalsLeg]);
		basketActions.beginGoalsFlight();
		// goals 编辑入口被门；had 编辑不受 goals 飞行影响（计数独立）
		basketActions.setGoalsLegs([]);
		basketActions.setHadLegs([LEG]);
	});
	expect(result.current.goalsLegs).toHaveLength(1);
	expect(result.current.hadLegs).toHaveLength(1);
	expect(result.current.goalsPending).toBe(true);
	expect(result.current.hadPending).toBe(false);
	act(() => {
		basketActions.replaceGoalsLegs([]); // 提交自写放行
	});
	expect(result.current.goalsLegs).toHaveLength(0);
	const persisted = JSON.parse(globalThis.localStorage.getItem("goalx:basket") ?? "{}");
	expect(persisted.goalsPending ?? false).toBe(false); // 标志不落盘
	act(() => {
		basketActions.endGoalsFlight();
		basketActions.setGoalsLegs([goalsLeg]); // 解冻后写入恢复
	});
	expect(result.current.goalsLegs).toHaveLength(1);
});

test("goals 重叠提交计数制：首个 settle 不解冻第二个", () => {
	const { result } = renderHook(() => useBasket());
	act(() => {
		basketActions.beginGoalsFlight();
		basketActions.beginGoalsFlight();
	});
	act(() => {
		basketActions.endGoalsFlight();
	});
	expect(result.current.goalsPending).toBe(true); // 仍剩一个在飞
	act(() => {
		basketActions.endGoalsFlight();
	});
	expect(result.current.goalsPending).toBe(false);
});
