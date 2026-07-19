"""Tests for the grid-specific ``x-grid-accesstags`` access control.

The session fixtures tag every collection (and every parquet row) with
``access_tag_id = 1``, so a header that doesn't include tag 1 must hide
everything, and one that does (or no header at all) must behave as public.
"""

from fastapi.testclient import TestClient

ALL_IDS = {"naip", "naip-10", "openaerialmap-10", "openaerialmap"}


def test_no_header_defaults_to_public(client: TestClient) -> None:
    response = client.get("/collections")
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS


def test_matching_tag_sees_everything(client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[1, 2]"}
    response = client.get("/collections", headers=headers)
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS

    response = client.get("/collections/naip", headers=headers)
    assert response.status_code == 200

    response = client.get("/search", params={"limit": 1}, headers=headers)
    assert response.status_code == 200
    assert len(response.json()["features"]) == 1


def test_non_matching_tag_hides_collections(client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[2]"}
    response = client.get("/collections", headers=headers)
    assert response.status_code == 200
    assert response.json()["collections"] == []


def test_non_matching_tag_404s_collection(client: TestClient) -> None:
    response = client.get("/collections/naip", headers={"x-grid-accesstags": "[2]"})
    assert response.status_code == 404


def test_non_matching_tag_404s_items(client: TestClient) -> None:
    response = client.get(
        "/collections/naip/items", headers={"x-grid-accesstags": "[2]"}
    )
    assert response.status_code == 404


def test_non_matching_tag_empty_search(client: TestClient) -> None:
    headers = {"x-grid-accesstags": "[2]"}
    response = client.get("/search", headers=headers)
    assert response.status_code == 200
    assert response.json()["features"] == []

    response = client.post("/search", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["features"] == []


def test_malformed_header_is_400(client: TestClient) -> None:
    for value in ("not a list", "foo][", "[1", "{'a': 1}", "['1) OR (true']", "1.5"):
        for path in ("/search", "/collections", "/collections/naip"):
            response = client.get(path, headers={"x-grid-accesstags": value})
            assert response.status_code == 400, (path, value, response.text)


def test_single_integer_header_accepted(client: TestClient) -> None:
    response = client.get("/collections", headers={"x-grid-accesstags": "1"})
    assert response.status_code == 200
    assert {c["id"] for c in response.json()["collections"]} == ALL_IDS


def test_access_tag_id_not_leaked_in_items(client: TestClient) -> None:
    response = client.get("/search", params={"limit": 1})
    response.raise_for_status()
    properties = response.json()["features"][0]["properties"]
    assert "access_tag_id" not in properties


def test_access_tag_id_not_leaked_even_when_requested(client: TestClient) -> None:
    # access_tag_id is purely internal — explicitly requesting it via
    # `fields` must not expose it either.
    response = client.get(
        "/search", params={"limit": 1, "fields": "id,geometry,access_tag_id"}
    )
    response.raise_for_status()
    properties = response.json()["features"][0]["properties"]
    assert "access_tag_id" not in properties
