import { useQuery } from "@tanstack/react-query";
import { client, type Status } from "../api/client";

async function fetchStatus(): Promise<Status> {
	const { data, error } = await client.GET("/api/v1/status");
	if (error !== undefined) {
		throw error;
	}
	return data;
}

export function HomePage() {
	const status = useQuery({ queryKey: ["status"], queryFn: fetchStatus });

	return (
		<main className="mx-auto flex min-h-screen max-w-3xl flex-col items-center justify-center gap-4 bg-white p-8 text-neutral-900">
			<h1 className="text-3xl font-semibold">GoalX</h1>
			{status.isPending ? <p data-testid="status-loading">Loading service status…</p> : null}
			{status.isError ? (
				<p data-testid="status-error" className="text-sm text-neutral-500">
					Backend unavailable — start it with <code>task server</code>.
				</p>
			) : null}
			{status.data ? (
				<p data-testid="status-ok" className="text-sm text-neutral-600">
					{status.data.app_name} v{status.data.app_version} · {status.data.environment}
				</p>
			) : null}
		</main>
	);
}
