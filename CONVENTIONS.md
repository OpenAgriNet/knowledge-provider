# Conventions

Repo-wide conventions for naming, versioning, git workflow, logging, and linting for python modules. Referenced from `AGENTS.md`.

## Naming Conventions

### Python
| Artifact | Convention | Example |
|---|---|---|
| Module | `snake_case.py` | `dss_models.py` |
| Class | `PascalCase` noun | `UserTurn` |
| Function | `snake_case` verb | `stream_dss_turn` |
| Constant | `UPPER_SNAKE_CASE` | `DEFAULT_CHANNEL` |
| Type alias | `PascalCase` | `LanguageCode = str` |

### Contract naming
- Field names: `snake_case` in Python and JSON — no transformation at the boundary.
- Language codes: BCP-47 only (`gu`, `hi`, `en`) — never full names (`gujarati`).
- IDs: always `str`, never `int` for newly added code.
- API versioning: version at module level on breaking changes, never suffix field names.
- RBAC roles: canonical vocabulary only — `superadmin`/`master_admin` (platform-wide) > `admin` (state-scoped, via `instances`/groups) > `content_curator` (upload/review/pipeline/search) > `viewer` (search only); state-scoped variants use `state_admin` / `state_approver` / `state_contributor` / `state_view`. No new role name without updating it in `docs/architecture-pipeline-rbac.md` too.

### Provider plugins
Each domain package (`ocr/`, `translation/`, `chunking/`, `vector_store/`) follows the same shape: a `base.py` protocol/ABC, one module per concrete provider (`chandra_vllm.py`, `gemma_vllm.py`, ...), and a `service.py` factory that dispatches on a `{DOMAIN}_PROVIDER` env var (`OCR_PROVIDER`, `TRANSLATION_PROVIDER`, `CHUNKING_PROVIDER`). A new provider implements the base contract and registers in that package's `service.py` — never branch on provider name inside `activities.py` or `api.py`.

### Environment variables
`UPPER_SNAKE_CASE`, grouped by prefix. Booleans: `true` / `false` only.
```
LLM_FALLBACK_ENABLED=true
HINDI_CHAT_ENABLED=false
MCP_TOOL_TIMEOUT_SECONDS=10
```

### Repository naming
kebab-case, specific to the capability. No `oan-`/`dpg-` prefix — decided against prefixing.
Examples: `decision-support-system`, `knowledge-provider`.

## Changelog
`CHANGELOG.md` at repo root. `[Unreleased]` section always present. It will be added for newly added changes.
```markdown
## [Unreleased]

## [1.1.0] - 2026-08-18

### Added
- UserTurn normalized turn contract (#42)
- get-crop-advisory MCP tool (#55)

### Fixed
- Fall back to source_lang when target_lang is empty (#38)
```

## Git Workflow

### Branch naming
`{type}/{issue-no}-{short-description}`
```
feat/42-dss-user-turn-contract
fix/38-translation-empty-target-lang
```

### Commit messages
`<type>: <summary in imperative mood> [#<issue-no>]`

Issue number makes commits grep-able: `git log --grep="#42"`.
Scopes are optional — add one only when the repo grows large enough that filtering by area is genuinely useful. Don't define them speculatively. 
This rule is for newly added commits.

| Type | When | Version impact |
|---|---|---|
| feat | New capability | MINOR |
| fix | Bug fix | PATCH |
| refactor | No behaviour change | None |
| chore | Tooling, deps | None |
| test | Tests only | None |
| docs | Docs only | None |

`BREAKING CHANGE:` in footer → MAJOR bump regardless of type.

```
feat: introduce UserTurn contract and stream_dss_turn [#42]
fix: fall back to source_lang when target_lang is empty [#38]
refactor: delegate v2 path to stream_dss_turn [#61]
```

### Pull requests
- Title: follows Conventional Commits (drives changelog).
- Body: always ends with `Closes #<issue-no>` — auto-closes issue on merge, enables cycle-time tracking.

```markdown
## What
Introduces the normalized UserTurn contract and stream_dss_turn entry point.

## Why
Any adopter can now call DSS without knowing about the underlying pipeline.

## Testing
Smoke tested on Gujarati and English with equivalent v1/v2 output.

Closes #42
```

Traceability chain: Issue #42 → Branch `feat/42-...` → Commits `[#42]` → PR "Closes #42" → Merged → Issue closed.

### Merge strategy
Rebase merge only for new PR's. Each commit lands individually on `main` — individual commits are the source of truth for the changelog and `git log` traceability.
- Squash within a branch is fine for cleanup (typos, formatting).
- Never squash the entire PR on merge — individual commit history is lost.
- If a PR is squash-merged by accident: the PR title and `Closes #issue` still preserve traceability and changelog correctness; only intra-PR commit granularity is lost.

## Logging
Always include `request_id`. Never log raw PII in newly added code.
```python
# Good
logger.info("request_id=%s moderation_category=%s", request_id, category)
logger.warning("request_id=%s pretranslation_failed=True falling_back=True", request_id)
```
Levels: `DEBUG` internal state · `INFO` turn lifecycle · `WARNING` recoverable failure · `ERROR` unrecoverable failure.

In `pipeline`/`worker` code with no HTTP request in scope, use `workflow_id` as the correlation key instead of `request_id` — it's the identifier that actually threads through Temporal signals, activities, and the audit log for a given document.

## Linting & Formatting
One tool across all python modules in this repo: **ruff**. Replaces black, flake8, and isort — one tool, one config, consistent across every repo.
- pre-commit: `ruff` lint + format runs automatically on every commit (fast, < 1s).
- pre-push: full test suite runs before code leaves local.
- CI: linting must also pass in CI — local hooks can be bypassed with `--no-verify` so CI is the safety net.
- Same ruff config copied into every python module — enforced in CI so it cannot drift silently between repos.