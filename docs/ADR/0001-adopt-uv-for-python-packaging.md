# Adopt uv for Python packaging

Context: the repo has no Python package manager beyond a flat `requirements.txt` (prod and test/lint deps mixed together, installed via plain `pip` in the Dockerfile), and no lockfile — so builds aren't reproducible and CI has nothing fast to install from. We're adding a GitHub Actions pipeline that needs to install deps in every PR/push.

Decision: migrate to `uv` as the Python package manager, replacing `requirements.txt` with `pyproject.toml` + a committed `uv.lock`, with runtime deps and a `dev` dependency-group (pytest, pytest-asyncio, pytest-cov, ruff) split apart. The Dockerfile switches from `pip install -r requirements.txt` to `uv sync --frozen`, so CI and the production image resolve dependencies from the exact same lockfile.

Alternatives considered: keeping `pip`/`requirements.txt` as canonical and using `uv pip install -r requirements.txt` in CI only as a faster drop-in installer. Rejected because it leaves the reproducibility gap (no lockfile) and the prod/dev dependency mixing in place — CI would be fast but no more correct than today.
