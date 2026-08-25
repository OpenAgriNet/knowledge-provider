# GitHub Actions CI pipeline

Status: ready to implement (settled via grilling session, 2026-08-25). See [ADR-0001](../../ADR/0001-adopt-uv-for-python-packaging.md) for the uv decision.

## Goal

Add `.github/workflows/ci.yml` (none exists today) covering both `pipeline/` (Python) and `ui/` (frontend): lint, test, coverage — failing the pipeline on any lint or test failure, publishing coverage as a GitHub artifact.

## Triggers

`pull_request` and `push` to `main`.

## Job structure

Two parallel jobs, `pipeline` and `ui`. Within each job: lint step gates the test step (test doesn't run if lint fails). Coverage is collected and uploaded per job as a separate artifact (`coverage-pipeline`, `coverage-ui`) — not merged.

## Python (`pipeline` job)

**Baseline facts:** Python 3.10 (pinned in `Dockerfile:2`, only source of truth). No `pyproject.toml`/lockfile exists — just a flat `requirements.txt` with prod and test deps (`pytest`, `pytest-asyncio`, `pytest-cov`) mixed together, installed via plain `pip` in the Dockerfile. No lint tool configured anywhere, though `CONVENTIONS.md` documents an aspirational ruff-only policy. `pytest.ini` already defines `testpaths=tests` and markers `unit`/`integration`/`slow`/`api`/`db`/`workflow` — but only `unit` (97), `api` (21), `db` (25) are actually used; `integration`/`slow`/`workflow` are unused today.

**Tasks:**

1. **Migrate to uv** ([ADR-0001](../../ADR/0001-adopt-uv-for-python-packaging.md)):
   - Create `pyproject.toml`: `[project]` with the 28 deps currently in `requirements.txt` (minus the 3 test deps), `requires-python = ">=3.10"`; `[dependency-groups] dev = ["pytest>=7.0.0", "pytest-asyncio>=0.21.0", "pytest-cov>=4.0.0", "ruff"]`.
   - Run `uv lock` to generate and commit `uv.lock`.
   - Delete `requirements.txt`; update any docs referencing it (`README.md` local-setup instructions).
   - Update `Dockerfile`: multi-stage-copy the `uv` binary from `ghcr.io/astral-sh/uv` and replace the `pip install -r requirements.txt` step with `uv sync --frozen` (keep the `INSTALL_TORCH` conditional logic and the `python -c "import pipeline.api"` sanity check).

2. **Add ruff**, config lives in `pyproject.toml`'s `[tool.ruff]` (fresh default: `line-length = 100`, `target-version = "py310"` — no existing OAN config was found to copy). Rule selection ended up as `E4/E7/E9/F/I` (ruff's own default subset of `E`), not the full `E` category: selecting all of `E` surfaces 279 `E501` (line-too-long) hits, since this codebase has never had a line-length policy enforced. Applies repo-wide to Python modules (`pipeline/`, `scripts/`, `tests/`) per the `CONVENTIONS.md` policy.

3. **Fix `tests/conftest.py`'s `test_client` fixture** so `test_api.py` doesn't require a live Temporal server / MinIO. `pipeline/api.py`'s real `lifespan()` calls `Client.connect()` and `Minio(...).bucket_exists()`; `test_client` currently enters `TestClient` without neutralizing that lifespan (unlike `test_scheme_catalog.py`'s `asgi_client` fixture, which already no-ops `api.app.router.lifespan_context` before entering). Apply the same no-op pattern to `test_client` so all 13 test files run with zero external services.

4. **CI steps** (`pipeline` job): checkout → `astral-sh/setup-uv` (with caching) → `uv sync --frozen` (includes dev group) → `uv run ruff check .` → `uv run pytest --cov=pipeline --cov-report=xml --cov-report=html` → `actions/upload-artifact` for `htmlcov/` as `coverage-pipeline`.

## Frontend (`ui` job)

**Baseline facts:** Vite + React 18, plain JS (no TypeScript). `ui/package.json` has no lockfile committed — `package-lock.json` is explicitly gitignored, and `ui/Dockerfile.prod` already has an `npm ci`-if-lockfile-else-`npm install` fallback that today always takes the `npm install` path. No ESLint, no Prettier, no test runner, zero test files. Node 20 (pinned via `ui/Dockerfile`'s `node:20-alpine`).

**Tasks:**

1. **Commit a lockfile**: remove `package-lock.json` from `ui/.gitignore`, run `npm install` once to generate it, commit it. This also makes `ui/Dockerfile.prod`'s existing `npm ci` path actually get taken.

2. **Add ESLint**: flat config (`eslint.config.js`), `eslint:recommended` + `eslint-plugin-react-hooks` + `eslint-plugin-react-refresh` — the standard Vite React template baseline, no extra stylistic rules.

3. **Add Vitest**: `vitest.config.js` (or merged into `vite.config.js`), `@vitest/coverage-v8` for coverage. Set `test.passWithNoTests: true` since there are no test files yet — writing actual UI tests is out of scope for this pipeline change.

4. **CI steps** (`ui` job): checkout → `actions/setup-node@v4` (node 20, cache npm via lockfile) → `npm ci` → `npm run lint` → `npm run test -- --coverage` → `actions/upload-artifact` for the coverage output dir as `coverage-ui`.

## Explicitly out of scope

- No coverage threshold gating (only collect + publish, per original ask).
- No CI service containers for Temporal/MinIO/Postgres — the conftest.py fix removes the need.
- No mypy / stylistic ESLint rules beyond the recommended baseline.
- No actual UI test cases written — only the runner + `passWithNoTests`.
- No GitHub branch-protection/required-status-check configuration (repo settings, not workflow YAML).
