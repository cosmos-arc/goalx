import { Link, useLocation } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { ThemeToggle } from "./theme-toggle";

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
		// 票 12：AppShell 改吃语义 token（bg-background 等），浅/暗双主题全应用生效
		<main className="mx-auto min-h-screen max-w-6xl bg-background p-6 text-foreground">
			<header className="mb-6 flex flex-wrap items-center justify-between gap-4">
				<h1 className="text-2xl font-semibold">GoalX · {title}</h1>
				<div className="flex items-center gap-3">
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
											? "rounded-md bg-primary px-3 py-1.5 text-primary-foreground"
											: "rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
									}
								>
									{item.label}
								</Link>
							);
						})}
					</nav>
					<ThemeToggle />
				</div>
			</header>
			{children}
		</main>
	);
}
