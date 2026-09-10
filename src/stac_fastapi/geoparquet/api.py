import json
import logging
import os
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypedDict

import obstore.store
import pystac.utils
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from rustac import DuckdbClient  # type: ignore[attr-defined]
from stac_fastapi.api.app import StacApi
from stac_fastapi.extensions.core.filter.client import BaseFiltersClient
from stac_fastapi.types.core import BaseCoreClient

from .client import Client
from .models import (
    CollectionSearchRequest,
    GetSearchRequestModel,
    ItemsGetRequestModel,
    PostSearchRequestModel,
    collection_search_ext,
    item_extensions,
)
from .settings import Settings

logger = logging.getLogger(__name__)

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


DEFAULT_CA_BUNDLE_PATH = "/tmp/ca-bundle.crt"
"""Where a downloaded CA bundle is written.

``/tmp`` is the only writable location in a Lambda, and it survives between
warm invocations, so the download happens once per execution environment.
"""


def fetch_ca_bundle() -> str | None:
    """Download the CA bundle named by ``STAC_FASTAPI_CA_BUNDLE_URI``.

    Returns the local path, or None when no bundle is configured.

    Keeping the certificates in object storage rather than in the image means
    they can be rotated without rebuilding and repushing it. A Lambda deployed
    as a container image can't use a layer for this - layers only apply to
    zip-packaged functions - so the bundle is fetched at startup instead.

    ``SSL_CERT_FILE`` is set from here so the rest of the process picks it up:
    it has to happen before anything builds an HTTPS client, which is why this
    runs first thing in :func:`create`.
    """
    uri = os.getenv("STAC_FASTAPI_CA_BUNDLE_URI")
    if not uri:
        return None

    destination = Path(os.getenv("STAC_FASTAPI_CA_BUNDLE_PATH", DEFAULT_CA_BUNDLE_PATH))
    if not destination.is_file():
        prefix, file_name = uri.rsplit("/", 1)
        store = obstore.store.from_url(prefix)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(bytes(store.get(file_name).bytes()))
        logger.info("Fetched CA bundle from %s to %s", uri, destination)

    os.environ["SSL_CERT_FILE"] = str(destination)
    return str(destination)


def _sql_literal(value: str) -> str:
    """Escape a value for use inside a single-quoted DuckDB SQL literal."""
    return value.replace("'", "''")


def s3_endpoint() -> tuple[str | None, bool]:
    """Resolve the S3 endpoint into ``(host[:port], use_ssl)``.

    DuckDB's ``ENDPOINT`` wants a bare host - it prepends the scheme itself,
    so handing it a URL produces nonsense like
    ``https://bucket.https://s3.example.gov/key``. Strip any scheme, and let
    an explicit ``http://`` turn SSL off.

    ``AWS_ENDPOINT_URL`` is the AWS SDK's own variable and is read as a
    fallback, so a deployment that sets it for boto3/obstore doesn't have to
    set a second one just for DuckDB.
    """
    raw = os.getenv("AWS_S3_ENDPOINT") or os.getenv("AWS_ENDPOINT_URL")
    if not raw:
        return None, True
    raw = raw.strip()
    # urlsplit only recognizes a netloc after "//", so add one when the value
    # is a bare host.
    split = urllib.parse.urlsplit(raw if "//" in raw else f"//{raw}")
    host = split.netloc or split.path.split("/", 1)[0]
    return (host or None), split.scheme != "http"


def configure_duckdb_client(duckdb_client: DuckdbClient) -> None:
    """Apply the S3 and TLS configuration DuckDB needs to read remote data.

    Kept in one place because every one of these has to be set on the DuckDB
    connection itself - none of them are picked up from the environment.
    """
    # DuckDB's httpfs talks through libcurl, which does *not* consult
    # SSL_CERT_FILE. Without this, a deployment using a private CA can point
    # every other client (boto3, obstore) at its bundle and still have DuckDB
    # fail with "SSL peer certificate or ssh remote key was not ok" - with no
    # way to fix it short of baking the certs into the image.
    if ca_cert_file := os.getenv("SSL_CERT_FILE"):
        if Path(ca_cert_file).is_file():
            duckdb_client.execute(f"SET ca_cert_file = '{_sql_literal(ca_cert_file)}';")
        else:
            # Pointing DuckDB at a missing bundle can fail every TLS request,
            # so fall back to the system trust store - loudly, because the
            # symptom otherwise is an opaque certificate error.
            logger.warning(
                "SSL_CERT_FILE is set to %r, which is not a file; leaving DuckDB "
                "on the system trust store",
                ca_cert_file,
            )

    # Some private endpoints only serve path-style addressing; DuckDB defaults
    # to vhost, which turns the bucket into a subdomain.
    url_style = os.getenv("AWS_S3_URL_STYLE")
    if url_style:
        duckdb_client.execute(f"SET s3_url_style = '{_sql_literal(url_style)}';")

    if os.getenv("STAC_FASTAPI_SKIP_S3_SECRET", "").lower() not in ("1", "true"):
        endpoint, use_ssl = s3_endpoint()
        params = ["TYPE S3", "PROVIDER CREDENTIAL_CHAIN", "REFRESH auto"]
        if endpoint:
            params.append(f"ENDPOINT '{_sql_literal(endpoint)}'")
            if not use_ssl:
                params.append("USE_SSL false")
        if url_style:
            params.append(f"URL_STYLE '{_sql_literal(url_style)}'")
        duckdb_client.execute(f"CREATE OR REPLACE SECRET ({', '.join(params)});")

    duckdb_client.execute("SET parquet_metadata_cache = true;")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[State]:
    client = app.extra["duckdb_client"]
    configure_duckdb_client(client)
    settings: Settings = app.extra["settings"]
    collections = app.extra["collections"]
    collection_dict = dict()
    hrefs = dict()
    for collection in collections:
        if collection["id"] in collection_dict:
            # Startup validation, not an HTTP context — fail app startup.
            raise ValueError(f"two collections with the same id: {collection['id']}")
        else:
            collection_dict[collection["id"]] = collection
        for key, asset in collection["assets"].items():
            if asset.get("type") == GEOPARQUET_MEDIA_TYPE:
                if collection["id"] in hrefs:
                    raise ValueError(
                        f"two hrefs for one collection: {collection['id']}"
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
    client: BaseCoreClient | None = None,
    filters_client: BaseFiltersClient | None = None,
) -> StacApi:
    """Build the STAC API application.

    ``client`` and ``filters_client`` default to the stock implementations;
    pass subclasses to layer extra behaviour (e.g. access control) on top.
    """
    # Before anything builds an HTTPS client - obstore below, and DuckDB in
    # `configure_duckdb_client` - so they all see the same trust store.
    fetch_ca_bundle()

    if duckdb_client is None:
        duckdb_client = DuckdbClient()
        configure_duckdb_client(duckdb_client)
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
        client=client or Client(),
        app=app_instance,  # Pass our customized FastAPI instance here
        search_get_request_model=GetSearchRequestModel,
        search_post_request_model=PostSearchRequestModel,
        items_get_request_model=ItemsGetRequestModel,
        collections_get_request_model=CollectionSearchRequest,
        # collection_search_ext contributes conformance classes only (its
        # register() is a no-op); the /collections request model is the
        # hand-crafted CollectionSearchRequest above.
        extensions=[*item_extensions(filters_client), collection_search_ext],
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
