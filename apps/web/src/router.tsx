import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { ErrorBoundary } from "react-error-boundary";
import { BankrollPage } from "./pages/bankroll-page";
import { BetsPage } from "./pages/bets-page";
import { PlaceholderPage } from "./pages/placeholder-page";
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

const indexRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/",
	component: TodayPage,
});
const reviewRoute = createRoute({
	getParentRoute: () => rootRoute,
	path: "/review",
	component: () => <PlaceholderPage title="复核" milestone="M3（LLM 线 + 双线融合）" />,
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
	component: () => <PlaceholderPage title="设置" milestone="M4（参数与数据源健康）" />,
});

const routeTree = rootRoute.addChildren([
	indexRoute,
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
