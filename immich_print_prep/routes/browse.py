"""Browsing the Immich library: albums, tags, timeline and search."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..context import AppContext
from ..deps import current_user, get_ctx, immich_client
from ..immich import ImmichClient, ImmichError

router = APIRouter(prefix="/api", tags=["browse"])

THUMB_SIZES = ("thumbnail", "preview")


def _fail(exc: ImmichError) -> HTTPException:
    code = status.HTTP_502_BAD_GATEWAY
    if exc.is_auth_error:
        code = status.HTTP_409_CONFLICT  # the UI reopens the API key panel
    return HTTPException(code, exc.message)


@router.get("/albums")
async def list_albums(client: ImmichClient = Depends(immich_client)):
    try:
        return {"albums": await client.albums()}
    except ImmichError as exc:
        raise _fail(exc) from exc


@router.get("/tags")
async def list_tags(client: ImmichClient = Depends(immich_client)):
    try:
        return {"tags": await client.tags()}
    except ImmichError as exc:
        raise _fail(exc) from exc


@router.get("/assets")
async def list_assets(
    album_id: Optional[str] = Query(default=None),
    tag_id: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, max_length=500),
    smart: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=10000),
    cursor: Optional[str] = Query(default=None, max_length=2048),
    size: int = Query(default=120, ge=1, le=1000),
    client: ImmichClient = Depends(immich_client),
):
    """One page of assets from an album, a tag, the timeline, or a search."""
    try:
        return await client.search(
            album_id=album_id, tag_id=tag_id, query=q, smart=smart,
            page=page, cursor=cursor, size=size,
        )
    except ImmichError as exc:
        raise _fail(exc) from exc


@router.get("/assets/{asset_id}/thumb")
async def asset_thumbnail(
    asset_id: str,
    size: str = Query(default="thumbnail"),
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """Proxy Immich thumbnails so the API key never reaches the browser."""
    if size not in THUMB_SIZES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown thumbnail size")

    cached = ctx.cache_get(asset_id, size)
    if cached is not None:
        return Response(cached, media_type="image/jpeg", headers=_cache_headers())

    client = ctx.client(username)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no Immich API key configured")
    try:
        data, content_type = await client.thumbnail(asset_id, size)
    except ImmichError as exc:
        raise _fail(exc) from exc

    ctx.cache_put(asset_id, size, data)
    return Response(data, media_type=content_type, headers=_cache_headers())


def _cache_headers() -> dict:
    # Immich asset ids are stable, so the browser may hold on to these.
    return {"Cache-Control": "private, max-age=86400"}
