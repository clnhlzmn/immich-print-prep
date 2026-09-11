"""Kicking off a download, watching its progress, and serving the zip."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..context import AppContext
from ..deps import current_user, get_ctx, require_json
from ..imaging import Adjustments, ImagingError
from ..jobs import run_prepare_job

router = APIRouter(prefix="/api", tags=["jobs"])


class PrepareBody(BaseModel):
    ids: Optional[List[str]] = Field(default=None, max_length=2000)
    # Per-set choices for recording the set in Immich. Anything left out (or a
    # blank name) falls back to the user's saved settings, which this never
    # changes. Names may use the same {datetime}/{date}/{time}/{count}
    # placeholders as the saved templates, or be plain text.
    create_album: Optional[bool] = None
    album_name: Optional[str] = Field(default=None, max_length=120)
    create_tag: Optional[bool] = None
    tag_name: Optional[str] = Field(default=None, max_length=120)


def _job_payload(row) -> Dict[str, Any]:
    try:
        detail = json.loads(row["detail"] or "{}")
    except (json.JSONDecodeError, TypeError):
        detail = {}
    return {
        "id": row["id"],
        "status": row["status"],
        "total": row["total"],
        "done": row["done"],
        "message": row["message"],
        "filename": row["filename"],
        "size": row["size"],
        "detail": detail,
        "created_at": row["created_at"],
        "download_url": "/api/jobs/%s/download" % row["id"] if row["status"] == "done" else None,
    }


@router.post("/prepare", dependencies=[Depends(require_json)])
async def prepare_download(
    body: PrepareBody,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    """Start preparing the print set; returns a job to poll."""
    # A fresh Prefs per request, so applying this set's overrides to it leaves
    # the saved settings alone.
    prefs = ctx.prefs(username)
    if body.create_album is not None:
        prefs.create_album = body.create_album
    if body.album_name and body.album_name.strip():
        prefs.album_name_template = body.album_name.strip()
    if body.create_tag is not None:
        prefs.create_tag = body.create_tag
    if body.tag_name and body.tag_name.strip():
        prefs.tag_name_template = body.tag_name.strip()
    wanted = set(body.ids) if body.ids else None
    items = []
    for item in ctx.db.selection_items(username):
        if wanted is not None and item["asset_id"] not in wanted:
            continue
        try:
            adj = Adjustments.from_dict(item["adjustments"], base=prefs.defaults)
        except ImagingError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "invalid adjustments for %s: %s" % (item["asset_id"], exc),
            ) from exc
        items.append({"asset_id": item["asset_id"], "info": item["info"], "adjustments": adj})

    if not items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the print set is empty")
    if ctx.api_key(username) is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no Immich API key configured")

    job_id = uuid.uuid4().hex
    ctx.db.create_job(job_id, username, total=len(items))
    task = asyncio.ensure_future(run_prepare_job(ctx, username, job_id, items, prefs))
    ctx.tasks[job_id] = task
    task.add_done_callback(lambda _: ctx.tasks.pop(job_id, None))
    return _job_payload(ctx.db.get_job(job_id))


def _owned_job(ctx: AppContext, username: str, job_id: str):
    row = ctx.db.get_job(job_id)
    if not row or row["username"] != username:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such job")
    return row


@router.get("/jobs")
def list_jobs(username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)):
    return {"jobs": [_job_payload(row) for row in ctx.db.recent_jobs(username)]}


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str, username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)
):
    return _job_payload(_owned_job(ctx, username, job_id))


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_json)])
def cancel_job(
    job_id: str, username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)
):
    row = _owned_job(ctx, username, job_id)
    task = ctx.tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    elif row["status"] in ("pending", "running"):
        ctx.db.update_job(job_id, status="cancelled", message="Cancelled")
    return {"status": "cancelling"}


@router.get("/jobs/{job_id}/download")
def download_job(
    job_id: str, username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)
):
    row = _owned_job(ctx, username, job_id)
    if row["status"] != "done" or not row["path"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "this download is not ready")
    path = Path(row["path"])
    if not path.exists():
        raise HTTPException(status.HTTP_410_GONE, "this download has expired")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=row["filename"] or "print-set.zip",
        headers={"Cache-Control": "no-store"},
    )
