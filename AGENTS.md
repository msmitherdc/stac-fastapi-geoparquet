# stac-fastapi-geoparquet

Guidance for coding agents (and people) working in this repository.

## Project overview

A [stac-fastapi](https://github.com/stac-utils/stac-fastapi) backend that
serves a STAC API straight from one or more
[stac-geoparquet](https://github.com/stac-utils/stac-geoparquet) files, with
no database. Queries run in an in-memory DuckDB connection through
[rustac](https://github.com/stac-utils/rustac-py)'s `DuckdbClient`, and the
files can be local or in object storage (S3 via DuckDB's `httpfs`, the
collections file via `obstore`).

- **Python:** 3.14 (`requires-python` in `pyproject.toml`)
- **Package manager:** [uv](https://docs.astral.sh/uv/); exact versions are in
  `uv.lock`
- **Key dependencies:** `stac-fastapi-*` 7.x, `rustac`, `fastapi`, `pydantic`
  2, `attrs`, `obstore`, `pystac`

## Layout

| Path | Purpose |
|------|---------|
| `src/stac_fastapi/geoparquet/api.py` | `create()` app factory, the `lifespan` that builds per-request state, and loading collections from the configured hrefs |
| `src/stac_fastapi/geoparquet/client.py` | `Client` (`BaseCoreClient`): collections, items, search, links, and the Query Extension → CQL2 translation |
| `src/stac_fastapi/geoparquet/filters.py` | `FiltersClient`: queryables, built from a `DESCRIBE` of the parquet schema |
| `src/stac_fastapi/geoparquet/models.py` | Request models and the list of enabled extensions |
| `src/stac_fastapi/geoparquet/search.py` | `FixedSearchGetRequest` and its `attrs` converters/validators |
| `src/stac_fastapi/geoparquet/settings.py` | `Settings` (`ApiSettings` subclass) |
| `src/stac_fastapi/geoparquet/main.py` | The ASGI `app` that `uvicorn`, `fastapi dev`, and the Lambda handler import |
| `src/stac_fastapi/geoparquet/static/` | Bundled Swagger UI assets, served at `/static` |
| `tests/` | pytest suite; `conftest.py` has the `settings` and `client` fixtures |
| `data/` | Checked-in test fixtures: `collections.json` and four parquet files |
| `scripts/` | `dev`, `lint`, `format`, `test`, `validate`, `generate-collections` |
| `infrastructure/aws/` | CDK app and Lambda handler (`mangum`) |
| `docs/` | mkdocs-material site |

### How a request flows

1. `api.create()` reads the collections file (`STAC_FASTAPI_COLLECTIONS_HREF`)
   and/or generates collections from a single parquet
   (`STAC_FASTAPI_GEOPARQUET_HREF`).
2. `lifespan` maps each collection id to its one
   `application/vnd.apache.parquet` asset href and yields `client`,
   `collections`, and `hrefs` as request state.
3. `Client` endpoints read collections through
   `Client.visible_collections(request)` and run each collection's part of a
   search through `Client.search_collection(...)`, which calls
   `DuckdbClient.search(href, **search_dict)`.

`visible_collections` and `search_collection` are the extension points for
subclasses (for example, to hide collections or add filters). New endpoints
and code paths must go through them rather than reading
`request.state.collections` or calling the DuckDB client directly.

## Development

```bash
uv sync                # install with the dev and validate groups
scripts/dev            # run against data/collections.json at http://127.0.0.1:8000
scripts/format         # ruff check --fix + ruff format
scripts/lint           # ruff check, ruff format --check, mypy (what CI runs)
scripts/test           # uv run pytest
scripts/validate       # starts the dev server and runs stac-api-validator
uv run mkdocs build --strict
```

CI (`.github/workflows/ci.yml`) runs lint, test, validate, and the strict docs
build on every PR, with `STAC_FASTAPI_SKIP_S3_SECRET=true`. Run the same
locally before opening a PR.

## Code conventions and pull requests

These rules describe how the code here is written today. When this guide and
the surrounding code disagree, follow the surrounding code and mention the
difference in the PR.

### Things agents commonly get wrong

- **rustac is not a full STAC API backend.** `DuckdbClient.search` doesn't
  implement the Query Extension (`query`), so `client.py` translates it to
  CQL2 first. It evaluates `filter` against the *projected* columns, so a
  `fields` include that omits a filtered column makes the filter match
  nothing. Check what rustac actually supports (see
  [Viewing dependencies](#viewing-dependencies)) before forwarding a new
  parameter.
- **GET and POST differ.** POST bodies are dumped with `by_alias=True`
  (`"filter-lang"`), while GET arrives as keyword arguments (`filter_lang`).
  GET also sends `query` as a JSON string and `fields` as a comma-separated
  string. Handle and test both methods.
- **`search_dict` is caller-visible.** Pagination and self links are built
  from it. Anything internal (extra filters, projected columns) goes on the
  per-collection copy passed to `search_collection`, never on `search_dict`.
- **Client errors are 4xx, not 500s.** Raise `HTTPException(400, ...)` for
  bad input and `NotFoundError` for missing collections or items. Don't let a
  `ValueError` or a `RustacError` escape for something the caller sent.
- **Warnings fail the tests.** `filterwarnings = ["error", ...]` in
  `pyproject.toml`. Fix the cause; add an `ignore` only for a deprecation in a
  dependency, with the exact message.
- **Don't hand-edit generated or fixture files:**
  - `uv.lock`: change it with `uv add` / `uv lock`
  - `src/stac_fastapi/geoparquet/static/`: vendored Swagger UI release files
  - `data/*.parquet` and `data/collections.json`: tests depend on their exact
    contents. Build modified copies in a `tmp_path` fixture instead (see
    `tests/test_access_tags.py`).
  - `CHANGELOG.md` and `.release-please-manifest.json`: maintained by
    release-please
- **Keep `docs/` and `README.md` in step.** A new setting, environment
  variable, or extension needs a line in the README. `mkdocs build --strict`
  fails on broken links.

### Principles

- **One concern per PR.** Keep a bug fix, a feature, a refactor, and a
  dependency bump in separate PRs.
- **Small diffs.** Don't reorder, rename, or restructure code you aren't
  otherwise changing. `ruff format` takes care of formatting.
- **Reuse before writing.** Look at what `stac-fastapi` and rustac already
  provide (request models, extensions, `NotFoundError`) before adding a
  helper.
- **Readable over clever.** Use plain control flow and early returns. Add an
  abstraction or a hook only when a second real caller needs it.
- **Tests come with the change.** Behavior changes and bug fixes need a test
  that fails without them.

### Python

- `ruff format` style: double quotes, 88-column wrapping. `ruff check`
  enables `E`, `F`, `I` (import sorting), and `UP`; `E501` is ignored, so
  long strings and URLs may run past 88.
- `mypy --strict` covers `src/` and `infrastructure/`. Annotate every
  function, including `-> None`. Use `X | None`, built-in generics
  (`list[str]`), and `collections.abc` for `Iterator` / `Callable`. Prefer a
  `cast()` with a reason over `# type: ignore`; when an ignore is needed, give
  the error code (`# type: ignore[attr-defined]`).
- Relative imports inside the package (`from .client import Client`), absolute
  imports in tests (`from stac_fastapi.geoparquet import Settings`).
- New settings go on `Settings` in `settings.py`, with an attribute docstring,
  and are documented in the README. Read environment variables in `api.py` /
  `main.py`, not deep inside the client.
- Use `logging.getLogger(__name__)`, never `print()`.
- Catch specific exceptions and turn caller mistakes into 400s.

### Comments and docstrings

- **Docstrings:** `"""Summary line.` in the imperative or present tense, then a
  blank line and detail only when needed. Use Sphinx roles for
  cross-references (``:func:`create` ``, ``:meth:`Client.search_collection` ``)
  and double backticks for literals. Settings and `TypedDict` fields get
  attribute docstrings underneath them (see `Settings` and `State`).
- **Comments:** explain *why*: a rustac or stac-fastapi quirk, a spec
  requirement, or a non-obvious invariant. Several lines are fine when the
  constraint is subtle (see `search_collection` in `access_tags.py`), but don't
  describe what the next line does.
- Regression tests may link the issue they cover
  (`# https://github.com/stac-utils/stac-fastapi-geoparquet/issues/18`).
- **Don't write:**
  - change narration (`# fixes the X bug`, `# new approach`). That belongs in
    the commit message.
  - references to the conversation, the agent, or "the user"
  - banner blocks or commented-out code
  - a `TODO` without an issue link

### Tests

- Plain pytest functions (`def test_<behavior>(client: TestClient) -> None:`)
  in `tests/test_<area>.py`, using the `client` fixture from `conftest.py`.
  `asyncio_mode = "auto"` is on, but endpoints are tested synchronously
  through `TestClient`.
- Assert the status with the body for context
  (`assert response.status_code == 200, response.text`) or call
  `response.raise_for_status()`, then assert on the JSON.
- Test the failure paths too: malformed parameters should return 400, not
  500. Cover GET and POST when a parameter is accepted by both.
- When a test needs different data, build it from `data/` in a fixture (DuckDB
  `COPY ... TO` into `tmp_path`) rather than adding binary fixtures.
- Watch for duplicate test function names; Python keeps only the last one.
- Never claim a test passed unless you ran it.

### Viewing dependencies

The pins are recent (stac-fastapi 7, FastAPI 0.14x, pydantic 2, rustac
0.9.x). Read the installed source rather than relying on memory or docs for
another version.

```bash
uv run python -c "import importlib.util as u; print(u.find_spec('rustac').origin)"
uv tree --package rustac --frozen
grep -A1 '^name = "rustac"$' uv.lock
```

rustac is a compiled extension, so its Python API is in the `.pyi` stubs next
to the installed module; its behavior is in
[rustac](https://github.com/stac-utils/rustac) at the matching tag. The
stac-fastapi extensions live under `.venv/lib/python3.14/site-packages/stac_fastapi/`.

Explain in the PR why a new dependency is needed. Version bumps go in their
own PR (Dependabot opens most of them).

### Commits

PRs are squash-merged, and [release-please](https://github.com/googleapis/release-please)
builds the changelog and version bumps from the commit subjects. The **PR
title** becomes that subject, so it must follow
[Conventional Commits](https://www.conventionalcommits.org/):

- `feat: support collection search on /collections`
- `fix: serve collections generated from stac_fastapi_geoparquet_href`
- `refactor: give Client two extension points for subclasses`
- `docs: ...`, `test: ...`, `ci: ...`, `chore: ...`, `build(deps): ...`

Use an imperative, lowercase description of about 72 characters or fewer. In
commit bodies, say what was wrong, why this fix, and what you verified.
Squash fixup commits (`oops`, `wip`) before pushing.

### Pull requests

- Branch as `<type>/<short-slug>` (`fix/no-redirect-slashes`,
  `feat/collection-search`).
- Keep the description short: one to three sentences on what changed and why,
  then **Testing:** with the commands you ran and their results, noting
  anything you didn't run.
- Before opening the PR:
  - [ ] The diff contains only this PR's concern.
  - [ ] `scripts/lint` and `scripts/test` pass.
  - [ ] New behavior and fixes have tests, including GET and POST and the 400
    paths.
  - [ ] README and docs cover any new setting or extension.
  - [ ] Comments explain *why*. No debug output or commented-out code.

## GRiD fork

This section applies to the
[msmitherdc/stac-fastapi-geoparquet](https://github.com/msmitherdc/stac-fastapi-geoparquet)
fork that GRiD deploys. Leave it out when proposing this file upstream.

### Keep GRiD code isolated

The fork stays mergeable with
[stac-utils/stac-fastapi-geoparquet](https://github.com/stac-utils/stac-fastapi-geoparquet),
and most changes should go upstream. GRiD-specific code is confined to:

- `src/stac_fastapi/geoparquet/access_tags.py`: the `x-grid-accesstags`
  middleware, `AccessTagClient`, `AccessTagFiltersClient`, the
  `access_tag_id` column, and the GRiD queryables (`data_program_id`,
  `datatype_name`, `datatype_category_name`, `dataclass`)
- `tests/test_access_tags.py`
- the `STAC_FASTAPI_ACCESS_TAGS` switch in `main.py` and the "Access tags"
  section of the README

Nothing else may import `access_tags`, mention GRiD, or know about
`access_tag_id`. If GRiD needs new behavior from the core, add a generic hook
to `Client`, `FiltersClient`, or `api.create()` (as `visible_collections`,
`search_collection`, `known_queryables`, and `skip_columns` were added) and
override it in `access_tags.py`. That way the hook can go upstream and the GRiD
part stays in the fork.

### Access control

`access_tags` is how GRiD keeps restricted collections from leaking. GRiD's
Django `v5_stac` view proxies every request here and sets
`x-grid-accesstags`.

- `main.py` builds the access-tag app unless `STAC_FASTAPI_ACCESS_TAGS=false`.
  Don't make it default to off; that serves restricted collections publicly.
- A collection's own `access_tag_id` scopes its rows, not the caller's full
  set of tags, because sibling collections can share one parquet file.
- `AccessTagClient.search_collection` re-checks every returned row because
  rustac's CQL2 translation drops the grouping of `A AND (B OR C)`. Keep the
  re-check even after rustac is fixed.
- A missing header means public-only (tag `1`); a malformed one is a 400. Fail
  closed.
- Any new endpoint that returns collections, items, or collection metadata
  needs a test in `tests/test_access_tags.py` showing that a caller without
  the tag can't see it.

### Remotes and PRs

- `origin` is the fork and `upstream` is stac-utils. `gh` defaults a fork's
  PRs to the parent repository, so pass `-R` explicitly:
  - fork: branch from `origin/main`, then
    `gh pr create -R msmitherdc/stac-fastapi-geoparquet --base main`
  - upstream: branch from `upstream/main` with only the generic change, then
    `gh pr create -R stac-utils/stac-fastapi-geoparquet --base main`
- Upstream has its own maintainers and pace. Keep upstream PRs small and free
  of GRiD names, data, or deployment details, and link the upstream PR from
  the matching fork PR.
- The fork's CI drops upstream's docs deploy and release jobs; don't restore
  them.
