import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, type TodayFixture } from "../api/goalx";
import { invalidateBets } from "../lib/query-keys";
import { errorText, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";
import { isPickable, type Leg, MAX_LEGS, makeLeg, type PickableFixture, type Selection } from "./had-quote-ui";
import { parlayAdviceInput, StakeAdviceNote } from "./stake-advice";
import {
	Drawer,
	DrawerClose,
	DrawerContent,
	DrawerDescription,
	DrawerFooter,
	DrawerHeader,
	DrawerTitle,
} from "./ui/drawer";

/**
 * had 选注篮共享件（lean-audit 票 04；review-20260928 票 06 收编研究页）：
 * 场次页/胜平负页/单场研究页三页共用。
 * - useHadBasket：6 状态 + 规则前置（同场替换/MAX_LEGS/单固首腿）+ 建议仓位
 *   输入 + createBet mutation（invalidate ["bets"]）；建议仓位的 leg EV 经
 *   第三参注入（缺省 = 列表页 fixture.ev 字段口径，研究页注入共识口径）；
 * - BasketBar：底部常驻条（已选摘要 + 组合赔率）；
 * - BasketDrawer：右侧抽屉（腿列表/建议仓位/模式/金额/策略版本/提交）。
 * 两页文本差异（空态提示）经 emptyHint prop 注入；进球玩法页是独立单关
 * 变体（同场多注/独立提交/goals-* testid），不进本共享件。
 */

export function useHadBasket(now: number, fixtures: TodayFixture[], legEvOverride?: (leg: Leg) => number | null) {
	const [legs, setLegs] = useState<Leg[]>([]);
	const [basketOpen, setBasketOpen] = useState(false);
	const [stake, setStake] = useState("100");
	const [strategyVersion, setStrategyVersion] = useState("");
	const [mode, setMode] = useState<"paper" | "live">("paper");
	const [message, setMessage] = useState<string | null>(null);
	const queryClient = useQueryClient();

	const combinedOdds = legs.reduce((acc, leg) => acc * leg.odds, 1);

	// 建议仓位输入（票 wb-06）：1 腿=单关口径；2 腿=串关联合口径（整注一个 Kelly，不分腿）
	const legEv =
		legEvOverride ??
		((leg: Leg): number | null => {
			const fixture = fixtures.find((row) => row.fixture_id === leg.fixture_id);
			return fixture?.ev?.[leg.selection] ?? null;
		});
	const basketAdvice =
		legs.length === 2
			? {
					...parlayAdviceInput(legs.map((leg) => ({ ev: legEv(leg), odds: leg.odds }))),
					note: "串关注额=单关口径：联合 EV/联合赔率整注计算（腿间独立性假设），不分腿",
				}
			: legs.length === 1 && legs[0] !== undefined
				? { ev: legEv(legs[0]), odds: legs[0].odds, note: "单关口径：共识 EV（欧共识×竞彩价−1）" }
				: null;

	/** 规则前置（票 04）：同场换选=替换+提示；2串1 上限=行内提示；非单固首腿=提示；停售/已开赛=忽略。 */
	function pick(fixture: PickableFixture, selection: Selection, odds: number) {
		if (!isPickable(fixture, now)) {
			return;
		}
		const existing = legs.find((leg) => leg.fixture_id === fixture.fixture_id);
		if (existing && existing.selection === selection) {
			setLegs(legs.filter((leg) => leg.fixture_id !== fixture.fixture_id));
			setMessage(null);
			return;
		}
		if (legs.length >= MAX_LEGS) {
			setMessage(`已达 ${MAX_LEGS}串1 上限——先在选注篮移除一腿`);
			return;
		}
		if (existing) {
			setLegs(legs.map((leg) => (leg.fixture_id === fixture.fixture_id ? makeLeg(fixture, selection, odds) : leg)));
			setMessage("同场只能选一腿（竞彩禁同场串关），已替换原选择");
			return;
		}
		if (legs.length === 0 && fixture.had_quote && fixture.had_quote.single_eligible !== true) {
			setMessage(`${fixture.match_code} 非单固：只能作为串关第二腿（先选单固场，提交时服务器校验）`);
		} else {
			setMessage(null);
		}
		setLegs([...legs, makeLeg(fixture, selection, odds)]);
	}

	function removeLeg(fixtureId: number) {
		setLegs(legs.filter((leg) => leg.fixture_id !== fixtureId));
		setMessage(null);
	}

	const createSuggestion = useMutation({
		mutationFn: () =>
			createBet({
				mode,
				stake: Number(stake),
				strategy_version: strategyVersion.trim() === "" ? null : strategyVersion.trim(),
				legs: legs.map((leg) => ({
					fixture_id: leg.fixture_id,
					market_code: "had",
					selection_code: leg.selection,
					locked_odds: leg.odds,
				})),
			}),
		onSuccess: (bet) => {
			setMessage(`已建建议 #${bet.id}（${legs.length === 1 ? "单关" : "2串1"}）— 去投注页锁定`);
			setLegs([]);
			setBasketOpen(false);
			invalidateBets(queryClient);
		},
		onError: (error) => setMessage(`建注失败：${errorText(error)}`),
	});

	return {
		legs,
		setLegs,
		basketOpen,
		setBasketOpen,
		stake,
		setStake,
		strategyVersion,
		setStrategyVersion,
		mode,
		setMode,
		message,
		setMessage,
		combinedOdds,
		legEv,
		basketAdvice,
		pick,
		removeLeg,
		createSuggestion,
	};
}

/** 抽屉底部三输入（模式/金额/策略版本）：had 篮与进球单关篮共用（票 08 批二），
 * testid 前缀与金额档（标签/最低注）经 prop 注入，e2e 锚不变。 */
export function BasketFormFields({
	testid,
	state,
	stakeLabel,
	stakeMin,
}: {
	testid: string;
	state: Pick<HadBasket, "mode" | "setMode" | "stake" | "setStake" | "strategyVersion" | "setStrategyVersion">;
	stakeLabel: string;
	stakeMin: number;
}) {
	const { mode, setMode, stake, setStake, strategyVersion, setStrategyVersion } = state;
	return (
		<div className="flex flex-wrap items-end gap-3">
			<label className="flex flex-col gap-1 text-xs">
				<span className="text-muted-foreground">模式</span>
				<select
					data-testid={`${testid}-mode`}
					className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
					value={mode}
					onChange={(event) => setMode(event.target.value === "live" ? "live" : "paper")}
				>
					<option value="paper">纸面</option>
					<option value="live">真金</option>
				</select>
			</label>
			<label className="flex flex-col gap-1 text-xs">
				<span className="text-muted-foreground">{stakeLabel}</span>
				<input
					required
					type="number"
					min={stakeMin}
					step="0.01"
					data-testid={`${testid}-stake`}
					className="w-24 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
					value={stake}
					onChange={(event) => setStake(event.target.value)}
				/>
			</label>
			<label className="flex flex-col gap-1 text-xs">
				<span className="text-muted-foreground">策略版本(可选)</span>
				<input
					data-testid={`${testid}-strategy`}
					placeholder="手动"
					className="w-32 rounded-md border border-input bg-background px-2 py-1.5 text-sm"
					value={strategyVersion}
					onChange={(event) => setStrategyVersion(event.target.value)}
				/>
			</label>
		</div>
	);
}

export type HadBasket = ReturnType<typeof useHadBasket>;

/** 底部常驻选注条：浏览全程可见已选腿摘要 + 组合赔率。 */
export function BasketBar({ basket, emptyHint }: { basket: HadBasket; emptyHint: string }) {
	const { legs, combinedOdds, setBasketOpen } = basket;
	return (
		<div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
			<div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
				<span className="text-sm">
					选注篮{" "}
					<span className={`${TABULAR_NUMS} font-medium`} data-testid="basket-count">
						{legs.length}/{MAX_LEGS}
					</span>
				</span>
				{legs.length > 0 ? (
					<span
						className={`${TABULAR_NUMS} hidden text-xs text-muted-foreground sm:inline`}
						data-testid="basket-summary"
					>
						{legs.map((leg) => `${leg.match_code} ${SELECTION_LABELS[leg.selection]}`).join(" × ")}
						{legs.length === MAX_LEGS ? ` · 组合赔率 ${combinedOdds.toFixed(2)}` : ""}
					</span>
				) : (
					<span className="hidden text-xs text-muted-foreground sm:inline">{emptyHint}</span>
				)}
				<button
					type="button"
					data-testid="basket-open"
					className="ml-auto rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
					disabled={legs.length === 0}
					onClick={() => setBasketOpen(true)}
				>
					展开选注篮
				</button>
			</div>
		</div>
	);
}

/** 右侧选注篮抽屉：腿列表 + 建议仓位 + 模式/金额/策略版本 + 提交。 */
export function BasketDrawer({
	basket,
	bankroll,
	emptyHint,
}: {
	basket: HadBasket;
	bankroll: number | null;
	emptyHint: string;
}) {
	const { legs, basketOpen, setBasketOpen, mode, basketAdvice, removeLeg, createSuggestion } = basket;
	return (
		<Drawer open={basketOpen} onOpenChange={setBasketOpen} swipeDirection="right">
			<DrawerContent className="mx-0 w-full sm:max-w-md">
				<DrawerHeader>
					<DrawerTitle>选注篮</DrawerTitle>
					<DrawerDescription>
						{legs.length === 1 && legs[0]?.single_eligible !== true
							? "当前腿非单固，只能作为串关腿（提交时服务器校验）"
							: "单关须单固；2串1 须不同场次且共同可投（提交时服务器校验）"}
					</DrawerDescription>
				</DrawerHeader>
				<div className="flex-1 overflow-y-auto p-4">
					{legs.length === 0 ? (
						<p className="text-sm text-muted-foreground">{emptyHint}</p>
					) : (
						<ul className="space-y-2">
							{legs.map((leg) => (
								<li
									key={leg.fixture_id}
									className="flex items-center justify-between rounded-md border border-border p-3 text-sm"
									data-testid="basket-leg"
								>
									<span>
										<span className="text-xs text-muted-foreground">{leg.match_code}</span>
										<br />
										{leg.home_team} vs {leg.away_team}
										<br />
										<span className="font-medium">{SELECTION_LABELS[leg.selection]}</span>
										<span className={`${TABULAR_NUMS} ml-2 text-muted-foreground`}>@{leg.odds.toFixed(2)}</span>
									</span>
									<button
										type="button"
										className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
										aria-label={`移除 ${leg.match_code}`}
										onClick={() => removeLeg(leg.fixture_id)}
									>
										移除
									</button>
								</li>
							))}
						</ul>
					)}
					{basketAdvice ? (
						<div className="mt-3">
							<StakeAdviceNote
								mode={mode}
								bankroll={bankroll}
								ev={basketAdvice.ev}
								odds={basketAdvice.odds}
								note={basketAdvice.note}
								testid="basket-stake-advice"
							/>
						</div>
					) : null}
				</div>
				<DrawerFooter>
					<BasketFormFields testid="basket" state={basket} stakeLabel="金额(¥)" stakeMin={1} />
					<button
						type="button"
						data-testid="basket-submit"
						className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
						disabled={createSuggestion.isPending || legs.length === 0}
						onClick={() => createSuggestion.mutate()}
					>
						建立建议
					</button>
					<div className="flex items-center justify-between text-xs">
						<DrawerClose className="rounded-md px-1 py-0.5 text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
							继续浏览
						</DrawerClose>
						<Link to="/bets" className="text-primary underline-offset-2 hover:underline">
							去投注页锁定 →
						</Link>
					</div>
				</DrawerFooter>
			</DrawerContent>
		</Drawer>
	);
}
