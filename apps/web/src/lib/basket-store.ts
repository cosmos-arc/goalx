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
};

const EMPTY: BasketState = {
	hadLegs: [],
	goalsLegs: [],
	stake: "100",
	strategyVersion: "",
	mode: "paper",
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
	const ok =
		typeof snap === "object" &&
		snap !== null &&
		typeof (snap as Record<string, unknown>)["value"] === "number" &&
		Number.isFinite((snap as Record<string, unknown>)["value"] as number) &&
		typeof (snap as Record<string, unknown>)["source"] === "string" &&
		((snap as Record<string, unknown>)["at"] === null || typeof (snap as Record<string, unknown>)["at"] === "string");
	return ok ? leg : { ...leg, probability: null };
}

let state: BasketState = loadInitial();
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
		};
	} catch {
		return EMPTY;
	}
}

function commit(next: BasketState): void {
	state = next;
	try {
		globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(next));
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
	setHadLegs(legs: Leg[]): void {
		commit({ ...state, hadLegs: legs });
	},
	setGoalsLegs(legs: GoalsLeg[]): void {
		commit({ ...state, goalsLegs: legs });
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

/** 测试隔离：清内存态与存储（test/setup.ts afterEach 调用）。 */
export function __resetBasketForTests(): void {
	state = EMPTY;
	try {
		globalThis.localStorage?.removeItem(STORAGE_KEY);
	} catch {
		// ignore
	}
	for (const notify of listeners) {
		notify();
	}
}
