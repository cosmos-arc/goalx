import { useSyncExternalStore } from "react";
import type { GoalsLeg } from "../components/goals-ui";
import type { Leg } from "../components/had-quote-ui";

/**
 * 全站选注篮单源（票 06）：had 三页（场次/胜平负/研究）+ 进球页共用
 * 一份草稿。模块级 store + useSyncExternalStore——仓内无状态库依赖，
 * ladder 首选最小心智；每次变更同步写 localStorage（路由切换不丢、
 * 刷新可恢复；人裁决：无过期，靠篮内开球时间展示自然淘汰）。
 *
 * request_id = 腿稳定幂等键（pick/带入时生成）：单关腿=该腿建注键，
 * 串关=腿键有序拼接（换选/替换=新键）；提交重试同键，服务端同键
 * 不落双行（bets.client_request_id 部分唯一索引）。
 */

const STORAGE_KEY = "goalx:basket";

type BasketState = {
	hadLegs: Leg[];
	goalsLegs: GoalsLeg[];
	stake: string;
	strategyVersion: string;
	mode: "paper" | "live";
	/** had 提交飞行中（票 03 全局冻结）：跨页共享——提交期间任何页的
	 * 篮编辑被门住；不持久化（刷新即解冻——mutation 随页面死）。 */
	hadPending: boolean;
	/** goals 提交飞行中（票 04，与 had 同族同语义、计数各自独立）。 */
	goalsPending: boolean;
};

const EMPTY: BasketState = {
	hadLegs: [],
	goalsLegs: [],
	stake: "100",
	strategyVersion: "",
	mode: "paper",
	hadPending: false,
	goalsPending: false,
};

/** 恢复形状守卫（correctness F4）：字段级校验——畸形腿直接过滤，
 * 不让 odds.toFixed 之类的渲染期崩溃混进恢复路径。 */
function isPersistableLeg(leg: unknown): boolean {
	if (typeof leg !== "object" || leg === null) {
		return false;
	}
	const candidate = leg as Record<string, unknown>;
	return (
		typeof candidate["request_id"] === "string" &&
		typeof candidate["fixture_id"] === "number" &&
		typeof candidate["odds"] === "number" &&
		typeof candidate["kickoff_utc"] === "string"
	);
}

/** 快照形状守卫（报告审计票 06）：畸形 probability 字段剥为 null（腿保留，
 * EV 降级依据缺失）——畸形 value 直进渲染/legEv 会 TypeError/NaN，
 * 与 F4 同类恢复路径崩溃面。 */
function sanitizeSnapshot<T extends { probability?: unknown }>(leg: T): T {
	const snap = leg.probability;
	if (snap === undefined || snap === null) {
		return leg;
	}
	const value = (snap as Record<string, unknown>)["value"];
	const ok =
		typeof snap === "object" &&
		snap !== null &&
		typeof value === "number" &&
		Number.isFinite(value) &&
		value >= 0 &&
		value <= 1 && // 概率区间（correctness F2）：越界值剥 null 不进预览
		typeof (snap as Record<string, unknown>)["source"] === "string" &&
		((snap as Record<string, unknown>)["at"] === null || typeof (snap as Record<string, unknown>)["at"] === "string");
	return ok ? leg : { ...leg, probability: null };
}

let state: BasketState = loadInitial();
let hadInFlight = 0; // had 提交飞行计数（>0=冻结；刷新归零解冻）
let goalsInFlight = 0; // goals 提交飞行计数（票 04；与 had 各自独立）
const listeners = new Set<() => void>();

/** 宽容恢复：形状不对/存储损坏按空篮（隐私模式读失败同此）。 */
function loadInitial(): BasketState {
	try {
		const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
		if (!raw) {
			return EMPTY;
		}
		const parsed = JSON.parse(raw) as Partial<BasketState>;
		return {
			hadLegs: Array.isArray(parsed.hadLegs) ? parsed.hadLegs.filter(isPersistableLeg).map(sanitizeSnapshot) : [],
			goalsLegs: Array.isArray(parsed.goalsLegs) ? parsed.goalsLegs.filter(isPersistableLeg) : [],
			stake: typeof parsed.stake === "string" ? parsed.stake : EMPTY.stake,
			strategyVersion: typeof parsed.strategyVersion === "string" ? parsed.strategyVersion : EMPTY.strategyVersion,
			mode: parsed.mode === "live" ? "live" : "paper",
			hadPending: false,
			goalsPending: false,
		};
	} catch {
		return EMPTY;
	}
}

function commit(next: BasketState): void {
	state = next;
	try {
		// 冻结标志不落盘（刷新后无在飞 mutation，门恒解冻）
		globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify({ ...next, hadPending: false, goalsPending: false }));
	} catch {
		// 存不进（隐私模式/满）——本次会话内状态仍生效
	}
	for (const notify of listeners) {
		notify();
	}
}

/** 全站篮草稿（had 腿 + goals 腿 + 提交参数）。 */
export function useBasket(): BasketState {
	return useSyncExternalStore(
		(onStoreChange) => {
			listeners.add(onStoreChange);
			return () => {
				listeners.delete(onStoreChange);
			};
		},
		() => state,
		() => EMPTY,
	);
}

export const basketActions = {
	/** 编辑入口（pick/换选/移除/带入）：飞行中被冻结门拦下（静默不动）。 */
	setHadLegs(legs: Leg[]): void {
		if (state.hadPending) {
			return;
		}
		commit({ ...state, hadLegs: legs });
	},
	/** 提交路径自写（清篮/冲突恢复腿）：不受冻结门——门只挡用户编辑。 */
	replaceHadLegs(legs: Leg[]): void {
		commit({ ...state, hadLegs: legs });
	},
	/** 提交飞行起止（useHadBasket 的 mutation onMutate/onSettled 驱动）。
	 * 引用计数非布尔（correctness F1）：重提交重叠时首个 settle 不得
	 * 提前解冻仍飞行的第二个提交。 */
	beginHadFlight(): void {
		hadInFlight += 1;
		if (!state.hadPending) {
			commit({ ...state, hadPending: true });
		}
	},
	endHadFlight(): void {
		hadInFlight = Math.max(0, hadInFlight - 1);
		if (hadInFlight === 0 && state.hadPending) {
			commit({ ...state, hadPending: false });
		}
	},
	/** goals 编辑入口（票 04 同族冻结）：飞行中被门拦下（静默不动）。 */
	setGoalsLegs(legs: GoalsLeg[]): void {
		if (state.goalsPending) {
			return;
		}
		commit({ ...state, goalsLegs: legs });
	},
	/** goals 提交路径自写：不受冻结门。 */
	replaceGoalsLegs(legs: GoalsLeg[]): void {
		commit({ ...state, goalsLegs: legs });
	},
	/** goals 提交飞行起止（引用计数，同 beginHadFlight 语义）。 */
	beginGoalsFlight(): void {
		goalsInFlight += 1;
		if (!state.goalsPending) {
			commit({ ...state, goalsPending: true });
		}
	},
	endGoalsFlight(): void {
		goalsInFlight = Math.max(0, goalsInFlight - 1);
		if (goalsInFlight === 0 && state.goalsPending) {
			commit({ ...state, goalsPending: false });
		}
	},
	setStake(stake: string): void {
		commit({ ...state, stake });
	},
	setStrategyVersion(strategyVersion: string): void {
		commit({ ...state, strategyVersion });
	},
	setMode(mode: "paper" | "live"): void {
		commit({ ...state, mode });
	},
};

/** 腿稳定幂等键（浏览器/jsdom 均可用；无 crypto.randomUUID 时降级）。 */
export function newRequestId(): string {
	const cryptoRef = globalThis.crypto;
	if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
		return cryptoRef.randomUUID();
	}
	return `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 一注的幂等键：腿键有序拼接（同腿集合重试同键；≤200 契约上限内；
 * 空腿=null=不带键旧行为）。 */
export function requestIdForLegs(legs: { request_id: string }[]): string | null {
	return legs.length === 0
		? null
		: legs
				.map((leg) => leg.request_id)
				.sort()
				.join("+");
}

/** 腿集换新幂等键（票 09）：意图冲突恢复原结果后，篮内腿换新键——
 * 下次提交=按新意图另建（改条款重提不换键=双注反模式，冲突确认后才换）。 */
export function withFreshRequestIds<T extends { request_id: string }>(legs: T[]): T[] {
	return legs.map((leg) => ({ ...leg, request_id: newRequestId() }));
}

/** 测试隔离：清内存态与存储（test/setup.ts afterEach 调用）。 */
export function __resetBasketForTests(): void {
	state = EMPTY;
	hadInFlight = 0; // 飞行计数随隔离清零（否则上一测试未 settle 的飞行冻结后来者）
	goalsInFlight = 0;
	try {
		globalThis.localStorage?.removeItem(STORAGE_KEY);
	} catch {
		// ignore
	}
	for (const notify of listeners) {
		notify();
	}
}
