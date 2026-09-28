import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { buildTargetPlan, type ColdVariants, generateColdVariants } from "../api/goalx";
import { errorText, TABULAR_NUMS } from "../lib/ui";
import { POOL_LABELS, type PoolPickEntry } from "./pool-slots";

/**
 * 彩池页两件工具（review-20260928 票 07 自 market-pool-page 拆出）：
 * - PoolGenerator：搏冷生成器（票 pool-v2/02）——基础票 + 冷度 1..N 贪心变体；
 * - PoolTargetPlanner：目标金额反推（票 pool-v2/03）——风险档票面 + 建议注数。
 * 共用 TicketRow 票行；两块各自的 mutation/消息状态收在本组件内，
 * 经 onAdopt 把采用票面回写给页面 picks；采用消息在组件本地呈现。
 */

/** 反推风险档标签（票 pool-v2/03）。 */
const RISK_LABELS: Record<string, string> = { steady: "稳", balanced: "中", bold: "搏" };

/** 生成器/反推票行：替换明细 + 命中概率/估计派彩/EV + 采用。 */
function TicketRow({
	label,
	ticket,
	testid,
	onAdopt,
}: {
	label: string;
	ticket: ColdVariants["base"];
	testid: string;
	onAdopt?: () => void;
}) {
	return (
		<div
			className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-border p-3 text-sm"
			data-testid={testid}
		>
			<span className="font-medium">{label}</span>
			{ticket.swaps.length > 0 ? (
				<span className="text-xs text-muted-foreground">
					{ticket.swaps
						.map(
							(swap) =>
								`第${swap.match_seq}场 ${POOL_LABELS[swap.from_code]}→${POOL_LABELS[swap.to_code]}（EV+${(swap.ev_gain * 100).toFixed(0)}%）`,
						)
						.join("；")}
				</span>
			) : null}
			{ticket.hit_prob !== null && ticket.hit_prob !== undefined ? (
				<span className={TABULAR_NUMS} title="各场概率连乘">
					命中 {(ticket.hit_prob * 100).toFixed(1)}%
				</span>
			) : (
				<span className="text-muted-foreground">命中率缺数据</span>
			)}
			{ticket.est_odds !== null && ticket.est_odds !== undefined ? (
				<span className={TABULAR_NUMS} title="返奖率 65% ÷ 各场份额连乘">
					@{ticket.est_odds.toFixed(0)}
				</span>
			) : null}
			{ticket.ev !== null && ticket.ev !== undefined ? (
				<span className={`${TABULAR_NUMS} ${ticket.ev > 0 ? "text-info" : "text-muted-foreground"}`}>
					EV{ticket.ev >= 0 ? "+" : ""}
					{(ticket.ev * 100).toFixed(0)}%
				</span>
			) : null}
			{onAdopt ? (
				<button
					type="button"
					data-testid={`${testid}-adopt`}
					className="ml-auto rounded-md border border-border px-2 py-1 text-xs"
					onClick={onAdopt}
				>
					采用
				</button>
			) : null}
		</div>
	);
}

/** picks 字典型 → 页面 Record<number, string>（票面 str(seq) 键转数字）。 */
function picksRecord(ticket: { picks: Record<string, string> }): Record<number, string> {
	return Object.fromEntries(Object.entries(ticket.picks).map(([seq, code]) => [Number(seq), code]));
}

export function PoolGenerator({
	activePeriod,
	pickedEntries,
	matchCount,
	onAdopt,
}: {
	activePeriod: string | undefined;
	pickedEntries: PoolPickEntry[];
	matchCount: number;
	onAdopt: (picks: Record<number, string>) => void;
}) {
	const [coldness, setColdness] = useState(2);
	const [generatorMessage, setGeneratorMessage] = useState<string | null>(null);
	const generatorMutation = useMutation({
		mutationFn: generateColdVariants,
		onSuccess: () => setGeneratorMessage(null),
	});
	const variants = generatorMutation.data;

	return (
		<section
			aria-labelledby="pool-generator-heading"
			className="mb-8 rounded-lg border border-border bg-card p-4"
			data-testid="pool-generator"
		>
			<h2 id="pool-generator-heading" className="text-sm font-medium">
				搏冷生成器
			</h2>
			<p className="mt-1 text-xs text-muted-foreground">
				基础票 = 当前全选（未选满时用各场最高概率），按「冷选项 EV − 基础向 EV」降序贪心替换；
				变体牺牲命中率抬估计派彩，采用前看 EV 与命中概率两栏。
			</p>
			<div className="mt-3 flex flex-wrap items-center gap-2">
				<label className="text-xs text-muted-foreground" htmlFor="pool-coldness">
					冷度（替换处数）
				</label>
				<select
					id="pool-coldness"
					data-testid="pool-coldness"
					value={coldness}
					onChange={(event) => setColdness(Number(event.target.value))}
					className="rounded-md border border-border bg-background px-2 py-1 text-sm"
				>
					<option value={1}>1 处</option>
					<option value={2}>2 处</option>
					<option value={3}>3 处</option>
				</select>
				<button
					type="button"
					data-testid="pool-generate"
					disabled={!activePeriod || generatorMutation.isPending}
					className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
					onClick={() =>
						generatorMutation.mutate({
							period_no: activePeriod ?? "",
							market_code: "ttt14",
							coldness,
							...(pickedEntries.length === matchCount && matchCount > 0
								? {
										base_picks: Object.fromEntries(
											pickedEntries.map(({ match, code }) => [String(match.match_seq), code]),
										),
									}
								: {}),
						})
					}
				>
					{generatorMutation.isPending ? "生成中…" : "生成变体"}
				</button>
				{generatorMutation.isError ? (
					<span className="text-xs text-destructive" data-testid="pool-generator-error">
						生成失败：{errorText(generatorMutation.error)}
					</span>
				) : null}
			</div>
			{variants ? (
				<div className="mt-3 space-y-2" data-testid="pool-variants">
					<p className="text-xs text-muted-foreground" data-testid="pool-generator-caliber">
						{variants.caliber}
					</p>
					<TicketRow
						label={`基础票（${Object.keys(variants.base.picks).length} 场）`}
						ticket={variants.base}
						testid="pool-variant-base"
					/>
					{variants.variants.map((variant) => (
						<TicketRow
							key={variant.swaps.map((s) => `${s.match_seq}${s.to_code}`).join("-")}
							label={`变体 ${variant.swaps.length}（${variant.swaps.length} 处冷门）`}
							ticket={variant}
							testid={`pool-variant-${variant.swaps.length}`}
							onAdopt={() => {
								onAdopt(picksRecord(variant));
								setGeneratorMessage(`已采用变体 ${variant.swaps.length} 作为当前选择`);
							}}
						/>
					))}
					{variants.variants.length === 0 ? (
						<p className="text-xs text-muted-foreground" data-testid="pool-variants-empty">
							本期无正期望冷门可替换（冷选项 EV 均为负或不如基础向）——跟大众是更优解。
						</p>
					) : null}
					{generatorMessage ? (
						<p className="text-xs text-info" data-testid="pool-generator-message">
							{generatorMessage}
						</p>
					) : null}
				</div>
			) : null}
		</section>
	);
}

export function PoolTargetPlanner({
	activePeriod,
	onAdopt,
}: {
	activePeriod: string | undefined;
	onAdopt: (picks: Record<number, string>) => void;
}) {
	const [targetAmount, setTargetAmount] = useState(10000);
	const [targetRisk, setTargetRisk] = useState<"steady" | "balanced" | "bold">("balanced");
	const [targetMessage, setTargetMessage] = useState<string | null>(null);
	const targetMutation = useMutation({ mutationFn: buildTargetPlan });
	const plan = targetMutation.data;

	return (
		<section
			aria-labelledby="pool-target-heading"
			className="mb-8 rounded-lg border border-border bg-card p-4"
			data-testid="pool-target"
		>
			<h2 id="pool-target-heading" className="text-sm font-medium">
				目标金额反推
			</h2>
			<p className="mt-1 text-xs text-muted-foreground">
				输入目标奖金，反推推荐票面与建议注数。估计派彩随最终池变——输出是"若命中估计得 X"，不承诺达成。
			</p>
			<div className="mt-3 flex flex-wrap items-center gap-2">
				<label className="text-xs text-muted-foreground" htmlFor="pool-target-amount">
					目标（¥）
				</label>
				<input
					id="pool-target-amount"
					data-testid="pool-target-amount"
					type="number"
					min={1}
					value={targetAmount}
					onChange={(event) => setTargetAmount(Number(event.target.value))}
					className="w-32 rounded-md border border-border bg-background px-2 py-1 text-sm"
				/>
				<select
					aria-label="风险档"
					data-testid="pool-target-risk"
					value={targetRisk}
					onChange={(event) => setTargetRisk(event.target.value as "steady" | "balanced" | "bold")}
					className="rounded-md border border-border bg-background px-2 py-1 text-sm"
				>
					<option value="steady">稳（14 场全稳）</option>
					<option value="balanced">中（任9 + 至多 1 冷）</option>
					<option value="bold">搏（任9 + 至多 3 冷）</option>
				</select>
				<button
					type="button"
					data-testid="pool-target-generate"
					disabled={!activePeriod || targetMutation.isPending || !Number.isFinite(targetAmount) || targetAmount <= 0}
					className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
					onClick={() =>
						targetMutation.mutate({
							period_no: activePeriod ?? "",
							market_code: "ttt14",
							target_amount: targetAmount,
							risk: targetRisk,
						})
					}
				>
					{targetMutation.isPending ? "反推中…" : "反推票面"}
				</button>
			</div>
			{plan ? (
				<div className="mt-3 space-y-2" data-testid="pool-target-result">
					<p className="text-xs text-muted-foreground">{plan.note}</p>
					<TicketRow
						label={`${RISK_LABELS[plan.risk]}档票面（${Object.keys(plan.ticket.picks).length} 场）`}
						ticket={plan.ticket}
						testid="pool-target-ticket"
						onAdopt={() => {
							onAdopt(picksRecord(plan.ticket));
							setTargetMessage("已采用反推票面作为当前选择");
						}}
					/>
					<p className="text-sm" data-testid="pool-target-units">
						单注估计派彩{" "}
						{plan.est_payout_per_unit === null || plan.est_payout_per_unit === undefined
							? "缺数据"
							: `¥${plan.est_payout_per_unit.toLocaleString("zh-CN")}`}
						，建议 <span className={TABULAR_NUMS}>{plan.suggested_units}</span> 注（¥2/注）达目标 ¥
						{targetAmount.toLocaleString("zh-CN")}
						{plan.target_reached ? "（单注即达标）" : ""}
					</p>
					{targetMessage ? (
						<p className="text-xs text-info" data-testid="pool-target-message">
							{targetMessage}
						</p>
					) : null}
				</div>
			) : null}
		</section>
	);
}
