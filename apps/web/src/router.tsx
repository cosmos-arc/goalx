import { createRootRoute, createRoute, createRouter, Outlet } from "@tanstack/react-router";
import { ErrorBoundary } from "react-error-boundary";
import { HomePage } from "./pages/home-page";

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
	component: HomePage,
});

const routeTree = rootRoute.addChildren([indexRoute]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
	interface Register {
		router: typeof router;
	}
}
