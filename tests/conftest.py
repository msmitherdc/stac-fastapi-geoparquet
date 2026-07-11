import json
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

# grid's Client.search() unconditionally injects `access_tag_id IN (...)`
# into every search filter (it isn't something a caller/test can opt out
# of), and Client._has_access() gates every collection on an `access_tag_id`
# field. The checked-in fixtures don't carry that field/column at all, so
# every content-asserting search would otherwise match zero rows. Rather
# than mutate the checked-in binary fixtures, materialize copies with
# `access_tag_id` added once per test session.
ACCESS_TAG_ID = 1


@pytest.fixture(scope="session")
def collections_with_access_tag(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_dir = tmp_path_factory.mktemp("access_tag_fixtures")
    duckdb_client = DuckdbClient()

    collections = json.loads(COLLECTIONS_PATH.read_text())
    for collection in collections:
        collection["access_tag_id"] = ACCESS_TAG_ID
        asset = collection["assets"]["data"]
        src = (DATA_DIR / asset["href"]).resolve()
        dst = tmp_dir / src.name
        duckdb_client.execute(
            f"COPY (SELECT *, {ACCESS_TAG_ID} AS access_tag_id FROM "
            f"read_parquet('{src}')) TO '{dst}' (FORMAT PARQUET);"
        )
        asset["href"] = str(dst)

    collections_path = tmp_dir / "collections.json"
    collections_path.write_text(json.dumps(collections))
    return collections_path


@pytest.fixture
def client(collections_with_access_tag: Path) -> Iterator[TestClient]:
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(collections_with_access_tag),
    )
    duckdb_client = DuckdbClient()  # no S3 secret, fine for local parquet files
    api = stac_fastapi.geoparquet.api.create(settings, duckdb_client=duckdb_client)
    with TestClient(api.app) as client:
        yield client
