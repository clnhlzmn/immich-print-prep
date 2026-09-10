from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from immich_print_prep.app import create_app  # noqa: E402
from immich_print_prep.config import load_config  # noqa: E402

from fake_immich import API_KEY, FakeImmich, FakeImmichServer  # noqa: E402


@pytest.fixture
def immich():
    fake = FakeImmich()
    with FakeImmichServer(fake) as server:
        fake.url = server.url
        yield fake


@pytest.fixture
def config_path(tmp_path, immich):
    path = tmp_path / "config.yaml"
    path.write_text(
        "immich_url: %s\n"
        "data_dir: %s\n"
        "users:\n"
        "  - colin\n"
        "  - username: alice\n"
        "    initial_password: start-here-123\n" % (immich.url, tmp_path / "data"),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config(config_path):
    return load_config(str(config_path))


@pytest.fixture
def client(config):
    app = create_app(config)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client, immich):
    """A claimed account with a working Immich API key."""
    response = client.post("/api/auth/login", json={"username": "colin", "password": ""})
    assert response.json()["status"] == "password_not_set"
    response = client.post(
        "/api/auth/set-password",
        json={"username": "colin", "current_password": "", "new_password": "print-me-please"},
    )
    assert response.status_code == 200, response.text
    response = client.put("/api/settings", json={"api_key": API_KEY})
    assert response.status_code == 200, response.text
    return client
