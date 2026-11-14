"""Tests for geoparquet files that store ``assets`` as a JSON string."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rustac import DuckdbClient  # type: ignore[attr-defined]

import stac_fastapi.geoparquet.api
from stac_fastapi.geoparquet import Settings

from .conftest import COLLECTIONS_PATH, DATA_DIR


@pytest.fixture(scope="module")
def json_assets_collections(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Copy the naip fixture with its ``assets`` struct rewritten as JSON text."""
    tmp_dir = tmp_path_factory.mktemp("json_assets_fixtures")
    collections = [
        collection
        for collection in json.loads(COLLECTIONS_PATH.read_text())
        if collection["id"] == "naip"
    ]
    asset = collections[0]["assets"]["data"]
    src = (DATA_DIR / asset["href"]).resolve()
    dst = tmp_dir / src.name
    DuckdbClient().execute(
        "COPY (SELECT * REPLACE (CAST(to_json(assets) AS VARCHAR) AS assets) "
        f"FROM read_parquet('{src}')) TO '{dst}' (FORMAT PARQUET);"
    )
    asset["href"] = str(dst)
    collections_path = tmp_dir / "collections.json"
    collections_path.write_text(json.dumps(collections))
    return collections_path


@pytest.fixture
def json_assets_client(json_assets_collections: Path) -> Iterator[TestClient]:
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(json_assets_collections),
    )
    api = stac_fastapi.geoparquet.api.create(settings, duckdb_client=DuckdbClient())
    with TestClient(api.app) as client:
        yield client


def _assert_assets_are_objects(features: list[dict[str, object]]) -> None:
    assert features
    for feature in features:
        assert isinstance(feature["assets"], dict), feature["assets"]
        assert feature["assets"]


def test_items_parse_json_assets(json_assets_client: TestClient) -> None:
    response = json_assets_client.get("/collections/naip/items")
    assert response.status_code == 200, response.text
    _assert_assets_are_objects(response.json()["features"])


def test_item_parses_json_assets(json_assets_client: TestClient) -> None:
    response = json_assets_client.get("/collections/naip/items", params={"limit": 1})
    assert response.status_code == 200, response.text
    item_id = response.json()["features"][0]["id"]
    response = json_assets_client.get(f"/collections/naip/items/{item_id}")
    assert response.status_code == 200, response.text
    _assert_assets_are_objects([response.json()])


def test_get_search_parses_json_assets(json_assets_client: TestClient) -> None:
    response = json_assets_client.get("/search", params={"collections": "naip"})
    assert response.status_code == 200, response.text
    _assert_assets_are_objects(response.json()["features"])


def test_post_search_parses_json_assets(json_assets_client: TestClient) -> None:
    response = json_assets_client.post("/search", json={"collections": ["naip"]})
    assert response.status_code == 200, response.text
    _assert_assets_are_objects(response.json()["features"])
