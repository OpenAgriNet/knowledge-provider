# Spec: Search results can be limited to one group

Story: https://github.com/OpenAgriNet/engineering-tracker/issues/202
PRD: none found (the only PRD on file covers complaints and scheme status)

## Architecture / approach

Two parts, done in this order.

### Part A: small refactor, `pipeline/api.py` becomes a package

Today `pipeline/api.py` is one 4.3k-line module. Everything that reaches it
does so by the module name `pipeline.api`: the Dockerfile
(`uvicorn pipeline.api:app` and `python -c "import pipeline.api"`), four
test files, and the `test_client` fixture in `tests/conftest.py`, which
patches `api.temporal_client`, `api.minio_client` and `api.app`.

The move keeps that name:

1. `git mv pipeline/api.py pipeline/api/__init__.py`. Python resolves
   `pipeline.api` to the package, so every import, the uvicorn target and
   the fixture patches keep working with no edits. (Java has no equivalent
   of code living in a package's init; think of it as the module keeping
   its name while gaining a folder.)
2. Change the 28 relative imports inside it from `from .x` to `from ..x`
   (one level deeper now). Includes the imports inside function bodies.
3. Create `pipeline/api/search.py` holding an `APIRouter` with
   `POST /search` and its private helper cluster, which is used by nothing
   else in the file: `_normalize_text`, `_tokenize`,
   `_prepare_query_for_e5` (unused today, drop it), `_token_overlap_score`,
   `_metadata_blob`, `_rank_desc`, `_expand_query`, `_bm25lite_scores`,
   `_rerank_hits`. Confirm with grep before moving that no other route
   calls them.
4. In `__init__.py`: `from .search import router as search_router` and
   `app.include_router(search_router)`, placed after `app = FastAPI(...)`.
5. Keep the inside-function import `from ..vector_store import
   get_default_index_name, get_vector_store` in `run_search`. The existing
   tests patch `pipeline.vector_store.get_vector_store`, and that only
   works because the name is looked up at call time.
6. Add one TODO block at the top of `__init__.py` listing the resources
   still to extract, one file each, by SRP: documents, pages, chunks,
   artifacts, vector indexes, catalog, audit, settings, auth, admin.
7. Update `CLAUDE.md` (the "Everything lives in `pipeline/`" paragraph and
   the module list) so it describes the package and the router pattern.

Why `__init__.py` rather than `pipeline/api/app.py` with a re-export:
the fixtures assign module globals (`api.temporal_client = mock`). A
re-export would not forward that assignment, so three test files would
need rewriting. Rejected for a "small" refactor. Once the init file is
thin, moving its remainder to `app.py` is a one-line follow-up.

Safety net for Part A (what proves nothing broke):

- `uv run pytest` is green before the move; same count after.
- A new `tests/test_api_routes.py` snapshot test: the sorted list of
  `(method, path)` from `app.routes`, captured once before the move,
  must equal the list after. Catches a dropped or renamed route in this
  and every later extraction.
- `tests/test_auth.py` already walks every route and fails on any
  ungated one, so the router's `RequireSearch` dependency is checked.
- `uv run python -c "import pipeline.api"` (same check the Dockerfile
  runs) and `uv run ruff check .`.
- `docker compose up -d --build` then `curl localhost:8001/health` and
  one `POST /search`, to prove the uvicorn target still resolves.

### Part B: the group filter

Add one optional request field, `group`, to `POST /search`. Normalise it
(strip, lowercase, empty means absent) in the endpoint, pass it down to the
vector store, and turn it into one more `must` condition on the existing
Qdrant filter. Echo the applied value in `effective_config.group`, the
response block that reports the settings the server actually used.

Why this is enough:

- Every chunk point already carries `instance` (the group code, e.g. `mh`)
  as a keyword-indexed payload field (`PAYLOAD_FIELDS` and
  `_payload_indexes` in `pipeline/vector_store/qdrant_store.py`).
- All three search modes already go through one filter object built by
  `_build_filter`. LEXICAL passes it as `scroll_filter`; TENSOR and HYBRID
  (which maps to TENSOR on Qdrant) pass it as `query_filter`. Adding the
  condition there covers all modes with one change.
- `/search` has no tenant scoping today. `group` is a filter, not an
  access check. Confirmed during review.

Naming: the API field is `group` (the story's term). The store kwarg and
payload key stay `instance`, because renaming inside the pipeline is out of
scope. The endpoint is the only place that maps one name to the other.

Alternatives considered and rejected:

- Post-filtering hits in `run_search` after the store returns. Rejected:
  the candidate pool would be mostly other groups, so a group with few
  matches could return zero hits even when Qdrant holds them.
- Adding a tenancy check so a user scoped to `mh` cannot pass `group=ka`.
  Rejected: `/search` applies no scoping today, the story does not ask for
  it, and DSS is a network-level caller. Track separately if wanted.
- Reusing `pipeline.auth.tenancy.normalize_instance`. Rejected: it maps an
  empty value to `DEFAULT_INSTANCE`, but for search an empty `group` must
  mean "no filter".
- Adding a group control to the search workbench UI. Rejected: not in the
  acceptance criteria.

## Data model

No data model changes. No re-indexing. The `instance` payload field and its
keyword index already exist on every collection the store creates.

## API contracts

Routes and their paths are unchanged by Part A. `POST /search` request,
one new optional field:

| field   | type   | rule                                                   |
|---------|--------|--------------------------------------------------------|
| `group` | string | optional. Trimmed and lowercased. Empty = absent. Non-string = 400. |

`POST /search` response, one new key under `effective_config`:

| key     | value                                               |
|---------|-----------------------------------------------------|
| `group` | the normalised group that was applied, or `null`    |

`VectorStore.search` (`pipeline/vector_store/base.py`) gains
`instance: Optional[str] = None`. `QdrantVectorStore.search` and
`_build_filter` gain the same kwarg.

Docs to update:

- `docs/openapi-search.yaml`: `SearchRequest.group` and
  `EffectiveConfig.group`.
- `README.md`, the `POST /search` bullet under "Index And Search": mention
  `group`.
- `CLAUDE.md`: the API package layout (Part A, step 7).

## Edge cases

| case | expected |
|------|----------|
| `group: "MH"` | treated as `mh`; `effective_config.group == "mh"` |
| `group: "  mh "` | treated as `mh` |
| `group: ""` or `group: null` or field absent | no filter; `effective_config.group == null`; results as today |
| `group: "xx"` with no such chunks | 200, `hits: []`, `final_count: 0` |
| `group` plus `include_expired: true` | both apply: group filter on, validity off |
| `group` plus `valid_on` | both conditions in the same `must` list |
| `group` with `search_mode: LEXICAL` | filter reaches `client.scroll(scroll_filter=...)` |
| `group` with `TENSOR` or `HYBRID` | filter reaches `client.query_points(query_filter=...)` |
| `group` plus `exclude_reference` | both in the same `must` list |
| `max_chunks_per_doc` and `top_k` | unchanged; applied after the filtered candidates return |

## Failure modes

| scenario | expected |
|----------|----------|
| `group` is a list, number or object | 400 `group must be a string` |
| Qdrant rejects the filter | existing path: 400 `Vector search failed (qdrant): ...` |
| Old collection without the `instance` keyword index | Qdrant still filters on an unindexed payload key, just slower. `_ensure_payload_indexes` adds the index on next `ensure_collection`. No special handling. |
| Old chunks tagged `default` instead of a real group | They never match a group filter. Not a code change. Operator check before release, see Test strategy. |
| A relative import missed in the package move | `import pipeline.api` fails at build time (Dockerfile step) and in every test. Caught before deploy. |
| A route lost in the extraction | `tests/test_api_routes.py` snapshot fails. |

## Implementation tasks

1. Capture the route snapshot before touching `api.py`
   (`tests/test_api_routes.py`).
   - [ ] Given the current `pipeline.api.app`, when the test collects sorted `(method, path)` pairs from `app.routes`, then it asserts equality with the list committed in the test file.
   - [ ] Given the full suite, when `uv run pytest` runs, then it is green and the pass count is recorded in the PR description.

2. Move `pipeline/api.py` to `pipeline/api/__init__.py`
   (`git mv`, then fix relative imports).
   - [ ] Given the move, when I run `grep -n "from \." pipeline/api/__init__.py`, then every relative import reads `from ..` and none reads `from .` except the router import added in task 3.
   - [ ] Given the move, when I run `uv run python -c "import pipeline.api"` and `uv run ruff check .`, then both pass.
   - [ ] Given the move, when `uv run pytest` runs, then the pass count equals the one from task 1 and `tests/test_api_routes.py` passes.

3. Extract the search route into `pipeline/api/search.py`
   (`APIRouter`, `POST /search`, the nine private helpers; `include_router` in `__init__.py`; TODO block; `CLAUDE.md`).
   - [ ] Given `grep` over `pipeline/api/__init__.py`, when I look for each moved helper name, then there are zero remaining references.
   - [ ] Given the app, when `tests/test_api_routes.py` runs, then the route list is unchanged.
   - [ ] Given the five existing `/search` tests in `tests/test_api.py`, when they run unchanged, then they pass (proves the `get_vector_store` patch still works).
   - [ ] Given `tests/test_auth.py`, when it walks the routes, then `POST /search` is still gated.
   - [ ] Given `CLAUDE.md`, when I read the architecture section, then it describes `pipeline/api/` as a package with `search.py` as the first router and names the TODO list.

4. Add the group condition to the Qdrant filter
   (`pipeline/vector_store/qdrant_store.py`: `_build_filter`,
   `QdrantVectorStore.search`; `pipeline/vector_store/base.py`: protocol).
   - [ ] Given `_build_filter(instance="mh")`, when I inspect `filt.must`, then it holds one `FieldCondition` with `key == "instance"` and `MatchValue(value="mh")`.
   - [ ] Given `_build_filter()` with no instance, when I inspect the result, then no `instance` condition is present (and the result is still `None` when nothing else is set).
   - [ ] Given `_build_filter(instance="mh", exclude_reference=True, valid_on=TODAY)`, when I inspect `filt.must`, then all three kinds of condition are present together.
   - [ ] Given `search(..., search_mode="LEXICAL", instance="mh")` against a fake client, when the call completes, then the fake's `scroll` received a `scroll_filter` containing the `instance == "mh"` condition.
   - [ ] Given `search(..., search_mode="TENSOR", instance="mh")` against a fake client, when the call completes, then the fake's `query_points` received a `query_filter` containing the `instance == "mh"` condition.
   - [ ] Given `search(...)` with no instance, when the call completes, then the filter passed to the client holds no `instance` condition.

5. Accept, normalise and echo `group` in `POST /search`
   (`pipeline/api/search.py`, `run_search`).
   - [ ] Given `{"query": "kisan", "group": "mh"}`, when I call `/search` with a fake store, then the store receives `instance == "mh"` and `effective_config.group == "mh"`.
   - [ ] Given `{"query": "kisan", "group": "MH"}`, when I call `/search`, then the store receives `instance == "mh"` and `effective_config.group == "mh"`.
   - [ ] Given `{"query": "kisan", "group": "  mh "}`, when I call `/search`, then the store receives `instance == "mh"`.
   - [ ] Given `{"query": "kisan"}` or `{"query": "kisan", "group": ""}` or `{"query": "kisan", "group": null}`, when I call `/search`, then the store receives `instance is None` and `effective_config.group` is `null`.
   - [ ] Given `{"query": "kisan", "group": ["mh"]}`, when I call `/search`, then the response is 400 and the detail says `group must be a string`.
   - [ ] Given `{"query": "kisan", "group": "xx"}` and a store that returns no hits, when I call `/search`, then the response is 200 with `hits == []` and `final_count == 0`.
   - [ ] Given `{"query": "kisan", "group": "mh", "include_expired": true}`, when I call `/search`, then the store receives `instance == "mh"` and `apply_validity is False`.

6. Integration test through the real Qdrant engine
   (`tests/test_search_group_integration.py`, marked `integration`).
   - [ ] Given an in-memory Qdrant (`QdrantClient(":memory:")`) wrapped in a real `QdrantVectorStore`, with `EMBEDDING_VECTOR_SIZE=8` and `embed_passages`/`embed_query` in `pipeline.vector_store.qdrant_store` patched to a deterministic fake, when chunks for `mh`, `ka` and `bv` are upserted, then `get_stats` reports all of them.
   - [ ] Given that store returned by a patched `get_vector_store`, when `POST /search` is called with `group: "mh"` in each of `HYBRID`, `TENSOR` and `LEXICAL`, then every hit has `instance == "mh"` and `effective_config.group == "mh"`.
   - [ ] Given the same store, when `POST /search` is called with no `group`, then hits include more than one `instance` value.
   - [ ] Given the same store, when `POST /search` is called with `group: "xx"`, then the response is 200 with zero hits.
   - [ ] Given the same store and a chunk whose `end_date` is in the past, when `POST /search` is called with `group: "mh"`, then that chunk is absent (group and validity compose in the real engine).

7. Document the field
   (`docs/openapi-search.yaml`, `README.md`).
   - [ ] Given the OpenAPI file, when I read `SearchRequest`, then `group` is listed as an optional string with the normalisation and single-value rules.
   - [ ] Given the OpenAPI file, when I read `EffectiveConfig`, then `group` is listed as nullable string.
   - [ ] Given the README `POST /search` bullet, when I read it, then it mentions `group`.

## Test strategy

Unit tests, no running Qdrant:

- Filter and store tests in a new `tests/test_qdrant_group_filter.py`,
  following `tests/test_qdrant_validity.py` (its `FakeClient` captures
  `scroll` and `query_points` kwargs; its `TestBuildFilter` inspects
  `filt.must`).
- Endpoint tests added to the search class in `tests/test_api.py`,
  following the `FakeStore` + `captured` pattern used by the `valid_on`
  tests there.
- Route snapshot test `tests/test_api_routes.py` (task 1), the safety
  net for the package move and every later extraction.

Integration test, real Qdrant engine, no Docker:

- `qdrant-client` 1.19 ships a local mode, `QdrantClient(":memory:")`,
  that runs the real filtering and vector engine in-process. The test
  builds `QdrantVectorStore(client=QdrantClient(":memory:"))`, so
  `ensure_collection`, `upsert`, `_build_filter`, `scroll` and
  `query_points` all execute for real.
- The embedding model is the only heavy piece. The test sets
  `EMBEDDING_VECTOR_SIZE=8` and patches `embed_passages` and `embed_query`
  in `pipeline.vector_store.qdrant_store` with a deterministic fake
  (hash each token into one of 8 slots). Search quality is not under test;
  the filter is.
- Records are built as plain dicts with `doc_id`, `chunk_num`, `instance`,
  `text`, `is_reference`, `start_date`, `end_date`, not via
  `_prepare_records`, which reads metadata files from disk.
- The API is exercised through the existing `test_client` fixture with
  `pipeline.vector_store.get_vector_store` patched to return the in-memory
  store. That makes it the first test that runs the route, the store and
  the engine together.
- Marked `@pytest.mark.integration`. It is the first test in the repo to
  use that marker. It runs in the default `uv run pytest` and in CI; no
  service is required.
- Fallback if local mode rejects something the real server accepts (for
  example a `DatetimeRange` edge): keep the test, point the client at
  `VECTOR_DB_URL` when set, and mark it `slow`.

Mode coverage: unit tests prove the filter reaches `scroll` (LEXICAL) and
`query_points` (TENSOR). HYBRID is covered at the endpoint level because
`run_search` maps it to TENSOR. The integration test runs all three
end-to-end.

Pre-release data check (operator, not code). Run against both the DEV and
PROD Qdrant and confirm the `default` count is zero or explained:

    curl -s -X POST http://localhost:6333/collections/documents-index/points/count \
      -H 'Content-Type: application/json' \
      -d '{"filter":{"must":[{"key":"instance","match":{"value":"default"}}]},"exact":true}'

Repeat with `"value":"mh"`, `"ka"`, `"bv"` to see the live spread.

Manual smoke against a running stack after Part A and after Part B:
`docker compose up -d --build`, `curl localhost:8001/health`, then
`POST /search` twice with the same query, once with `group: "mh"` and once
without. Every hit in the first response has `instance == "mh"`; the
second matches today's behaviour.
