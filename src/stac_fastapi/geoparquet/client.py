import ast
import copy
import json
import os
import urllib.parse
from datetime import datetime as dt
from typing import Any, cast

from fastapi import HTTPException
from pydantic import ValidationError
from rustac import DuckdbClient  # type: ignore[attr-defined]
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.errors import NotFoundError
from stac_fastapi.types.search import BaseSearchPostRequest
from stac_fastapi.types.stac import Collection, Collections, Item, ItemCollection
from stac_pydantic.shared import BBox
from starlette.requests import Request

from .models import PostSearchRequestModel

DEFAULT_LIMIT = 10_000

# rustac's DuckdbClient does not implement the STAC Query Extension's `query`
# parameter directly (it raises `RustacError: query is not implemented`), so
# it must be translated into an equivalent CQL2 filter before being forwarded.
_QUERY_EXT_OPERATORS = {
    "eq": "=",
    "neq": "<>",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


def _cql2_text_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    return str(value)


def _query_ext_to_cql2_text(query: dict[str, dict[str, Any]]) -> str:
    """Translate a STAC Query Extension object into a CQL2-text filter."""
    clauses = []
    for prop, ops in query.items():
        for op, value in ops.items():
            if op in _QUERY_EXT_OPERATORS:
                clauses.append(
                    f"{prop} {_QUERY_EXT_OPERATORS[op]} {_cql2_text_literal(value)}"
                )
            elif op == "in":
                values = ", ".join(_cql2_text_literal(v) for v in value)
                clauses.append(f"{prop} IN ({values})")
            elif op == "startsWith":
                clauses.append(f"{prop} LIKE {_cql2_text_literal(f'{value}%')}")
            elif op == "endsWith":
                clauses.append(f"{prop} LIKE {_cql2_text_literal(f'%{value}')}")
            elif op == "contains":
                clauses.append(f"{prop} LIKE {_cql2_text_literal(f'%{value}%')}")
            else:
                raise HTTPException(400, f"Unsupported query operator: {op!r}")
    return " AND ".join(clauses)


def _query_ext_to_cql2_json(query: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Translate a STAC Query Extension object into a CQL2-json filter."""
    clauses: list[dict[str, Any]] = []
    for prop, ops in query.items():
        for op, value in ops.items():
            if op in _QUERY_EXT_OPERATORS:
                clauses.append(
                    {
                        "op": _QUERY_EXT_OPERATORS[op],
                        "args": [{"property": prop}, value],
                    }
                )
            elif op == "in":
                clauses.append({"op": "in", "args": [{"property": prop}, list(value)]})
            elif op == "startsWith":
                clauses.append(
                    {"op": "like", "args": [{"property": prop}, f"{value}%"]}
                )
            elif op == "endsWith":
                clauses.append(
                    {"op": "like", "args": [{"property": prop}, f"%{value}"]}
                )
            elif op == "contains":
                clauses.append(
                    {"op": "like", "args": [{"property": prop}, f"%{value}%"]}
                )
            else:
                raise HTTPException(400, f"Unsupported query operator: {op!r}")
    if len(clauses) == 1:
        return clauses[0]
    return {"op": "and", "args": clauses}


class Client(BaseCoreClient):
    """A stac-fastapi-geoparquet client."""

    def all_collections(self, **kwargs: Any) -> Collections:
        request = kwargs.pop("request")

        # ---- access-tag filtering (grid-specific) -------------------------
        atl = [1]
        atlh = dict(request.headers).get("x-grid-accesstags")
        if atlh:
            atl = ast.literal_eval(atlh)
        collections = cast(dict[str, Collection], request.state.collections)
        collections = {
            cname: coll
            for cname, coll in collections.items()
            if collections[cname].get("access_tag_id", 1) in atl
        }

        # ---- collection search parameters (injected by CollectionSearchRequest) --
        ids: list[str] | None = kwargs.pop("ids", None)
        bbox: tuple[float, float, float, float] | None = kwargs.pop("bbox", None)
        datetime_str: str | None = kwargs.pop("datetime", None)
        q: list[str] | None = kwargs.pop("q", None)
        limit: int | None = kwargs.pop("limit", None)
        offset: int | None = kwargs.pop("offset", None)

        matched = list(collections.values())

        # Filter by ids — exact match OR partial substring match.
        # A collection passes if its id exactly equals any term OR contains any
        # term as a substring (case-insensitive), so `ids=naip` matches both
        # "naip" and "naip-10".
        if ids:
            ids_lower = [term.lower() for term in ids]

            def _id_matches(coll_id: str) -> bool:
                cid = coll_id.lower()
                return any(term == cid or term in cid for term in ids_lower)

            matched = [c for c in matched if _id_matches(c.get("id", ""))]

        # Free-text search: check title, description, keywords
        if q:

            def _matches_q(coll: Collection, terms: list[str]) -> bool:
                haystack = " ".join(
                    filter(
                        None,
                        [
                            coll.get("title", ""),
                            coll.get("description", ""),
                            " ".join(coll.get("keywords", [])),
                        ],
                    )
                ).lower()
                return all(term.lower() in haystack for term in terms)

            matched = [c for c in matched if _matches_q(c, q)]

        # Spatial filter: collection extent bbox must overlap request bbox
        if bbox:
            req_min_lon, req_min_lat, req_max_lon, req_max_lat = (
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
            )

            def _extent_overlaps_bbox(coll: Collection) -> bool:
                try:
                    coll_bbox = (
                        coll.get("extent", {}).get("spatial", {}).get("bbox", [[]])[0]
                    )
                    if not coll_bbox or len(coll_bbox) < 4:
                        return True  # no spatial info — include by default
                    c_min_lon, c_min_lat, c_max_lon, c_max_lat = (
                        float(coll_bbox[0]),
                        float(coll_bbox[1]),
                        float(coll_bbox[2]),
                        float(coll_bbox[3]),
                    )
                    # Overlaps when NOT (one is entirely to the left/right/above/below)
                    return not (
                        c_max_lon < req_min_lon
                        or c_min_lon > req_max_lon
                        or c_max_lat < req_min_lat
                        or c_min_lat > req_max_lat
                    )
                except Exception:
                    return True

            matched = [c for c in matched if _extent_overlaps_bbox(c)]

        # Temporal filter: collection temporal extent must overlap request datetime
        if datetime_str:
            req_start, req_end = _parse_datetime_interval(datetime_str)

            def _temporal_overlaps(coll: Collection) -> bool:
                try:
                    interval = (
                        coll.get("extent", {})
                        .get("temporal", {})
                        .get("interval", [[None, None]])[0]
                    )
                    coll_start_str, coll_end_str = interval[0], interval[1]
                    coll_start = (
                        _parse_rfc3339(coll_start_str) if coll_start_str else None
                    )
                    coll_end = _parse_rfc3339(coll_end_str) if coll_end_str else None
                    # Open collection end means "still active"
                    effective_end = coll_end if coll_end else dt.max
                    effective_start = coll_start if coll_start else dt.min
                    effective_req_end = req_end if req_end else dt.max
                    effective_req_start = req_start if req_start else dt.min
                    return (
                        effective_start <= effective_req_end
                        and effective_end >= effective_req_start
                    )
                except Exception:
                    return True

            matched = [c for c in matched if _temporal_overlaps(c)]

        # Pagination
        total_matched = len(matched)
        applied_offset = offset or 0
        applied_limit = limit or total_matched
        page = matched[applied_offset : applied_offset + applied_limit]

        # Build next/prev links if paginating
        links: list[dict[str, Any]] = [
            {
                "href": str(request.url_for("Landing Page")),
                "rel": "root",
                "type": "application/json",
            },
            {
                "href": str(request.url),
                "rel": "self",
                "type": "application/json",
            },
        ]
        next_offset = applied_offset + applied_limit
        if next_offset < total_matched:
            next_params = dict(request.query_params)
            next_params["offset"] = str(next_offset)
            if limit:
                next_params["limit"] = str(applied_limit)
            links.append(
                {
                    "href": str(request.url_for("Get Collections"))
                    + "?"
                    + urllib.parse.urlencode(next_params),
                    "rel": "next",
                    "type": "application/json",
                }
            )
        if applied_offset > 0:
            prev_offset = max(0, applied_offset - applied_limit)
            prev_params = dict(request.query_params)
            prev_params["offset"] = str(prev_offset)
            if limit:
                prev_params["limit"] = str(applied_limit)
            links.append(
                {
                    "href": str(request.url_for("Get Collections"))
                    + "?"
                    + urllib.parse.urlencode(prev_params),
                    "rel": "prev",
                    "type": "application/json",
                }
            )

        return Collections(
            collections=[collection_with_links(c, request) for c in page],
            links=links,
        )

    def get_collection(self, collection_id: str, **kwargs: Any) -> Collection:
        request = kwargs.pop("request")
        collections = cast(dict[str, Collection], request.state.collections)
        if collection := collections.get(collection_id):
            atl = [1]
            atlh = dict(request.headers).get("x-grid-accesstags")
            if atlh:
                atl = ast.literal_eval(atlh)
            if collection.get("access_tag_id") not in atl:
                raise NotFoundError(f"Collection does not exist: {collection_id}")
            return collection_with_links(collection, request)
        else:
            raise NotFoundError(f"Collection does not exist: {collection_id}")

    def get_item(self, item_id: str, collection_id: str, **kwargs: Any) -> Item:
        item_collection = self.get_search(
            ids=[item_id],
            collections=[collection_id],
            **kwargs,
        )
        if len(item_collection["features"]) == 1:
            return Item(**item_collection["features"][0])
        else:
            raise NotFoundError(
                f"Item does not exist: {item_id} in collection {collection_id}"
            )

    def get_search(  # type: ignore
        self,
        collections: list[str] | None = None,
        ids: list[str] | None = None,
        bbox: BBox | str | None = None,
        intersects: str | None = None,
        datetime: str | None = None,
        limit: int | None = 10,
        **kwargs: Any,
    ) -> ItemCollection:
        request = kwargs.pop("request")

        if intersects:
            maybe_intersects = json.loads(intersects)
        else:
            maybe_intersects = None

        if isinstance(bbox, str):
            if bbox.startswith("["):
                bbox = bbox[1:]
            if bbox.endswith("]"):
                bbox = bbox[:-1]
            try:
                bbox = cast(BBox, [float(s) for s in bbox.split(",")])
            except ValueError as e:
                raise HTTPException(400, f"invalid bbox: {e}")

        try:
            search = BaseSearchPostRequest(
                collections=collections,
                ids=ids,
                bbox=bbox,
                intersects=maybe_intersects,
                datetime=datetime,
                limit=limit,
            )
        except ValidationError as e:
            raise HTTPException(400, f"invalid request: {e}")

        return self.search(
            request=request,
            search=search,
            url=str(request.url_for("Search")),
            **kwargs,
        )

    def item_collection(
        self,
        collection_id: str,
        bbox: BBox | None = None,
        datetime: str | None = None,
        limit: int = 10,
        token: str | None = None,
        **kwargs: Any,
    ) -> ItemCollection:
        request = kwargs.pop("request")
        offset = kwargs.pop("offset", None)
        # qp = dict(request.query_params)
        # offset = qp.pop("offset", None)
        search = PostSearchRequestModel(
            collections=[collection_id],
            bbox=bbox,
            datetime=datetime,
            limit=limit,
            offset=offset,
        )
        return self.search(
            request=request,
            search=cast(BaseSearchPostRequest, search),
            url=str(request.url_for("Get ItemCollection", collection_id=collection_id)),
            **kwargs,
        )

    def post_search(
        self, search_request: BaseSearchPostRequest, **kwargs: Any
    ) -> ItemCollection:
        request = kwargs.pop("request")
        return self.search(
            search=search_request,
            request=request,
            url=str(request.url_for("Search")),
            **kwargs,
        )

    def search(
        self,
        *,
        request: Request,
        url: str,
        search: BaseSearchPostRequest,
        **kwargs: Any,
    ) -> ItemCollection:

        def _has_access(coll: Collection | None, atl: list[int]) -> bool:
            if atl is None or coll is None:
                return False
            return coll.get("access_tag_id") in atl

        client = cast(DuckdbClient, request.state.client)
        hrefs = cast(dict[str, str], request.state.hrefs)

        s3end = os.getenv("AWS_S3_ENDPOINT")
        skip_s3 = os.getenv("STAC_FASTAPI_SKIP_S3_SECRET", "").lower() in ("1", "true")
        if not skip_s3:
            if s3end:
                client.execute(
                    f"CREATE OR REPLACE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REFRESH auto, ENDPOINT '{s3end}');"
                )
            else:
                client.execute(
                    "CREATE OR REPLACE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REFRESH auto);"
                )

        # Resolve the access tag list early — used both to gate the collections
        # list and to inject the CQL filter below.
        atl = [1]
        atlh = dict(request.headers).get("x-grid-accesstags")
        if atlh:
            atl = ast.literal_eval(atlh)
        all_collections = cast(dict[str, Collection], request.state.collections)

        if search.collections:
            # Caller specified explicit collections — honour the request but
            # silently drop any the user's access tags don't cover.
            collections = [
                c
                for c in search.collections
                if _has_access(all_collections.get(c), atl)
            ]
        else:
            # No collections specified — use every href the user can access.
            collections = [
                c for c in hrefs.keys() if _has_access(all_collections.get(c), atl)
            ]

        search_dict = search.model_dump(exclude_none=True, by_alias=True)
        search_dict.update(**kwargs)

        search_dict.pop("filter_crs", None)
        if filter_expr := search_dict.pop("filter_expr", None):
            search_dict["filter"] = filter_expr
        # Capture filter_lang before any cleanup so the AT injection below
        # always has the correct value (fixes the None-when-POST-has-no-filter-lang bug).
        # The key varies by request method: POST's `search_dict` comes from
        # `model_dump(by_alias=True)`, which uses the pydantic alias
        # "filter-lang" (hyphen); GET's arrives via **kwargs from a FastAPI
        # dependency function, which can only use the valid identifier
        # "filter_lang" (underscore). Popping only one always missed the
        # other, leaving `filter_lang` None and silently discarding any real
        # `filter` in favor of just the access_tag_id clause below.
        filter_lang: str | None = search_dict.pop(
            "filter-lang", None
        ) or search_dict.pop("filter_lang", None)
        if filter_lang:
            search_dict["filter-lang"] = filter_lang
        if "filter" not in search_dict:
            search_dict.pop("filter-lang", None)
        if fields := search_dict.pop("fields", None):
            if isinstance(fields, list):
                include = []
                exclude = []
                for field in fields:
                    if field.startswith("-"):
                        exclude.append(field)
                    else:
                        include.append(field)
                search_dict.update({"include": include, "exclude": exclude})
            elif isinstance(fields, dict):
                search_dict.update(
                    {
                        "include": list(fields.get("include", [])),
                        "exclude": list(fields.get("exclude", [])),
                    }
                )
            else:
                raise HTTPException(400, f"unexpected fields type: {fields}")
        if sortby := search_dict.pop("sortby", None):
            search_dict["sortby"] = sortby

        # rustac's DuckdbClient evaluates `filter` against the *projected*
        # columns, not the full row, so a caller-supplied `include` that
        # omits `access_tag_id` would silently make the access_tag_id filter
        # injected below match nothing. Keep it in the projection and strip
        # it back out of the response if the caller didn't ask for it.
        requested_include = search_dict.get("include")
        strip_access_tag_id = False
        if requested_include and "access_tag_id" not in requested_include:
            search_dict["include"] = [*requested_include, "access_tag_id"]
            strip_access_tag_id = True

        # Translate the Query Extension's `query` into an equivalent CQL2
        # filter — rustac's DuckdbClient only understands `filter`/CQL2 and
        # raises RustacError("query is not implemented") if `query` reaches it.
        if query := search_dict.pop("query", None):
            filter_lang = filter_lang or "cql2-text"
            if filter_lang == "cql2-text":
                query_filter: Any = _query_ext_to_cql2_text(query)
                search_dict["filter"] = (
                    f"({search_dict['filter']}) AND ({query_filter})"
                    if search_dict.get("filter")
                    else query_filter
                )
            elif filter_lang == "cql2-json":
                query_filter = _query_ext_to_cql2_json(query)
                search_dict["filter"] = (
                    {"op": "and", "args": [search_dict["filter"], query_filter]}
                    if search_dict.get("filter")
                    else query_filter
                )
            else:
                raise HTTPException(
                    400,
                    f"Unsupported filter-lang: {filter_lang!r} for the 'query' extension.",
                )
            search_dict["filter-lang"] = filter_lang

        # The caller-visible filter (their own `filter`/`query`, merged, but
        # not yet ANDed with the grid-specific access_tag_id clause below) -
        # this is what belongs in public pagination links, not the merged one.
        public_filter = search_dict.get("filter")

        # Inject access_tag_id as a CQL filter
        if filter_lang:
            if filter_lang == "cql2-text":
                if search_dict.get("filter"):
                    search_dict["filter"] = (
                        f"({search_dict['filter']}) AND access_tag_id IN ({', '.join(str(i) for i in atl)})"
                    )
                else:
                    search_dict["filter"] = (
                        f"access_tag_id IN ({', '.join(str(i) for i in atl)})"
                    )
            elif filter_lang == "cql2-json":
                if search_dict.get("filter"):
                    search_dict["filter"] = {
                        "op": "and",
                        "args": [
                            search_dict["filter"],
                            {"op": "in", "args": [{"property": "access_tag_id"}, atl]},
                        ],
                    }
                else:
                    search_dict["filter"] = {
                        "op": "in",
                        "args": [{"property": "access_tag_id"}, atl],
                    }
            else:
                raise ValueError(
                    f"Unsupported filter-lang: {filter_lang!r}. Expected 'cql2-text' or 'cql2-json'."
                )
        else:
            search_dict["filter"] = (
                f"access_tag_id IN ({', '.join(str(i) for i in atl)})"
            )
            filter_lang = "cql2-text"

        limit = search_dict.get("limit", DEFAULT_LIMIT)
        offset = search_dict.get("offset", 0) or 0
        items: list[Item] = []
        while collections:
            collection = collections.pop(0)
            if href := hrefs.get(collection):
                collection_search_dict = copy.deepcopy(search_dict)
                collection_search_dict.update(
                    {
                        "collections": [],
                        "limit": limit,
                        "offset": offset,
                    }
                )

                collection_items = client.search(href, **collection_search_dict)
                for item in collection_items:
                    stac_item = cast(Item, item)
                    if strip_access_tag_id:
                        stac_item.get("properties", {}).pop("access_tag_id", None)
                    items.append(self.item_with_links(stac_item, request, collection))
                if len(items) >= limit:
                    collections.insert(0, collection)
                    offset = offset + len(collection_items)
                    break
                else:
                    limit = limit - len(collection_items)
                    offset = 0

        num_items = len(items)

        if collections and ((search.limit or DEFAULT_LIMIT) <= num_items):
            next_search = copy.deepcopy(search_dict)
            if public_filter:
                next_search["filter"] = public_filter
            else:
                next_search.pop("filter", None)
                next_search.pop("filter-lang", None)
            next_search["limit"] = search.limit or DEFAULT_LIMIT
            next_search["offset"] = offset
            next_search["collections"] = collections
        else:
            next_search = None

        links: list[dict[str, Any]] = [
            {
                "href": str(request.url_for("Landing Page")),
                "rel": "root",
                "type": "application/json",
            }
        ]
        if request.method == "GET":
            links.append(
                {
                    "href": str(request.url),
                    "rel": "self",
                    "type": "application/geo+json",
                    "method": "GET",
                }
            )
            if next_search:
                if "collections" in next_search:
                    next_search["collections"] = ",".join(collections)
                if bbox := next_search.get("bbox"):
                    next_search["bbox"] = ",".join(map(str, bbox))

                # Filter out all variations of None/empty fields safely
                clean_next_search = {
                    k: v
                    for k, v in next_search.items()
                    if v is not None
                    and v not in ("", "None", "[None]", "['None']", '["None"]')
                }

                #  Encode using doseq=True so lists/sequences are properly formatted
                links.append(
                    {
                        "href": url
                        + "?"
                        + urllib.parse.urlencode(clean_next_search, doseq=True),
                        "rel": "next",
                        "type": "application/geo+json",
                        "method": "GET",
                    }
                )
        else:
            links.append(
                {
                    "href": str(request.url),
                    "rel": "self",
                    "type": "application/geo+json",
                    "method": "POST",
                    "body": search_dict,
                }
            )
            if next_search:
                links.append(
                    {
                        "href": url,
                        "rel": "next",
                        "type": "application/geo+json",
                        "method": "POST",
                        "body": next_search,
                    }
                )

        return {
            "type": "FeatureCollection",
            "features": items,
            "links": links,
        }

    def item_with_links(self, item: Item, request: Request, collection: str) -> Item:
        links = [
            {
                "href": str(request.url_for("Landing Page")),
                "rel": "root",
                "type": "application/json",
            },
        ]
        item["collection"] = collection
        href = str(request.url_for("Get Collection", collection_id=collection))
        links.append({"href": href, "rel": "collection", "type": "application/json"})
        links.append({"href": href, "rel": "parent", "type": "application/json"})
        if item_id := item.get("id"):
            links.append(
                {
                    "href": str(
                        request.url_for(
                            "Get Item",
                            collection_id=collection,
                            item_id=item_id,
                        )
                    ),
                    "rel": "self",
                    "type": "application/geo+json",
                }
            )
        for link in item.get("links", []):
            if link["rel"] not in ("root", "parent", "collection", "self"):
                links.append(link)
        item["links"] = links
        return item


def collection_with_links(collection: Collection, request: Request) -> Collection:
    collection["links"] = [
        {
            "href": str(request.url_for("Landing Page")),
            "rel": "root",
            "type": "application/json",
        },
        {
            "href": str(request.url_for("Landing Page")),
            "rel": "parent",
            "type": "application/json",
        },
        {
            "href": str(
                request.url_for("Get Collection", collection_id=collection["id"])
            ),
            "rel": "self",
            "type": "application/json",
        },
        {
            "href": str(
                request.url_for("Get ItemCollection", collection_id=collection["id"])
            ),
            "rel": "items",
            "type": "application/geo+json",
        },
        {
            "href": str(
                request.url_for("Collection Queryables", collection_id=collection["id"])
            ),
            "rel": "http://www.opengis.net/def/rel/ogc/1.0/queryables",
            "type": "application/schema+json",
            "title": "Queryables",
        },
    ]
    return collection


# ---------------------------------------------------------------------------
# Helpers for temporal collection search
# ---------------------------------------------------------------------------


def _parse_rfc3339(value: str) -> dt:
    """Parse an RFC 3339 datetime string, handling the trailing 'Z'."""
    return dt.fromisoformat(value.replace("Z", "+00:00"))


def _parse_datetime_interval(value: str) -> tuple[dt | None, dt | None]:
    """Parse a STAC datetime/interval string into (start, end).

    Single datetime  →  (datetime, datetime)
    Closed interval  →  (start, end)
    Open start (..)  →  (None, end)
    Open end   (..)  →  (start, None)
    """
    if "/" not in value:
        d = _parse_rfc3339(value.strip())
        return d, d

    raw_start, raw_end = value.split("/", 1)
    start = (
        None if raw_start.strip() in ("..", "") else _parse_rfc3339(raw_start.strip())
    )
    end = None if raw_end.strip() in ("..", "") else _parse_rfc3339(raw_end.strip())
    return start, end
