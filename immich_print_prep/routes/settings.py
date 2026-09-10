"""Account info, the user's Immich API key, and their preferences."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..context import AppContext, Prefs
from ..deps import current_user, get_ctx, require_json
from ..imaging import Adjustments, ImagingError
from ..immich import REQUIRED_PERMISSIONS, ImmichClient, ImmichError

router = APIRouter(prefix="/api", tags=["settings"])


class SettingsBody(BaseModel):
    api_key: Optional[str] = Field(default=None, max_length=512)
    clear_api_key: bool = False
    create_album: Optional[bool] = None
    album_name_template: Optional[str] = Field(default=None, max_length=120)
    create_tag: Optional[bool] = None
    tag_name_template: Optional[str] = Field(default=None, max_length=120)
    zip_name_template: Optional[str] = Field(default=None, max_length=120)
    defaults: Optional[Dict[str, Any]] = None


def _me_payload(ctx: AppContext, username: str) -> Dict[str, Any]:
    settings = ctx.db.get_settings(username)
    prefs = Prefs(settings.get("prefs"), ctx.config.defaults)
    return {
        "username": username,
        "site_title": ctx.config.site_title,
        "immich_url": ctx.config.immich_url,
        "has_api_key": bool(ctx.secrets.decrypt(settings.get("api_key"))),
        "prefs": prefs.to_dict(),
        "server_defaults": ctx.config.defaults.to_dict(),
        "selection_count": ctx.db.selection_count(username),
    }


@router.get("/me")
def me(username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)):
    return _me_payload(ctx, username)


@router.put("/settings", dependencies=[Depends(require_json)])
async def update_settings(
    body: SettingsBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    stored = ctx.db.get_settings(username)
    prefs = Prefs(stored.get("prefs"), ctx.config.defaults)

    if body.defaults is not None:
        try:
            prefs.defaults = Adjustments.from_dict(body.defaults, base=prefs.defaults)
        except ImagingError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    for field in ("create_album", "create_tag"):
        value = getattr(body, field)
        if value is not None:
            setattr(prefs, field, bool(value))
    for field in ("album_name_template", "tag_name_template", "zip_name_template"):
        value = getattr(body, field)
        if value is not None and value.strip():
            setattr(prefs, field, value.strip())

    api_key = (body.api_key or "").strip()
    encrypted = None
    if api_key and not body.clear_api_key:
        # Refuse a key Immich will not accept rather than storing a dud.
        client = ImmichClient(ctx.config.immich_url, api_key, verify_tls=ctx.config.verify_tls)
        try:
            await client.verify_access()
        except ImmichError as exc:
            detail = exc.message
            if exc.status == 403:
                detail = (
                    "That key cannot list albums. Give it at least %s in Immich."
                    % ", ".join(REQUIRED_PERMISSIONS)
                )
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST if exc.is_auth_error else status.HTTP_502_BAD_GATEWAY,
                detail,
            ) from exc
        finally:
            await client.aclose()
        encrypted = ctx.secrets.encrypt(api_key)

    ctx.db.save_settings(
        username,
        api_key=encrypted,
        prefs=prefs.to_dict(),
        clear_api_key=bool(body.clear_api_key),
    )
    return _me_payload(ctx, username)


@router.get("/immich/status")
async def immich_status(
    username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)
):
    """Whether the stored API key currently works, for the settings panel."""
    client = ctx.client(username)
    if client is None:
        return {"connected": False, "reason": "no API key configured"}
    try:
        await client.verify_access()
    except ImmichError as exc:
        return {"connected": False, "reason": exc.message}

    # Naming the account is a nicety; a key without `user.read` is still fine.
    user = {}
    try:
        user = await client.me()
    except ImmichError:
        user = {}
    return {
        "connected": True,
        "user": {"email": user.get("email"), "name": user.get("name"), "id": user.get("id")},
        "version": await client.server_version(),
        "immich_url": ctx.config.immich_url,
        "required_permissions": list(REQUIRED_PERMISSIONS),
    }
