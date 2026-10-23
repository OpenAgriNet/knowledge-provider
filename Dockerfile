# syntax=docker/dockerfile:1
# Docker Hardened Images (dhi.io; needs `docker login dhi.io`). Build runs in the
# -dev variant (shell, root); the shipped image is the minimal nonroot runtime.
# Both put the interpreter at /usr/bin/python, so the venv copies across intact.
FROM dhi.io/python:3.10-dev AS build

WORKDIR /app

# Install system dependencies
# apt-get is skipped — curl is not needed at runtime.
# Uncomment below only if you need libreoffice (office→PDF) or curl:
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     curl \
#     && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

# Use the base image's own Python 3.10 rather than letting uv download a
# managed interpreter — there's no point fetching a second one.
ENV UV_PYTHON_DOWNLOADS=never

# Copy dependency files and install (no dev group: pytest/ruff aren't needed
# at runtime). pyproject.toml pins torch to the CPU-only wheel index —
# sentence-transformers depends on it, and default PyPI torch bundles CUDA
# and is far larger for no benefit in this container.
# Cache mounts persist uv's download cache across builds (even cache-busted
# ones) without baking it into the image layer, so a rebuild only
# re-downloads packages that actually changed.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Copy pipeline code
COPY pipeline/ ./pipeline/

# Catch imports that work locally but fail on the shipped Python 3.10 image.
RUN python -c "import pipeline.api"

# Copy test data for e2e tests
COPY test_data/ ./test_data/

# Create books directory
RUN mkdir -p /app/books

# Writable data dirs for the nonroot runtime user (uid 65532). New named
# volumes mounted here inherit this ownership; volumes created by an earlier
# root-run image need a one-time chown.
RUN mkdir -p /data/documents && chown -R 65532:65532 /data /app/books

FROM dhi.io/python:3.10

WORKDIR /app
COPY --from=build /app /app
COPY --from=build --chown=65532:65532 /data /data
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8001

# Default command (overridden in docker-compose)
CMD ["uvicorn", "pipeline.api:app", "--host", "0.0.0.0", "--port", "8001"]
