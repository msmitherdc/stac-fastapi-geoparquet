from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rustac import DuckdbClient  # type: ignore[attr-defined]

import stac_fastapi.geoparquet.api
from stac_fastapi.geoparquet import Settings

DATA_DIR = Path(__file__).parents[1] / "data"
COLLECTIONS_PATH = DATA_DIR / "collections.json"
NAIP_PATH = DATA_DIR / "naip.parquet"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(COLLECTIONS_PATH),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    duckdb_client = DuckdbClient()  # no S3 secret, fine for local parquet files
    api = stac_fastapi.geoparquet.api.create(settings, duckdb_client=duckdb_client)
    with TestClient(api.app) as client:
        yield client
