import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { type ReactNode, useState } from "react";
import { createBet, type TodayFixture } from "../api/goalx";
import { basketActions, basketStore, requestIdForLegs, useBasket } from "../lib/basket-store";
import { submitBasketUnits } from "../lib/basket-submit";
import { type SlipForm, type SlipFormPlan, slipFormPlan, slipFormSummary } from "../lib/combo-engine";
import { invalidateLegQuoteQueries } from "../lib/query-keys";
import { errorText, localTime, SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";
import { useNow } from "../lib/use-now";
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
 * 篮共享件（lean-audit 票 04；review-20260928 票 06 收编研究页；票 08 续作
 * 参数化双族）：had 三页（场次/胜平负/研究）用 useHadBasket+BasketBar+
 * BasketDrawer；进球页用参数化 BasketBarView/BasketDrawerView（腿摘要渲染
 * 器/testid 前缀/文案注入——goals 手搓 ~140 行复刻删除）。
 * - useHadBasket：篮草稿走全站单源 store（票 06：路由切换/刷新可恢复，
 *   与进球页共用一份草稿）+ 规则前置（同场替换/MAX_LEGS/单固首腿）+
 *   建议仓位输入 + createBet mutation（提交政策走 lib/basket-submit 共享环）；
 *   腿 EV 只读入篮时概率快照（报告审计票 06：第三参=pick 时快照来源，缺省
 *   今日列表 eu_prob，研究页注入共识口径——跨场腿 EV 不随当前页漂移，
 *   缺快照=依据缺失）；
 * - 票型（report-audit 票 02）：篮内 ≥2 腿时显式票型——两笔单关（逐腿独立
 *   建注）/一注 2串1（整注）；预览与提交同走 ticketPlan 单源，无静默切换；
 * - 提交幂等（票 06）：单关腿=腿 request_id、整注=腿键有序拼接，服务端
 *   同键不落双行——批量一成一败后重试只补发失败腿且不重复已建注；
 * - BasketBar：had 底部常驻条（已选摘要 + 组合赔率 + 跨玩法草稿提示）；
 * - BasketDrawer：had 右侧抽屉（腿列表含开球时间/票型/建议仓位/模式/金额/
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
	const { hadLegs: legs, stake, strategyVersion, mode, hadPending } = useBasket();
	const [basketOpen, setBasketOpen] = useState(false);
	const [message, setMessage] = useState<string | null>(null);
	// 票型状态 = 全 app 一份记忆（读 localStorage，缺省单关合计）；卡/篮
	// 两处选择器都是这份状态的视图（票 02：带入什么票型就提交什么票型）
	const [slipForm, setSlipForm] = useState<SlipForm>(readStoredSlipForm);

	function chooseSlipForm(form: SlipForm) {
		if (hadPending) {
			return; // 票 01：飞行中换票型=同篮跨票型重提（幂等键变，无法合并）——卡/抽屉同一入口门
		}
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
		// 票 05 E15 + 票 08 收敛：预检只认 store 布尔 hadPending（本页 isPending
		// 是其子集）——不拦会编辑被门静默吞掉但 toast 仍报「已替换」（说谎）
		if (!isPickable(fixture, now) || hadPending) {
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
		if (hadPending) {
			return; // 同 pick：飞行中锁定编辑（含跨页飞行，票 05 E15；票 08 收敛只认 store 布尔）
		}
		setLegs(legs.filter((leg) => leg.fixture_id !== fixtureId));
		setMessage(null);
	}

	const createSuggestion = useMutation({
		// 票 08：飞行状态机 withFlight 包提交——计数起落/门控/异常解冻全在
		// store implementation；冲突/价变/失败政策走共享环 submitBasketUnits
		mutationFn: () => {
			const strategy = strategyVersion.trim() === "" ? null : strategyVersion.trim();
			const post = (betLegs: Leg[]) =>
				createBet({
					mode,
					stake: Number(stake),
					strategy_version: strategy,
					// 票 06 幂等：腿键稳定（单关=腿键；整注=腿键有序拼接），
					// 同键重试服务端回带既有注不落双行
					client_request_id: requestIdForLegs(betLegs),
					legs: betLegs.map((leg) => ({
						fixture_id: leg.fixture_id,
						market_code: "had",
						selection_code: leg.selection,
						locked_odds: leg.odds,
					})),
				});
			// 票型分叉（票 02）=单元划分：singles 且 ≥2 腿=逐腿独立建注（同一
			// 注额），parlay/单腿=一注整提。提交即票型，无静默切换
			const singles = slipForm === "singles" && legs.length >= 2;
			return basketStore.withFlight("had", () =>
				submitBasketUnits({
					client: queryClient,
					units: singles ? legs.map((leg) => [leg]) : [legs],
					post,
					successNote: (created, labels) =>
						singles
							? `已建建议 #${created.map((bet) => bet.id).join("、#")}（${created.length} 笔单关：${labels.join("、")}）— 去投注页锁定`
							: `已建建议 #${created[0]?.id}（${legs.length === 1 ? "单关" : `${legs.length}串1`}，${labels[0] ?? ""}）— 去投注页锁定`,
					failureNote: (error, created, index) =>
						singles
							? `第 ${index + 1} 笔失败：${errorText(error)}${created.length > 0 ? `（已建 #${created.map((bet) => bet.id).join("、#")}，失败腿留篮可重试）` : ""}`
							: `建注失败：${errorText(error)}`,
					// 整注旧文案无「第 1 笔」前缀（review 修：had 行为不回退）
					unitLabel: singles ? (index) => `第 ${index + 1} 笔` : () => "",
					// 前缀失效今日场次（键形 ["today-fixtures", days]）+ 逐场研究页
					// 报价查询（票 06）——correctness F1：打错键=不刷新，页面停在
					// 旧价上确认流死循环
					invalidatePriceSource: invalidateLegQuoteQueries,
					replaceLegs: basketActions.replaceHadLegs,
					report: setMessage,
					closeBasket: () => setBasketOpen(false),
				}),
			);
		},
	});

	return {
		legs,
		setLegs,
		/** had 提交飞行中（store 全局，跨页可见；applyCombo 等入口门用）。 */
		hadPending,
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
 * testid 前缀与金额档（标签/最低注）经 prop 注入，e2e 锚不变。
 * disabled=提交飞行冻结（票 01）：三输入皆提交载荷——飞行中改值=重试口径漂移。 */
function BasketFormFields({
	testid,
	state,
	stakeLabel,
	stakeMin,
	disabled = false,
}: {
	testid: string;
	state: Pick<HadBasket, "mode" | "setMode" | "stake" | "setStake" | "strategyVersion" | "setStrategyVersion">;
	stakeLabel: string;
	stakeMin: number;
	disabled?: boolean;
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
					disabled={disabled}
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
					disabled={disabled}
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
					disabled={disabled}
					onChange={(event) => setStrategyVersion(event.target.value)}
				/>
			</label>
		</div>
	);
}

export type HadBasket = ReturnType<typeof useHadBasket>;

/** 提交钮（票 01 防重入，had 篮与进球篮共用）：门=store 全局 pending——切页
 * 重挂载后本页 isPending=false 看不到别页飞行；同步入口检查兜 disabled
 * 渲染前的连点窗口（检查须在 mutate 前：票 08 起冻结由 mutationFn 内
 * withFlight 置位，放进环里会拦掉本次合法请求）。 */
function BasketSubmitButton({
	testid,
	pending,
	disabled = false,
	label,
	onSubmit,
}: {
	testid: string;
	/** store 全局飞行标志（hadPending/goalsPending）。 */
	pending: boolean;
	/** 与飞行无关的禁用因（如空篮）。 */
	disabled?: boolean;
	label: string;
	onSubmit: () => void;
}) {
	return (
		<button
			type="button"
			data-testid={testid}
			className="w-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-40"
			disabled={pending || disabled}
			onClick={() => {
				if (pending) {
					return;
				}
				onSubmit();
			}}
		>
			{pending ? "提交中…" : label}
		</button>
	);
}

/** 票型选择器（票 02，卡/篮两用）：两票型预览并列（单源摘要），当前票型可选。
 * disabled=提交飞行冻结（票 01）：飞行中换票型=同篮跨票型重提，幂等键不同无法合并。 */
export function SlipFormChooser({
	form,
	onChoose,
	singlesPlan,
	parlayPlan,
	testidPrefix,
	missingEvNote = false,
	disabled = false,
}: {
	form: SlipForm;
	onChoose: (form: SlipForm) => void;
	singlesPlan: SlipFormPlan | null;
	parlayPlan: SlipFormPlan | null;
	testidPrefix: string;
	missingEvNote?: boolean;
	disabled?: boolean;
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
						disabled={disabled}
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
						disabled={disabled}
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

/**
 * 参数化底栏（票 08 续作，审计 19 修法）：had/goals 两族共用壳，腿摘要/
 * 计数上限/跨玩法提示经 props 注入——goals 页 ~140 行手搓底栏/抽屉复刻删除，
 * 文案与计数语义漂移面收进一处。testid 前缀经 prop（"basket"|"goals-basket"），
 * e2e 锚不变。
 */
export function BasketBarView<L>({
	testidPrefix,
	legs,
	maxLegs,
	summary,
	emptyHint,
	countNote,
	crossNote,
	onOpen,
}: {
	testidPrefix: string;
	legs: L[];
	maxLegs: number;
	/** 非空时展示（空态回 emptyHint）；组合赔率等后缀由调用方拼好。 */
	summary: string | null;
	emptyHint: string;
	/** 计数旁口径小字（goals：独立单关）。 */
	countNote?: string;
	/** 跨玩法草稿提示（另一族篮非空时调用方算好文案）。 */
	crossNote?: string | null;
	onOpen: () => void;
}) {
	return (
		<div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-background/95 backdrop-blur">
			<div className="mx-auto flex max-w-6xl items-center gap-3 px-6 py-3">
				<span className="text-sm">
					选注篮{" "}
					<span className={`${TABULAR_NUMS} font-medium`} data-testid={`${testidPrefix}-count`}>
						{legs.length}/{maxLegs}
					</span>
					{countNote ? <span className="ml-2 hidden text-xs text-muted-foreground sm:inline">{countNote}</span> : null}
				</span>
				{legs.length > 0 && summary !== null ? (
					<span
						className={`${TABULAR_NUMS} hidden text-xs text-muted-foreground sm:inline`}
						data-testid={`${testidPrefix}-summary`}
					>
						{summary}
					</span>
				) : (
					<span className="hidden text-xs text-muted-foreground sm:inline">{emptyHint}</span>
				)}
				{crossNote ? (
					<span className="hidden text-xs text-info sm:inline" data-testid={`${testidPrefix}-cross`}>
						{crossNote}
					</span>
				) : null}
				<button
					type="button"
					data-testid={`${testidPrefix}-open`}
					className="ml-auto rounded-md border border-border bg-background px-3 py-1.5 text-sm font-medium transition-colors hover:bg-muted disabled:pointer-events-none disabled:opacity-50"
					disabled={legs.length === 0}
					onClick={onOpen}
				>
					展开选注篮
				</button>
			</div>
		</div>
	);
}

/** 参数化抽屉壳（票 08 续作）：布局/空态/表单三输入/提交钮/底部链接共享，
 * 腿摘要渲染器与中段（票型/建议仓位）经 props 注入。 */
export function BasketDrawerView<L>({
	testidPrefix,
	open,
	onOpenChange,
	title,
	description,
	emptyHint,
	legs,
	legKey,
	renderLeg,
	removeLabel,
	onRemove,
	children,
	form,
	submit,
}: {
	testidPrefix: string;
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	description: ReactNode;
	emptyHint: string;
	legs: L[];
	legKey: (leg: L) => string;
	/** 腿摘要块（左 span 内容）；倒计时滴答钟由调用方注入渲染器闭包。 */
	renderLeg: (leg: L) => ReactNode;
	removeLabel: (leg: L) => string;
	onRemove: (leg: L) => void;
	/** 中段（had：票型选择+建议仓位；goals：建议仓位）。 */
	children?: ReactNode;
	form: {
		state: Pick<HadBasket, "mode" | "setMode" | "stake" | "setStake" | "strategyVersion" | "setStrategyVersion">;
		stakeLabel: string;
		stakeMin: number;
		disabled: boolean;
	};
	submit: {
		pending: boolean;
		disabled: boolean;
		label: string;
		onSubmit: () => void;
	};
}) {
	return (
		<Drawer open={open} onOpenChange={onOpenChange} swipeDirection="right">
			<DrawerContent className="mx-0 w-full sm:max-w-md">
				<DrawerHeader>
					<DrawerTitle>{title}</DrawerTitle>
					<DrawerDescription>{description}</DrawerDescription>
				</DrawerHeader>
				<div className="flex-1 overflow-y-auto p-4">
					{legs.length === 0 ? (
						<p className="text-sm text-muted-foreground">{emptyHint}</p>
					) : (
						<ul className="space-y-2">
							{legs.map((leg) => (
								<li
									key={legKey(leg)}
									className="flex items-center justify-between rounded-md border border-border p-3 text-sm"
									data-testid={`${testidPrefix}-leg`}
								>
									{renderLeg(leg)}
									<button
										type="button"
										className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
										aria-label={removeLabel(leg)}
										onClick={() => onRemove(leg)}
									>
										移除
									</button>
								</li>
							))}
						</ul>
					)}
					{children}
				</div>
				<DrawerFooter>
					<BasketFormFields
						testid={testidPrefix}
						state={form.state}
						stakeLabel={form.stakeLabel}
						stakeMin={form.stakeMin}
						disabled={form.disabled}
					/>
					<BasketSubmitButton
						testid={`${testidPrefix}-submit`}
						pending={submit.pending}
						disabled={submit.disabled}
						label={submit.label}
						onSubmit={submit.onSubmit}
					/>
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

/** had 底栏（票 08 参数化后的 had 视图）：三页调用点签名不变。 */
export function BasketBar({ basket, emptyHint }: { basket: HadBasket; emptyHint: string }) {
	const { legs, combinedOdds, setBasketOpen, slipForm } = basket;
	const { goalsLegs } = useBasket();
	return (
		<BasketBarView
			testidPrefix="basket"
			legs={legs}
			maxLegs={MAX_LEGS}
			summary={
				legs.length > 0
					? `${legs.map((leg) => `${leg.match_code} ${SELECTION_LABELS[leg.selection]}`).join(" × ")}${
							legs.length === MAX_LEGS && slipForm === "parlay" ? ` · 组合赔率 ${combinedOdds.toFixed(2)}` : ""
						}`
					: null
			}
			emptyHint={emptyHint}
			crossNote={goalsLegs.length > 0 ? `另有进球草稿 ${goalsLegs.length} 注（进球页提交）` : null}
			onOpen={() => setBasketOpen(true)}
		/>
	);
}

/** had 右侧选注篮抽屉：腿列表 + 票型（≥2 腿）+ 建议仓位 + 模式/金额/策略版本 + 提交。 */
export function BasketDrawer({
	basket,
	bankroll,
	emptyHint,
}: {
	basket: HadBasket;
	/** 建议输入口径的余额（票 05 E16）：undefined=读取中；null=读取失败。 */
	bankroll: number | null | undefined;
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
		hadPending,
	} = basket;
	const { goalsPending } = useBasket(); // 共享提交参数跨族冻结（票 01 F1）
	const now = useNow(); // 抽屉倒计时共享滴答钟（B-2b 补面：不冻结在一次性渲染）
	// 票级预览（票 02 单源）：抽屉里看到的数字 == 提交后落库口径
	const planSingles = planTicketLegs !== null ? slipFormPlan(planTicketLegs, "singles") : null;
	const planParlay = planTicketLegs !== null && legs.length >= 2 ? slipFormPlan(planTicketLegs, "parlay") : null;
	const stakeLabel =
		legs.length >= 2 && slipForm === "singles" ? "单关金额(¥/注)" : legs.length >= 2 ? "整注金额(¥)" : "金额(¥)";
	return (
		<BasketDrawerView
			testidPrefix="basket"
			open={basketOpen}
			onOpenChange={setBasketOpen}
			title="选注篮"
			description={
				legs.length === 1 && legs[0]?.single_eligible !== true
					? "当前腿非单固，只能作为串关腿（提交时服务器校验）"
					: "单关须单固；2串1 须不同场次且共同可投（提交时服务器校验）"
			}
			emptyHint={emptyHint}
			legs={legs}
			legKey={(leg) => String(leg.fixture_id)}
			renderLeg={(leg) => {
				const countdown = kickoffInfo(leg.kickoff_utc, now); // 抽屉共享滴答钟（B-2b 补面）
				return (
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
				);
			}}
			removeLabel={(leg) => `移除 ${leg.match_code}`}
			onRemove={(leg) => removeLeg(leg.fixture_id)}
			form={{ state: basket, stakeLabel, stakeMin: 1, disabled: hadPending || goalsPending }}
			submit={{
				pending: hadPending,
				disabled: legs.length === 0,
				label: legs.length >= 2 && slipForm === "singles" ? `建 ${legs.length} 笔单关` : "建立建议",
				onSubmit: () => createSuggestion.mutate(),
			}}
		>
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
						disabled={hadPending}
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
		</BasketDrawerView>
	);
}
