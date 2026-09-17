import { Link, useLocation } from "@tanstack/react-router";
import type { ReactNode } from "react";
import type { AppRoute } from "../lib/ui";
import { ThemeToggle } from "./theme-toggle";

/**
 * 票 03 定稿的两级扁平导航（票 13 落地；票 wb-01 今日→场次）：不设组标题。
 * 一级 = 高频页（总览/场次/投注/历史/验证/资金）；次级 = 低处页脚位（词典/复核/设置）。
 */
const PRIMARY_NAV = [
	{ to: "/", label: "总览" },
	{ to: "/fixtures", label: "场次" },
	{ to: "/bets", label: "投注" },
	{ to: "/history", label: "历史" },
	{ to: "/validation", label: "验证" },
	{ to: "/bankroll", label: "资金" },
] as const;

const SECONDARY_NAV = [
	{ to: "/glossary", label: "词典" },
	{ to: "/review", label: "复核" },
	{ to: "/settings", label: "设置" },
] as const;

type NavItem = { to: AppRoute; label: string };

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
	return (
		<Link
			to={item.to}
			aria-current={active ? "page" : undefined}
			className={
				active
					? "rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground"
					: "rounded-md px-3 py-1.5 text-muted-foreground hover:bg-muted hover:text-foreground"
			}
		>
			{item.label}
		</Link>
	);
}

export function AppShell({ title, children }: { title: string; children: ReactNode }) {
	const location = useLocation();
	return (
		// 票 12：语义 token（bg-background 等）浅/暗双主题全应用生效
		<div className="flex min-h-screen flex-col bg-background text-foreground">
			<header className="border-b border-border">
				<div className="mx-auto flex w-full max-w-6xl flex-wrap items-center justify-between gap-x-4 gap-y-2 px-6 py-2.5">
					<h1 className="text-xl font-semibold">GoalX · {title}</h1>
					<div className="flex items-center gap-3">
						<nav aria-label="主导航" className="flex flex-wrap items-center gap-1 text-sm">
							{PRIMARY_NAV.map((item) => (
								<NavLink key={item.to} item={item} active={location.pathname === item.to} />
							))}
						</nav>
						<ThemeToggle />
					</div>
				</div>
			</header>
			<main className="mx-auto w-full max-w-6xl flex-1 px-6 py-6">{children}</main>
			<footer className="border-t border-border">
				<nav
					aria-label="次级导航"
					className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-1 px-6 py-2 text-xs"
				>
					{SECONDARY_NAV.map((item) => (
						<NavLink key={item.to} item={item} active={location.pathname === item.to} />
					))}
				</nav>
			</footer>
		</div>
	);
}
