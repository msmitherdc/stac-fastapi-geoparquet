from typing import Annotated

import attr
import stac_fastapi.api.models
from fastapi import Query
from stac_fastapi.api.models import ItemCollectionUri
from stac_fastapi.extensions.core import (
    CollectionSearchFilterExtension,
    FilterExtension,
    FreeTextExtension,
)
from stac_fastapi.extensions.core.collection_search import CollectionSearchExtension
from stac_fastapi.extensions.core.fields import (
    FieldsConformanceClasses,
    FieldsExtension,
)
from stac_fastapi.extensions.core.filter import FilterConformanceClasses
from stac_fastapi.extensions.core.free_text import FreeTextConformanceClasses
from stac_fastapi.extensions.core.pagination import OffsetPaginationExtension
from stac_fastapi.extensions.core.query import QueryConformanceClasses, QueryExtension
from stac_fastapi.extensions.core.sort import SortConformanceClasses, SortExtension
from stac_fastapi.types.search import APIRequest, BaseSearchPostRequest
from stac_pydantic.shared import BBox

from .filters import FiltersClient
from .search import FixedSearchGetRequest, _bbox_converter, _ids_converter

# ---------------------------------------------------------------------------
# Item-search extensions (used for GET /search, POST /search, GET /items)
# ---------------------------------------------------------------------------
filter_extension = FilterExtension(client=FiltersClient())
filter_extension.conformance_classes.append(
    FilterConformanceClasses.ADVANCED_COMPARISON_OPERATORS
)
fields_extension = FieldsExtension()
fields_extension.conformance_classes.append(FieldsConformanceClasses.ITEMS)

ITEM_EXTENSIONS = [
    fields_extension,
    filter_extension,
    FreeTextExtension(),
    OffsetPaginationExtension(),
    QueryExtension(),
    SortExtension(),
]

# ---------------------------------------------------------------------------
# Collection-search extensions (used for GET /collections)
# ---------------------------------------------------------------------------

collection_search_extensions = [
    CollectionSearchFilterExtension(conformance_classes=[FilterConformanceClasses.COLLECTIONS]),
    FieldsExtension(conformance_classes=[FieldsConformanceClasses.COLLECTIONS]),
    FreeTextExtension(conformance_classes=[FreeTextConformanceClasses.COLLECTIONS]),
    QueryExtension(conformance_classes=[QueryConformanceClasses.COLLECTIONS]),
    SortExtension(conformance_classes=[SortConformanceClasses.COLLECTIONS]),
]

collection_search_ext = CollectionSearchExtension.from_extensions(
    collection_search_extensions
)

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

GetSearchRequestModel = stac_fastapi.api.models.create_get_request_model(
    base_model=FixedSearchGetRequest, extensions=ITEM_EXTENSIONS
)
PostSearchRequestModel = stac_fastapi.api.models.create_post_request_model(
    base_model=BaseSearchPostRequest, extensions=ITEM_EXTENSIONS
)
ItemsGetRequestModel = stac_fastapi.api.models.create_get_request_model(
    base_model=ItemCollectionUri, extensions=ITEM_EXTENSIONS
)

def _q_converter(
    val: Annotated[
        str | None,
        Query(
            description=(
                "Free-text search terms. Matches against collection title, "
                "description, and keywords (case-insensitive, space-separated)."
            ),
            openapi_examples={
                "user-provided": {"value": None},
                "single-term": {"value": "imagery"},
                "multi-term": {"value": "aerial high-resolution"},
            },
        ),
    ] = None,
) -> list[str] | None:
    """Split a space-separated free-text query string into individual terms."""
    if val:
        return [t.strip() for t in val.split() if t.strip()]
    return None


@attr.s
class CollectionSearchRequest(APIRequest):
    """Query parameters for ``GET /collections`` collection search."""

    ids: list[str] | None = attr.ib(default=None, converter=_ids_converter)
    bbox: BBox | None = attr.ib(default=None, converter=_bbox_converter)
    datetime: Annotated[
        str | None,
        Query(
            description=(
                "Only return collections whose temporal extent overlaps this value. "
                "Either a date-time or an interval (open or closed). "
                "Date and time expressions adhere to RFC 3339. "
                "Open intervals are expressed using double-dots."
            ),
            openapi_examples={
                "user-provided": {"value": None},
                "datetime": {"value": "2018-02-12T23:20:50Z"},
                "closed-interval": {"value": "2018-02-12T00:00:00Z/2018-03-18T12:31:12Z"},
                "open-interval-from": {"value": "2018-02-12T00:00:00Z/.."},
                "open-interval-to": {"value": "../2018-03-18T12:31:12Z"},
            },
        ),
    ] = attr.ib(default=None)
    q: list[str] | None = attr.ib(default=None, converter=_q_converter)
    limit: Annotated[
        int | None,
        Query(
            ge=1,
            le=10_000,
            description="Maximum number of collections to return (1–10 000).",
        ),
    ] = attr.ib(default=None)
    offset: Annotated[
        int | None,
        Query(ge=0, description="Number of collections to skip for pagination."),
    ] = attr.ib(default=None)


# Override the GET model with the hand-crafted one so the /collections
# endpoint uses CollectionSearchRequest instead of the auto-generated model.
collection_search_ext.GET = CollectionSearchRequest
