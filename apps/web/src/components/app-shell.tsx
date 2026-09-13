import { Link, useLocation } from "@tanstack/react-router";
import type { ReactNode } from "react";

const NAV_ITEMS = [
	{ to: "/", label: "今日" },
	{ to: "/review", label: "复核" },
	{ to: "/bets", label: "投注" },
	{ to: "/bankroll", label: "资金" },
	{ to: "/validation", label: "验证" },
	{ to: "/settings", label: "设置" },
] as const;

export function AppShell({ title, children }: { title: string; children: ReactNode }) {
	const location = useLocation();
	return (
		<main className="mx-auto min-h-screen max-w-6xl bg-white p-6 text-neutral-900">
			<header className="mb-6 flex flex-wrap items-baseline justify-between gap-4">
				<h1 className="text-2xl font-semibold">GoalX · {title}</h1>
				<nav aria-label="主导航" className="flex flex-wrap gap-3 text-sm">
					{NAV_ITEMS.map((item) => {
						const active = location.pathname === item.to;
						return (
							<Link
								key={item.to}
								to={item.to}
								aria-current={active ? "page" : undefined}
								className={
									active
										? "rounded-md bg-neutral-900 px-3 py-1.5 text-white"
										: "rounded-md px-3 py-1.5 text-neutral-600 hover:bg-neutral-100"
								}
							>
								{item.label}
							</Link>
						);
					})}
				</nav>
			</header>
			{children}
		</main>
	);
}
