"""A real browser walk through the whole UI, from first sign-in to download.

Skipped unless Playwright and a Chromium build are installed:

    pip install playwright && playwright install chromium
"""

from __future__ import annotations

import io
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn

from immich_print_prep.app import create_app
from immich_print_prep.config import load_config

from fake_immich import API_KEY, FakeImmich, FakeImmichServer

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
).sync_playwright


class AppServer:
    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("app server did not start")
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.servers[0].sockets[0].getsockname()[1]


@pytest.fixture
def site(tmp_path):
    fake = FakeImmich()
    ids = [
        fake.add_asset("beach.jpg", 4000, 3000),
        fake.add_asset("portrait.jpg", 3000, 4000),
    ]
    fake.add_album("Holiday", ids)

    with FakeImmichServer(fake) as immich:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "immich_url: %s\ndata_dir: %s\nusers:\n  - colin\n" % (immich.url, tmp_path / "data"),
            encoding="utf-8",
        )
        with AppServer(create_app(load_config(str(config_path)))) as app:
            yield app, fake


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # no browser binary installed
            pytest.skip("chromium is not installed: %s" % exc)
        # A zone 14 hours from UTC, so a name stamped with the server's clock
        # instead of the browser's cannot pass by coincidence.
        context = browser.new_context(
            viewport={"width": 1400, "height": 1000}, timezone_id="Pacific/Kiritimati"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert not errors, "javascript errors: %s" % errors


def test_full_walkthrough(site, page):
    app, fake = site

    # --- first sign-in claims the account ---
    page.goto(app.url)
    page.wait_for_url("**/login")
    page.fill("#username", "colin")
    page.click("#submit")
    page.wait_for_selector("text=Choose a password")
    page.fill("#new-password", "print-me-please")
    page.fill("#confirm-password", "print-me-please")
    page.click("#submit")
    page.wait_for_url(app.url + "/")

    # --- the settings panel opens by itself, asking for an API key ---
    dialog = page.locator("#settings-dialog")
    dialog.wait_for(state="visible")
    dialog.locator("input[type=password]").first.fill(API_KEY)
    dialog.get_by_role("button", name="Save key").click()
    page.wait_for_selector("text=Connected to")
    dialog.get_by_role("button", name="Close").click()

    # --- things that are meant to be hidden stay hidden ---
    page.wait_for_selector(".album-card")
    assert not page.locator("#search-form").is_visible()
    assert not page.locator("#btn-back").is_visible()

    # --- browse an album and add a photo to the print set ---
    assert page.locator(".album-card").count() == 1
    page.click(".album-card")
    page.wait_for_selector(".asset")
    assert page.locator(".asset").count() == 2

    page.locator(".asset").first.click()
    page.click("#btn-add-selected")
    page.wait_for_selector("#set-count:text('1')")
    page.wait_for_selector(".asset.in-set")

    # --- adding the whole album skips what is already there ---
    page.click("#btn-add-everything")
    page.wait_for_selector("#set-count:text('2')")

    # --- proofs render in the print set ---
    page.click("#tab-set")
    page.wait_for_selector(".set-card img")
    # The width/height boxes only belong to a custom size.
    assert not page.locator("#set-toolbar .grid2").is_visible()
    assert page.locator(".set-card").count() == 2
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('.set-card img'))"
        ".every((img) => img.complete && img.naturalWidth > 0)"
    )
    ratios = page.evaluate(
        "() => Array.from(document.querySelectorAll('.set-card img'))"
        ".map((img) => img.naturalWidth / img.naturalHeight)"
    )
    assert all(abs(ratio - 0.8) < 0.01 for ratio in ratios), ratios  # every proof is 8x10

    # --- bulk change: 5x7, cropped to fill ---
    page.select_option("#set-toolbar select >> nth=0", "5x7")
    page.select_option("#set-toolbar select >> nth=2", "crop")
    page.get_by_role("button", name="Apply to all").click()
    page.wait_for_selector("text=5 × 7 in · 300 dpi · cropped to fill")

    # --- per-photo editor: crop, then save ---
    page.locator(".set-card").first.get_by_role("button", name="Adjust").click()
    editor = page.locator("#editor-dialog")
    editor.wait_for(state="visible")
    page.wait_for_function(
        "() => { const img = document.querySelector('#editor-dialog .stage img');"
        " return img && img.complete && img.naturalWidth > 0; }"
    )
    editor.get_by_role("button", name="Crop to frame").click()
    box = editor.locator(".croprect")
    box.wait_for(state="visible")
    before = box.bounding_box()
    page.mouse.move(before["x"] + before["width"] / 2, before["y"] + before["height"] / 2)
    page.mouse.down()
    page.mouse.move(before["x"] + before["width"] / 2 + 40, before["y"] + before["height"] / 2, steps=8)
    page.mouse.up()
    after = box.bounding_box()
    assert after["x"] > before["x"]
    editor.get_by_role("button", name="Save").click()
    editor.wait_for(state="hidden")
    page.wait_for_selector(".set-card .pill:text('custom')")

    # --- per-set album choice, pre-filled from the saved default ---
    record = page.locator("#set-record")
    album_row = record.locator(".record-row").nth(0)
    tag_row = record.locator(".record-row").nth(1)
    assert album_row.locator("input[type=text]").input_value() == "{datetime}-print-set"
    assert album_row.locator("input[type=text]").is_disabled()      # album is off by default
    assert not record.get_by_role("button", name="Use my defaults").is_visible()

    album_row.locator("input[type=checkbox]").check()
    album_row.locator("input[type=text]").fill("Walkthrough {date}")
    page.wait_for_selector("#set-record .name-preview:text('Walkthrough 20')")
    assert record.get_by_role("button", name="Use my defaults").is_visible()
    assert tag_row.locator(".name-preview").inner_text() == ""        # tag stays off

    # --- prepare and download ---
    with page.expect_download(timeout=60000) as download_info:
        page.click("#btn-prepare")
        page.wait_for_selector("text=Download zip", timeout=60000)
    download = download_info.value
    assert download.suggested_filename.endswith("-print-set.zip")
    page.wait_for_selector("text=Added to a new Immich album: Walkthrough 20")
    names = [a["albumName"] for a in fake.albums.values() if a["albumName"].startswith("Walkthrough ")]
    kiritimati_dates = {
        (datetime.now(timezone.utc) + timedelta(hours=14, minutes=back)).strftime("%Y-%m-%d")
        for back in (-5, 0)
    }
    assert len(names) == 1 and names[0].split(" ", 1)[1] in kiritimati_dates, names

    with open(download.path(), "rb") as handle:
        with zipfile.ZipFile(io.BytesIO(handle.read())) as archive:
            photos = [name for name in archive.namelist() if name.endswith(".jpg")]
            assert len(photos) == 2
            from PIL import Image

            with Image.open(io.BytesIO(archive.read(photos[0]))) as image:
                assert image.size == (1500, 2100)   # 5x7 at 300 dpi


def _claim_and_connect(page, app):
    page.goto(app.url)
    page.wait_for_url("**/login")
    page.fill("#username", "colin")
    page.click("#submit")
    page.wait_for_selector("text=Choose a password")
    page.fill("#new-password", "print-me-please")
    page.fill("#confirm-password", "print-me-please")
    page.click("#submit")
    page.wait_for_url(app.url + "/")
    dialog = page.locator("#settings-dialog")
    dialog.wait_for(state="visible")
    dialog.locator("input[type=password]").first.fill(API_KEY)
    dialog.get_by_role("button", name="Save key").click()
    page.wait_for_selector("text=Connected to")
    dialog.get_by_role("button", name="Close").click()


def test_a_caption_can_be_rewritten_for_one_print(tmp_path, page):
    fake = FakeImmich()
    photo = fake.add_asset(
        "lake.jpg", 3000, 2000,
        description="Fourth of July at the lake",
        taken_at="2026-07-04T19:30:05.000Z",
        time_zone="America/Chicago",
        place=("Austin", "Texas", "United States"),
        faces=[{"name": "Alice", "cx": 0.25}, {"name": "Bob", "cx": 0.75}],
    )
    fake.add_album("Lake", [photo])

    with FakeImmichServer(fake) as immich:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "immich_url: %s\ndata_dir: %s\nusers:\n  - colin\n" % (immich.url, tmp_path / "data"),
            encoding="utf-8",
        )
        with AppServer(create_app(load_config(str(config_path)))) as app:
            _claim_and_connect(page, app)
            page.wait_for_selector(".album-card")
            page.click(".album-card")
            page.wait_for_selector(".asset")
            page.click("#btn-add-everything")
            page.wait_for_selector("#set-count:text('1')")
            page.click("#tab-set")
            page.wait_for_selector(".set-card img")

            # Captions on for the whole set.
            page.locator("#set-toolbar").get_by_label("Caption in the border").check()
            page.get_by_role("button", name="Apply to all").click()
            page.wait_for_selector(".set-card :text('padded · caption')")

            # The editor shows Immich's caption, in the photo's own time zone.
            page.locator(".set-card").first.get_by_role("button", name="Adjust").click()
            editor = page.locator("#editor-dialog")
            editor.wait_for(state="visible")
            page.wait_for_function(
                "() => (document.querySelector('#editor-dialog textarea') || {}).value"
                "?.includes('2026-07-04 14:30:05 CDT')"
            )
            box = editor.locator("textarea")
            assert box.input_value() == (
                "2026-07-04 14:30:05 CDT\nAustin, Texas, United States"
                "\nFourth of July at the lake\nIn this photo: Alice, Bob"
            )

            # Unticking a part drops its line, leaving the rest in order.
            editor.get_by_label("Location").uncheck()
            page.wait_for_function(
                "() => (document.querySelector('#editor-dialog textarea') || {}).value"
                "?.startsWith('2026-07-04 14:30:05 CDT\\nFourth')"
            )
            assert not editor.get_by_role("button", name="Use Immich's text").is_visible()

            box.fill("Lake day with Alice and Bob")
            assert editor.get_by_role("button", name="Use Immich's text").is_visible()
            editor.get_by_role("button", name="Save").click()
            editor.wait_for(state="hidden")

            with page.expect_download(timeout=60000) as download_info:
                page.click("#btn-prepare")
                page.wait_for_selector("text=Download zip", timeout=60000)
            with zipfile.ZipFile(download_info.value.path()) as archive:
                manifest = archive.read("print-set-manifest.txt").decode()
            assert "caption: Lake day with Alice and Bob" in manifest


def test_a_print_can_be_opened_larger_from_the_print_set(tmp_path, page):
    fake = FakeImmich()
    first = fake.add_asset("first.jpg", 3000, 2000)
    second = fake.add_asset("second.jpg", 2000, 3000)
    fake.add_album("Both", [first, second])

    with FakeImmichServer(fake) as immich:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "immich_url: %s\ndata_dir: %s\nusers:\n  - colin\n" % (immich.url, tmp_path / "data"),
            encoding="utf-8",
        )
        with AppServer(create_app(load_config(str(config_path)))) as app:
            _claim_and_connect(page, app)
            page.wait_for_selector(".album-card")
            page.click(".album-card")
            page.wait_for_selector(".asset")
            page.click("#btn-add-everything")
            page.wait_for_selector("#set-count:text('2')")
            page.click("#tab-set")
            page.wait_for_selector(".set-card img")
            page.wait_for_function(
                "() => [...document.querySelectorAll('.set-card img')].every(i => i.complete && i.naturalWidth)"
            )
            card_width = page.evaluate("document.querySelector('.set-card img').naturalWidth")

            page.locator(".set-card .proof").first.click()
            viewer = page.locator("#viewer-dialog")
            viewer.wait_for(state="visible")
            page.wait_for_function(
                "() => { const i = document.querySelector('#viewer-dialog img');"
                " return i && i.complete && i.naturalWidth > 0; }"
            )
            assert page.evaluate("document.querySelector('#viewer-dialog img').naturalWidth") > card_width
            assert viewer.locator("h2").inner_text() == "first.jpg"
            assert "1 of 2" in viewer.inner_text()

            page.keyboard.press("ArrowRight")
            page.wait_for_selector("#viewer-dialog h2:text('second.jpg')")
            assert "2 of 2" in viewer.inner_text()

            # Adjust takes over from the viewer.
            viewer.get_by_role("button", name="Adjust").click()
            viewer.wait_for(state="hidden")
            editor = page.locator("#editor-dialog")
            editor.wait_for(state="visible")
            assert editor.locator("h2").inner_text() == "second.jpg"
            editor.get_by_role("button", name="Cancel").click()

            # And it closes with Escape.
            page.locator(".set-card .proof").first.click()
            viewer.wait_for(state="visible")
            page.keyboard.press("Escape")
            viewer.wait_for(state="hidden")


@pytest.fixture
def phone():
    """A page the size of a phone screen."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip("chromium is not installed: %s" % exc)
        context = browser.new_context(
            viewport={"width": 390, "height": 844}, device_scale_factor=3,
            is_mobile=True, has_touch=True,
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert not errors, "javascript errors: %s" % errors


def test_the_app_fits_a_phone_screen(tmp_path, phone):
    fake = FakeImmich()
    fake.add_album("Lake", [fake.add_asset("lake.jpg", 3000, 2000), fake.add_asset("dock.jpg", 2000, 3000)])

    with FakeImmichServer(fake) as immich:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "immich_url: %s\ndata_dir: %s\nusers:\n  - colin\n" % (immich.url, tmp_path / "data"),
            encoding="utf-8",
        )
        with AppServer(create_app(load_config(str(config_path)))) as app:
            phone.goto(app.url)
            phone.wait_for_url("**/login")
            phone.fill("#username", "colin")
            phone.click("#submit")
            phone.wait_for_selector("text=Choose a password")
            phone.fill("#new-password", "print-me-please")
            phone.fill("#confirm-password", "print-me-please")
            phone.click("#submit")
            phone.wait_for_url(app.url + "/")
            # The settings dialog is hard to tap on a small screen; store the key directly.
            phone.request.put(app.url + "/api/settings", data={"api_key": API_KEY})
            phone.reload()

            sideways = "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            phone.wait_for_selector(".album-card")
            assert phone.evaluate(sideways) == 0, "the library scrolls sideways"

            phone.click(".album-card")
            phone.wait_for_selector(".asset")
            phone.click("#btn-add-everything")
            phone.wait_for_selector("#set-count:text('2')")
            phone.click("#tab-set")
            phone.wait_for_selector(".set-card img")
            assert phone.evaluate(sideways) == 0, "the print set scrolls sideways"

            # The bar above Prepare stays out of the way until it is opened.
            assert phone.evaluate("() => !document.querySelector('#set-record details').open")
            assert "no album" in phone.locator("#set-record summary").inner_text()
            share = phone.evaluate(
                "() => document.querySelector('#set-actions').getBoundingClientRect().height"
                " / window.innerHeight"
            )
            assert share < 0.3, "the action bar takes %d%% of the screen" % round(share * 100)

            phone.locator("#set-record summary").click()
            assert phone.evaluate("() => document.querySelector('#set-record details').open")
            assert phone.evaluate(sideways) == 0, "the opened options scroll sideways"
