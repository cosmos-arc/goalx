import type { HadQuoteStatus, TodayFixture } from "../api/goalx";
import { SELECTION_LABELS, SELECTIONS, type Selection, TABULAR_NUMS } from "../lib/ui";
import { GlossaryTerm } from "./glossary-term";
import { Badge } from "./ui/badge";

/**
 * 场次轴共享 UI 原语（票 wb-02 抽出）：资格徽章/选注钮/had 判读助手。
 * 场次列表页与研究页共用，保证两页编码口径逐像素一致
 * （票 04 定稿的编码硬约束：EV 红涨绿跌、资格蓝红灰、琥珀警示）。
 * 票 wb-03：可投卡片从场次页下沉至此（玩法页推荐流复用同卡同编码），
 * 并加 feed 态——非可投场按钮禁用 + 资格徽章说明原因。
 */

export type { Selection };
export { SELECTIONS };

export const MAX_LEGS = 2;

export const FLAG_LABELS: Record<string, string> = {
	ev_deviation: "EV 偏差≥5%",
	few_books: "样本少",
	low_confidence: "共识低置信",
	not_joined: "未 join 欧赔",
};

export const REASON_LABELS: Record<string, string> = {
	sale_stopped: "已停售",
	kickoff_passed: "已开赛",
	stale_source: "报价过期",
	jc_three_way_incomplete: "竞彩三向不全",
	jc_no_quote: "无竞彩报价",
	sale_status_unknown: "销售状态未知",
	sale_status_unknown_value: "销售状态未知",
	single_eligibility_unknown: "单固资格未知",
	jc_observed_at_unknown: "观测时点未知",
	jc_source_time_unknown: "源时间未知",
	eu_no_quote: "无欧赔",
	eu_no_valid_books: "无有效欧赔",
	eu_observed_at_unknown: "欧赔观测未知",
};

/** 选注交互所需的最小结构：今日列表行与研究页视图都满足（票 wb-02 共用）。 */
export type PickableFixture = {
	fixture_id: number;
	match_code: string;
	home_team: string;
	away_team: string;
	kickoff_utc: string;
	had_quote?: HadQuoteStatus | null;
};

export type Leg = {
	fixture_id: number;
	match_code: string;
	home_team: string;
	away_team: string;
	selection: Selection;
	odds: number;
	single_eligible: boolean | null;
};

export function oddsText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : value.toFixed(2);
}

/** EV 数字永远只按正负红绿（票 04 定稿）：红 = 正、绿 = 负，近零中性。 */
export function evClass(value: number): string {
	if (value > 0.002) return "text-profit";
	if (value < -0.002) return "text-loss";
	return "text-muted-foreground";
}

export function evText(value: number): string {
	return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

export function localTime(utc: string): string {
	const date = new Date(utc);
	if (Number.isNaN(date.getTime())) {
		return "—";
	}
	return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

/** 开赛倒计时（前端可推导，不加 API 字段）：已开赛/分钟（<2h 琥珀紧迫）/小时。 */
export function kickoffInfo(utc: string, now: number): { passed: boolean; text: string; urgent: boolean } {
	const kickoff = new Date(utc).getTime();
	if (Number.isNaN(kickoff)) {
		return { passed: false, text: "—", urgent: false };
	}
	const offsetMin = (kickoff - now) / 60_000;
	if (offsetMin <= 0) {
		return { passed: true, text: "已开赛", urgent: false };
	}
	if (offsetMin < 120) {
		return { passed: false, text: `${Math.round(offsetMin)} 分钟后`, urgent: true };
	}
	return { passed: false, text: `${Math.floor(offsetMin / 60)} 小时后`, urgent: false };
}

/** 报价年龄（分钟）；jc_updated_at 缺失时返回 null（时间格省略该段）。 */
export function minutesAgo(iso: string | null | undefined, now: number): number | null {
	if (!iso) {
		return null;
	}
	const at = new Date(iso).getTime();
	if (Number.isNaN(at)) {
		return null;
	}
	return Math.max(0, Math.round((now - at) / 60_000));
}

/** 可投 = 判定 valid + 在售 + 未开赛（证据链完整且新鲜，不等于必成交）。 */
export function isPickable(fixture: PickableFixture, now: number): boolean {
	const quote = fixture.had_quote;
	if (quote?.status !== "valid" || quote.sale_state !== "on_sale") {
		return false;
	}
	const kickoff = new Date(fixture.kickoff_utc).getTime();
	return !Number.isNaN(kickoff) && kickoff > now;
}

export function makeLeg(fixture: PickableFixture, selection: Selection, odds: number): Leg {
	return {
		fixture_id: fixture.fixture_id,
		match_code: fixture.match_code,
		home_team: fixture.home_team,
		away_team: fixture.away_team,
		selection,
		odds,
		single_eligible: fixture.had_quote?.single_eligible ?? null,
	};
}

/** 资格徽章（票 04 编码）：蓝 = 可投 / 红 = 拒绝 + 原因 / 灰框 = 证据未知；无判定弱化。 */
export function EligibilityBadge({ quote }: { quote: HadQuoteStatus | null | undefined }) {
	if (!quote) {
		return <span className="text-xs text-muted-foreground">无判定</span>;
	}
	const reasons = (quote.reasons ?? []).map((reason) => REASON_LABELS[reason] ?? reason).join("/");
	if (quote.status === "valid") {
		return (
			<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-valid">
				<Badge className="bg-info/10 text-info">可投</Badge>
				{quote.single_eligible === true ? (
					// 票 18：单固徽章接词典 tooltip（克制——只接指标名，虚线下划线在框内）
					<span className="rounded border border-border px-1 text-xs text-muted-foreground">
						<GlossaryTerm id="single" />
					</span>
				) : null}
			</span>
		);
	}
	if (quote.status === "rejected") {
		return (
			<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-rejected">
				<Badge variant="outline" className="border-destructive/40 text-destructive">
					拒绝
				</Badge>
				{reasons ? <span className="text-xs text-muted-foreground">{reasons}</span> : null}
			</span>
		);
	}
	return (
		<span className="flex flex-wrap items-center gap-1" data-testid="had-quote-unknown">
			<Badge variant="outline">证据未知</Badge>
			{reasons ? <span className="text-xs text-muted-foreground">{reasons}</span> : null}
		</span>
	);
}

export function OddsButton({
	fixture,
	selection,
	value,
	selected,
	disabled,
	onPick,
	testid,
	size = "sm",
}: {
	fixture: PickableFixture;
	selection: Selection;
	value: number | null | undefined;
	selected: boolean;
	disabled: boolean;
	onPick: (fixture: PickableFixture, selection: Selection, odds: number) => void;
	/** 卡片与表格各有一组选注钮：卡片用 pick-card-* 前缀，表格保留 pick-*（e2e 契约）。 */
	testid: string;
	size?: "sm" | "md";
}) {
	return (
		<button
			type="button"
			disabled={disabled || value === null || value === undefined}
			data-testid={testid}
			aria-label={`${fixture.match_code} ${SELECTION_LABELS[selection]} @${oddsText(value)}`}
			className={`rounded-md border ${TABULAR_NUMS} transition-colors ${
				size === "md" ? "px-3 py-1.5 text-sm" : "px-2 py-1 text-xs"
			} ${
				selected
					? "border-primary bg-primary text-primary-foreground"
					: disabled
						? "border-transparent text-muted-foreground/40"
						: "border-border hover:bg-muted"
			}`}
			onClick={() => {
				if (value !== null && value !== undefined) {
					onPick(fixture, selection, value);
				}
			}}
		>
			{oddsText(value)}
		</button>
	);
}

/**
 * 可投卡片（票 04 定稿，票 wb-03 从场次页下沉为共享）：联赛/倒计时/EV 三向 +
 * 最强标注/快捷选注/仅串关标记。场次页区块即过滤结果，不重复"可投"徽章；
 * 玩法页推荐流（票 wb-03）渲染全部场次——非可投卡按钮禁用并以资格徽章说明原因。
 */
export function EligibleCard({
	fixture,
	now,
	legs,
	onPick,
	testidBase = "fixtures-card",
	pickTestidBase = "pick-card",
	dayNote,
}: {
	fixture: TodayFixture;
	now: number;
	legs: Leg[];
	onPick: (fixture: PickableFixture, selection: Selection, odds: number) => void;
	/** 卡片根 testid 前缀：场次页 fixtures-card-*（e2e 契约），玩法页 market-card-*。 */
	testidBase?: string;
	/** 快捷选注钮 testid 前缀：场次页 pick-card-*（e2e 契约），玩法页 market-pick-*。 */
	pickTestidBase?: string;
	/** 跨日标签（玩法页推荐流跨 3 日窗口时传入，如"明天"）；场次页按日过滤不传。 */
	dayNote?: string | undefined;
}) {
	const pickable = isPickable(fixture, now);
	const cd = kickoffInfo(fixture.kickoff_utc, now);
	const evValues = SELECTIONS.map((sel) => fixture.ev?.[sel]).filter((v): v is number => v !== null && v !== undefined);
	const best = evValues.length > 0 ? SELECTIONS.find((sel) => fixture.ev?.[sel] === Math.max(...evValues)) : undefined;
	const selected = (sel: Selection) =>
		legs.some((leg) => leg.fixture_id === fixture.fixture_id && leg.selection === sel);
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
					{fixture.had_quote?.single_eligible === true ? null : (
						<span className="rounded border border-border px-1 text-muted-foreground">仅串关</span>
					)}
				</span>
				<span className="flex items-center gap-2">
					{/* 推荐流含非可投场：徽章说明原因（可投场不重复徽章——区块即过滤结果的约束保留） */}
					{pickable ? null : <EligibilityBadge quote={fixture.had_quote} />}
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
			<div className="mb-3 flex flex-wrap items-baseline gap-2 text-xs">
				<span className="text-muted-foreground">EV</span>
				{SELECTIONS.map((sel) => {
					const value = fixture.ev?.[sel];
					return value === null || value === undefined ? (
						<span key={sel} className={`${TABULAR_NUMS} text-muted-foreground`}>
							{SELECTION_LABELS[sel]} —
						</span>
					) : (
						<span key={sel} className={`${TABULAR_NUMS} ${evClass(value)}`}>
							{SELECTION_LABELS[sel]} {evText(value)}
						</span>
					);
				})}
				{(fixture.flags ?? [])
					.filter((flag) => flag in FLAG_LABELS)
					// 票 39：低置信（<4）覆盖 few_books（<3）区间——两标并打时只显示
					// 低置信文案，合并为一个显示位（方案待人追认，见词典 low-confidence）
					.filter((flag) => flag !== "few_books" || !(fixture.flags ?? []).includes("low_confidence"))
					.map((flag) => (
						<span
							key={flag}
							data-testid={`flag-${flag}`}
							className="rounded bg-warning/10 px-1.5 py-0.5 text-foreground"
						>
							{flag === "low_confidence" ? <GlossaryTerm id="low-confidence" /> : FLAG_LABELS[flag]}
						</span>
					))}
				{best ? (
					<span className={`${TABULAR_NUMS} ml-auto text-muted-foreground`} title="共识 EV 最高的一向">
						最强 {SELECTION_LABELS[best]}
					</span>
				) : null}
			</div>
			{fixture.stale_line ? (
				<p
					className="mb-2 text-xs text-muted-foreground"
					data-testid={`stale-line-${fixture.fixture_id}`}
					title={`竞彩定格 ${localTime(fixture.stale_line.jc_last_move)} · as-of ${localTime(fixture.stale_line.as_of)}`}
				>
					上次调盘 {stalenessText(fixture.stale_line.minutes_since_move)} 前
					{fixture.stale_line.drift !== null &&
					fixture.stale_line.drift !== undefined &&
					fixture.stale_line.drift_selection
						? `；sharp 参考 ${staleSelectionLabel(fixture.stale_line.drift_selection)} ${driftText(fixture.stale_line.drift)}（${
								fixture.stale_line.sharp_ref === "pinnacle" ? "主锚" : "共识"
							}）`
						: ""}
				</p>
			) : null}
			<div className="flex items-center gap-2">
				{SELECTIONS.map((sel) => (
					<OddsButton
						key={sel}
						fixture={fixture}
						selection={sel}
						value={fixture.jc_odds[sel]}
						selected={selected(sel)}
						disabled={!pickable}
						onPick={onPick}
						testid={`${pickTestidBase}-${fixture.fixture_id}-${sel}`}
						size="md"
					/>
				))}
			</div>
		</article>
	);
}

/** 陈旧时长文案（票 48 只读信号）：分/时/天就地换算。 */
function stalenessText(minutes: number): string {
	if (minutes < 60) return `${minutes} 分钟`;
	if (minutes < 24 * 60) {
		const hours = Math.floor(minutes / 60);
		const rest = minutes % 60;
		return rest > 0 ? `${hours} 小时 ${rest} 分` : `${hours} 小时`;
	}
	return `${Math.floor(minutes / (24 * 60))} 天`;
}

/** sharp 参考漂移文案：带符号百分比（正=该向概率上行）；|pct|<0.05 归零。 */
function driftText(drift: number): string {
	const pct = drift * 100;
	if (Math.abs(pct) < 0.05) return "0.0%";
	return `${pct > 0 ? "+" : ""}${pct.toFixed(1)}%`;
}

function staleSelectionLabel(selection: string): string {
	return SELECTION_LABELS[selection] ?? selection;
}
