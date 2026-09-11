"""End to end: browse Immich, build a print set, download the zip."""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from PIL import Image

from fake_immich import API_KEY, RESTRICTED_KEY


@pytest.fixture
def library(immich):
    ids = [
        immich.add_asset("beach.jpg", 4000, 3000),      # landscape
        immich.add_asset("portrait.jpg", 3000, 4000),   # portrait
        immich.add_asset("square.jpg", 2000, 2000),
    ]
    immich.add_album("Holiday", ids[:2])
    immich.add_tag("print-me", [ids[2]])
    return ids


def wait_for_job(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/jobs/%s" % job_id).json()
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish: %s" % job)


def test_api_key_must_be_valid(client):
    client.post(
        "/api/auth/set-password",
        json={"username": "colin", "current_password": "", "new_password": "long-enough-1"},
    )
    assert client.get("/api/albums").status_code == 409       # nothing stored yet
    bad = client.put("/api/settings", json={"api_key": "nope"})
    assert bad.status_code == 400
    assert client.put("/api/settings", json={"api_key": API_KEY}).status_code == 200
    assert client.get("/api/me").json()["has_api_key"] is True
    assert client.get("/api/immich/status").json()["connected"] is True


def test_browse_albums_tags_and_search(signed_in, library):
    albums = signed_in.get("/api/albums").json()["albums"]
    assert [album["name"] for album in albums] == ["Holiday"]
    assert albums[0]["assetCount"] == 2

    tags = signed_in.get("/api/tags").json()["tags"]
    assert [tag["value"] for tag in tags] == ["print-me"]

    assets = signed_in.get("/api/assets", params={"album_id": albums[0]["id"]}).json()
    assert assets["count"] == 2
    assert {item["filename"] for item in assets["items"]} == {"beach.jpg", "portrait.jpg"}

    tagged = signed_in.get("/api/assets", params={"tag_id": tags[0]["id"]}).json()
    assert [item["filename"] for item in tagged["items"]] == ["square.jpg"]

    found = signed_in.get("/api/assets", params={"q": "beach"}).json()
    assert [item["filename"] for item in found["items"]] == ["beach.jpg"]

    smart = signed_in.get("/api/assets", params={"q": "a day at the sea", "smart": True}).json()
    assert smart["count"] == 3


def test_thumbnails_are_proxied(signed_in, library):
    response = signed_in.get("/api/assets/%s/thumb" % library[0])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert Image.open(io.BytesIO(response.content)).size[0] > 0


def test_whole_album_can_be_added_and_removed(signed_in, library, immich):
    album_id = next(iter(immich.albums))
    result = signed_in.post("/api/selection/add-source", json={"album_id": album_id}).json()
    assert result["added"] == 2
    assert signed_in.get("/api/selection").json()["count"] == 2

    # Adding the same album again is a no-op.
    assert signed_in.post("/api/selection/add-source", json={"album_id": album_id}).json()["added"] == 0

    selection = signed_in.get("/api/selection").json()
    assert selection["items"][0]["adjustments"]["width_in"] == 8.0
    assert selection["items"][0]["adjustments"]["rotate"] == "auto"

    signed_in.post("/api/selection/remove", json={"ids": [selection["items"][0]["id"]]})
    assert signed_in.get("/api/selection").json()["count"] == 1

    signed_in.post("/api/selection/clear", json={})
    assert signed_in.get("/api/selection").json()["count"] == 0


def test_tag_and_search_sources(signed_in, library, immich):
    tag_id = next(iter(immich.tags))
    assert signed_in.post("/api/selection/add-source", json={"tag_id": tag_id}).json()["added"] == 1
    signed_in.post("/api/selection/clear", json={})
    assert signed_in.post("/api/selection/add-source", json={"q": "portrait"}).json()["added"] == 1


def test_adjustments_bulk_and_per_photo(signed_in, library):
    signed_in.post("/api/selection/add", json={"assets": [{"id": library[0]}, {"id": library[1]}]})

    bulk = signed_in.put(
        "/api/selection/adjustments",
        json={"adjustments": {"width_in": 5, "height_in": 7, "fit": "crop"}},
    ).json()
    assert all(item["adjustments"]["width_in"] == 5 for item in bulk["items"])
    assert all(item["adjustments"]["fit"] == "crop" for item in bulk["items"])

    one = signed_in.put(
        "/api/selection/adjustments",
        json={"ids": [library[0]], "adjustments": {"rotate": "cw", "crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}}},
    ).json()
    by_id = {item["id"]: item for item in one["items"]}
    assert by_id[library[0]]["adjustments"]["rotate"] == "cw"
    assert by_id[library[0]]["adjustments"]["crop"]["w"] == 0.5
    assert by_id[library[1]]["adjustments"]["rotate"] == "auto"

    reset = signed_in.put("/api/selection/adjustments", json={"ids": [library[0]], "reset": True}).json()
    assert {item["id"]: item["customised"] for item in reset["items"]}[library[0]] is False

    bad = signed_in.put("/api/selection/adjustments", json={"adjustments": {"dpi": 99999}})
    assert bad.status_code == 400


def test_preview_and_geometry(signed_in, library):
    signed_in.post("/api/selection/add", json={"assets": [{"id": library[0]}]})

    proof = signed_in.get("/api/selection/%s/preview" % library[0], params={"max_edge": 300})
    assert proof.status_code == 200
    image = Image.open(io.BytesIO(proof.content))
    assert image.size == (240, 300)   # the 8x10 aspect, scaled to max_edge

    cropped = signed_in.get(
        "/api/selection/%s/preview" % library[0], params={"max_edge": 300, "crop": "0,0,0.5,0.5", "fit": "crop"}
    )
    assert cropped.status_code == 200

    geometry = signed_in.get("/api/selection/%s/geometry" % library[0]).json()
    assert geometry["width"] < geometry["height"]      # landscape source, rotated upright
    assert geometry["target_ratio"] == pytest.approx(0.8)

    source = signed_in.get("/api/selection/%s/source" % library[0])
    assert Image.open(io.BytesIO(source.content)).size[0] > 0

    assert signed_in.get("/api/selection/%s/preview" % library[2]).status_code == 404  # not in the set


def test_prepare_download_and_record_in_immich(signed_in, library, immich):
    album_id = next(iter(immich.albums))
    signed_in.post("/api/selection/add-source", json={"album_id": album_id})
    signed_in.put("/api/settings", json={
        "create_album": True,          # leaves album_name_template at its default
        "create_tag": True,
        "tag_name_template": "printed-{date}",
    })

    job = signed_in.post("/api/prepare", json={}).json()
    assert job["total"] == 2
    job = wait_for_job(signed_in, job["id"])
    assert job["status"] == "done", job
    assert job["detail"]["failures"] == []

    response = signed_in.get(job["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = sorted(archive.namelist())
        assert "print-set-manifest.txt" in names
        photos = [name for name in names if name.endswith(".jpg")]
        assert len(photos) == 2
        for name in photos:
            with Image.open(io.BytesIO(archive.read(name))) as image:
                assert image.size == (2400, 3000)
                assert image.info["dpi"] == (300, 300)
        manifest = archive.read("print-set-manifest.txt").decode()
        assert "8x10 in @ 300 dpi" in manifest

    # The set was recorded back in Immich.
    new_albums = [
        name for name in (a["albumName"] for a in immich.albums.values())
        if name.endswith("-print-set")
    ]
    assert len(new_albums) == 1
    assert len(immich.album_assets[[k for k, v in immich.albums.items() if v["albumName"] == new_albums[0]][0]]) == 2
    tagged = [t for t in immich.tags.values() if t["value"].startswith("printed-")]
    assert len(tagged) == 1
    assert len(immich.tag_assets[tagged[0]["id"]]) == 2      # the tag actually holds the photos
    assert job["detail"]["tag"]["name"] == tagged[0]["value"]


def test_prepare_reports_failures_without_losing_the_rest(signed_in, library, immich):
    signed_in.post("/api/selection/add", json={"assets": [{"id": library[0]}, {"id": "missing-asset"}]})
    job = signed_in.post("/api/prepare", json={}).json()
    job = wait_for_job(signed_in, job["id"])
    assert job["status"] == "done"
    assert len(job["detail"]["failures"]) == 1
    with zipfile.ZipFile(io.BytesIO(signed_in.get(job["download_url"]).content)) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".jpg")]) == 1


def test_prepare_needs_a_non_empty_set(signed_in):
    assert signed_in.post("/api/prepare", json={}).status_code == 400


def test_jobs_are_private_to_their_owner(signed_in, library, client, config):
    signed_in.post("/api/selection/add", json={"assets": [{"id": library[0]}]})
    job = signed_in.post("/api/prepare", json={}).json()
    wait_for_job(signed_in, job["id"])

    from fastapi.testclient import TestClient

    from immich_print_prep.app import create_app

    with TestClient(create_app(config)) as other:
        other.post(
            "/api/auth/set-password",
            json={"username": "alice", "current_password": "start-here-123", "new_password": "another-one-1"},
        )
        assert other.get("/api/jobs/%s" % job["id"]).status_code == 404
        assert other.get("/api/jobs/%s/download" % job["id"]).status_code == 404


def test_print_sets_are_per_user(signed_in, library, client, config):
    signed_in.post("/api/selection/add", json={"assets": [{"id": library[0]}]})

    from fastapi.testclient import TestClient

    from immich_print_prep.app import create_app

    with TestClient(create_app(config)) as other:
        other.post(
            "/api/auth/set-password",
            json={"username": "alice", "current_password": "start-here-123", "new_password": "another-one-1"},
        )
        assert other.get("/api/selection").json()["count"] == 0
    assert signed_in.get("/api/selection").json()["count"] == 1


def test_removing_the_api_key_takes_effect_immediately(signed_in, library):
    assert signed_in.get("/api/albums").status_code == 200
    assert signed_in.put("/api/settings", json={"clear_api_key": True}).json()["has_api_key"] is False
    assert signed_in.get("/api/albums").status_code == 409
    assert signed_in.get("/api/immich/status").json()["connected"] is False

    # Putting it back works without a restart.
    signed_in.put("/api/settings", json={"api_key": API_KEY})
    assert signed_in.get("/api/albums").status_code == 200


def test_a_key_without_user_read_is_still_accepted(signed_in, library, immich):
    """Immich keys carry granular permissions; `user.read` is not one we need."""
    saved = signed_in.put("/api/settings", json={"api_key": RESTRICTED_KEY})
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_api_key"] is True

    status = signed_in.get("/api/immich/status").json()
    assert status["connected"] is True
    assert status["user"]["email"] is None      # cannot name the account, and that is fine
    assert "album.read" in status["required_permissions"]

    assert signed_in.get("/api/albums").status_code == 200
    assert signed_in.get("/api/assets").status_code == 200

    # A permission it really is missing reports as 403, not as a missing key.
    denied = signed_in.get("/api/tags")
    assert denied.status_code == 403
    assert "permission" in denied.json()["detail"]


def test_a_key_immich_rejects_outright_is_not_saved(signed_in):
    refused = signed_in.put("/api/settings", json={"api_key": "not-a-real-key"})
    assert refused.status_code == 400
    assert signed_in.get("/api/me").json()["has_api_key"] is True   # the old key is untouched


def test_default_set_names_lead_with_the_date(signed_in):
    """`<datetime>-print-set`, so sets sort chronologically wherever they land."""
    prefs = signed_in.get("/api/me").json()["prefs"]
    assert prefs["album_name_template"] == "{datetime}-print-set"
    assert prefs["tag_name_template"] == "{datetime}-print-set"
    assert prefs["zip_name_template"] == "{datetime}-print-set"


def test_the_v0_1_0_default_is_replaced_but_a_chosen_name_is_kept(signed_in, ctx_prefs):
    stale, chosen = ctx_prefs
    assert stale.album_name_template == "{datetime}-print-set"
    assert chosen.album_name_template == "prints/{date}"


@pytest.fixture
def odd_formats(immich):
    """A HEIC photo, and a file this server cannot decode at all."""
    return {
        "heic": immich.add_asset("IMG_4021.HEIC", 4000, 3000, fmt="HEIF"),
        "raw": immich.add_asset(
            "DSC_0001.NEF", 3000, 4000, original=b"II*\x00 not really a raw file"
        ),
    }


def test_heic_photos_come_out_of_the_zip_as_prints(signed_in, odd_formats):
    signed_in.post("/api/selection/add", json={"assets": [{"id": odd_formats["heic"]}]})
    job = wait_for_job(signed_in, signed_in.post("/api/prepare", json={}).json()["id"])
    assert job["status"] == "done"
    assert job["detail"]["failures"] == []

    with zipfile.ZipFile(io.BytesIO(signed_in.get(job["download_url"]).content)) as archive:
        photos = [name for name in archive.namelist() if name.endswith(".jpg")]
        assert len(photos) == 1
        with Image.open(io.BytesIO(archive.read(photos[0]))) as image:
            assert image.size == (2400, 3000)
            assert image.info["dpi"] == (300, 300)


def test_an_undecodable_original_falls_back_to_the_immich_rendition(signed_in, odd_formats):
    signed_in.post("/api/selection/add", json={"assets": [{"id": odd_formats["raw"]}]})
    job = wait_for_job(signed_in, signed_in.post("/api/prepare", json={}).json()["id"])
    assert job["status"] == "done"
    assert job["detail"]["failures"] == []
    assert len(job["detail"]["from_rendition"]) == 1

    with zipfile.ZipFile(io.BytesIO(signed_in.get(job["download_url"]).content)) as archive:
        photos = [name for name in archive.namelist() if name.endswith(".jpg")]
        assert len(photos) == 1
        with Image.open(io.BytesIO(archive.read(photos[0]))) as image:
            assert image.size == (2400, 3000)
        manifest = archive.read("print-set-manifest.txt").decode()
        assert "from Immich JPEG rendition" in manifest


def test_the_fallback_works_on_an_immich_without_fullsize(signed_in, immich, odd_formats):
    """Older releases have no `fullsize` rendition; the preview one still works."""
    immich.supports_fullsize = False
    signed_in.post("/api/selection/add", json={"assets": [{"id": odd_formats["raw"]}]})
    job = wait_for_job(signed_in, signed_in.post("/api/prepare", json={}).json()["id"])
    assert job["status"] == "done", job
    assert job["detail"]["failures"] == []
    assert len(job["detail"]["from_rendition"]) == 1


def _prepare_with_tag(client, immich, album_id):
    client.post("/api/selection/add-source", json={"album_id": album_id})
    client.put("/api/settings", json={"create_tag": True, "tag_name_template": "printed-{date}"})
    return wait_for_job(client, client.post("/api/prepare", json={}).json()["id"])


def test_a_tag_immich_refuses_to_fill_is_reported(signed_in, library, immich):
    """A bulk add answers 200 even when it takes nothing; say so out loud."""
    immich.bulk_failure = "no_permission"
    job = _prepare_with_tag(signed_in, immich, next(iter(immich.albums)))

    assert job["status"] == "done"
    assert job["detail"]["failures"] == []          # the photos themselves were fine
    assert job["detail"]["tag"]["added"] == 0
    assert job["detail"]["tag"]["reasons"] == ["no_permission"]
    assert "added none of the 2 photos" in job["detail"]["record_error"]
    assert "no_permission" in job["detail"]["record_error"]
    # Immich tags are owner-only, so say that rather than leaving them guessing.
    assert "only lets you tag photos you own" in job["detail"]["record_error"]


def test_a_partly_filled_tag_is_reported(signed_in, library, immich):
    immich.bulk_failure = "not_found"
    immich.bulk_failure_after = 1
    job = _prepare_with_tag(signed_in, immich, next(iter(immich.albums)))

    assert job["detail"]["tag"]["added"] == 1
    assert "added 1 of 2 photos" in job["detail"]["record_error"]


def test_a_tag_that_immich_fills_reports_no_problem(signed_in, library, immich):
    job = _prepare_with_tag(signed_in, immich, next(iter(immich.albums)))
    assert job["detail"]["tag"]["added"] == 2
    assert job["detail"]["tag"]["failed"] == 0
    assert "record_error" not in job["detail"]
    tag_id = job["detail"]["tag"]["id"]
    assert len(immich.tag_assets[tag_id]) == 2


def test_a_key_without_tag_asset_permission_is_reported(signed_in, library, immich):
    """The other way tagging fails: Immich refuses the whole call, not each asset."""
    immich.restricted_denies.add("tag.asset")
    signed_in.put("/api/settings", json={"api_key": RESTRICTED_KEY})
    job = _prepare_with_tag(signed_in, immich, next(iter(immich.albums)))

    assert job["status"] == "done"
    assert job["detail"]["failures"] == []
    assert "permission" in job["detail"]["record_error"]
    assert "tag" not in job["detail"]
