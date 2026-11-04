/** 彩池返奖率（镜像后端 data.pool.POOL_RETURN_RATE，口径同源）。 */
export const POOL_RETURN_RATE = 0.65;

/**
 * 彩池票级 EV（报告审计票 08，人裁决：修公式 + 维持降级口径）：
 * ticketProb=∏p、ticketShare=∏s、ticketOdds=R/∏s、ticketEV=∏p×R/∏s−1
 * ——返奖率整票只扣一次（后端 pool_strategy.ticket_view 正典的镜像）。
 * 场级份额折算的"赔率"已含 R，严禁再进串关连乘（R^N 会把 +58.69% 的
 * 票算成 −56.42%，正负 EV 判反）。任一腿缺概率/份额 → null
 * （缺份额 = 建议降级，语义维持）。HAD 固赔串关仍走 parlayAdviceInput，
 * 与本函数分属两种口径。
 */
export function poolTicketAdvice(
	legs: ReadonlyArray<{ prob: number | null; share: number | null }>,
): { ev: number; odds: number } | null {
	let prob = 1;
	let share = 1;
	for (const leg of legs) {
		if (leg.prob === null || leg.share === null || leg.share <= 0) {
			return null;
		}
		prob *= leg.prob;
		share *= leg.share;
	}
	const odds = POOL_RETURN_RATE / share;
	return { ev: prob * odds - 1, odds };
}
