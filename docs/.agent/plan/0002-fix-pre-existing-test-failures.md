# Fix pre-existing test/lint failures surfaced by CI

Status: needs-triage

Discovered while wiring up [0001-github-ci-pipeline](./0001-github-ci-pipeline.md): test and lint failures unrelated to the CI/uv/eslint tooling work, confirmed pre-existing. CI intentionally ships red on these until someone with the right context fixes them.

## Python: `tests/test_email_otp.py` and `tests/test_translation.py` (9 failures)

Unrelated to the uv migration — no lockfile ever existed, so any fresh install today reproduces them.

## `tests/test_email_otp.py` (7 failures)

Drift from the security-hardening commit `f28869e` ("keyed OTP-code hashing, close the residual bypass window"):

- `test_verify_maps_keycloak_errors_to_actionable_text` (4 cases) and `test_verify_flags_a_missing_flow_binding_as_misconfiguration`: `verify_otp` now returns a generalized `401: "That code is no longer valid. Request a new one."` for all failure modes (likely deliberate, to close a user-enumeration/error-oracle window) instead of the specific messages/status codes the tests assert. Need someone with auth context to confirm this generalization is intentional and update the tests to match, or restore differentiated errors if it was accidental.
- `test_verify_returns_the_full_token_set`: same generalized-401 issue blocks the happy path from being tested at all.
- `test_config_falls_back_to_vite_client_id`: asserts `cfg.send_url`, but `EmailOtpConfig` no longer has that attribute (renamed/restructured in the same refactor).
- `test_send_otp_passes_through_realm_policy`: fails with `sqlite3.OperationalError: no such table: email_otps` — the test DB init path doesn't create this table; a real schema-setup gap, not just a stale assertion.

## `tests/test_translation.py` (1 failure)

`test_load_translation_config_defaults` expects `config.model == "gemma-4-31b-it"` (matching `pipeline/config.py`'s default), but `pipeline/translation/service.py`'s own fallback is `"google/gemma-4-31b-it"` — the two defaults have drifted apart. Needs a decision on which is the actually-correct model name before either the test or one of the two source defaults can be fixed.

## Frontend: `ui/src` (4 ESLint errors)

Surfaced by wiring up `eslint` for the first time (see [0001-github-ci-pipeline](./0001-github-ci-pipeline.md)); the mechanical unused-import/dead-code findings were cleaned up as part of that work, but these four touch actual control flow and need a feature/security-context call, not a blind fix:

- `src/auth/AuthProvider.jsx:227` (`no-unsafe-finally`): a `return` inside a `finally` block (`if (cancelled) return`), guarding against a React StrictMode double-invoke race. May be intentional, but `finally`-returns can silently swallow the try/catch's own control flow — needs someone with the auth-flow context to confirm or restructure.
- `src/views/DocumentOpsView.jsx`: three unused declarations that look like incomplete wiring rather than dead code — `saveChunk` (a whole async handler, never called from any UI element), `canApproveOcr` (computed but never read — might be a missing gate on an approve action), `totalPages` (computed but never read). Each needs a decision: finish wiring it up, or confirm it's safe to delete.

## Out of scope for this entry

Actually deciding/fixing the correct behavior — this just records what CI will show as red and why, so it isn't mistaken for a regression from the CI pipeline work itself.
