import * as jestDomMatchers from "@testing-library/jest-dom/matchers";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, expect } from "vitest";
import { server } from "../mocks/server";

// The matcher-only entry point is workspace-isolation safe: unlike the
// convenience `/vitest` entry point, it does not resolve Vitest from inside
// jest-dom's package directory.
expect.extend(jestDomMatchers);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
	server.resetHandlers();
	cleanup();
});
afterAll(() => server.close());
