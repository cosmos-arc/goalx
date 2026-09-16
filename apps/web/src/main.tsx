import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { applyTheme, resolveTheme } from "./lib/theme";
import { router } from "./router";
import "./styles/globals.css";

// 首帧渲染前应用主题（票 12）：持久化值优先，否则跟随系统偏好，避免闪错主题
applyTheme(resolveTheme());

const queryClient = new QueryClient();

const container = document.getElementById("root");
if (!container) {
	throw new Error("#root element missing in index.html");
}

createRoot(container).render(
	<StrictMode>
		<QueryClientProvider client={queryClient}>
			<RouterProvider router={router} />
		</QueryClientProvider>
	</StrictMode>,
);
