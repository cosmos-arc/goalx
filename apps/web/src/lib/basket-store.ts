import { useSyncExternalStore } from "react";
import type { GoalsLeg } from "../components/goals-ui";
import type { Leg } from "../components/had-quote-ui";

/**
 * 全站选注篮单源（票 06）：had 三页（场次/胜平负/研究）+ 进球页共用
 * 一份草稿。模块级 store + useSyncExternalStore——仓内无状态库依赖，
 * ladder 首选最小心智；每次变更同步写 localStorage（路由切换不丢、
 * 刷新可恢复；人裁决：无过期，靠篮内开球时间展示自然淘汰）。
 *
 * 票 08 提交 seam：飞行状态机（withFlight/isFrozen/frozenAny）住本模块
 * ——begin/end 引用计数、编辑门控、恢复写（replaceLegs 不受门）全在
 * implementation；页面只消费 frozen 布尔。createBasketStore 全量工厂
 * 收闭包，生产保留默认实例导出（页面 import 零改动），测试自建实例。
 *
 * request_id = 腿稳定幂等键（pick/带入时生成）：单关腿=该腿建注键，
 * 串关=腿键有序拼接（换选/替换=新键）；提交重试同键，服务端同键
 * 不落双行（bets.client_request_id 部分唯一索引）。
 */

const STORAGE_KEY = "goalx:basket";

export type BasketKind = "had" | "goals";

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

/** 宽容恢复：形状不对/存储损坏按空篮（隐私模式读失败同此）。 */
function loadInitial(storageKey: string): BasketState {
	try {
		const raw = globalThis.localStorage?.getItem(storageKey);
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

/**
 * 篮 store 全量工厂（票 08）：state/listeners/飞行计数收进闭包。
 * 生产用默认实例；测试自建实例互不渗透，删模块级 __resetBasketForTests。
 */
export function createBasketStore(storageKey = STORAGE_KEY) {
	let state: BasketState = loadInitial(storageKey);
	const inFlight: Record<BasketKind, number> = { had: 0, goals: 0 };
	const listeners = new Set<() => void>();

	function commit(next: BasketState): void {
		state = next;
		try {
			// 冻结标志不落盘（刷新后无在飞 mutation，门恒解冻）
			globalThis.localStorage?.setItem(storageKey, JSON.stringify({ ...next, hadPending: false, goalsPending: false }));
		} catch {
			// 存不进（隐私模式/满）——本次会话内状态仍生效
		}
		for (const notify of listeners) {
			notify();
		}
	}

	function beginFlight(kind: BasketKind): void {
		inFlight[kind] += 1;
		if (!state[`${kind}Pending`]) {
			commit({ ...state, [`${kind}Pending`]: true });
		}
	}

	function endFlight(kind: BasketKind): void {
		inFlight[kind] = Math.max(0, inFlight[kind] - 1);
		if (inFlight[kind] === 0 && state[`${kind}Pending`]) {
			commit({ ...state, [`${kind}Pending`]: false });
		}
	}

	return {
		subscribe(listener: () => void): () => void {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
		getSnapshot(): BasketState {
			return state;
		},
		/** 飞行状态机谓词（票 08）：kind 飞行中=编辑门住。 */
		isFrozen(kind: BasketKind): boolean {
			return inFlight[kind] > 0;
		},
		/** 任一篮飞行中（共享提交参数门的谓词）。 */
		frozenAny(): boolean {
			return inFlight.had > 0 || inFlight.goals > 0;
		},
		/** 包提交（票 08）：飞行计数起落 + 异常也解冻（finally）。 */
		async withFlight<T>(kind: BasketKind, action: () => Promise<T>): Promise<T> {
			beginFlight(kind);
			try {
				return await action();
			} finally {
				endFlight(kind);
			}
		},
		/** 编辑入口（pick/换选/移除/带入）：飞行中被冻结门拦下（静默不动）。 */
		setLegs(kind: BasketKind, legs: Leg[] | GoalsLeg[]): void {
			if (inFlight[kind] > 0) {
				return;
			}
			commit(kind === "had" ? { ...state, hadLegs: legs as Leg[] } : { ...state, goalsLegs: legs as GoalsLeg[] });
		},
		/** 提交路径自写（清篮/冲突恢复腿）：不受冻结门——门只挡用户编辑。 */
		replaceLegs(kind: BasketKind, legs: Leg[] | GoalsLeg[]): void {
			commit(kind === "had" ? { ...state, hadLegs: legs as Leg[] } : { ...state, goalsLegs: legs as GoalsLeg[] });
		},
		/** 共享提交参数（金额/模式/策略版本）为两篮一份：任一飞行中都冻结
		 * （票 01 F1）——had 飞行中从 goals 侧改 stake=had 失败重提口径漂移；
		 * 与 setLegs 同款静默门。 */
		setStake(stake: string): void {
			if (this.frozenAny()) {
				return;
			}
			commit({ ...state, stake });
		},
		setStrategyVersion(strategyVersion: string): void {
			if (this.frozenAny()) {
				return;
			}
			commit({ ...state, strategyVersion });
		},
		setMode(mode: "paper" | "live"): void {
			if (this.frozenAny()) {
				return;
			}
			commit({ ...state, mode });
		},
		/** 测试隔离（setup.ts afterEach 调默认实例；自建实例各测各清）。 */
		reset(): void {
			state = EMPTY;
			inFlight.had = 0; // 飞行计数随隔离清零（否则未 settle 飞行冻结后来者）
			inFlight.goals = 0;
			try {
				globalThis.localStorage?.removeItem(storageKey);
			} catch {
				// ignore
			}
			for (const notify of listeners) {
				notify();
			}
		},
	};
}

/** 生产默认实例（页面 import 零改动）。 */
export const basketStore = createBasketStore();

/** 全站篮草稿（had 腿 + goals 腿 + 提交参数）。 */
export function useBasket(): BasketState {
	return useSyncExternalStore(basketStore.subscribe, basketStore.getSnapshot, () => EMPTY);
}

/** 页面门面（票 08）：沿用既有命名，内走状态机单源。 */
export const basketActions = {
	setHadLegs: (legs: Leg[]): void => basketStore.setLegs("had", legs),
	replaceHadLegs: (legs: Leg[]): void => basketStore.replaceLegs("had", legs),
	setGoalsLegs: (legs: GoalsLeg[]): void => basketStore.setLegs("goals", legs),
	replaceGoalsLegs: (legs: GoalsLeg[]): void => basketStore.replaceLegs("goals", legs),
	setStake: (stake: string): void => basketStore.setStake(stake),
	setStrategyVersion: (strategyVersion: string): void => basketStore.setStrategyVersion(strategyVersion),
	setMode: (mode: "paper" | "live"): void => basketStore.setMode(mode),
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
