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
- Query text, the raw `/search` payload and the caller's username or email stay in the exported lines, as do email-OTP lines naming an operator's email. Operators are staff, so shipping their identity is accepted. Farmer questions carry no personal data, and a support engineer needs the question to judge a poor answer. Unlike the DSS collector, there is no allowlist.
- This repo's compose ships a collector and ClickHouse under the `observability` profile, for laptops only. Shared environments point at the existing ClickStack collector instead of running their own.

Consequences:
- Logs only. Traces and metrics are not emitted.
- Every API request logs one line on `pipeline.api.requests`: method, path with query string, status and duration, never a body. A pure ASGI middleware times the whole response, so streamed downloads are timed to their last byte and a failure mid-stream is logged.
- Status below 400 logs at INFO (successful `/health` at DEBUG, since the UI polls it). 4xx logs at WARNING and 5xx at ERROR, with the error detail (validation errors keep only type, field and message, never the submitted value), or the HTTP reason when there is none. A 5xx raised from a caught exception carries that exception's traceback. Unhandled exceptions log at ERROR with their traceback. uvicorn's own access log doesn't reach the root logger and is not shipped.
- A Temporal worker interceptor (`pipeline/activity_logging.py`) logs every workflow's and activity's start and its completion, failure or cancellation, so new workflows and activities are covered without extra code. Activity failures log at WARNING without a traceback, because each attempt may be retried and Temporal's own `Completing activity as failed` line already carries it. Workflow failures log at ERROR with the traceback.
- Startup messages from the API and worker are log lines, not `print`s. A failed API startup or a worker crash logs at ERROR with its traceback, and the log buffer is flushed before the process exits.
- `/search` lines don't carry transaction or message ids yet. That comes with the next story, once callers send them.
- The shared-environment Helm values (endpoint, ingestion key) are set in the helmcharts repo, not here.
