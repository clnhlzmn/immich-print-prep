"""Async client for the Immich REST API.

Only a small slice of the API is used: listing albums and tags, searching, and
fetching/creating assets. The calls stick to the long-standing flat request
fields (rather than the newer `filter`/`cursor` shapes) because those work on
every Immich release this is likely to meet, old or new.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import httpx

USER_AGENT = "immich-print-prep"

# Immich API keys carry granular permissions. These are the ones this app needs;
# the last four are only used by the optional "record the set in Immich" step.
REQUIRED_PERMISSIONS = ("album.read", "asset.read", "asset.view", "asset.download")
OPTIONAL_PERMISSIONS = (
    "tag.read", "album.create", "albumAsset.create", "tag.create", "tag.asset", "face.read",
)


class ImmichError(Exception):
    """An API call failed."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


def normalise_asset(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce an Immich asset to the handful of fields the UI needs."""
    exif = raw.get("exifInfo") or {}
    width = raw.get("width") or exif.get("exifImageWidth")
    height = raw.get("height") or exif.get("exifImageHeight")
    ratio = None
    if width and height:
        try:
            ratio = float(width) / float(height)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = None
    return {
        "id": raw.get("id"),
        "filename": raw.get("originalFileName") or raw.get("id"),
        "type": raw.get("type") or "IMAGE",
        "width": width,
        "height": height,
        "ratio": ratio,
        "createdAt": raw.get("localDateTime") or raw.get("fileCreatedAt"),
        "isFavorite": bool(raw.get("isFavorite")),
        "thumbhash": raw.get("thumbhash"),
    }


class ImmichClient:
    """One client per request/job; wraps an httpx.AsyncClient."""

    def __init__(self, base_url: str, api_key: str, verify_tls: bool = True, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url + "/api",
            headers={
                "x-api-key": api_key,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            verify=verify_tls,
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> ImmichClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- plumbing ----------

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError("could not reach Immich at %s (%s)" % (self.base_url, exc)) from exc
        if response.status_code >= 400:
            raise ImmichError(_error_message(response), response.status_code)
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._request(method, path, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ImmichError("Immich returned a non-JSON response for %s" % path) from exc

    # ---------- identity ----------

    async def me(self) -> Dict[str, Any]:
        """The signed-in Immich account. Needs the `user.read` permission."""
        return await self._json("GET", "/users/me")

    async def verify_access(self) -> None:
        """Check a key by doing something the app actually does.

        Listing albums needs `album.read`, which any usable key must grant.
        Deliberately not `/users/me`: that needs `user.read`, which this app has
        no other reason to ask for, and keys are often issued without it.
        """
        await self._json("GET", "/albums")

    async def server_version(self) -> str:
        try:
            data = await self._json("GET", "/server/version")
        except ImmichError:
            return "unknown"
        if isinstance(data, dict):
            parts = [data.get("major"), data.get("minor"), data.get("patch")]
            if all(p is not None for p in parts):
                return ".".join(str(p) for p in parts)
        return "unknown"

    # ---------- browsing ----------

    async def albums(self) -> List[Dict[str, Any]]:
        data = await self._json("GET", "/albums") or []
        albums = []
        for album in data:
            albums.append(
                {
                    "id": album.get("id"),
                    "name": album.get("albumName"),
                    "assetCount": album.get("assetCount", 0),
                    "thumbnailAssetId": album.get("albumThumbnailAssetId"),
                    "shared": bool(album.get("shared")),
                    "updatedAt": album.get("updatedAt"),
                    "startDate": album.get("startDate"),
                    "endDate": album.get("endDate"),
                }
            )
        albums.sort(key=lambda a: (a["name"] or "").lower())
        return albums

    async def tags(self) -> List[Dict[str, Any]]:
        data = await self._json("GET", "/tags") or []
        tags = [
            {
                "id": tag.get("id"),
                "name": tag.get("name"),
                "value": tag.get("value") or tag.get("name"),
                "color": tag.get("color"),
            }
            for tag in data
        ]
        tags.sort(key=lambda t: (t["value"] or "").lower())
        return tags

    async def search(
        self,
        *,
        album_id: Optional[str] = None,
        tag_id: Optional[str] = None,
        query: Optional[str] = None,
        smart: bool = False,
        page: int = 1,
        cursor: Optional[str] = None,
        size: int = 100,
    ) -> Dict[str, Any]:
        """One page of assets. `smart=True` runs Immich's contextual search."""
        body: Dict[str, Any] = {"size": max(1, min(size, 1000)), "withExif": True, "type": "IMAGE"}
        if cursor:
            body["cursor"] = cursor
        else:
            body["page"] = max(1, page)
        if album_id:
            body["albumIds"] = [album_id]
        if tag_id:
            body["tagIds"] = [tag_id]

        if smart:
            body["query"] = query or ""
            path = "/search/smart"
        else:
            path = "/search/metadata"
            if query:
                body["originalFileName"] = query
            body["order"] = "desc"

        data = await self._json("POST", path, json=body) or {}
        assets = (data.get("assets") or {}) if isinstance(data, dict) else {}
        items = [normalise_asset(item) for item in (assets.get("items") or [])]
        next_page = assets.get("nextPage")
        return {
            "items": items,
            "total": assets.get("total"),
            "count": assets.get("count", len(items)),
            "nextPage": int(next_page) if next_page not in (None, "") else None,
            "nextCursor": assets.get("nextCursor"),
        }

    async def album_assets(self, album_id: str) -> List[Dict[str, Any]]:
        """Every asset in an album.

        Older Immich returns the assets inline with the album; newer releases
        dropped that field, so fall back to a paged search.
        """
        album = await self._json("GET", "/albums/%s" % album_id) or {}
        inline = album.get("assets")
        if isinstance(inline, list) and inline:
            return [normalise_asset(item) for item in inline]
        return await self.all_search_assets(album_id=album_id)

    async def all_search_assets(
        self,
        *,
        album_id: Optional[str] = None,
        tag_id: Optional[str] = None,
        query: Optional[str] = None,
        smart: bool = False,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        """Page through a search until it runs out (or `limit` is reached)."""
        out: List[Dict[str, Any]] = []
        page: Optional[int] = 1
        cursor: Optional[str] = None
        while len(out) < limit:
            result = await self.search(
                album_id=album_id, tag_id=tag_id, query=query, smart=smart,
                page=page or 1, cursor=cursor, size=1000,
            )
            out.extend(result["items"])
            cursor = result.get("nextCursor")
            page = result.get("nextPage")
            if not result["items"] or (not cursor and not page):
                break
        return out[:limit]

    async def asset_details(self, asset_id: str) -> Dict[str, Any]:
        """The facts a caption is made from. Needs `asset.read`."""
        raw = await self._json("GET", "/assets/%s" % asset_id) or {}
        exif = raw.get("exifInfo") or {}
        return {
            "description": exif.get("description"),
            # dateTimeOriginal is the true instant; timeZone says where it was taken.
            "dateTimeOriginal": exif.get("dateTimeOriginal") or raw.get("fileCreatedAt"),
            "timeZone": exif.get("timeZone"),
            "localDateTime": raw.get("localDateTime"),
            # Immich reverse-geocodes the GPS fix into these; any may be missing.
            "city": exif.get("city"),
            "state": exif.get("state"),
            "country": exif.get("country"),
            "make": exif.get("make"),
            "model": exif.get("model"),
            "lensModel": exif.get("lensModel"),
            "focalLength": exif.get("focalLength"),
            "fNumber": exif.get("fNumber"),
            "exposureTime": exif.get("exposureTime"),
            "people": [
                {"name": person.get("name"), "isHidden": bool(person.get("isHidden"))}
                for person in raw.get("people") or []
                if isinstance(person, dict)
            ],
        }

    async def faces(self, asset_id: str) -> List[Dict[str, Any]]:
        """Detected faces with their boxes and people. Needs `face.read`."""
        data = await self._json("GET", "/faces", params={"id": asset_id})
        return data if isinstance(data, list) else []

    async def asset(self, asset_id: str) -> Dict[str, Any]:
        return normalise_asset(await self._json("GET", "/assets/%s" % asset_id) or {})

    # ---------- media ----------

    async def thumbnail(self, asset_id: str, size: str = "thumbnail") -> Tuple[bytes, str]:
        response = await self._request(
            "GET", "/assets/%s/thumbnail" % asset_id, params={"size": size}
        )
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def original(self, asset_id: str) -> bytes:
        response = await self._request("GET", "/assets/%s/original" % asset_id)
        return response.content

    async def rendition(self, asset_id: str) -> bytes:
        """The largest JPEG Immich can produce for an asset.

        Immich renders every asset to JPEG for the web, including formats this
        server cannot decode itself (camera raw, and HEIC on a build without
        HEIF support). `fullsize` is full resolution; older releases do not
        offer it, so fall back to the preview rendition.
        """
        last: Optional[ImmichError] = None
        for size in ("fullsize", "preview"):
            try:
                data, _ = await self.thumbnail(asset_id, size)
                return data
            except ImmichError as exc:
                if exc.status not in (400, 404):
                    raise
                last = exc
        raise last or ImmichError("no rendition available for %s" % asset_id)

    # ---------- writing back ----------

    async def create_album(
        self, name: str, asset_ids: Optional[List[str]] = None, description: str = ""
    ) -> Tuple[Dict[str, Any], BulkResult]:
        """Create an album and fill it, reporting what Immich actually took."""
        body: Dict[str, Any] = {"albumName": name}
        if description:
            body["description"] = description
        album = await self._json("POST", "/albums", json=body) or {}
        album_id = album.get("id")
        if not album_id:
            raise ImmichError("Immich created no album for %r" % name)
        return album, await self.add_to_album(album_id, asset_ids or [])

    async def add_to_album(self, album_id: str, asset_ids: List[str]) -> BulkResult:
        result = BulkResult()
        for batch in _batched(asset_ids, 500):
            result.absorb(
                await self._json("PUT", "/albums/%s/assets" % album_id, json={"ids": batch})
            )
        return result

    async def create_tag(self, name: str) -> Dict[str, Any]:
        return await self._json("POST", "/tags", json={"name": name}) or {}

    async def tag_assets(self, tag_id: str, asset_ids: List[str]) -> BulkResult:
        result = BulkResult()
        for batch in _batched(asset_ids, 500):
            result.absorb(
                await self._json("PUT", "/tags/%s/assets" % tag_id, json={"ids": batch})
            )
        return result


class BulkResult:
    """What Immich did with a bulk add.

    These endpoints answer 200 with one entry per asset, each carrying its own
    `success` flag and reason - so "the tag was created but holds nothing" is a
    successful HTTP call, and the only way to notice is to read the body.
    """

    def __init__(self) -> None:
        self.added = 0
        self.failed: List[Dict[str, str]] = []

    def absorb(self, payload: Any) -> None:
        if not isinstance(payload, list):
            # Some endpoints answer {"count": n} instead of a per-id list.
            if isinstance(payload, dict) and "count" in payload:
                self.added += int(payload.get("count") or 0)
            return
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if entry.get("success"):
                self.added += 1
            else:
                self.failed.append(
                    {
                        "id": str(entry.get("id") or ""),
                        "error": str(entry.get("error") or "unknown"),
                    }
                )

    @property
    def reasons(self) -> List[str]:
        return sorted({entry["error"] for entry in self.failed})

    def summary(self) -> Dict[str, Any]:
        return {"added": self.added, "failed": len(self.failed), "reasons": self.reasons}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "BulkResult(added=%d, failed=%d)" % (self.added, len(self.failed))


def _batched(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _error_message(response: httpx.Response) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("message") or payload.get("error") or "")
    except ValueError:
        detail = (response.text or "")[:200]
    if response.status_code == 401:
        return "Immich rejected the API key (401)"
    if response.status_code == 403:
        return (
            "Immich denied access (403) - the API key is missing a permission"
            " for this action%s" % (": " + detail if detail else "")
        )
    return "Immich request failed (%d)%s" % (
        response.status_code, ": " + detail if detail else ""
    )
