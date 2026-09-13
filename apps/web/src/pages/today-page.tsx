import { useQuery } from "@tanstack/react-query";
import { fetchTodayFixtures, type TodayFixture } from "../api/goalx";
import { AppShell } from "../components/app-shell";

const FLAG_LABELS: Record<string, string> = {
	ev_deviation: "EV 偏差≥5%",
	not_joined: "未 join 欧赔",
	few_books: "样本少",
};

function formatKickoff(utc: string): string {
	return utc.slice(5, 16).replace("T", " ");
}

function oddsText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : value.toFixed(2);
}

function probText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function evText(value: number | null | undefined): string {
	return value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

function fixtureRow(fixture: TodayFixture) {
	const evs = [fixture.ev?.h, fixture.ev?.d, fixture.ev?.a].filter(
		(value): value is number => value !== null && value !== undefined,
	);
	const maxAbsEv = evs.length > 0 ? Math.max(...evs.map(Math.abs)) : 0;
	return (
		<tr key={fixture.fixture_id} data-testid="today-row" className={fixture.joined ? "" : "opacity-60"}>
			<td className="px-2 py-1.5 text-neutral-500">{formatKickoff(fixture.kickoff_utc)}</td>
			<td className="px-2 py-1.5">{fixture.match_code}</td>
			<td className="px-2 py-1.5">
				{fixture.competition}
				{fixture.tier === "tier1" ? (
					<span className="ml-1 rounded bg-amber-100 px-1 text-xs text-amber-800">T1</span>
				) : null}
			</td>
			<td className="px-2 py-1.5 font-medium">
				{fixture.home_team} vs {fixture.away_team}
				{fixture.is_single ? (
					<span className="ml-1 rounded bg-neutral-100 px-1 text-xs text-neutral-600">单固</span>
				) : null}
			</td>
			<td className="px-2 py-1.5 tabular-nums">
				{oddsText(fixture.jc_odds.h)} / {oddsText(fixture.jc_odds.d)} / {oddsText(fixture.jc_odds.a)}
			</td>
			<td className="px-2 py-1.5 tabular-nums text-neutral-600">
				{fixture.eu_prob
					? `${probText(fixture.eu_prob.h)} / ${probText(fixture.eu_prob.d)} / ${probText(fixture.eu_prob.a)}`
					: "—"}
			</td>
			<td className="px-2 py-1.5 tabular-nums" data-testid="ev-cell">
				{fixture.ev ? (
					<span className={maxAbsEv >= 0.05 ? "font-semibold text-amber-700" : ""}>
						{evText(fixture.ev.h)} / {evText(fixture.ev.d)} / {evText(fixture.ev.a)}
					</span>
				) : (
					"—"
				)}
			</td>
			<td className="px-2 py-1.5 text-center tabular-nums">{fixture.books || "—"}</td>
			<td className="px-2 py-1.5 text-neutral-500">{fixture.jc_updated_at?.slice(5, 16).replace("T", " ") ?? "—"}</td>
			<td className="px-2 py-1.5">
				{(fixture.flags ?? []).length === 0 ? (
					"—"
				) : (
					<span className="flex flex-wrap gap-1">
						{(fixture.flags ?? []).map((flag) => (
							<span
								key={flag}
								data-testid={`flag-${flag}`}
								className="rounded bg-red-50 px-1.5 py-0.5 text-xs text-red-700"
							>
								{FLAG_LABELS[flag] ?? flag}
							</span>
						))}
					</span>
				)}
			</td>
		</tr>
	);
}

export function TodayPage() {
	const today = useQuery({ queryKey: ["today"], queryFn: () => fetchTodayFixtures() });

	return (
		<AppShell title="今日">
			<p className="mb-4 text-sm text-neutral-500">
				竞彩场次对照表：竞彩赔率 vs 欧洲共识隐含概率（Shin 去晦）、EV、books 数、调盘时点。 采集：
				<code>task ingest-jingcai</code> / <code>task ingest-odds</code>。
			</p>
			{today.isPending ? <p data-testid="today-loading">加载今日场次…</p> : null}
			{today.isError ? (
				<p data-testid="today-error" className="text-sm text-neutral-500">
					后端不可用或当日无数据 — 用 <code>task server</code> 启动 API，先跑一次
					<code>task ingest-jingcai</code>。
				</p>
			) : null}
			{today.data ? (
				today.data.length === 0 ? (
					<p data-testid="today-empty" className="text-sm text-neutral-500">
						当日无在售场次。
					</p>
				) : (
					<div className="overflow-x-auto">
						<table className="w-full text-sm">
							<thead>
								<tr className="border-b border-neutral-200 text-left text-xs text-neutral-500">
									<th className="px-2 py-2">时间(UTC)</th>
									<th className="px-2 py-2">编号</th>
									<th className="px-2 py-2">联赛</th>
									<th className="px-2 py-2">主 vs 客</th>
									<th className="px-2 py-2">竞彩 H/D/A</th>
									<th className="px-2 py-2">欧共识 p</th>
									<th className="px-2 py-2">EV H/D/A</th>
									<th className="px-2 py-2">books</th>
									<th className="px-2 py-2">调盘</th>
									<th className="px-2 py-2">标记</th>
								</tr>
							</thead>
							<tbody className="divide-y divide-neutral-100">{today.data.map(fixtureRow)}</tbody>
						</table>
					</div>
				)
			) : null}
		</AppShell>
	);
}
