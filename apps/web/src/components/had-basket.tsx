import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { createBet, type TodayFixture } from "../api/goalx";
import { basketActions, requestIdForLegs, useBasket } from "../lib/basket-store";
import { type SlipForm, type SlipFormPlan, slipFormPlan, slipFormSummary } from "../lib/combo-engine";
import { fixtureResearchKey, invalidateBets } from "../lib/query-keys";
import { currentQuotes, errorText, localTime, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";
import {
	isPickable,
	kickoffInfo,
	type Leg,
	MAX_LEGS,
	makeLeg,
	type PickableFixture,
	type ProbabilitySnapshot,
	type Selection,
	todayProbabilitySnapshot,
} from "./had-quote-ui";
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
 * - useHadBasket：篮草稿走全站单源 store（票 06：路由切换/刷新可恢复，
 *   与进球页共用一份草稿）+ 规则前置（同场替换/MAX_LEGS/单固首腿）+
 *   建议仓位输入 + createBet mutation（invalidate ["bets"]）；腿 EV 只读
 *   入篮时概率快照（报告审计票 06：第三参=pick 时快照来源，缺省今日列表
 *   eu_prob，研究页注入共识口径——跨场腿 EV 不随当前页漂移，缺快照=
 *   依据缺失）；
 * - 票型（report-audit 票 02）：篮内 ≥2 腿时显式票型——两笔单关（逐腿独立
 *   建注）/一注 2串1（整注）；预览与提交同走 ticketPlan 单源，无静默切换；
 * - 提交幂等（票 06）：单关腿=腿 request_id、整注=腿键有序拼接，服务端
 *   同键不落双行——批量一成一败后重试只补发失败腿且不重复已建注；
 * - BasketBar：底部常驻条（已选摘要 + 组合赔率 + 跨玩法草稿提示）；
 * - BasketDrawer：右侧抽屉（腿列表含开球时间/票型/建议仓位/模式/金额/
 *   策略版本/提交）。两页文本差异（空态提示）经 emptyHint prop 注入。
 */

/** 票型记忆键（票 02 人裁决：记住上次选择，首次默认单关合计=引擎本义；
 * 卡上选择与篮内选择同一份记忆——带入与手选共用一个默认，无分叉）。 */
const SLIP_FORM_STORAGE_KEY = "goalx:slip-form";

function readStoredSlipForm(): SlipForm {
	try {
		return globalThis.localStorage?.getItem(SLIP_FORM_STORAGE_KEY) === "parlay" ? "parlay" : "singles";
	} catch {
		return "singles"; // 隐私模式/禁存储：回默认（theme.ts 同款降级）
	}
}

function persistSlipForm(form: SlipForm): void {
	try {
		globalThis.localStorage?.setItem(SLIP_FORM_STORAGE_KEY, form);
	} catch {
		// 存不进就算了——本次会话内状态仍生效
	}
}

/**
 * 入篮时概率快照来源（报告审计票 06 人裁决）：pick 时逐腿落快照（值+
 * 来源+时点），篮内 EV/预览只用快照——切研究页另一场 EV 不随当前页漂移，
 * 快照缺失=依据缺失（不用当前页概率顶替、不用 0 冒充）。缺省来源=今日
 * 列表 eu_prob；研究页注入共识口径。
 */
export type ProbabilityAtPick = (fixture: PickableFixture, selection: Selection) => ProbabilitySnapshot | null;

export function useHadBasket(now: number, fixtures: TodayFixture[], probabilityAtPick?: ProbabilityAtPick) {
	// 票 06：草稿单源——had 腿/金额/模式/策略版本全站一份（localStorage 持久化）
	const { hadLegs: legs, stake, strategyVersion, mode } = useBasket();
	const [basketOpen, setBasketOpen] = useState(false);
	const [message, setMessage] = useState<string | null>(null);
	// 票型状态 = 全 app 一份记忆（读 localStorage，缺省单关合计）；卡/篮
	// 两处选择器都是这份状态的视图（票 02：带入什么票型就提交什么票型）
	const [slipForm, setSlipForm] = useState<SlipForm>(readStoredSlipForm);

	function chooseSlipForm(form: SlipForm) {
		setSlipForm(form);
		persistSlipForm(form);
	}
	const setLegs = basketActions.setHadLegs;
	const queryClient = useQueryClient();

	const combinedOdds = legs.reduce((acc, leg) => acc * leg.odds, 1);

	// 票 06：快照来源缺省=今日列表共识（todayProbabilitySnapshot 单一出处）
	const snapshotFor: ProbabilityAtPick =
		probabilityAtPick ??
		((fixture, selection) => {
			const row = fixtures.find((item) => item.fixture_id === fixture.fixture_id);
			return row ? todayProbabilitySnapshot(row, selection) : null;
		});
	// 票 06：篮内 EV 只读入篮快照——不查当前页/列表（跨场腿不冒充）
	const legEv = (leg: Leg): number | null => (leg.probability != null ? leg.probability.value * leg.odds - 1 : null);
	const multiLeg = legs.length >= 2;
	const firstLeg = legs[0];
	const basketAdvice =
		multiLeg && slipForm === "singles" && firstLeg !== undefined
			? {
					ev: legEv(firstLeg),
					odds: firstLeg.odds,
					note: "单关注额口径：每注独立（示例第一腿，第二腿同法各算各的）",
				}
			: multiLeg
				? {
						...parlayAdviceInput(legs.map((leg) => ({ ev: legEv(leg), odds: leg.odds }))),
						note: "串关注额=单关口径：联合 EV/联合赔率整注计算（腿间独立性假设），不分腿",
					}
				: legs.length === 1 && legs[0] !== undefined
					? { ev: legEv(legs[0]), odds: legs[0].odds, note: "单关口径：共识 EV（欧共识×竞彩价−1）" }
					: null;

	/** 票级预览（票 02 单源）：抽屉展示的数字与提交结果同一函数；提交循环
	 * 的载荷（legs + 金额输入）即此处输入——预览与落库不各算各的。 */
	const planTicketLegs =
		legs.length === 0 ? null : legs.map((leg) => ({ ev: legEv(leg) ?? 0, stake: Number(stake) || 0 }));
	// EV 缺失的腿按 0 估（诚实降级提示，不静默）：预览标注而非谎称精确
	const planMissingEv = legs.length > 0 && legs.some((leg) => legEv(leg) === null);

	/** 规则前置（票 04；票 08① 调序）：同场换选=替换+提示（先于 2串1 上限——
	 * 换选不占新名额，腿满时可直接切换已选场的方向）；再点同选项=取消；
	 * 非单固首腿=提示；停售/已开赛=忽略。 */
	function pick(fixture: PickableFixture, selection: Selection, odds: number) {
		if (!isPickable(fixture, now) || createSuggestion.isPending) {
			return; // 提交中篮内容冻结（票 02：逐腿派发期间所见即所提交）
		}
		const existing = legs.find((leg) => leg.fixture_id === fixture.fixture_id);
		if (existing && existing.selection === selection) {
			setLegs(legs.filter((leg) => leg.fixture_id !== fixture.fixture_id));
			setMessage(null);
			return;
		}
		if (existing) {
			setLegs(
				legs.map((leg) =>
					leg.fixture_id === fixture.fixture_id
						? makeLeg(fixture, selection, odds, snapshotFor(fixture, selection))
						: leg,
				),
			);
			setMessage("同场只能选一腿（竞彩禁同场串关），已替换原选择");
			return;
		}
		if (legs.length >= MAX_LEGS) {
			setMessage(`已达 ${MAX_LEGS}串1 上限——先在选注篮移除一腿`);
			return;
		}
		if (legs.length === 0 && fixture.had_quote && fixture.had_quote.single_eligible !== true) {
			setMessage(`${fixture.match_code} 非单固：只能作为串关第二腿（先选单固场，提交时服务器校验）`);
		} else {
			setMessage(null);
		}
		setLegs([...legs, makeLeg(fixture, selection, odds, snapshotFor(fixture, selection))]);
	}

	function removeLeg(fixtureId: number) {
		if (createSuggestion.isPending) {
			return; // 同 pick：飞行中锁定编辑
		}
		setLegs(legs.filter((leg) => leg.fixture_id !== fixtureId));
		setMessage(null);
	}

	/** 价变后刷新全部选注面（票 06）：今日列表 + 逐场研究页报价查询——
	 * 停留在研究页重选时页面拿新价，旧价 409 确认流死循环不再。 */
	function invalidateLegQuotes() {
		void queryClient.invalidateQueries({ queryKey: ["today-fixtures"] });
		for (const leg of legs) {
			void queryClient.invalidateQueries({ queryKey: fixtureResearchKey(leg.fixture_id) });
		}
	}

	const createSuggestion = useMutation({
		mutationFn: async () => {
			const strategy = strategyVersion.trim() === "" ? null : strategyVersion.trim();
			const legPayload = (leg: Leg) => ({
				fixture_id: leg.fixture_id,
				market_code: "had",
				selection_code: leg.selection,
				locked_odds: leg.odds,
			});
			const post = (betLegs: Leg[]) =>
				createBet({
					mode,
					stake: Number(stake),
					strategy_version: strategy,
					// 票 06 幂等：腿键稳定（单关=腿键；整注=腿键有序拼接），
					// 同键重试服务端回带既有注不落双行
					client_request_id: requestIdForLegs(betLegs),
					legs: betLegs.map(legPayload),
				});
			// 票型分叉（票 02）：parlay/单腿 = 一注整提（现行路径）；
			// singles 且 ≥2 腿 = 逐腿独立建注（同一注额），提交即票型，无静默切换
			if (!(slipForm === "singles" && legs.length >= 2)) {
				const bet = await post(legs);
				setMessage(`已建建议 #${bet.id}（${legs.length === 1 ? "单关" : `${legs.length}串1`}）— 去投注页锁定`);
				setLegs([]);
				setBasketOpen(false);
				invalidateBets(queryClient);
				return;
			}
			const created: number[] = [];
			for (const [index, leg] of legs.entries()) {
				try {
					const bet = await post([leg]);
					created.push(bet.id);
				} catch (error) {
					const quotes = currentQuotes(error);
					if (quotes !== null && quotes.length > 0) {
						// 价变（票 04 裁决①）：清篮 + 刷新列表按新价重选——与整注
						// 路径同流；已建成的注保留账面（消息与列表可见）。
						// ponytail: MAX_LEGS=2 下清篮无未尝试后缀可丢；扩上限时
						// 价变腿之后的未提交腿须留篮而非清空（correctness F2）
						setLegs([]);
						setBasketOpen(false);
						if (created.length > 0) {
							invalidateBets(queryClient);
						}
						invalidateLegQuotes();
						setMessage(
							`第 ${index + 1} 笔报价已变（${quotes
								.map((q) => `${q.selection_code} → ${q.server_odds.toFixed(2)}`)
								.join(
									"、",
								)}）——已刷新列表${created.length > 0 ? `，已建 #${created.join("、#")}` : ""}；请按新价重新选注`,
						);
						return;
					}
					// 非价变失败：已成注保留账面、失败腿留在篮内可重试
					setLegs(legs.slice(created.length));
					if (created.length > 0) {
						invalidateBets(queryClient); // 部分成注也要刷新列表（对账可见）
					}
					setMessage(
						`第 ${index + 1} 笔失败：${errorText(error)}${created.length > 0 ? `（已建 #${created.join("、#")}，失败腿留篮可重试）` : ""}`,
					);
					return;
				}
			}
			setMessage(`已建建议 #${created.join("、#")}（${created.length} 笔单关）— 去投注页锁定`);
			setLegs([]);
			setBasketOpen(false);
			invalidateBets(queryClient);
		},
		onError: (error) => {
			// 价变（票 04 裁决①）：清篮+刷新场次列表（新价随查询回到页面），
			// 用户按新价重新选注——旧价不经确认直进成绩的红线在服务器侧
			const quotes = currentQuotes(error);
			if (quotes !== null && quotes.length > 0) {
				setLegs([]);
				setBasketOpen(false);
				// 前缀失效今日场次（键形 ["today-fixtures", days]）+ 逐场研究页
				// 报价查询（票 06）——correctness F1：打错键=不刷新，页面停在
				// 旧价上确认流死循环
				invalidateLegQuotes();
				setMessage(
					`报价已变（${quotes
						.map((q) => `${q.selection_code} → ${q.server_odds.toFixed(2)}`)
						.join("、")}）——已刷新列表，请按新价重新选注`,
				);
				return;
			}
			setMessage(`建注失败：${errorText(error)}`);
		},
	});

	return {
		legs,
		setLegs,
		basketOpen,
		setBasketOpen,
		stake,
		setStake: basketActions.setStake,
		strategyVersion,
		setStrategyVersion: basketActions.setStrategyVersion,
		mode,
		setMode: basketActions.setMode,
		message,
		setMessage,
		slipForm,
		chooseSlipForm,
		combinedOdds,
		legEv,
		basketAdvice,
		planTicketLegs,
		planMissingEv,
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

/** 票型选择器（票 02，卡/篮两用）：两票型预览并列（单源摘要），当前票型可选。 */
export function SlipFormChooser({
	form,
	onChoose,
	singlesPlan,
	parlayPlan,
	testidPrefix,
	missingEvNote = false,
}: {
	form: SlipForm;
	onChoose: (form: SlipForm) => void;
	singlesPlan: SlipFormPlan | null;
	parlayPlan: SlipFormPlan | null;
	testidPrefix: string;
	missingEvNote?: boolean;
}) {
	return (
		<div className="space-y-2 rounded-md border border-border p-3" data-testid={`${testidPrefix}-forms`}>
			<div className="flex gap-2" role="radiogroup" aria-label="票型">
				<label className="flex flex-1 cursor-pointer items-center gap-2 rounded border border-border px-2 py-1.5 text-sm has-[:checked]:border-primary">
					<input
						type="radio"
						name={`${testidPrefix}-slip-form`}
						data-testid={`${testidPrefix}-form-singles`}
						checked={form === "singles"}
						onChange={() => onChoose("singles")}
					/>
					两笔单关
				</label>
				<label className="flex flex-1 cursor-pointer items-center gap-2 rounded border border-border px-2 py-1.5 text-sm has-[:checked]:border-primary">
					<input
						type="radio"
						name={`${testidPrefix}-slip-form`}
						data-testid={`${testidPrefix}-form-parlay`}
						checked={form === "parlay"}
						onChange={() => onChoose("parlay")}
					/>
					一注 2串1
				</label>
			</div>
			{singlesPlan ? (
				<p
					className={`${TABULAR_NUMS} text-xs text-muted-foreground`}
					data-testid={`${testidPrefix}-form-plan-singles`}
				>
					两笔单关：{slipFormSummary(singlesPlan)}
				</p>
			) : null}
			{parlayPlan ? (
				<p className={`${TABULAR_NUMS} text-xs text-muted-foreground`} data-testid={`${testidPrefix}-form-plan-parlay`}>
					一注 2串1：{slipFormSummary(parlayPlan)}
				</p>
			) : null}
			{missingEvNote ? (
				<p className="text-xs text-warning" data-testid={`${testidPrefix}-form-plan-missing-ev`}>
					部分腿 EV 缺失，预期按 0 估——结构预览，非精确口径
				</p>
			) : null}
		</div>
	);
}

/** 底部常驻选注条：浏览全程可见已选腿摘要 + 组合赔率 + 跨玩法草稿提示。 */
export function BasketBar({ basket, emptyHint }: { basket: HadBasket; emptyHint: string }) {
	const { legs, combinedOdds, setBasketOpen, slipForm } = basket;
	const { goalsLegs } = useBasket();
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
						{legs.length === MAX_LEGS && slipForm === "parlay" ? ` · 组合赔率 ${combinedOdds.toFixed(2)}` : ""}
					</span>
				) : (
					<span className="hidden text-xs text-muted-foreground sm:inline">{emptyHint}</span>
				)}
				{goalsLegs.length > 0 ? (
					<span className="hidden text-xs text-info sm:inline" data-testid="basket-cross-goals">
						另有进球草稿 {goalsLegs.length} 注（进球页提交）
					</span>
				) : null}
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

/** 右侧选注篮抽屉：腿列表 + 票型（≥2 腿）+ 建议仓位 + 模式/金额/策略版本 + 提交。 */
export function BasketDrawer({
	basket,
	bankroll,
	emptyHint,
}: {
	basket: HadBasket;
	bankroll: number | null;
	emptyHint: string;
}) {
	const {
		legs,
		basketOpen,
		setBasketOpen,
		mode,
		basketAdvice,
		removeLeg,
		createSuggestion,
		slipForm,
		chooseSlipForm,
		planTicketLegs,
		planMissingEv,
	} = basket;
	// 票级预览（票 02 单源）：抽屉里看到的数字 == 提交后落库口径
	const planSingles = planTicketLegs !== null ? slipFormPlan(planTicketLegs, "singles") : null;
	const planParlay = planTicketLegs !== null && legs.length >= 2 ? slipFormPlan(planTicketLegs, "parlay") : null;
	const stakeLabel =
		legs.length >= 2 && slipForm === "singles" ? "单关金额(¥/注)" : legs.length >= 2 ? "整注金额(¥)" : "金额(¥)";
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
							{legs.map((leg) => {
								const countdown = kickoffInfo(leg.kickoff_utc, Date.now());
								return (
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
											{leg.probability != null ? (
												<span
													className={`${TABULAR_NUMS} ml-2 text-xs text-muted-foreground`}
													data-testid="basket-leg-prob"
													title={`EV 依据=入篮时快照（来源 ${leg.probability.source} · 时点 ${leg.probability.at ?? "未知"}）`}
												>
													p={leg.probability.value.toFixed(2)}
												</span>
											) : (
												<span className="ml-2 text-xs text-warning" data-testid="basket-leg-no-prob">
													EV 依据缺失
												</span>
											)}
											<span
												className={`${TABULAR_NUMS} ml-2 text-xs ${countdown.urgent ? "text-warning" : "text-muted-foreground"}`}
												data-testid="basket-leg-deadline"
											>
												开球 {localTime(leg.kickoff_utc)} · {countdown.text}
											</span>
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
								);
							})}
						</ul>
					)}
					{/* 票型显式化（票 02）：≥2 腿必须选票型，预览/命中条件并列，无静默切换 */}
					{legs.length >= 2 ? (
						<div className="mt-3">
							<SlipFormChooser
								form={slipForm}
								onChoose={chooseSlipForm}
								singlesPlan={planSingles}
								parlayPlan={planParlay}
								testidPrefix="basket"
								missingEvNote={planMissingEv}
							/>
						</div>
					) : null}
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
					<BasketFormFields testid="basket" state={basket} stakeLabel={stakeLabel} stakeMin={1} />
					<button
						type="button"
						data-testid="basket-submit"
						className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
						disabled={createSuggestion.isPending || legs.length === 0}
						onClick={() => createSuggestion.mutate()}
					>
						{legs.length >= 2 && slipForm === "singles" ? `建 ${legs.length} 笔单关` : "建立建议"}
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
