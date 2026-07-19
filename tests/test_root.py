from fastapi.testclient import TestClient
from pystac import Catalog


def test_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    d = response.json()
    d["links"] = []  # to stop pystac from resolving links
    catalog = Catalog.from_dict(d)
    catalog.validate()


def test_conformance_advertises_collection_search(client: TestClient) -> None:
    response = client.get("/conformance")
    assert response.status_code == 200
    conforms_to = response.json()["conformsTo"]
    assert any("collection-search" in c for c in conforms_to), conforms_to
