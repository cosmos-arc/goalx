import type { GoalsFixture, GoalsSelection } from "../api/goalx";
import { type GoalsMarket, isGoalsPickable } from "../lib/combo-engine";
import { TABULAR_NUMS } from "../lib/ui";
import { evClass, evText, kickoffInfo, localTime } from "./had-quote-ui";

/**
 * 进球类共享 UI 原语（票 wb-05）：选项标签、选注钮、可投卡。
 * 与胜平负页同编码基调（红涨绿跌、琥珀警示、蓝=可投资格语义不复用——
 * 进球类无欧赔证据链，资格=在售+单固+未开赛，页面口径标注接词典）。
 */

/** ttg 档位人话标签（"7" 归并尾部 → 7+）。 */
export function ttgLabel(code: string): string {
	return code === "7" ? "7+" : code;
}

const CRS_OTHER_LABELS: Record<string, string> = {
	h_other: "胜其他",
	d_other: "平其他",
	a_other: "负其他",
};

/** crs 选项人话标签（精确比分原样，三档其他给官方叫法）。 */
export function crsLabel(code: string): string {
	return CRS_OTHER_LABELS[code] ?? code;
}

export function goalsLabel(market: GoalsMarket, code: string): string {
	return market === "ttg" ? ttgLabel(code) : crsLabel(code);
}

/** 进球类选注篮一注（独立单关，非串关腿）。 */
export type GoalsLeg = {
	fixture_id: number;
	match_code: string;
	home_team: string;
	away_team: string;
	market: GoalsMarket;
	selection: string;
	/** 人话标签（提交仍用 market/selection 原码）。 */
	label: string;
	odds: number;
	/** 模型×竞彩价口径 EV（票 wb-06 选注篮建议仓位用；无模型场为 null）。 */
	ev: number | null;
};

/** 选项钮：赔率 + EV 着色；无报价/不可投禁用（诚实显示网格）。 */
export function GoalsOddsButton({
	fixture,
	market,
	selection,
	now,
	selected,
	onPick,
	testid,
}: {
	fixture: GoalsFixture;
	market: GoalsMarket;
	selection: GoalsSelection;
	now: number;
	selected: boolean;
	onPick: (fixture: GoalsFixture, market: GoalsMarket, selection: GoalsSelection) => void;
	testid: string;
}) {
	const pickable = isGoalsPickable(fixture, market, now);
	const disabled = !pickable || selection.odds === null || selection.odds === undefined;
	return (
		<button
			type="button"
			disabled={disabled}
			data-testid={testid}
			aria-label={`${fixture.match_code} ${market === "ttg" ? "总进球" : "比分"} ${goalsLabel(
				market,
				selection.code,
			)} @${selection.odds?.toFixed(2) ?? "—"}`}
			className={`flex min-w-14 flex-col items-center rounded-md border px-2 py-1.5 text-xs transition-colors ${
				TABULAR_NUMS
			} ${
				selected
					? "border-primary bg-primary text-primary-foreground"
					: disabled
						? "border-transparent text-muted-foreground/40"
						: "border-border hover:bg-muted"
			}`}
			onClick={() => {
				if (!disabled) {
					onPick(fixture, market, selection);
				}
			}}
		>
			<span className="font-medium">{goalsLabel(market, selection.code)}</span>
			<span>{selection.odds?.toFixed(2) ?? "—"}</span>
			<span
				className={
					selection.ev === null || selection.ev === undefined ? "text-muted-foreground/70" : evClass(selection.ev)
				}
			>
				{selection.ev === null || selection.ev === undefined ? "EV —" : evText(selection.ev)}
			</span>
		</button>
	);
}

/**
 * 进球类可投卡（票 wb-05）：场次头（联赛/倒计时/停售与单固标记）+ 选项网格。
 * 无模型覆盖的场次网格照常（EV 列空缺）——概率空缺是诚实状态，不是错误。
 */
export function GoalsMarketCard({
	fixture,
	market,
	now,
	legs,
	onPick,
	testidBase = "goals-card",
	pickTestidBase = "goals-pick",
	dayNote,
}: {
	fixture: GoalsFixture;
	market: GoalsMarket;
	now: number;
	legs: GoalsLeg[];
	onPick: (fixture: GoalsFixture, market: GoalsMarket, selection: GoalsSelection) => void;
	testidBase?: string;
	pickTestidBase?: string;
	dayNote?: string | undefined;
}) {
	const block = market === "ttg" ? fixture.ttg : fixture.crs;
	const cd = kickoffInfo(fixture.kickoff_utc, now);
	const pickable = isGoalsPickable(fixture, market, now);
	const hasModel = fixture.model_version != null;
	const selected = (code: string) =>
		legs.some((leg) => leg.fixture_id === fixture.fixture_id && leg.market === market && leg.selection === code);
	return (
		<article
			className="rounded-lg border border-border bg-card p-4"
			data-testid={`${testidBase}-${fixture.fixture_id}`}
		>
			<div className="mb-2 flex items-center justify-between gap-2">
				<span className="flex items-center gap-1.5 text-xs text-muted-foreground">
					{dayNote ? <span className="rounded border border-border px-1">{dayNote}</span> : null}
					{fixture.competition}
					{fixture.tier === "tier1" ? (
						<span className="rounded border border-border px-1 text-muted-foreground">T1</span>
					) : null}
					{block?.single_eligible === true ? null : (
						<span className="rounded border border-border px-1 text-muted-foreground">仅串关</span>
					)}
				</span>
				<span className="flex items-center gap-2">
					{pickable ? null : (
						<span data-testid={`goals-status-${fixture.fixture_id}`} className="text-xs text-muted-foreground">
							{block?.sale_state === "stopped" ? "已停售" : "不可单关"}
						</span>
					)}
					<span
						className={`text-xs ${TABULAR_NUMS} ${cd.urgent ? "font-medium text-warning" : "text-muted-foreground"}`}
					>
						{localTime(fixture.kickoff_utc)} · {cd.text}
					</span>
				</span>
			</div>
			<p className="mb-1 text-sm font-medium">
				{fixture.home_team} <span className="text-muted-foreground">vs</span> {fixture.away_team}
				<span className="ml-2 text-xs font-normal text-muted-foreground">{fixture.match_code}</span>
			</p>
			<p className="mb-3 text-xs text-muted-foreground" data-testid={`goals-model-note-${fixture.fixture_id}`}>
				{hasModel ? (
					<>概率由比分矩阵推导（{fixture.model_version}）；EV = 模型概率 × 竞彩价 − 1</>
				) : (
					"无模型覆盖——概率/EV 空缺（仅五大联赛在售场次有 Forecast）"
				)}
			</p>
			<div className="flex flex-wrap gap-1.5">
				{(block?.selections ?? []).map((selection) => (
					<GoalsOddsButton
						key={selection.code}
						fixture={fixture}
						market={market}
						selection={selection}
						now={now}
						selected={selected(selection.code)}
						onPick={onPick}
						testid={`${pickTestidBase}-${fixture.fixture_id}-${market}-${selection.code}`}
					/>
				))}
			</div>
		</article>
	);
}
