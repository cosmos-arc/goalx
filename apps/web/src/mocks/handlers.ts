import { HttpResponse, http } from "msw";

export const statusFixture = {
	app_name: "goalx-backend",
	app_version: "0.1.0",
	environment: "testing",
} as const;

export const handlers = [http.get("*/api/v1/status", () => HttpResponse.json(statusFixture))];
