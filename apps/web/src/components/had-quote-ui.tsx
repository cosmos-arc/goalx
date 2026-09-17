import type { HadQuoteStatus } from "../api/goalx";
import { SELECTION_LABELS, TABULAR_NUMS } from "../lib/ui";
import { GlossaryTerm } from "./glossary-term";
import { Badge } from "./ui/badge";

/**
 * 场次轴共享 UI 原语（票 wb-02 抽出）：资格徽章/选注钮/had 判读助手。
 * 场次列表页与研究页共用，保证两页编码口径逐像素一致
 * （票 04 定稿的编码硬约束：EV 红涨绿跌、资格蓝红灰、琥珀警示）。
 */

export type Selection = "h" | "d" | "a";

export const SELECTIONS: Selection[] = ["h", "d", "a"];
export const MAX_LEGS = 2;

export const FLAG_LABELS: Record<string, string> = {
	ev_deviation: "EV 偏差≥5%",
	few_books: "样本少",
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
