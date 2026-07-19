from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rustac import DuckdbClient  # type: ignore[attr-defined]

import stac_fastapi.geoparquet.api
from stac_fastapi.geoparquet import Settings

from .conftest import COLLECTIONS_PATH, NAIP_PATH


@pytest.fixture
def extension_directory() -> Path:
    return Path(__file__).parent / "duckdb-extensions"


def test_create(extension_directory: Path) -> None:
    duckdb_client = DuckdbClient(extension_directory=str(extension_directory))
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(COLLECTIONS_PATH),
    )
    api = stac_fastapi.geoparquet.api.create(
        duckdb_client=duckdb_client, settings=settings
    )
    with TestClient(api.app) as client:
        response = client.get("/search")
        assert response.status_code == 200


def test_create_from_parquet_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # This doesn't pass its own duckdb_client, so `create()` would otherwise
    # try to create a real S3 credential-chain secret - CI runners have no
    # AWS credentials configured, so that hard-fails with
    # `RustacError: ... Secret Validation Failure`.
    monkeypatch.setenv("STAC_FASTAPI_SKIP_S3_SECRET", "true")
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_geoparquet_href=str(NAIP_PATH),
    )
    api = stac_fastapi.geoparquet.api.create(settings=settings)
    with TestClient(api.app) as client:
        # Auto-generated collections carry no access_tag_id — they must be
        # treated as public everywhere, not just in the collections list.
        response = client.get("/search")
        assert response.status_code == 200
        assert len(response.json()["features"]) > 0

        response = client.get("/collections")
        collection_ids = [c["id"] for c in response.json()["collections"]]
        assert collection_ids == ["naip"]

        response = client.get("/collections/naip")
        assert response.status_code == 200

        response = client.get("/collections/naip/items")
        assert response.status_code == 200
        assert len(response.json()["features"]) > 0
