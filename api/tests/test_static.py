"""The built web app is served from the API's origin. No database needed; skipped until web/dist is built."""
import pytest
from fastapi.testclient import TestClient

from app import main

pytestmark = pytest.mark.skipif(not main.WEB_DIST.is_dir(), reason="build web/dist first")


@pytest.fixture()
def client():
    return TestClient(main.app)


def test_serves_pwa_files(client):
    assert "standalone" in client.get("/manifest.webmanifest").text
    assert client.get("/sw.js").status_code == 200


def test_unknown_route_falls_back_to_index(client):
    r = client.get("/game/123")
    assert r.status_code == 200 and "<div id=\"root\">" in r.text


@pytest.mark.parametrize("path", ["/..%2F..%2Fdocker-compose.yml", "/%2E%2E/%2E%2E/docker-compose.yml"])
def test_no_path_traversal(client, path):
    r = client.get(path)
    assert "services:" not in r.text and "<div id=\"root\">" in r.text


def test_unknown_api_path_is_404(client):
    assert client.get("/api/nope").status_code == 404
