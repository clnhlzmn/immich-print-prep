"""Login, first-login password creation, and password changes."""

from __future__ import annotations


def test_unknown_user_is_rejected(client):
    response = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


def test_first_login_claims_the_account(client):
    response = client.post("/api/auth/login", json={"username": "colin", "password": ""})
    assert response.json() == {
        "status": "password_not_set",
        "username": "colin",
        "needs_initial_password": False,
        "min_password_length": 8,
    }

    response = client.post(
        "/api/auth/set-password",
        json={"username": "colin", "current_password": "", "new_password": "long-enough-1"},
    )
    assert response.status_code == 200
    assert client.get("/api/me").json()["username"] == "colin"

    # Once claimed, the password is required.
    client.post("/api/auth/logout", json={})
    assert client.post("/api/auth/login", json={"username": "colin", "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "colin", "password": "long-enough-1"}).status_code == 200


def test_account_with_an_initial_password_requires_it(client):
    assert client.post("/api/auth/login", json={"username": "alice", "password": ""}).json() == {
        "status": "password_not_set",
        "username": "alice",
        "needs_initial_password": True,
        "min_password_length": 8,
    }
    bad = client.post(
        "/api/auth/set-password",
        json={"username": "alice", "current_password": "nope", "new_password": "long-enough-1"},
    )
    assert bad.status_code == 401
    good = client.post(
        "/api/auth/set-password",
        json={"username": "alice", "current_password": "start-here-123", "new_password": "long-enough-1"},
    )
    assert good.status_code == 200


def test_short_passwords_are_refused(client):
    response = client.post(
        "/api/auth/set-password",
        json={"username": "colin", "current_password": "", "new_password": "short"},
    )
    assert response.status_code == 400
    assert "at least 8" in response.json()["detail"]


def test_change_password_signs_other_devices_out(client, config):
    from fastapi.testclient import TestClient

    from immich_print_prep.app import create_app

    client.post(
        "/api/auth/set-password",
        json={"username": "colin", "current_password": "", "new_password": "first-password"},
    )
    with TestClient(create_app(config)) as other:
        # A second device with a session of its own.
        assert other.post(
            "/api/auth/login", json={"username": "colin", "password": "first-password"}
        ).status_code == 200
        assert other.get("/api/me").status_code == 200

        response = client.post(
            "/api/auth/change-password",
            json={"current_password": "first-password", "new_password": "second-password"},
        )
        assert response.status_code == 200
        # The device that changed it stays signed in; the other one does not.
        assert client.get("/api/me").status_code == 200
        assert other.get("/api/me").status_code == 401


def test_api_requires_a_session(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/selection").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_mutations_require_a_json_content_type(signed_in):
    response = signed_in.post(
        "/api/selection/clear", content="ids=1", headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 415


def test_the_front_end_is_revalidated_rather_than_cached_blind(client, signed_in):
    """An unversioned module a browser caches heuristically serves a stale UI."""
    module = client.get("/static/controls.js")
    assert module.status_code == 200
    assert module.headers["cache-control"] == "no-cache"

    # Cheap to obey: the ETag still answers the check with an empty 304.
    again = client.get("/static/controls.js", headers={"If-None-Match": module.headers["etag"]})
    assert again.status_code == 304
    assert not again.content

    # The shells that load those modules must revalidate too.
    assert signed_in.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/login").headers["cache-control"] == "no-cache"
