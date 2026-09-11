"""The "prepare for printing" worker: fetch, convert, zip, and record the set."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .context import AppContext, Prefs, render_name_template
from .imaging import Adjustments, ImagingError, UnsupportedImageError, prepare
from .immich import ImmichClient, ImmichError

log = logging.getLogger(__name__)

# How many originals to pull from Immich at once. Immich is usually on the same
# LAN, but originals are large; four keeps the pipe busy without hammering it.
FETCH_CONCURRENCY = 4

_UNSAFE_FILE_RE = re.compile(r"[^\w\-.() ]", re.UNICODE)


def safe_stem(name: str) -> str:
    stem = Path(name or "photo").stem
    stem = _UNSAFE_FILE_RE.sub("_", stem).strip(". ")
    return stem[:80] or "photo"


class JobCancelled(Exception):
    pass


async def run_prepare_job(
    ctx: AppContext,
    username: str,
    job_id: str,
    items: List[Dict[str, Any]],
    prefs: Prefs,
) -> None:
    """Prepare every selected asset and leave a zip on disk for download."""
    moment = datetime.now()
    zip_path = ctx.config.jobs_dir / ("%s.zip" % job_id)
    zip_name = render_name_template(prefs.zip_name_template, moment, len(items)) + ".zip"
    failures: List[Tuple[str, str]] = []
    manifest: List[str] = []
    succeeded_ids: List[str] = []
    renditions: List[str] = []
    done = 0

    client = ctx.client(username)
    if client is None:
        ctx.db.update_job(job_id, status="error", message="no Immich API key configured")
        return

    ctx.db.update_job(job_id, status="running", message="Fetching photos from Immich")
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(
        index: int, item: Dict[str, Any]
    ) -> Optional[Tuple[int, str, bytes, Adjustments, bool]]:
        asset_id = item["asset_id"]
        info = item.get("info") or {}
        adj = item["adjustments"]
        from_rendition = False
        try:
            async with semaphore:
                data = await client.original(asset_id)
            try:
                rendered = await loop.run_in_executor(ctx.executor, prepare, data, adj)
            except UnsupportedImageError:
                # Camera raw, or HEIC on a build without HEIF support: Immich
                # already keeps a JPEG of every asset, so print that instead of
                # dropping the photo from the set.
                async with semaphore:
                    data = await client.rendition(asset_id)
                rendered = await loop.run_in_executor(ctx.executor, prepare, data, adj)
                from_rendition = True
        except ImmichError as exc:
            failures.append((info.get("filename") or asset_id, exc.message))
            return None
        except (ImagingError, OSError, ValueError) as exc:
            failures.append((info.get("filename") or asset_id, str(exc)))
            return None
        stem = safe_stem(info.get("filename") or asset_id)
        name = "%03d-%s%s" % (index + 1, stem, adj.extension())
        return index, name, rendered, adj, from_rendition

    used_names = set()
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            tasks = [asyncio.ensure_future(one(index, item)) for index, item in enumerate(items)]
            try:
                for future in asyncio.as_completed(tasks):
                    result = await future
                    done += 1
                    if result is not None:
                        index, name, data, adj, from_rendition = result
                        while name in used_names:  # two sources with one name
                            stem, _, ext = name.rpartition(".")
                            name = "%s_%d.%s" % (stem, index, ext)
                        used_names.add(name)
                        succeeded_ids.append(items[index]["asset_id"])
                        # ZIP_STORED: JPEGs do not compress, and skipping deflate
                        # keeps a large set from pinning a CPU for no gain.
                        archive.writestr(name, data)
                        if from_rendition:
                            renditions.append(name)
                        manifest.append(
                            "%s\t%s\t%.10gx%.10g in @ %d dpi\t%s\t%s%s"
                            % (
                                name,
                                items[index]["info"].get("filename") or items[index]["asset_id"],
                                adj.width_in, adj.height_in, adj.dpi,
                                "crop" if adj.fit == "crop" else "pad %s" % adj.background,
                                items[index]["asset_id"],
                                "\tfrom Immich JPEG rendition" if from_rendition else "",
                            )
                        )
                    ctx.db.update_job(
                        job_id, done=done,
                        message="Prepared %d of %d" % (done, len(items)),
                    )
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                raise

            archive.writestr(
                "print-set-manifest.txt",
                _manifest_text(zip_name, moment, manifest, failures, renditions),
            )
    except asyncio.CancelledError:
        zip_path.unlink(missing_ok=True)
        ctx.db.update_job(job_id, status="cancelled", message="Cancelled")
        return
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as job state
        log.exception("prepare job %s failed", job_id)
        zip_path.unlink(missing_ok=True)
        ctx.db.update_job(job_id, status="error", message=str(exc))
        return

    detail: Dict[str, Any] = {
        "failures": [{"file": name, "error": error} for name, error in failures],
        "prepared": len(items) - len(failures),
        "from_rendition": renditions,
    }

    # Record the set back in Immich, if the user asked for it.
    try:
        if prefs.create_album and succeeded_ids:
            name = render_name_template(prefs.album_name_template, moment, len(succeeded_ids))
            album = await client.create_album(
                name, succeeded_ids, description="Prepared for printing by immich-print-prep"
            )
            detail["album"] = {"id": album.get("id"), "name": name}
        if prefs.create_tag and succeeded_ids:
            name = render_name_template(prefs.tag_name_template, moment, len(succeeded_ids))
            tag = await _ensure_tag(client, name)
            if tag.get("id"):
                await client.tag_assets(tag["id"], succeeded_ids)
                detail["tag"] = {"id": tag.get("id"), "name": name}
    except ImmichError as exc:
        detail["record_error"] = exc.message

    ctx.db.update_job(
        job_id,
        status="done",
        done=done,
        message="Ready to download",
        filename=zip_name,
        path=str(zip_path),
        size=zip_path.stat().st_size if zip_path.exists() else 0,
        detail=json.dumps(detail),
    )


async def _ensure_tag(client: ImmichClient, name: str) -> Dict[str, Any]:
    """Create the tag, or reuse it if Immich already has one by that name."""
    try:
        return await client.create_tag(name)
    except ImmichError:
        for tag in await client.tags():
            if (tag.get("value") or tag.get("name")) == name:
                return tag
        raise


def _manifest_text(
    zip_name: str,
    moment: datetime,
    rows: List[str],
    failures: List[Tuple[str, str]],
    renditions: Optional[List[str]] = None,
) -> str:
    lines = [
        "%s" % zip_name,
        "Prepared %s by immich-print-prep" % moment.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "file\tsource\tprint size\tfit\tasset id",
    ]
    lines.extend(sorted(rows))
    if renditions:
        lines.extend([
            "",
            "Printed from Immich's JPEG rendition rather than the original file"
            " (%d): %s" % (len(renditions), ", ".join(sorted(renditions))),
        ])
    if failures:
        lines.extend(["", "Failed (%d):" % len(failures)])
        lines.extend("%s\t%s" % (name, error) for name, error in failures)
    return "\n".join(lines) + "\n"
