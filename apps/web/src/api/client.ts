import createClient from "openapi-fetch";
import type { components, paths } from "./generated/schema";

export type Status = components["schemas"]["StatusResponse"];

// Same-origin by default: in dev the Vite server proxies /api to the backend.
// jsdom tests get an absolute base automatically (Node fetch rejects relative
// URLs); VITE_API_BASE_URL overrides for split deployments.
const configuredBase = import.meta.env["VITE_API_BASE_URL"];
export const apiBaseUrl =
	configuredBase || (typeof globalThis.location === "undefined" ? "" : globalThis.location.origin);

export const client = createClient<paths>({
	baseUrl: apiBaseUrl,
	// Resolve fetch at call time: openapi-fetch otherwise captures
	// globalThis.fetch when this module loads, which predates test-time
	// interceptors (msw) patching it.
	fetch: (...args: Parameters<typeof globalThis.fetch>) => globalThis.fetch(...args),
});
