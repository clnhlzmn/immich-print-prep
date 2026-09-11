"""A tiny stand-in for an Immich server, enough to exercise the whole flow."""

from __future__ import annotations

import io
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from PIL import Image

API_KEY = "test-api-key"

# A key issued without `user.read` or `tag.read`, the way Immich lets you do.
RESTRICTED_KEY = "restricted-api-key"
RESTRICTED_DENIES = {"user.read", "tag.read"}


def make_image(width: int, height: int, colour=(200, 120, 60), fmt: str = "JPEG") -> bytes:
    image = Image.new("RGB", (width, height), colour)
    # A couple of blocks so orientation changes are visible in assertions.
    image.paste((20, 20, 20), (0, 0, max(1, width // 4), max(1, height // 4)))
    buf = io.BytesIO()
    if fmt.upper() in ("HEIF", "HEIC"):
        import pillow_heif

        pillow_heif.register_heif_opener()
        image.save(buf, format="HEIF", quality=80)
    else:
        image.save(buf, format=fmt, quality=92)
    return buf.getvalue()


class FakeImmich:
    def __init__(self, supports_fullsize: bool = True):
        # Older Immich releases reject size=fullsize; flip this to exercise the
        # fall back to the preview rendition.
        self.supports_fullsize = supports_fullsize
        # Set to a BulkIdErrorReason to make bulk adds reject assets with 200,
        # the way Immich does for photos the key may see but not modify.
        self.bulk_failure: Optional[str] = None
        self.bulk_failure_after = 0
        # Permissions RESTRICTED_KEY does not carry; tests can add to this.
        self.restricted_denies = set(RESTRICTED_DENIES)
        self.assets: Dict[str, Dict[str, Any]] = {}
        self.albums: Dict[str, Dict[str, Any]] = {}
        self.tags: Dict[str, Dict[str, Any]] = {}
        self.album_assets: Dict[str, List[str]] = {}
        self.tag_assets: Dict[str, List[str]] = {}
        self.app = self._build()

    # ---------- fixtures ----------

    def add_asset(
        self,
        filename: str,
        width: int,
        height: int,
        fmt: str = "JPEG",
        original: Optional[bytes] = None,
    ) -> str:
        """Add an asset. `original` overrides the stored file (e.g. camera raw
        this server cannot decode) while Immich still renders JPEGs for it."""
        asset_id = str(uuid.uuid4())
        rendered = make_image(width, height, fmt=fmt)
        self.assets[asset_id] = {
            "id": asset_id,
            "originalFileName": filename,
            "type": "IMAGE",
            "width": width,
            "height": height,
            "fileCreatedAt": "2026-01-01T00:00:00.000Z",
            "localDateTime": "2026-01-01T00:00:00.000Z",
            "isFavorite": False,
            "thumbhash": None,
            "data": original if original is not None else rendered,
            "rendition": rendered if original is not None else None,
        }
        return asset_id

    def add_album(self, name: str, asset_ids: List[str]) -> str:
        album_id = str(uuid.uuid4())
        self.albums[album_id] = {"id": album_id, "albumName": name}
        self.album_assets[album_id] = list(asset_ids)
        return album_id

    def add_tag(self, value: str, asset_ids: List[str]) -> str:
        tag_id = str(uuid.uuid4())
        self.tags[tag_id] = {"id": tag_id, "name": value, "value": value}
        self.tag_assets[tag_id] = list(asset_ids)
        return tag_id

    def _public(self, asset_id: str) -> Dict[str, Any]:
        asset = dict(self.assets[asset_id])
        asset.pop("data", None)
        return asset

    # ---------- the API ----------

    def _build(self) -> FastAPI:
        app = FastAPI()
        fake = self

        def auth(x_api_key: Optional[str], permission: Optional[str] = None) -> None:
            if x_api_key not in (API_KEY, RESTRICTED_KEY):
                raise HTTPException(401, "invalid api key")
            if x_api_key == RESTRICTED_KEY and permission in fake.restricted_denies:
                raise HTTPException(403, "Missing required permission: %s" % permission)

        @app.get("/api/users/me")
        def me(x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key, "user.read")
            return {"id": "user-1", "email": "test@example.com", "name": "Test User"}

        @app.get("/api/server/version")
        def version(x_api_key: Optional[str] = Header(default=None)):
            return {"major": 1, "minor": 200, "patch": 0}

        @app.get("/api/albums")
        def albums(x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            return [
                {
                    **album,
                    "assetCount": len(fake.album_assets[album["id"]]),
                    "albumThumbnailAssetId": (fake.album_assets[album["id"]] or [None])[0],
                    "albumUsers": [],
                    "shared": False,
                    "createdAt": "2026-01-01T00:00:00.000Z",
                    "updatedAt": "2026-01-01T00:00:00.000Z",
                    "description": "",
                    "hasSharedLink": False,
                    "isActivityEnabled": True,
                }
                for album in fake.albums.values()
            ]

        @app.get("/api/albums/{album_id}")
        def album_info(album_id: str, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            if album_id not in fake.albums:
                raise HTTPException(404, "no such album")
            # Modern Immich no longer inlines assets; force the search fallback.
            return {**fake.albums[album_id], "assetCount": len(fake.album_assets[album_id])}

        @app.post("/api/albums", status_code=201)
        async def create_album(request: Request, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            body = await request.json()
            album_id = fake.add_album(body["albumName"], body.get("assetIds") or [])
            return {**fake.albums[album_id], "assetCount": len(fake.album_assets[album_id])}

        @app.put("/api/albums/{album_id}/assets")
        async def add_assets(
            album_id: str, request: Request, x_api_key: Optional[str] = Header(default=None)
        ):
            auth(x_api_key)
            body = await request.json()
            return fake._bulk_add(fake.album_assets.setdefault(album_id, []), body["ids"])

        @app.get("/api/tags")
        def tags(x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key, "tag.read")
            return [
                {**tag, "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z"}
                for tag in fake.tags.values()
            ]

        @app.post("/api/tags", status_code=201)
        async def create_tag(request: Request, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            body = await request.json()
            tag_id = fake.add_tag(body["name"], [])
            return fake.tags[tag_id]

        @app.put("/api/tags/{tag_id}/assets")
        async def tag_assets(
            tag_id: str, request: Request, x_api_key: Optional[str] = Header(default=None)
        ):
            auth(x_api_key, "tag.asset")
            body = await request.json()
            return fake._bulk_add(fake.tag_assets.setdefault(tag_id, []), body["ids"])

        @app.post("/api/search/metadata")
        async def search_metadata(request: Request, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            body = await request.json()
            ids = list(fake.assets)
            if body.get("albumIds"):
                ids = [i for i in ids if i in fake.album_assets.get(body["albumIds"][0], [])]
            if body.get("tagIds"):
                ids = [i for i in ids if i in fake.tag_assets.get(body["tagIds"][0], [])]
            if body.get("originalFileName"):
                needle = body["originalFileName"].lower()
                ids = [i for i in ids if needle in fake.assets[i]["originalFileName"].lower()]
            return fake._page(ids, body)

        @app.post("/api/search/smart")
        async def search_smart(request: Request, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            body = await request.json()
            return fake._page(list(fake.assets), body)

        @app.get("/api/assets/{asset_id}/thumbnail")
        def thumbnail(asset_id: str, size: str = Query(default="thumbnail"),
                      x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key, "asset.view")
            if size not in ("thumbnail", "preview", "fullsize"):
                raise HTTPException(400, "unknown size")
            if size == "fullsize" and not fake.supports_fullsize:
                raise HTTPException(400, "size fullsize is not supported")
            if asset_id not in fake.assets:
                raise HTTPException(404, "no such asset")
            asset = fake.assets[asset_id]
            # Immich serves JPEG renditions even for files it stores as HEIC or
            # raw, which is exactly what the fallback path relies on.
            source = asset["rendition"] or asset["data"]
            with Image.open(io.BytesIO(source)) as image:
                if size != "fullsize":
                    image.thumbnail((250 if size == "thumbnail" else 1440,) * 2)
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="JPEG")
            return Response(buf.getvalue(), media_type="image/jpeg")

        @app.get("/api/assets/{asset_id}/original")
        def original(asset_id: str, x_api_key: Optional[str] = Header(default=None)):
            auth(x_api_key)
            if asset_id not in fake.assets:
                raise HTTPException(404, "no such asset")
            return Response(fake.assets[asset_id]["data"], media_type="image/jpeg")

        return app

    def _bulk_add(self, target: List[str], asset_ids: List[str]) -> List[Dict[str, Any]]:
        """Immich answers 200 with a per-asset verdict, not an all-or-nothing error."""
        out = []
        for index, asset_id in enumerate(asset_ids):
            rejected = self.bulk_failure and index >= self.bulk_failure_after
            if rejected:
                out.append({"id": asset_id, "success": False, "error": self.bulk_failure})
            else:
                target.append(asset_id)
                out.append({"id": asset_id, "success": True})
        return out

    def _page(self, ids: List[str], body: Dict[str, Any]) -> Dict[str, Any]:
        size = int(body.get("size") or 100)
        page = int(body.get("page") or 1)
        start = (page - 1) * size
        chunk = ids[start:start + size]
        has_more = start + size < len(ids)
        return {
            "albums": {"total": 0, "count": 0, "items": [], "facets": []},
            "assets": {
                "total": len(ids),
                "count": len(chunk),
                "items": [self._public(i) for i in chunk],
                "facets": [],
                "nextPage": str(page + 1) if has_more else None,
                "nextCursor": None,
            },
        }


class FakeImmichServer:
    """Runs a FakeImmich on a real port, so the client is exercised for real."""

    def __init__(self, fake: FakeImmich, port: int = 0):
        self.fake = fake
        config = uvicorn.Config(fake.app, host="127.0.0.1", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> FakeImmichServer:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            raise RuntimeError("fake Immich did not start")
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        socket = self.server.servers[0].sockets[0]
        return "http://127.0.0.1:%d" % socket.getsockname()[1]
