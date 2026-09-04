"""Optional GRiD access-tag filtering.

Nothing in this module is imported by the stock application — it layers
row- and collection-level access control on top of the generic client via the
:meth:`Client.visible_collections` and :meth:`Client.search_collection` hooks,
plus one ASGI middleware that parses the ``x-grid-accesstags`` request header.

Enable it by building the app with :func:`create` instead of
:func:`stac_fastapi.geoparquet.api.create`::

    from stac_fastapi.geoparquet import access_tags

    api = access_tags.create()
    app = api.app

Collections carrying an ``access_tag_id`` field are only visible to callers
whose header includes that tag, and searches against them are restricted to
rows whose ``access_tag_id`` column matches. Collections *without* the field
(and parquet without the column) are untouched, so a mixed catalog works.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import HTTPException
from rustac import DuckdbClient  # type: ignore[attr-defined]
from stac_fastapi.api.app import StacApi
from stac_fastapi.types.stac import Collection, Item
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .api import create as create_api
from .client import Client
from .filters import _STAC_CORE_QUERYABLES, FiltersClient
from .settings import Settings

ACCESS_TAGS_HEADER = "x-grid-accesstags"
"""Request header carrying the caller's access tag ids."""

ACCESS_TAG_FIELD = "access_tag_id"
"""Collection field / parquet column holding a row's access tag id."""

DEFAULT_ACCESS_TAG = 1
"""Tag assumed for callers that send no header.

Collections and rows tagged with it are effectively public.
"""

_INVALID_HEADER = f"invalid {ACCESS_TAGS_HEADER} header: expected a list of integers"

# Curated queryables for the GRiD-specific columns. Merged into the STAC core
# set so `/collections/{id}/queryables` describes them properly.
GRID_QUERYABLES: dict[str, dict[str, Any]] = {
    "data_program_id": {
        "type": "integer",
        "title": "Data Program Id",
        "description": "The GRiD Data Program Unique ID",
    },
    "datatype_name": {
        "type": "string",
        "title": "Datatype Name",
        "description": "The specific datatype of the items in this collection",
    },
    "datatype_category_name": {
        "type": "string",
        "title": "Datatype Category Name",
        "description": "The general category of the datatypes",
    },
    "dataclass": {
        "type": "string",
        "title": "Dataclass",
        "description": "The class of the data. Raster / Vector / Pointcloud / Mesh",
    },
}


def parse_access_tags(header: str | None) -> list[int]:
    """Parse the ``x-grid-accesstags`` header value into a list of tag ids.

    A missing header means the caller only has the public tag. A malformed
    header is a client error (400), not a server crash.
    """
    if header is None:
        return [DEFAULT_ACCESS_TAG]
    try:
        parsed = ast.literal_eval(header)
    except ValueError, SyntaxError, MemoryError, RecursionError:
        raise HTTPException(400, _INVALID_HEADER)
    if isinstance(parsed, int) and not isinstance(parsed, bool):
        return [parsed]
    if isinstance(parsed, (list, tuple)) and all(
        isinstance(tag, int) and not isinstance(tag, bool) for tag in parsed
    ):
        return list(parsed)
    raise HTTPException(400, _INVALID_HEADER)


def access_tags(request: Request) -> list[int]:
    """Return the access tags resolved for this request.

    Falls back to public-only if :func:`access_tags_middleware` isn't
    installed, so a misconfigured app fails closed rather than open.
    """
    return getattr(request.state, "access_tags", [DEFAULT_ACCESS_TAG])


async def access_tags_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Resolve the caller's access tags once per request.

    Runs before routing, so a malformed header is rejected uniformly on every
    endpoint rather than only the ones that read the tags.
    """
    try:
        request.state.access_tags = parse_access_tags(
            request.headers.get(ACCESS_TAGS_HEADER)
        )
    except HTTPException as e:
        # Middleware sits outside the app's exception handlers, so the
        # response has to be built here.
        return JSONResponse(
            status_code=e.status_code,
            content={"code": "BadRequest", "description": e.detail},
        )
    return await call_next(request)


def apply_access_filter(search_dict: dict[str, Any], tags: list[int]) -> None:
    """AND an ``access_tag_id IN (...)`` clause into ``search_dict``'s filter.

    Mutates ``search_dict`` in place. Only called for collections whose
    metadata carries an ``access_tag_id``, so the backing geoparquet is
    expected to have the column.
    """
    filter_lang = search_dict.get("filter-lang") or "cql2-text"
    if filter_lang == "cql2-text":
        clause = f"{ACCESS_TAG_FIELD} IN ({', '.join(str(tag) for tag in tags)})"
        if existing := search_dict.get("filter"):
            search_dict["filter"] = f"({existing}) AND {clause}"
        else:
            search_dict["filter"] = clause
            search_dict["filter-lang"] = "cql2-text"
    elif filter_lang == "cql2-json":
        clause_json: dict[str, Any] = {
            "op": "in",
            "args": [{"property": ACCESS_TAG_FIELD}, list(tags)],
        }
        if existing := search_dict.get("filter"):
            if isinstance(existing, str):
                try:
                    existing = json.loads(existing)
                except json.JSONDecodeError as e:
                    raise HTTPException(400, f"invalid cql2-json filter: {e}")
            search_dict["filter"] = {"op": "and", "args": [existing, clause_json]}
        else:
            search_dict["filter"] = clause_json
            search_dict["filter-lang"] = "cql2-json"
    else:
        raise HTTPException(
            400,
            f"Unsupported filter-lang: {filter_lang!r}. Expected 'cql2-text' or 'cql2-json'.",
        )


class AccessTagClient(Client):
    """A :class:`Client` that honours the caller's access tags."""

    def visible_collections(self, request: Request) -> dict[str, Collection]:
        tags = access_tags(request)
        collections = super().visible_collections(request)
        return {
            collection_id: collection
            for collection_id, collection in collections.items()
            if collection.get(ACCESS_TAG_FIELD, DEFAULT_ACCESS_TAG) in tags
        }

    def search_collection(
        self,
        collection_id: str,
        href: str,
        search_dict: dict[str, Any],
        request: Request,
    ) -> list[Item]:
        collection = self.visible_collections(request)[collection_id]
        raw_tag = collection.get(ACCESS_TAG_FIELD)
        tagged = raw_tag is not None
        if tagged:
            # Scope to *this collection's own* tag, not the caller's full
            # granted set. Several collections can share one physical parquet
            # file, sliced only by access_tag_id (e.g. "…-Raster-504" and
            # "…-Raster-2304" over the same href), so filtering by the
            # caller's whole set would let rows belonging to a sibling
            # collection surface under this one whenever the caller happens to
            # hold both tags. `visible_collections` has already confirmed the
            # caller may see this collection's tag.
            collection_tag = cast(int, raw_tag)
            apply_access_filter(search_dict, [collection_tag])
            # rustac's DuckdbClient evaluates `filter` against the *projected*
            # columns, not the full row, so a caller-supplied `include` that
            # omits `access_tag_id` would silently make the injected access
            # filter match nothing. Keep it in the projection; it's always
            # stripped from the response below.
            projection: list[str] | None = search_dict.get("include")
            if projection and ACCESS_TAG_FIELD not in projection:
                projection.append(ACCESS_TAG_FIELD)

        items = super().search_collection(collection_id, href, search_dict, request)

        if tagged:
            for item in items:
                # access_tag_id is purely an internal filtering column — never
                # expose it, even if the caller asked for it via `fields`.
                item.get("properties", {}).pop(ACCESS_TAG_FIELD, None)
        return items


class AccessTagFiltersClient(FiltersClient):
    """Queryables that hide ``access_tag_id`` and describe the GRiD columns."""

    known_queryables = {**_STAC_CORE_QUERYABLES, **GRID_QUERYABLES}
    skip_columns = FiltersClient.skip_columns | {ACCESS_TAG_FIELD}


def create(
    settings: Settings | None = None,
    duckdb_client: DuckdbClient | None = None,
) -> StacApi:
    """Build the STAC API application with access-tag filtering enabled."""
    api = create_api(
        settings=settings,
        duckdb_client=duckdb_client,
        client=AccessTagClient(),
        filters_client=AccessTagFiltersClient(),
    )
    api.app.middleware("http")(access_tags_middleware)
    return api
