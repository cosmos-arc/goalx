import { Link, useLocation } from "@tanstack/react-router";

/**
 * 玩法切换（票 wb-03）：胜平负 / 进球 / 14场任9 三入口。
 * 一级导航只放"玩法"一个入口（两级扁平基调不动）；组内切换在玩法页顶部，
 * 形态沿用页内 Tab 先例（语义 nav + aria-current）。进球已上线（票 wb-05
 * ttg/crs），14场任9 仍为占位引导态（随票 07）。
 */
const MARKET_ENTRIES = [
	{ to: "/markets/had", label: "胜平负" },
	{ to: "/markets/goals", label: "进球" },
	{ to: "/markets/pool", label: "14场任9" },
] as const;

export function MarketTabs() {
	const location = useLocation();
	return (
		<nav aria-label="玩法" data-testid="market-tabs" className="mb-5 flex flex-wrap gap-1.5 text-sm">
			{MARKET_ENTRIES.map((market) => {
				const active = location.pathname === market.to;
				return (
					<Link
						key={market.to}
						to={market.to}
						aria-current={active ? "page" : undefined}
						className={`rounded-md border px-3 py-1.5 transition-colors ${
							active
								? "border-primary bg-primary font-medium text-primary-foreground"
								: "border-border text-muted-foreground hover:bg-muted hover:text-foreground"
						}`}
					>
						{market.label}
					</Link>
				);
			})}
		</nav>
	);
}
