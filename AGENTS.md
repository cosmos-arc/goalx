# AGENTS.md

Guidance for ZCode agents working in this repository.

## Project

**goalx** — FastAPI backend + React web monorepo under the `cosmos-arc` org
(remote: `git@github.com:cosmos-arc/goalx.git`), MIT licensed. The tech-stack
skeleton is ported from the `ditto` repo with quant/business libraries
removed; the backend is organized into domain packages (see ADR-0008).

## Layout

- `apps/backend/` — Python package `goalx_backend` (uv workspace member, src layout)
  with domain packages `data/` (fixtures/odds/results + `data/ingest/`),
  `modelling/` (DC model, score matrix, forecast), `evaluation/` (backtest,
  baseline, CLV, haircut, validation), `betting/` (lifecycle, settlement
  orchestration, ledger audit); `api/`, `cli.py`, `flows.py`, `main.py` are
  delivery. Each domain package owns its tables' SQL — no raw SQL outside the
  owning package (ADR-0008).
- `apps/web/` — `@goalx/web` React app (bun workspace member)
- `contracts/openapi/v1.json` — reviewed OpenAPI contract, single source of truth
- `docs/agents/` — agent-skill configuration (issue tracker, triage labels, domain docs)
- Root — uv + bun workspaces, `Taskfile.yml`, pre-commit, CI under `.github/`

## Commands

All development goes through go-task (`brew install go-task`); every command
below assumes `task`:

- `task bootstrap` — uv sync + bun install + Playwright chromium + pre-commit hooks
- `task check` — the full local gate, including e2e (CI additionally runs the
  CI-only security workflow); run before pushing
- `task fmt` / `task fmt-check` / `task lint` — ruff format + lint
- `task type` — basedpyright strict
- `task test` (coverage gate ≥90%) / `task test-fast` — backend
- `task web-lint` / `task web-type` / `task web-coverage` / `task web-build` / `task web-e2e` — web
- `task contract-export` + `task contract-codegen` — after any API change, commit the resulting diffs together (CI's `check-contract` fails on drift)

## Conventions

- Python: ruff strict (docstrings, annotations, security rules) + basedpyright
  `standard` with `strict` on `apps/backend/src`; tests relax ANN/D via
  per-file-ignores. Line length 88, double quotes.
- TypeScript/Biome: tabs, width 120, double quotes, semicolons; `noExplicitAny`
  is an error; generated files (`src/api/generated/**`) are lint-exempt.
- Commits: Conventional Commits, enforced by the commit-msg hook; `main` is
  protected by `no-commit-to-branch`.
- API changes flow contract-first: edit the FastAPI app → `task contract-export`
  → review the `contracts/openapi/v1.json` diff → `task contract-codegen` →
  commit both. Redocly `recommended-strict` lint gates the contract; the two
  rule exemptions in `redocly.yaml` (security-defined, operation-4xx-response)
  must be removed when real endpoints/auth land.

## Gotchas

- uv is version-pinned (`required-version` in `pyproject.toml`) and Python is
  uv-managed (`only-managed`); run `task python-install` to sync, never `pip`.
- Don't re-add `orjson`/`ORJSONResponse` (deprecated in FastAPI 0.13x). With
  typed responses the direct pydantic-core path measured ~15× faster than the
  old `jsonable_encoder` + orjson pipeline on nested models (374 µs vs 5.6 ms
  per 1000-item page): the old pipeline's cost is the pure-Python
  `jsonable_encoder` walk, not the dumper. Revisit only for large
  non-Pydantic dict payloads, where orjson beats stdlib json ~5×.
- Bun uses the **isolated** linker. Two consequences learned the hard way:
  jest-dom must be imported via `@testing-library/jest-dom/matchers` (the
  `/vitest` entry point fails to resolve vitest), and openapi-fetch captures
  `globalThis.fetch` at `createClient()` time — pass the call-time thunk as we
  do in `src/api/client.ts` or msw interception silently fails.
- Node fetch rejects relative URLs in jsdom tests; the API client resolves its
  base from `globalThis.location.origin` (override with `VITE_API_BASE_URL`).
- The `.gitignore` covers Python/Bun/coverage artifacts and `.scratch/` (local
  issue tracker files are intentionally untracked). `.scratch/` is working
  memory only: when a feature or review cycle closes, copy its durable
  artifacts (spec, research notes, audit reports) into `docs/plans/` and commit
  them — preserving the relative layout so cross-links keep resolving (see
  `docs/plans/goalx-quant/` for the pattern).
- Branching: `main` is protected (`no-commit-to-branch` hook + fail-closed CI);
  land changes via short-lived branches and PRs, as in ditto.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`) unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
