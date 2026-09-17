import { useQuery } from "@tanstack/react-query";
import { fetchStakeAdvice, type StakeAdviceInput } from "../api/goalx";
import { TABULAR_NUMS } from "../lib/ui";
import { GlossaryTerm } from "./glossary-term";

/**
 * 建议仓位展示（票 wb-06，只读不改单）：选注篮与组合头部共用一块。
 *
 * 数据源 = 后端 stake-advice 端点（单一事实来源：flat 红线 / ¼Kelly /
 * 1–5% 截断 / EV≤0 → ¥0 全在服务端）；前端只做输入组装与降级说明——
 * bankroll 读取失败、EV/赔率缺数据时不伪造建议（诚实留白）。
 *
 * 串关：调用方传联合赔率/联合 EV（`parlayAdviceInput`），注额=单关口径
 * （整注一个 Kelly，不分腿），由 note 自行标注。
 */

/** 一腿的建议输入（EV 缺数据时诚实置 null——无 EV 不给比例建议）。 */
export type AdviceLeg = {
	ev: number | null | undefined;
	odds: number;
};

/** 串关联合输入：联合赔率 = Π odds，联合 EV = Π(1+ev) − 1（腿间独立性假设）。 */
export function parlayAdviceInput(legs: AdviceLeg[]): { ev: number | null; odds: number } {
	const odds = legs.reduce((acc, leg) => acc * leg.odds, 1);
	let ev: number | null = 1;
	for (const leg of legs) {
		if (leg.ev === null || leg.ev === undefined) {
			ev = null;
			break;
		}
		ev *= 1 + leg.ev;
	}
	return { ev: ev === null ? null : ev - 1, odds };
}

export type StakeAdviceNoteProps = {
	mode: "paper" | "live";
	/** bankroll 真金余额；null = 资金池读取失败（诚实降级）。 */
	bankroll: number | null;
	ev: number | null;
	odds: number | null;
	/** live 单注上限（1%–5%，默认 5%；票 wb-07 三档复用）。 */
	capFraction?: number;
	/** 口径补充（如"串关注额=单关口径""按最优注口径"）。 */
	note?: string | undefined;
	testid: string;
};

export function StakeAdviceNote({ mode, bankroll, ev, odds, capFraction, note, testid }: StakeAdviceNoteProps) {
	const enabled = bankroll !== null && ev !== null && odds !== null;
	const cap = capFraction ?? 0.05;
	const body: StakeAdviceInput | null =
		enabled && bankroll !== null && ev !== null && odds !== null
			? { mode, bankroll, ev, odds, cap_fraction: cap }
			: null;
	const adviceQuery = useQuery({
		queryKey: ["stake-advice", mode, bankroll, ev, odds, cap],
		queryFn: () => fetchStakeAdvice(body as StakeAdviceInput),
		enabled,
	});

	let detail: string | undefined;
	if (bankroll === null) {
		detail = "资金池读取失败——建议仓位暂缺（去资金页重试后再看）";
	} else if (ev === null || odds === null) {
		detail = "该选择缺 EV/赔率数据——不给注额建议（无 EV 不伪造建议，见词典 EV 词条）";
	} else if (adviceQuery.isPending) {
		detail = "建议计算中…";
	} else if (adviceQuery.isError) {
		detail = "建议服务不可用——暂缺注额建议（可重试）";
	} else {
		detail = adviceQuery.data.reason;
	}

	return (
		<div data-testid={testid} className="rounded-md border border-border bg-muted/40 p-3 text-xs">
			<p className="font-medium text-foreground">
				<GlossaryTerm id="stake-advice">建议仓位</GlossaryTerm>（只读）：
				{adviceQuery.data ? (
					<span className={`${TABULAR_NUMS} ml-1`}>¥{adviceQuery.data.stake.toFixed(2)}</span>
				) : (
					<span className="ml-1 text-muted-foreground">—</span>
				)}
				<span className="ml-1 font-normal text-muted-foreground">
					（{mode === "paper" ? "纸面 flat" : "真金 ¼Kelly"}·<GlossaryTerm id="kelly">Kelly</GlossaryTerm>）
				</span>
			</p>
			<p className="mt-1 text-muted-foreground" data-testid={`${testid}-reason`}>
				{detail}
			</p>
			{note ? <p className="mt-1 text-muted-foreground">口径：{note}</p> : null}
		</div>
	);
}
