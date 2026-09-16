import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { ErrorBoundary } from "react-error-boundary";
import { BankrollPage } from "./pages/bankroll-page";
import { BetsPage } from "./pages/bets-page";
import { GlossaryPage } from "./pages/glossary-page";
import { HistoryPage } from "./pages/history-page";
import { OverviewPage } from "./pages/overview-page";
import { StubPage } from "./pages/stub-page";
import { TodayPage } from "./pages/today-page";
import { ValidationPage } from "./pages/validation-page";

function RootLayout() {
	return (
		<ErrorBoundary fallback={<p role="alert">Something went wrong. Reload the page to retry.</p>}>
			<Outlet />
		</ErrorBoundary>
	);
}

const rootRoute = createRootRoute({
	component: RootLayout,
	notFoundComponent: () => <p>Page not found.</p>,
});

// 票 03 定稿的路由结构（票 13 落地）：`/` 换总览占位、`/today` 承接今日、
// 新增 /history /glossary 占位；/review /settings 维持 M3/M4 引导态，其余路由不动零迁移。
const indexRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/",
	component: OverviewPage,
});
const todayRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/today",
	component: TodayPage,
});
const historyRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/history",
	component: HistoryPage,
});
const glossaryRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/glossary",
	component: GlossaryPage,
});
const reviewRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/review",
	component: () => (
		<StubPage
			title="复核"
			message="复核页用于赛后复盘与 LLM 情报的双线对照审阅。"
			hint="随 M3（LLM 线 + 双线融合）上线。"
		/>
	),
});
const betsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/bets",
	component: BetsPage,
});
const bankrollRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/bankroll",
	component: BankrollPage,
});
const validationRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/validation",
	component: ValidationPage,
});
const settingsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/settings",
	component: () => <StubPage title="设置" message="设置页将集中参数与数据源健康管理。" hint="随 M4 上线。" />,
});

const routeTree = rootRoute.addChildren([
	indexRoute,
	todayRoute,
	historyRoute,
	glossaryRoute,
	reviewRoute,
	betsRoute,
	bankrollRoute,
	validationRoute,
	settingsRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
	interface Register {
		router: typeof router;
	}
}
