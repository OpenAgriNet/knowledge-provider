---
status: accepted
---

# Ship API and worker logs to ClickHouse over OTLP

Context: The API and worker only log to stdout, so a support engineer has to reach into a server to see why a farmer's search went wrong. The decision support system already sends its telemetry to ClickHouse, where HyperDX reads it (engineering-tracker#241).

Decision:
- The API and worker attach an OpenTelemetry log handler to the root logger when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. With it unset, nothing changes. The handler goes on root because `/search` and `api.py` log through `logging.info(...)` directly, not through module loggers.
- Only `INFO` and above is exported. The level sits on the handler, so `LOG_LEVEL=DEBUG` still prints locally without shipping debug output.
- `opentelemetry.*` records are not exported, so the exporter's own failures can't feed back into it.
- Lines are batched and dropped when the collector is unreachable. Search and uploads never wait on the log store.
- Endpoint and auth headers come from the standard `OTEL_*` variables. Service name falls back to `knowledge-provider` when `OTEL_SERVICE_NAME` is unset, so lines are never labelled `unknown_service`.
- An export gives up after 5 seconds unless `OTEL_EXPORTER_OTLP_TIMEOUT` says otherwise, so shutdown with the collector down fits inside Docker's 10-second stop grace.
- Operator emails are not shipped. `/search` lines name the caller by Keycloak user id, and email-OTP lines go to the `pipeline.auth.email_otp` logger, which is not exported. They still print locally.
- Query text and the raw `/search` payload stay in the exported lines. Farmer questions carry no personal data, and a support engineer needs the question to judge a poor answer. Unlike the DSS collector, there is no allowlist.
- This repo's compose ships a collector and ClickHouse under the `observability` profile, for laptops only. Shared environments point at the existing ClickStack collector instead of running their own.

Consequences:
- Logs only. Traces and metrics are not emitted.
- uvicorn access lines don't propagate to root, so they are not shipped.
- `print(...)` output, used for some worker and API startup messages, stays on stdout and is not shipped.
- `/search` lines don't carry transaction or message ids yet. That comes with the next story, once callers send them.
- The shared-environment Helm values (endpoint, ingestion key) are set in the helmcharts repo, not here.
