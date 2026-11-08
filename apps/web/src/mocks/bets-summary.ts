import type { BetsDayBucket, BetsModeSummary, BetsSummary } from "../api/goalx";
import { beijingBusinessDate } from "../lib/ui";

/**
 * 聚合输入的最小结构面（Bet 与 as const fixture 数组均可赋值——不锚
 * Bet 全形，fixture 的 readonly legs 不至于把类型顶爆）。
 */
type SummarySource = {
	readonly mode: string;
	readonly purchased: boolean;
	readonly status: string;
	readonly settled_at: string | null;
	readonly profit: number | null;
	readonly stake: number;
	readonly actual_stake?: number | null;
	readonly ev_snapshot?: { readonly ev_consensus?: number | null; readonly ev_model?: number | null } | null;
};

/**
 * msw 测试面的 /bets/summary 聚合（iterate 票 10）：镜像服务端
 * betting/summary.py 口径（结算=已锁定且已结算；日桶=北京业务日；EV 双口径
 * 和/计数），让测试从同一份 Bet[] 派生一致的聚合 mock。服务端正确性由
 * 后端测试钉死；本函数只保证前后端 mock 同源，口径漂移以后端测试为准。
 */
const round2 = (value: number): number => Math.round(value * 100) / 100;
const round6 = (value: number): number => Math.round(value * 1e6) / 1e6;

export function summarizeBets(bets: readonly SummarySource[]): BetsSummary {
	const face = (mode: "paper" | "live"): BetsModeSummary => {
		const scoped = bets.filter((bet) => bet.mode === mode);
		const settled = scoped.filter((bet) => bet.purchased && bet.settled_at !== null);
		const buckets = new Map<string, BetsDayBucket>();
		for (const bet of settled) {
			const date = beijingBusinessDate(new Date(bet.settled_at ?? "").getTime());
			const bucket =
				buckets.get(date) ??
				({
					date,
					settled_n: 0,
					won_n: 0,
					decided_n: 0,
					pnl: 0,
					sum_ev_consensus: 0,
					n_ev_consensus: 0,
					sum_ev_model: 0,
					n_ev_model: 0,
				} satisfies BetsDayBucket);
			bucket.settled_n += 1;
			if (bet.status === "won") {
				bucket.won_n += 1;
			}
			if (bet.status !== "void") {
				bucket.decided_n += 1;
			}
			// correctness F4：桶内原值累计、发射时才舍入（对齐服务端
			// summary.py——逐笔舍入在子分利润下会漂出服务端不产的数）
			bucket.pnl += bet.profit ?? 0;
			const consensus = bet.ev_snapshot?.ev_consensus ?? null;
			if (consensus !== null) {
				bucket.sum_ev_consensus += consensus;
				bucket.n_ev_consensus += 1;
			}
			const model = bet.ev_snapshot?.ev_model ?? null;
			if (model !== null) {
				bucket.sum_ev_model += model;
				bucket.n_ev_model += 1;
			}
			buckets.set(date, bucket);
		}
		const days = [...buckets.values()]
			.map((bucket) => ({
				...bucket,
				pnl: round2(bucket.pnl),
				sum_ev_consensus: round6(bucket.sum_ev_consensus),
				sum_ev_model: round6(bucket.sum_ev_model),
			}))
			.sort((a, b) => (a.date < b.date ? -1 : 1));
		return {
			mode,
			bets_n: scoped.length,
			open_n: scoped.filter((bet) => bet.status === "open").length,
			settled_n: settled.length,
			won_n: days.reduce((acc, day) => acc + day.won_n, 0),
			decided_n: days.reduce((acc, day) => acc + day.decided_n, 0),
			total_pnl: round2(days.reduce((acc, day) => acc + day.pnl, 0)),
			stake_settled: round2(settled.reduce((acc, bet) => acc + (bet.actual_stake ?? bet.stake), 0)),
			sum_ev_consensus: round6(days.reduce((acc, day) => acc + day.sum_ev_consensus, 0)),
			n_ev_consensus: days.reduce((acc, day) => acc + day.n_ev_consensus, 0),
			sum_ev_model: round6(days.reduce((acc, day) => acc + day.sum_ev_model, 0)),
			n_ev_model: days.reduce((acc, day) => acc + day.n_ev_model, 0),
			days,
		};
	};
	return { paper: face("paper"), live: face("live") };
}
