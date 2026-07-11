import json
import os
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

import obstore.store
import pystac.utils
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from rustac import DuckdbClient  # type: ignore[attr-defined]
from stac_fastapi.api.app import StacApi

from .client import Client
from .models import (
    ITEM_EXTENSIONS,
    CollectionSearchRequest,
    GetSearchRequestModel,
    ItemsGetRequestModel,
    PostSearchRequestModel,
)
from .settings import Settings

GEOPARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"


class State(TypedDict):
    """Application state."""

    client: DuckdbClient
    """The DuckDB client.

    It's just an in-memory DuckDB connection with the spatial extension enabled.
    """

    collections: dict[str, dict[str, Any]]
    """A mapping of collection id to collection."""

    hrefs: dict[str, str]
    """A mapping of collection id to geoparquet href."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[State]:
    client = app.extra["duckdb_client"]
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
    settings: Settings = app.extra["settings"]
    collections = app.extra["collections"]
    collection_dict = dict()
    hrefs = dict()
    for collection in collections:
        if collection["id"] in collection_dict:
            raise HTTPException(
                500, f"two collections with the same id: {collection['id']}"
            )
        else:
            collection_dict[collection["id"]] = collection
        for key, asset in collection["assets"].items():
            if asset.get("type") == GEOPARQUET_MEDIA_TYPE:
                if collection["id"] in hrefs:
                    raise HTTPException(
                        500, f"two hrefs for one collection: {collection['id']}"
                    )
                else:
                    hrefs[collection["id"]] = pystac.utils.make_absolute_href(
                        asset["href"],
                        settings.stac_fastapi_collections_href,
                        start_is_dir=False,
                    )
    yield {
        "client": client,
        "collections": collection_dict,
        "hrefs": hrefs,
    }


def create(
    settings: Settings | None = None,
    duckdb_client: DuckdbClient | None = None,
) -> StacApi:
    if duckdb_client is None:
        duckdb_client = DuckdbClient()
        skip_s3 = os.getenv("STAC_FASTAPI_SKIP_S3_SECRET", "").lower() in ("1", "true")
        if not skip_s3:
            s3end = os.getenv("AWS_S3_ENDPOINT")
            if s3end:
                duckdb_client.execute(
                    f"CREATE OR REPLACE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REFRESH auto, ENDPOINT '{s3end}');"
                )
            else:
                duckdb_client.execute(
                    "CREATE OR REPLACE SECRET (TYPE S3, PROVIDER CREDENTIAL_CHAIN, REFRESH auto);"
                )
        duckdb_client.execute("SET parquet_metadata_cache = true;")
    if settings is None:
        settings = Settings(
            stac_fastapi_landing_id=os.getenv("STAC_FASTAPI_LANDING_ID", ""),
            stac_fastapi_title=os.getenv("STAC_FASTAPI_TITLE", ""),
            stac_fastapi_description=os.getenv("STAC_FASTAPI_DESCRIPTION", ""),
        )

    if settings.stac_fastapi_collections_href:
        if urllib.parse.urlparse(settings.stac_fastapi_collections_href).scheme:
            href = settings.stac_fastapi_collections_href
        else:
            href = "file://" + str(
                Path(settings.stac_fastapi_collections_href).absolute()
            )
        prefix, file_name = href.rsplit("/", 1)
        store = obstore.store.from_url(prefix)
        result = store.get(file_name)
        collections = json.loads(bytes(result.bytes()))
    else:
        collections = []

    if settings.stac_fastapi_geoparquet_href:
        collections.extend(
            collections_from_geoparquet_href(
                settings.stac_fastapi_geoparquet_href,
                duckdb_client,
            )
        )

    # Create the FastAPI application with docs_url=None to prevent FastAPI from serving CDN-based UI
    app_instance = FastAPI(
        lifespan=lifespan,
        openapi_url=settings.openapi_url,
        docs_url=None,  # Disabled so we can register our own custom endpoint below
        redoc_url=None,  # Set to None if you also want to customize/disable Redoc
        settings=settings,
        collections=collections,
        duckdb_client=duckdb_client,
        redirect_slashes=False,
    )

    # 2. Add the custom route to serve the swagger UI with local CDN alternatives
    @app_instance.get("/api.html", include_in_schema=False, name="swagger_ui_html")
    async def custom_swagger_ui_html(req: Request) -> HTMLResponse:
        # Dynamically extract root_path from the incoming request scope
        root_path = req.scope.get("root_path", "").rstrip("/")

        openapi_url = root_path + app_instance.openapi_url
        oauth2_redirect_url = app_instance.swagger_ui_oauth2_redirect_url
        if oauth2_redirect_url:
            oauth2_redirect_url = root_path + oauth2_redirect_url

        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=app_instance.title + " - OpenAPI UI",
            oauth2_redirect_url=oauth2_redirect_url,
            swagger_js_url=root_path + "/static/swagger-ui-bundle.js",
            swagger_css_url=root_path + "/static/swagger-ui.css",
        )

    api = StacApi(
        settings=settings,
        client=Client(),
        app=app_instance,  # Pass our customized FastAPI instance here
        search_get_request_model=GetSearchRequestModel,
        search_post_request_model=PostSearchRequestModel,
        items_get_request_model=ItemsGetRequestModel,
        collections_get_request_model=CollectionSearchRequest,
        extensions=ITEM_EXTENSIONS,
    )
    return api


def collections_from_geoparquet_href(
    href: str, duckdb_client: DuckdbClient
) -> list[dict[str, Any]]:
    collections = duckdb_client.get_collections(href)
    for collection in collections:
        collection["links"] = []
        collection["assets"] = {"data": {"href": href, "type": GEOPARQUET_MEDIA_TYPE}}
    return collections
