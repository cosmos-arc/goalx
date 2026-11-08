import type { QueryClient } from "@tanstack/react-query";
import type { Bet } from "../api/goalx";
import { withFreshRequestIds } from "./basket-store";
import { invalidateBets } from "./query-keys";
import { betTermsLabel, conflictRestoreMessage, currentQuotes, intentConflict } from "./ui";

/**
 * 篮提交共享环（票 08 续作）：had 整注/had 逐腿单关/goals 逐腿单关三路共用
 * 一个逐单元派发环——意图冲突（票 09）/价变 409（票 04 裁决①）/普通失败
 * 三族政策只写一次（此前 had/goals 两份平行实现已两处漂移：goals 旧价 409
 * 死循环）。单元划分=票型：整注=[全部腿] 一单元，逐腿单关=每腿一单元；
 * 提交幂等（票 06）由各路 post 的 client_request_id 承担（单关=腿键，
 * 整注=腿键有序拼接），同键重试服务端不落双行。
 */

/** 提交单元腿最小形（幂等键 + 价变失效用的场次键）。 */
export type SubmitLeg = { fixture_id: number; request_id: string };

export type BasketSubmitConfig<L extends SubmitLeg> = {
	client: QueryClient;
	/** 提交单元（票型分叉在调用方：整注一单元 / 单关每腿一单元）。 */
	units: L[][];
	post: (unit: L[]) => Promise<Bet>;
	/** 单元消息内缀（goals 单关「（场次 选项）」；had 两路无）。 */
	infix?: (unit: L[]) => string;
	/** 冲突/价变消息的单元前缀（review 修：had 整注旧文案无「第 N 笔」——
	 * 单元路径不回退 had 行为，整注传 () => ""）。 */
	unitLabel?: (index: number, unit: L[]) => string;
	/** 全部单元建成后的成功文案。 */
	successNote: (created: Bet[], labels: string[]) => string;
	/** 普通失败（非冲突非价变）的失败文案。 */
	failureNote: (error: unknown, created: Bet[], index: number) => string;
	/** 价变后价格源失效（goals 额外刷 goals-market；had=今日场次+研究页）。 */
	invalidatePriceSource: (client: QueryClient, fixtureIds: number[]) => void;
	/** 冲突恢复/失败留篮写（提交路径自写，不走冻结门）。 */
	replaceLegs: (legs: L[]) => void;
	report: (message: string) => void;
	closeBasket: () => void;
};

export async function submitBasketUnits<L extends SubmitLeg>(config: BasketSubmitConfig<L>): Promise<void> {
	const { client, units, post } = config;
	const infix = config.infix ?? (() => "");
	const unitLabel = config.unitLabel ?? ((index: number) => `第 ${index + 1} 笔`);
	const allLegs = units.flat();
	if (allLegs.length === 0) {
		return;
	}
	const created: Bet[] = [];
	const labels: string[] = []; // 服务器实际返回的模式/金额（票 09：回带注≠本次输入）
	for (const [index, unit] of units.entries()) {
		try {
			const bet = await post(unit);
			created.push(bet);
			labels.push(betTermsLabel(bet));
		} catch (error) {
			// 意图冲突（票 09）：同键原提交其实已成功——恢复原结果、失败单元换新键
			// 留篮（改条款重提不换键=双注反模式；冲突确认后才换），未提交单元不动
			const conflict = intentConflict(error);
			if (conflict !== null) {
				invalidateBets(client);
				config.replaceLegs([...withFreshRequestIds(unit), ...units.slice(index + 1).flat()]);
				config.report(
					conflictRestoreMessage(`${unitLabel(index, unit)}${infix(unit)}`, conflict) +
						(created.length > 0 ? `；此前已建 #${created.map((bet) => bet.id).join("、#")}` : ""),
				);
				return;
			}
			const quotes = currentQuotes(error);
			if (quotes !== null && quotes.length > 0) {
				// 价变（票 04 裁决①）：清篮+失效价格源，用户按新价重选——旧价不经
				// 确认直进成绩的红线在服务器侧；已成注保留账面（消息与列表可见）
				config.replaceLegs([]);
				config.closeBasket();
				if (created.length > 0) {
					invalidateBets(client);
				}
				config.invalidatePriceSource(
					client,
					allLegs.map((leg) => leg.fixture_id),
				);
				config.report(
					`${unitLabel(index, unit)}${infix(unit)}报价已变（${quotes
						.map((q) => `${q.selection_code} → ${q.server_odds.toFixed(2)}`)
						.join(
							"、",
						)}）——已刷新列表${created.length > 0 ? `，已建 #${created.map((bet) => bet.id).join("、#")}` : ""}；请按新价重新选注`,
				);
				return;
			}
			// 普通失败：失败单元起整段留篮（同键重试只补发失败腿）；已成注保留账面
			config.replaceLegs(units.slice(index).flat());
			if (created.length > 0) {
				invalidateBets(client);
			}
			config.report(config.failureNote(error, created, index));
			return;
		}
	}
	config.report(config.successNote(created, labels));
	config.replaceLegs([]);
	config.closeBasket();
	invalidateBets(client);
}
