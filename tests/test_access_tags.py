"""Tests for the optional ``x-grid-accesstags`` access control.

These exercise the opt-in app built by
:func:`stac_fastapi.geoparquet.access_tags.create`; the stock app (the
``client`` fixture in ``conftest.py``) has none of this behaviour.

The fixtures below tag every collection (and every parquet row) with
``access_tag_id = 1``, so a header that doesn't include tag 1 must hide
everything, and one that does (or no header at all) must behave as public.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from rustac import DuckdbClient  # type: ignore[attr-defined]

from stac_fastapi.geoparquet import Settings, access_tags

from .conftest import COLLECTIONS_PATH, DATA_DIR

ALL_IDS = {"naip", "naip-10", "openaerialmap-10", "openaerialmap"}
ACCESS_TAG_ID = 1
PRIVATE_ACCESS_TAG = 7


@pytest.fixture(scope="session")
def collections_with_access_tag(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Materialize copies of the checked-in fixtures with an access tag.

    The checked-in collections.json / parquet carry no ``access_tag_id`` at
    all, so rather than mutate binary fixtures, copy them once per session
    with the column and field added.
    """
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
def access_tag_client(collections_with_access_tag: Path) -> Iterator[TestClient]:
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(collections_with_access_tag),
    )
    duckdb_client = DuckdbClient()  # no S3 secret, fine for local parquet files
    api = access_tags.create(settings, duckdb_client=duckdb_client)
    with TestClient(api.app) as client:
        yield client


@pytest.fixture(scope="session")
def mixed_tag_collections(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A single collection whose rows carry a mix of access tags.

    Rows alternate between tag 1 (public) and tag 7, so a search can be
    checked for row-level — not just collection-level — filtering.
    """
    tmp_dir = tmp_path_factory.mktemp("mixed_tag_fixtures")
    duckdb_client = DuckdbClient()

    collections = json.loads(COLLECTIONS_PATH.read_text())
    collection = next(c for c in collections if c["id"] == "naip")
    # Visible to both tags, so only the row filter can hide anything.
    collection["access_tag_id"] = ACCESS_TAG_ID
    src = (DATA_DIR / collection["assets"]["data"]["href"]).resolve()
    dst = tmp_dir / "mixed.parquet"
    # A small slice keeps every search below the page limit, so the tests can
    # compare whole result sets rather than pages of them; taking it from two
    # years means a caller-supplied filter has something to exclude.
    duckdb_client.execute(
        "COPY (SELECT *, CASE WHEN row_number() OVER () % 2 = 0 THEN "
        f"{ACCESS_TAG_ID} ELSE {PRIVATE_ACCESS_TAG} END AS access_tag_id FROM ("
        f"""(SELECT * FROM read_parquet('{src}') WHERE "naip:year" = '2022' LIMIT 100)"""
        " UNION ALL "
        f"""(SELECT * FROM read_parquet('{src}') WHERE "naip:year" = '2021' LIMIT 100)"""
        f")) TO '{dst}' (FORMAT PARQUET);"
    )
    collection["assets"]["data"]["href"] = str(dst)

    collections_path = tmp_dir / "collections.json"
    collections_path.write_text(json.dumps([collection]))
    return collections_path


@pytest.fixture
def mixed_tag_client(mixed_tag_collections: Path) -> Iterator[TestClient]:
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(mixed_tag_collections),
    )
    api = access_tags.create(settings, duckdb_client=DuckdbClient())
    with TestClient(api.app) as client:
        yield client


def test_no_header_defaults_to_public(access_tag_client: TestClient) -> None:
    response = access_tag_client.get("/collections")
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS


def test_matching_tag_sees_everything(access_tag_client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[1, 2]"}
    response = access_tag_client.get("/collections", headers=headers)
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS

    response = access_tag_client.get("/collections/naip", headers=headers)
    assert response.status_code == 200

    response = access_tag_client.get("/search", params={"limit": 1}, headers=headers)
    assert response.status_code == 200
    assert len(response.json()["features"]) == 1


def test_non_matching_tag_hides_collections(access_tag_client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[2]"}
    response = access_tag_client.get("/collections", headers=headers)
    assert response.status_code == 200
    assert response.json()["collections"] == []


def test_non_matching_tag_404s_collection(access_tag_client: TestClient) -> None:
    response = access_tag_client.get(
        "/collections/naip", headers={"x-grid-accesstags": "[2]"}
    )
    assert response.status_code == 404


def test_non_matching_tag_404s_items(access_tag_client: TestClient) -> None:
    response = access_tag_client.get(
        "/collections/naip/items", headers={"x-grid-accesstags": "[2]"}
    )
    assert response.status_code == 404


def test_non_matching_tag_empty_search(access_tag_client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[2]"}
    response = access_tag_client.get("/search", headers=headers)
    assert response.status_code == 200
    assert response.json()["features"] == []

    response = access_tag_client.post("/search", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["features"] == []


def test_malformed_header_is_400(access_tag_client: TestClient) -> None:
    for value in ("not a list", "foo][", "[1", "{'a': 1}", "['1) OR (true']", "1.5"):
        for path in ("/search", "/collections", "/collections/naip"):
            response = access_tag_client.get(path, headers={"x-grid-accesstags": value})
            assert response.status_code == 400, (path, value, response.text)


def test_single_integer_header_accepted(access_tag_client: TestClient) -> None:
    response = access_tag_client.get("/collections", headers={"x-grid-accesstags": "1"})
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS


def test_access_tag_id_not_leaked_in_items(access_tag_client: TestClient) -> None:
    response = access_tag_client.get("/search", params={"limit": 1})
    response.raise_for_status()
    properties = response.json()["features"][0]["properties"]
    assert "access_tag_id" not in properties


def test_access_tag_id_not_leaked_even_when_requested(
    access_tag_client: TestClient,
) -> None:
    # access_tag_id is purely internal — explicitly requesting it via
    # `fields` must not expose it either.
    response = access_tag_client.get(
        "/search", params={"limit": 1, "fields": "id,geometry,access_tag_id"}
    )
    response.raise_for_status()
    properties = response.json()["features"][0]["properties"]
    assert "access_tag_id" not in properties


def test_access_tag_id_not_a_queryable(access_tag_client: TestClient) -> None:
    response = access_tag_client.get("/collections/naip/queryables")
    response.raise_for_status()
    assert "access_tag_id" not in response.json()["properties"]


def test_stock_app_ignores_the_header(client: TestClient) -> None:
    # The stock app doesn't know about access tags at all — the header is
    # inert rather than an error, and nothing is hidden.
    response = client.get("/collections", headers={"x-grid-accesstags": "[2]"})
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS


def _search_ids(client: TestClient, tags: str, **params: str) -> set[str]:
    response = client.get(
        "/search",
        params={"limit": "1000", **params},
        headers={"x-grid-accesstags": tags},
    )
    response.raise_for_status()
    return {f["id"] for f in response.json()["features"]}


def _ids_with_tag(collections_path: Path, tag: int) -> set[str]:
    """The ids actually carrying ``tag`` in the backing parquet."""
    collection = json.loads(collections_path.read_text())[0]
    href = collection["assets"]["data"]["href"]
    table = DuckdbClient().query_to_table(
        f"SELECT id FROM read_parquet('{href}') WHERE access_tag_id = {tag}"
    )
    return set(table.column("id").to_pylist())


def test_rows_outside_the_collection_tag_are_hidden(
    mixed_tag_client: TestClient, mixed_tag_collections: Path
) -> None:
    # The collection owns tag 1 and its file also holds tag-7 rows. Only the
    # tag-1 rows may ever surface under it - including for a caller who holds
    # tag 7 as well, since the collection's own tag is what scopes the rows.
    public_rows = _ids_with_tag(mixed_tag_collections, ACCESS_TAG_ID)
    private_rows = _ids_with_tag(mixed_tag_collections, PRIVATE_ACCESS_TAG)
    assert public_rows and private_rows

    for header in (
        f"[{ACCESS_TAG_ID}]",
        f"[{ACCESS_TAG_ID}, {PRIVATE_ACCESS_TAG}]",
    ):
        returned = _search_ids(mixed_tag_client, header)
        assert returned == public_rows, header
        assert returned.isdisjoint(private_rows), header


def test_row_filter_survives_a_caller_filter(
    mixed_tag_client: TestClient, mixed_tag_collections: Path
) -> None:
    # A caller-supplied filter is ANDed with the access filter, not swallowed
    # by it, and cannot widen what the access filter allows.
    public_rows = _ids_with_tag(mixed_tag_collections, ACCESS_TAG_ID)
    private_rows = _ids_with_tag(mixed_tag_collections, PRIVATE_ACCESS_TAG)

    filtered = _search_ids(
        mixed_tag_client, f"[{ACCESS_TAG_ID}]", filter="naip:year='2022'"
    )
    assert filtered
    assert filtered < public_rows  # the caller's filter still narrows
    assert filtered.isdisjoint(private_rows)


# ---------------------------------------------------------------------------
# Cross-tag leakage: multiple collections sharing one physical parquet file,
# sliced only by access_tag_id (GRiD's real-world pattern — e.g.
# "DGED5_Reflective_{2m}-Raster-504" and "…-Raster-2304" both point at the
# same href, distinguished only by which access_tag_id each collection owns).
#
# A caller granted *both* tags querying the "504" collection must never see
# rows whose row-level access_tag_id is 2304 — the per-collection filter has
# to scope to that collection's own tag, not to the caller's whole granted
# set. (Reported against a live deployment: a row tagged 2304 in the backing
# parquet was returned under the sibling "-504" collection.)
# ---------------------------------------------------------------------------


@pytest.fixture
def shared_href_collections(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two collections, tags 504 and 2304, backed by the *same* parquet file."""
    tmp_dir = tmp_path_factory.mktemp("shared_href_fixtures")
    duckdb_client = DuckdbClient()

    all_collections = json.loads(COLLECTIONS_PATH.read_text())
    naip = next(c for c in all_collections if c["id"] == "naip")
    src = (DATA_DIR / naip["assets"]["data"]["href"]).resolve()

    # One physical file holding rows for both tags, mirroring the real
    # layout where a single data-program parquet contains every access tag.
    shared_path = tmp_dir / "shared.parquet"
    duckdb_client.execute(
        f"""
        COPY (
            SELECT * REPLACE (
                CASE WHEN row_number() OVER () % 2 = 0 THEN 504 ELSE 2304 END
                AS access_tag_id
            )
            FROM (SELECT *, 1 AS access_tag_id FROM read_parquet('{src}'))
        ) TO '{shared_path}' (FORMAT PARQUET);
        """
    )

    def _collection(collection_id: str, access_tag_id: int) -> dict[str, Any]:
        coll = json.loads(json.dumps(naip))  # deep copy
        coll["id"] = collection_id
        coll["access_tag_id"] = access_tag_id
        coll["assets"]["data"]["href"] = str(shared_path)
        return coll

    collections = [
        _collection("raster-504", 504),
        _collection("raster-2304", 2304),
    ]
    collections_path = tmp_dir / "collections.json"
    collections_path.write_text(json.dumps(collections))
    return collections_path


@pytest.fixture
def shared_href_client(shared_href_collections: Path) -> Iterator[TestClient]:
    settings = Settings(
        stac_fastapi_landing_id="test",
        stac_fastapi_title="test",
        stac_fastapi_description="test",
        stac_fastapi_collections_href=str(shared_href_collections),
    )
    duckdb_client = DuckdbClient()
    api = access_tags.create(settings, duckdb_client=duckdb_client)
    with TestClient(api.app) as client:
        yield client


def test_collection_scoped_to_its_own_access_tag(
    shared_href_client: TestClient, shared_href_collections: Path
) -> None:
    # A caller granted BOTH tags must only see items whose row-level
    # access_tag_id matches the collection they're querying — not any row
    # visible under either of their granted tags.
    duckdb_client = DuckdbClient()
    collections = json.loads(shared_href_collections.read_text())
    href = collections[0]["assets"]["data"]["href"]
    ids_by_tag: dict[int, set[str]] = {}
    for tag in (504, 2304):
        table = duckdb_client.query_to_table(
            f"SELECT id FROM read_parquet('{href}') WHERE access_tag_id = {tag}"
        )
        ids_by_tag[tag] = set(table.column("id").to_pylist())
    assert ids_by_tag[504] and ids_by_tag[2304]
    assert ids_by_tag[504].isdisjoint(ids_by_tag[2304])

    headers = {"x-grid-accesstags": "[504, 2304]"}

    response = shared_href_client.get(
        "/collections/raster-504/items", params={"limit": 10_000}, headers=headers
    )
    response.raise_for_status()
    returned_504 = {f["id"] for f in response.json()["features"]}
    assert returned_504, "expected some items under raster-504"
    assert returned_504 <= ids_by_tag[504]
    assert returned_504.isdisjoint(ids_by_tag[2304])

    response = shared_href_client.get(
        "/collections/raster-2304/items", params={"limit": 10_000}, headers=headers
    )
    response.raise_for_status()
    returned_2304 = {f["id"] for f in response.json()["features"]}
    assert returned_2304, "expected some items under raster-2304"
    assert returned_2304 <= ids_by_tag[2304]
    assert returned_2304.isdisjoint(ids_by_tag[504])


def test_unscoped_search_still_partitions_by_collection(
    shared_href_client: TestClient,
) -> None:
    # `/search` with no explicit `collections` walks every collection the
    # caller can see, one at a time — each iteration must still scope to
    # that collection's own tag.
    headers = {"x-grid-accesstags": "[504, 2304]"}
    response = shared_href_client.get(
        "/search", params={"limit": 10_000}, headers=headers
    )
    response.raise_for_status()
    data = response.json()
    by_collection: dict[str, set[str]] = {}
    for feature in data["features"]:
        by_collection.setdefault(feature["collection"], set()).add(feature["id"])
    assert set(by_collection) == {"raster-504", "raster-2304"}
    assert by_collection["raster-504"].isdisjoint(by_collection["raster-2304"])
