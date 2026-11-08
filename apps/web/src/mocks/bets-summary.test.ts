import { expect, test } from "vitest";
import { summarizeBets } from "./bets-summary";

/** correctness F4：桶内原值累计、发射时才舍入——对齐服务端 summary.py（逐笔舍入 0.005+0.005 会漂成 0.02）。 */
test("sub-cent profits accumulate raw in-bucket and round once at emission", () => {
	const bets = [
		{
			mode: "paper",
			purchased: true,
			status: "won",
			settled_at: "2026-11-01T03:00:00+00:00",
			profit: 0.005,
			stake: 2,
			actual_stake: null,
			ev_snapshot: null,
		},
		{
			mode: "paper",
			purchased: true,
			status: "won",
			settled_at: "2026-11-01T05:00:00+00:00",
			profit: 0.005,
			stake: 2,
			actual_stake: null,
			ev_snapshot: null,
		},
	];
	const summary = summarizeBets(bets);
	expect(summary.paper.days[0]?.pnl).toBe(0.01);
	expect(summary.paper.total_pnl).toBe(0.01);
});
