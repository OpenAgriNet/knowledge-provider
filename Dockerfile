# syntax=docker/dockerfile:1
FROM dhi.io/uv:0.13.8 AS uv
FROM dhi.io/python:3.10.22-dev AS build

WORKDIR /app

COPY --from=uv /usr/local/bin/uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN mkdir -p /runtime/opt/hf-cache /runtime/model-cache

FROM dhi.io/python:3.10.22 AS app

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/opt/hf-cache \
    HOME=/opt/hf-cache \
    PYTHONUNBUFFERED=1

COPY --from=build --chown=65532:65532 /runtime/ /
COPY --from=build /app/.venv /app/.venv
COPY pipeline/ ./pipeline/
COPY migrations/ ./migrations/
COPY scripts/install_schema_pack.py ./scripts/install_schema_pack.py
COPY test_data/ ./test_data/

USER 65532:65532
RUN ["python", "-c", "import pipeline.api"]

EXPOSE 8001
CMD ["uvicorn", "pipeline.api:app", "--host", "0.0.0.0", "--port", "8001"]

FROM dhi.io/node:24.22.3-dev AS liteparse-deps
RUN npm install --prefix /opt/liteparse --omit=dev --no-audit --no-fund @llamaindex/liteparse@2.18.0

FROM app AS with-liteparse
COPY --from=liteparse-deps /usr/bin/node /usr/bin/env /usr/bin/
COPY --from=liteparse-deps /opt/liteparse /opt/liteparse
ENV LITEPARSE_EXECUTABLE=/opt/liteparse/node_modules/.bin/lit
RUN ["/opt/liteparse/node_modules/.bin/lit", "--version"]

FROM app AS standard
