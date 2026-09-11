"""The print set: which assets are in it and how each one should be prepared."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from ..captions import CaptionSource, caption_parts, compose, resolve_source
from ..context import AppContext
from ..deps import current_user, get_ctx, immich_client, require_json
from ..imaging import (
    Adjustments,
    ImagingError,
    oriented_source,
    source_geometry,
)
from ..imaging import (
    preview as render_preview,
)
from ..immich import ImmichClient, ImmichError

router = APIRouter(prefix="/api/selection", tags=["selection"])

# A ceiling on how many assets one bulk action may add, so a stray click on a
# huge library cannot fill the database (and later, the disk).
MAX_SELECTION = 2000


class AddBody(BaseModel):
    ids: List[str] = Field(default_factory=list, max_length=MAX_SELECTION)
    assets: List[Dict[str, Any]] = Field(default_factory=list, max_length=MAX_SELECTION)


class SourceBody(BaseModel):
    album_id: Optional[str] = None
    tag_id: Optional[str] = None
    q: Optional[str] = Field(default=None, max_length=500)
    smart: bool = False


class RemoveBody(BaseModel):
    ids: List[str] = Field(default_factory=list, max_length=MAX_SELECTION)


class AdjustmentsBody(BaseModel):
    ids: Optional[List[str]] = Field(default=None, max_length=MAX_SELECTION)
    adjustments: Optional[Dict[str, Any]] = None
    reset: bool = False


def _effective(item: Dict[str, Any], defaults: Adjustments) -> Adjustments:
    try:
        return Adjustments.from_dict(item.get("adjustments"), base=defaults)
    except ImagingError:
        return defaults


def _payload(ctx: AppContext, username: str) -> Dict[str, Any]:
    defaults = ctx.prefs(username).defaults
    items = ctx.db.selection_items(username)
    return {
        "count": len(items),
        "defaults": defaults.to_dict(),
        "items": [
            {
                "id": item["asset_id"],
                "info": item["info"],
                "customised": item["adjustments"] is not None,
                "adjustments": _effective(item, defaults).to_dict(),
            }
            for item in items
        ],
    }


def _guard_capacity(ctx: AppContext, username: str, incoming: int) -> None:
    current = ctx.db.selection_count(username)
    if current + incoming > MAX_SELECTION:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "a print set holds at most %d photos (this would make %d)"
            % (MAX_SELECTION, current + incoming),
        )


@router.get("")
def get_selection(username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)):
    return _payload(ctx, username)


@router.post("/add", dependencies=[Depends(require_json)])
def add_assets(
    body: AddBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    assets = [asset for asset in body.assets if asset.get("id")]
    known = {asset["id"] for asset in assets}
    assets.extend({"id": asset_id} for asset_id in body.ids if asset_id not in known)
    _guard_capacity(ctx, username, len(assets))
    added = ctx.db.add_to_selection(username, assets)
    return {"added": added, "count": ctx.db.selection_count(username)}


@router.post("/add-source", dependencies=[Depends(require_json)])
async def add_from_source(
    body: SourceBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
    client: ImmichClient = Depends(immich_client),
):
    """Add every asset in an album, under a tag, or matching a search."""
    try:
        if body.album_id:
            assets = await client.album_assets(body.album_id)
        elif body.tag_id:
            assets = await client.all_search_assets(tag_id=body.tag_id, limit=MAX_SELECTION)
        elif body.q is not None:
            assets = await client.all_search_assets(
                query=body.q, smart=body.smart, limit=MAX_SELECTION
            )
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing to add")
    except ImmichError as exc:
        code = status.HTTP_502_BAD_GATEWAY
        if exc.status == 401:
            code = status.HTTP_409_CONFLICT
        elif exc.status == 403:
            code = status.HTTP_403_FORBIDDEN
        raise HTTPException(code, exc.message) from exc

    assets = [asset for asset in assets if (asset.get("type") or "IMAGE") == "IMAGE"]
    existing = set(ctx.db.selection_ids(username))
    fresh = [asset for asset in assets if asset["id"] not in existing]
    _guard_capacity(ctx, username, len(fresh))
    added = ctx.db.add_to_selection(username, fresh)
    return {
        "added": added,
        "skipped": len(assets) - added,
        "count": ctx.db.selection_count(username),
    }


@router.post("/remove", dependencies=[Depends(require_json)])
def remove_assets(
    body: RemoveBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    removed = ctx.db.remove_from_selection(username, body.ids)
    return {"removed": removed, "count": ctx.db.selection_count(username)}


@router.post("/clear", dependencies=[Depends(require_json)])
def clear_selection(username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)):
    ctx.db.clear_selection(username)
    return {"count": 0}


@router.put("/adjustments", dependencies=[Depends(require_json)])
def set_adjustments(
    body: AdjustmentsBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """Apply adjustments to some (or, with no ids, all) of the print set."""
    targeted = body.ids is not None
    ids = body.ids if targeted else ctx.db.selection_ids(username)
    if body.reset:
        ctx.db.set_adjustments(username, ids, None)
        return _payload(ctx, username)

    defaults = ctx.prefs(username).defaults
    try:
        merged = Adjustments.from_dict(body.adjustments, base=defaults)
    except ImagingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # A crop rectangle and a hand-written caption only belong to the photo they
    # were made for. An edit aimed at one photo carries them; a sweep over the
    # whole set leaves each photo's own alone.
    payload = merged.to_dict()
    if targeted and len(ids) == 1:
        ctx.db.set_adjustments(username, ids, payload)
    else:
        for asset_id in ids:
            item = ctx.db.get_selection_item(username, asset_id)
            existing = (item or {}).get("adjustments") or {}
            ctx.db.set_adjustments(
                username,
                [asset_id],
                dict(payload, crop=existing.get("crop"), caption_text=existing.get("caption_text")),
            )
    return _payload(ctx, username)


async def _source_bytes(ctx: AppContext, username: str, asset_id: str) -> bytes:
    """Bytes to render a proof from: Immich's preview rendition, cached."""
    cached = ctx.cache_get(asset_id, "preview")
    if cached is not None:
        return cached
    client = ctx.client(username)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no Immich API key configured")
    try:
        data, _ = await client.thumbnail(asset_id, "preview")
    except ImmichError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.message) from exc
    ctx.cache_put(asset_id, "preview", data)
    return data


def _adjustments_from_query(
    ctx: AppContext, username: str, asset_id: str, overrides: Dict[str, Any]
) -> Adjustments:
    item = ctx.db.get_selection_item(username, asset_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not in the print set")
    base = _effective(item, ctx.prefs(username).defaults)
    try:
        return Adjustments.from_dict(overrides, base=base)
    except ImagingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _query_overrides(
    rotate: Optional[str], fit: Optional[str], width_in: Optional[float],
    height_in: Optional[float], background: Optional[str], crop: Optional[str],
    caption: Optional[bool] = None, caption_date: Optional[bool] = None,
    caption_description: Optional[bool] = None, caption_people: Optional[bool] = None,
    caption_text: Optional[str] = None, caption_auto: Optional[bool] = None,
) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {}
    for key, value in (
        ("rotate", rotate), ("fit", fit), ("width_in", width_in),
        ("height_in", height_in), ("background", background),
    ):
        if value is not None:
            overrides[key] = value
    for key, flag in (
        ("caption", caption), ("caption_date", caption_date),
        ("caption_description", caption_description), ("caption_people", caption_people),
    ):
        if flag is not None:
            overrides[key] = flag
    # caption_auto asks for Immich's text even if the photo has its own saved.
    if caption_auto:
        overrides["caption_text"] = None
    elif caption_text is not None:
        overrides["caption_text"] = caption_text
    if crop is not None:
        if crop in ("", "none"):
            overrides["crop"] = None
        else:
            parts = crop.split(",")
            if len(parts) != 4:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "crop must be 'x,y,w,h'")
            try:
                overrides["crop"] = dict(zip("xywh", [float(part) for part in parts]))
            except ValueError as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "crop must be numeric") from exc
    return overrides


async def _caption_source(
    ctx: AppContext, username: str, asset_id: str, fresh: bool = False
) -> CaptionSource:
    if not fresh:
        cached = ctx.caption_source_get(username, asset_id)
        if cached is not None:
            return cached
    client = ctx.client(username)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no Immich API key configured")
    source = await resolve_source(client, asset_id)
    ctx.caption_source_put(username, asset_id, source)
    return source


@router.get("/{asset_id}/preview")
async def preview_asset(
    asset_id: str,
    rotate: Optional[str] = Query(default=None),
    fit: Optional[str] = Query(default=None),
    width_in: Optional[float] = Query(default=None),
    height_in: Optional[float] = Query(default=None),
    background: Optional[str] = Query(default=None),
    crop: Optional[str] = Query(default=None),
    caption: Optional[bool] = Query(default=None),
    caption_date: Optional[bool] = Query(default=None),
    caption_description: Optional[bool] = Query(default=None),
    caption_people: Optional[bool] = Query(default=None),
    caption_text: Optional[str] = Query(default=None, max_length=1000),
    caption_auto: Optional[bool] = Query(default=None),
    max_edge: int = Query(default=700, ge=100, le=2000),
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """A proof of exactly what the prepared file will look like, caption included."""
    overrides = _query_overrides(
        rotate, fit, width_in, height_in, background, crop,
        caption, caption_date, caption_description, caption_people, caption_text, caption_auto,
    )
    adj = _adjustments_from_query(ctx, username, asset_id, overrides)
    data = await _source_bytes(ctx, username, asset_id)
    text = None
    if adj.caption:
        text = compose(await _caption_source(ctx, username, asset_id), adj) or None
    loop = asyncio.get_event_loop()
    try:
        rendered = await loop.run_in_executor(
            ctx.executor, render_preview, data, adj, max_edge, text
        )
    except (ImagingError, OSError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "could not render preview: %s" % exc
        ) from exc
    return Response(
        rendered, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"}
    )


@router.get("/{asset_id}/caption")
async def asset_caption(
    asset_id: str,
    rotate: Optional[str] = Query(default=None),
    crop: Optional[str] = Query(default=None),
    caption_date: Optional[bool] = Query(default=None),
    caption_description: Optional[bool] = Query(default=None),
    caption_people: Optional[bool] = Query(default=None),
    caption_text: Optional[str] = Query(default=None, max_length=1000),
    caption_auto: Optional[bool] = Query(default=None),
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """Immich's caption for this photo, for the editor to show and let the user edit.

    Rotation and crop matter: the people line is ordered as the photo is viewed
    upright and leaves out anyone the crop removes.
    """
    overrides = _query_overrides(
        rotate, None, None, None, None, crop,
        None, caption_date, caption_description, caption_people, caption_text, caption_auto,
    )
    adj = _adjustments_from_query(ctx, username, asset_id, overrides)
    source = await _caption_source(ctx, username, asset_id, fresh=True)
    from_immich = replace(adj, caption_text=None)
    return {
        "auto": compose(source, from_immich),
        "override": adj.caption_text,
        "caption": compose(source, adj),
        "parts": caption_parts(source, from_immich),
        "notes": source.notes,
    }


@router.get("/{asset_id}/geometry")
async def asset_geometry(
    asset_id: str,
    rotate: Optional[str] = Query(default=None),
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """Dimensions of the image as the crop editor sees it (after rotation)."""
    adj = _adjustments_from_query(ctx, username, asset_id, {"rotate": rotate} if rotate else {})
    data = await _source_bytes(ctx, username, asset_id)
    loop = asyncio.get_event_loop()
    geometry = await loop.run_in_executor(ctx.executor, source_geometry, data, adj)
    geometry["target_ratio"] = adj.target_ratio()
    geometry["crop"] = adj.crop.to_dict() if adj.crop else None
    return geometry


@router.get("/{asset_id}/source")
async def asset_source(
    asset_id: str,
    rotate: Optional[str] = Query(default=None),
    max_edge: int = Query(default=1400, ge=200, le=2400),
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """The rotated, un-cropped image the crop editor works on."""
    adj = _adjustments_from_query(ctx, username, asset_id, {"rotate": rotate} if rotate else {})
    data = await _source_bytes(ctx, username, asset_id)
    loop = asyncio.get_event_loop()
    try:
        rendered = await loop.run_in_executor(
            ctx.executor, oriented_source, data, adj.without_crop(), max_edge
        )
    except (ImagingError, OSError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "could not read that photo: %s" % exc
        ) from exc
    return Response(rendered, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
