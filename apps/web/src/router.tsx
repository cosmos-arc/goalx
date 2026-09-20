import { createRootRoute, createRoute, createRouter, Outlet, redirect } from "@tanstack/react-router";
import { ErrorBoundary } from "react-error-boundary";
import { BankrollPage } from "./pages/bankroll-page";
import { BetsPage } from "./pages/bets-page";
import { FixtureResearchPage } from "./pages/fixture-research-page";
import { FixturesPage } from "./pages/fixtures-page";
import { GlossaryPage } from "./pages/glossary-page";
import { HistoryPage } from "./pages/history-page";
import { MarketGoalsPage } from "./pages/market-goals-page";
import { MarketHadPage } from "./pages/market-had-page";
import { MarketPoolPage } from "./pages/market-pool-page";
import { OverviewPage } from "./pages/overview-page";
import { ReviewPage } from "./pages/review-page";
import { StubPage } from "./pages/stub-page";
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
// 票 wb-01：/today 让位 /fixtures（场次页 3 日窗口），旧路径重定向不破坏书签。
// 票 wb-02：/fixtures/$id 单场研究页（场次轴第二层）。
// 票 wb-03：/markets 玩法组上线——胜平负真实页 + 进球/14场任9 占位引导态。
// 票 wb-05：/markets/goals 换进球玩法真实页（ttg/crs）。
// 票 wb-07：/markets/pool 换 14场任9 骨架页（结构先行，彩池数据源外部依赖）。
const indexRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/",
	component: OverviewPage,
});
const fixturesRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/fixtures",
	component: FixturesPage,
});
const fixtureResearchRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/fixtures/$id",
	component: FixtureResearchPage,
});
const todayRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/today",
	beforeLoad: () => {
		throw redirect({ to: "/fixtures" });
	},
});
const marketsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/markets",
	beforeLoad: () => {
		// 玩法组默认落胜平负（组内三入口由玩法页顶部 MarketTabs 切换）
		throw redirect({ to: "/markets/had" });
	},
});
const marketHadRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/markets/had",
	component: MarketHadPage,
});
const marketGoalsRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/markets/goals",
	component: MarketGoalsPage,
});
const marketPoolRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/markets/pool",
	component: MarketPoolPage,
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
	// 票 14：复核页点亮（复核队列 + 结论三分类 + 盲评二选一）
	component: ReviewPage,
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
	fixturesRoute,
	fixtureResearchRoute,
	todayRoute,
	marketsRoute,
	marketHadRoute,
	marketGoalsRoute,
	marketPoolRoute,
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
