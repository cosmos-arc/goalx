import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { beforeEach, expect, test, vi } from "vitest";
import type { TodayFixture } from "../api/goalx";
import { basketStore } from "../lib/basket-store";
import { server } from "../mocks/server";
import { useHadBasket } from "./had-basket";
import type { PickableFixture } from "./had-quote-ui";

/**
 * 报告审计票 06：入篮时概率快照——EV 只读快照，不随当前研究页漂移；
 * 无快照=依据缺失（不用当前页概率顶替、不用 0 冒充）。
 */

const FUTURE_KICKOFF = new Date(Date.now() + 3 * 3600_000).toISOString();

const PICKABLE: PickableFixture = {
	fixture_id: 1,
	match_code: "周六001",
	home_team: "阿森纳",
	away_team: "切尔西",
	kickoff_utc: FUTURE_KICKOFF,
	had_quote: {
		as_of: "2026-11-07T09:00:00+00:00",
		status: "valid",
		sale_state: "on_sale",
		single_eligible: true,
		eu_books: 3,
	},
};

const OTHER: PickableFixture = {
	...PICKABLE,
	fixture_id: 2,
	match_code: "周六002",
	home_team: "利物浦",
	away_team: "埃弗顿",
};

function withClient() {
	const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	const wrapper = ({ children }: { children: ReactNode }) => (
		<QueryClientProvider client={client}>{children}</QueryClientProvider>
	);
	return { client, wrapper };
}

beforeEach(() => {
	localStorage.clear();
	basketStore.reset();
});

test("pick 落入篮时概率快照；EV 只读快照不查当前页（票 06 验收 1）", () => {
	const { wrapper } = withClient();
	const researchConsensus = { h: 0.53, d: 0.27, a: 0.22 };
	const { result } = renderHook(
		() =>
			useHadBasket(Date.now(), [], (_fixture, selection) => ({
				value: researchConsensus[selection],
				source: "research:consensus",
				at: null,
			})),
		{ wrapper },
	);
	act(() => {
		result.current.pick(PICKABLE, "h", 1.92);
	});
	const leg = result.current.legs[0];
	expect(leg).toBeDefined();
	expect(leg?.probability).toEqual({ value: 0.53, source: "research:consensus", at: null });
	// 修复前：研究页对每腿用当前场概率（B 场被 A 场 0.53 算成 +59%）
	if (leg === undefined) throw new Error("leg missing");
	expect(result.current.legEv(leg)).toBeCloseTo(0.53 * 1.92 - 1, 10);

	// 切到"另一场研究页"（hook 重建、共识截然不同）：既有腿 EV 不漂移
	const otherPage = renderHook(
		() =>
			useHadBasket(Date.now(), [], (_fixture, selection) => ({
				value: selection === "h" ? 0.9 : 0.05,
				source: "research:consensus",
				at: null,
			})),
		{ wrapper },
	);
	if (leg === undefined) throw new Error("leg missing");
	expect(otherPage.result.current.legEv(leg)).toBeCloseTo(0.53 * 1.92 - 1, 10);
});

test("缺省快照来源=今日列表 eu_prob；时点=欧赔观测钟；无共识=依据缺失 null（票 06 验收 2）", () => {
	const { wrapper } = withClient();
	const now = Date.now();
	const EU_AGE_SECONDS = 3600;
	const fixtures = [
		{
			fixture_id: 1,
			match_code: "周六001",
			business_date: "2026-11-07",
			competition: "英超",
			tier: "TIER1",
			home_team: "阿森纳",
			away_team: "切尔西",
			kickoff_utc: FUTURE_KICKOFF,
			is_single: true,
			joined: true,
			jc_odds: { h: 1.92, d: 3.4, a: 3.6 },
			jc_updated_at: "2026-11-07T09:00:00+00:00",
			books: 3,
			eu_prob: { h: 0.5, d: 0.25, a: 0.25 },
			had_quote: { as_of: "2026-11-07T09:00:00+00:00", status: "valid", eu_age_seconds: EU_AGE_SECONDS, eu_books: 3 },
			flags: [],
		},
		{
			fixture_id: 2,
			match_code: "周六002",
			business_date: "2026-11-07",
			competition: "英超",
			tier: "TIER1",
			home_team: "利物浦",
			away_team: "埃弗顿",
			kickoff_utc: FUTURE_KICKOFF,
			is_single: true,
			joined: false,
			jc_odds: { h: 2.5, d: 3.1, a: 2.7 },
			jc_updated_at: null,
			books: 0,
			eu_prob: null, // 未 join 欧赔：无共识
			flags: [],
		},
	] as unknown as TodayFixture[];
	const { result } = renderHook(() => useHadBasket(now, fixtures), { wrapper });

	act(() => {
		result.current.pick(PICKABLE, "h", 1.92); // 列表有共识 → 快照落 eu_prob
	});
	act(() => {
		result.current.pick(OTHER, "a", 2.7); // 无共识 → null=依据缺失
	});
	const [snapLeg, bareLeg] = result.current.legs;
	// 时点=概率观测钟（as_of−age 同载荷同钟域），非竞彩调盘钟、
	// 也不随 pick 驻留时长漂移（correctness F1）
	expect(snapLeg?.probability).toEqual({
		value: 0.5,
		source: "today-list:eu_prob",
		at: new Date(new Date("2026-11-07T09:00:00+00:00").getTime() - EU_AGE_SECONDS * 1000).toISOString(),
	});
	if (snapLeg === undefined || bareLeg === undefined) throw new Error("legs missing");
	expect(result.current.legEv(snapLeg)).toBeCloseTo(0.5 * 1.92 - 1, 10);
	expect(bareLeg.probability).toBeNull();
	expect(result.current.legEv(bareLeg)).toBeNull(); // 不冒充
});

test("旧篮恢复腿（无快照）EV=依据缺失，不回退当前页列表 EV（票 06 验收 2）", () => {
	const { wrapper } = withClient();
	// 旧形状腿直接进篮（修复前持久化的篮）
	const { result } = renderHook(() => useHadBasket(Date.now(), []), { wrapper });
	act(() => {
		result.current.setLegs([
			{
				fixture_id: 9,
				match_code: "周六009",
				home_team: "旧",
				away_team: "腿",
				kickoff_utc: FUTURE_KICKOFF,
				selection: "h",
				odds: 2.1,
				single_eligible: true,
				request_id: "legacy",
			},
		]);
	});
	const restored = result.current.legs[0];
	if (restored === undefined) throw new Error("leg missing");
	expect(result.current.legEv(restored)).toBeNull();
});

// ---- 票 03：提交飞行中的全局冻结（pending 下沉 basket-store）----

test("提交飞行中，别页编辑被门拦下；settle 后清腿落库（票 03 验收）", async () => {
	const { wrapper } = withClient();
	const pageA = renderHook(() => useHadBasket(Date.now(), []), { wrapper });
	act(() => {
		pageA.result.current.pick(PICKABLE, "h", 1.92);
	});
	expect(pageA.result.current.legs).toHaveLength(1);

	// 服务器挂起不响应：提交进入飞行，onMutate 已置全局 pending
	let release = () => {};
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	server.use(
		http.post(
			"*/api/v1/bets",
			async () => {
				await gate;
				return HttpResponse.json({ id: 9, mode: "paper", stake: 100, legs: [], status: "open" }, { status: 201 });
			},
			{ once: true },
		),
	);
	await act(async () => {
		pageA.result.current.createSuggestion.mutate();
	});

	// 别页（新 hook 实例 isPending=false）加腿：被 store 冻结门拦下
	const pageB = renderHook(() => useHadBasket(Date.now(), []), { wrapper });
	expect(pageB.result.current.createSuggestion.isPending).toBe(false);
	act(() => {
		pageB.result.current.pick(OTHER, "a", 3.1);
	});
	expect(pageB.result.current.legs).toHaveLength(1); // 仍只有飞行前那腿
	expect(pageB.result.current.legs[0]?.fixture_id).toBe(1);

	// 服务器返回 201 → 提交自写清腿不受门，settle 解冻
	release();
	await act(async () => {
		await vi.waitFor(() => {
			expect(pageB.result.current.legs).toHaveLength(0);
		});
	});
});

// ---- 票 05 E15：跨页飞行中 pick 的「已替换」toast 说谎 ----

test("跨页飞行中同场换选：编辑被门拦且不报「已替换」（票 05 E15）", async () => {
	const { wrapper } = withClient();
	const pageA = renderHook(() => useHadBasket(Date.now(), []), { wrapper });
	act(() => {
		pageA.result.current.pick(PICKABLE, "h", 1.92);
	});

	// 别页提交挂起 → 全局 hadPending
	let release = () => {};
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	server.use(
		http.post(
			"*/api/v1/bets",
			async () => {
				await gate;
				return HttpResponse.json({ id: 9, mode: "paper", stake: 100, legs: [], status: "open" }, { status: 201 });
			},
			{ once: true },
		),
	);
	await act(async () => {
		pageA.result.current.createSuggestion.mutate();
	});

	// 本页（isPending=false）同场换选：旧实现 setLegs 被门吞但 toast 报已替换
	const pageB = renderHook(() => useHadBasket(Date.now(), []), { wrapper });
	act(() => {
		pageB.result.current.pick(PICKABLE, "d", 3.4);
	});
	expect(pageB.result.current.legs).toHaveLength(1);
	expect(pageB.result.current.legs[0]?.fixture_id).toBe(1);
	expect(pageB.result.current.legs[0]?.selection).toBe("h"); // 未被替换
	expect(pageB.result.current.message).toBeNull(); // 无「已替换」谎报

	release();
	await act(async () => {
		await vi.waitFor(() => {
			expect(pageB.result.current.legs).toHaveLength(0);
		});
	});
});
