"""Shared application state and the per-user settings model."""

from __future__ import annotations

import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .imaging import Adjustments, ImagingError
from .immich import ImmichClient
from .security import SecretBox

# Names that Immich (and file systems) will accept without complaint.
_UNSAFE_NAME_RE = re.compile(r"[^\w \-.()\[\]{}#@+,']", re.UNICODE)

DEFAULT_ALBUM_TEMPLATE = "{datetime}-print-set"
DEFAULT_ZIP_TEMPLATE = "{datetime}-print-set"

# v0.1.0 shipped the date last. Anyone still carrying that stored default gets
# the new one; a template they actually chose is left alone. Delete this once
# no v0.1.0 database is in use.
LEGACY_TEMPLATES = frozenset({"print-set-{datetime}"})

# How much of the thumbnail cache to keep on disk.
CACHE_LIMIT_BYTES = 512 * 1024 * 1024


def _template(stored: Any, default: str) -> str:
    """The stored template, unless it is unset or a superseded default."""
    value = str(stored or "").strip()
    return default if not value or value in LEGACY_TEMPLATES else value


class Prefs:
    """Per-user preferences, layered over the server-wide defaults."""

    def __init__(self, raw: Optional[Dict[str, Any]], server_defaults: Adjustments):
        raw = raw or {}
        self.create_album = bool(raw.get("create_album", False))
        self.album_name_template = _template(raw.get("album_name_template"), DEFAULT_ALBUM_TEMPLATE)
        self.create_tag = bool(raw.get("create_tag", False))
        self.tag_name_template = _template(raw.get("tag_name_template"), DEFAULT_ALBUM_TEMPLATE)
        self.zip_name_template = _template(raw.get("zip_name_template"), DEFAULT_ZIP_TEMPLATE)
        try:
            self.defaults = Adjustments.from_dict(raw.get("defaults"), base=server_defaults)
        except ImagingError:
            self.defaults = server_defaults

    def to_dict(self) -> Dict[str, Any]:
        return {
            "create_album": self.create_album,
            "album_name_template": self.album_name_template,
            "create_tag": self.create_tag,
            "tag_name_template": self.tag_name_template,
            "zip_name_template": self.zip_name_template,
            "defaults": self.defaults.to_dict(),
        }


def render_name_template(template: str, moment: Optional[datetime] = None, count: int = 0) -> str:
    """Expand `{datetime}`/`{date}`/`{time}`/`{count}` in a set name."""
    moment = moment or datetime.now()
    values = {
        "datetime": moment.strftime("%Y-%m-%d_%H%M%S"),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H%M%S"),
        "count": str(count),
    }
    out = template
    for key, value in values.items():
        out = out.replace("{%s}" % key, value)
    out = _UNSAFE_NAME_RE.sub("_", out).strip()
    return out[:120] or render_name_template(DEFAULT_ALBUM_TEMPLATE, moment, count)


class AppContext:
    """Everything the request handlers need, assembled once at startup."""

    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.secrets = SecretBox(config.secret_key)
        self.executor = ThreadPoolExecutor(
            max_workers=max(2, min(8, (os.cpu_count() or 2))), thread_name_prefix="prep"
        )
        # Running prepare jobs, so they can be cancelled from a later request.
        self.tasks: Dict[str, Any] = {}
        # One Immich client per (user, API key), so a grid of thumbnails reuses
        # connections instead of opening one per request. Replaced clients are
        # closed by the housekeeping pass, which always has a running loop.
        self._clients: Dict[Tuple[str, str], ImmichClient] = {}
        self._retired: List[ImmichClient] = []
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.db.ensure_users(user.username for user in config.users)

    def close(self) -> None:
        self.executor.shutdown(wait=False)

    async def aclose(self) -> None:
        """Shut down the executor and close every pooled Immich client."""
        self.close()
        clients = list(self._clients.values()) + self._retired
        self._clients.clear()
        self._retired.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - nothing useful to do at shutdown
                pass

    async def close_retired_clients(self) -> None:
        while self._retired:
            client = self._retired.pop()
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    # ---------- per-user helpers ----------

    def prefs(self, username: str) -> Prefs:
        return Prefs(self.db.get_settings(username).get("prefs"), self.config.defaults)

    def api_key(self, username: str) -> Optional[str]:
        return self.secrets.decrypt(self.db.get_settings(username).get("api_key"))

    def client(self, username: str) -> Optional[ImmichClient]:
        """A pooled client for this user, or None if they have no API key."""
        key = self.api_key(username)
        if not key:
            for stale in [entry for entry in self._clients if entry[0] == username]:
                self._retired.append(self._clients.pop(stale))
            return None

        fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        cached = self._clients.get((username, fingerprint))
        if cached is not None:
            return cached
        # A different key for the same user means the old client is done with.
        for stale in [entry for entry in self._clients if entry[0] == username]:
            self._retired.append(self._clients.pop(stale))
        client = ImmichClient(self.config.immich_url, key, verify_tls=self.config.verify_tls)
        self._clients[(username, fingerprint)] = client
        return client

    # ---------- thumbnail cache ----------

    def cache_path(self, asset_id: str, size: str) -> Path:
        digest = hashlib.sha256(("%s/%s" % (asset_id, size)).encode("utf-8")).hexdigest()
        return self.cache_dir / digest[:2] / ("%s.bin" % digest)

    def cache_get(self, asset_id: str, size: str) -> Optional[bytes]:
        path = self.cache_path(asset_id, size)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:  # keep the sweeper honest about what is being used
            os.utime(path, None)
        except OSError:
            pass
        return data

    def cache_put(self, asset_id: str, size: str, data: bytes) -> None:
        path = self.cache_path(asset_id, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp%d" % os.getpid())
        try:
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError:
            tmp.unlink(missing_ok=True)

    def sweep_cache(self, limit: int = CACHE_LIMIT_BYTES) -> int:
        """Drop least-recently-used cache entries above the size limit."""
        entries: list[Tuple[float, int, Path]] = []
        total = 0
        for path in self.cache_dir.rglob("*.bin"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        if total <= limit:
            return 0
        freed = 0
        for _, size, path in sorted(entries):
            try:
                path.unlink()
            except OSError:
                continue
            freed += size
            total -= size
            if total <= limit:
                break
        return freed

    def sweep_jobs(self) -> None:
        """Delete expired job records and the zip files they point at."""
        for row in self.db.expired_jobs():
            path = row["path"]
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
            self.db.delete_job(row["id"])
        # Orphaned files (crash between writing a zip and recording it).
        cutoff = time.time() - 24 * 3600
        for path in self.config.jobs_dir.glob("*.zip"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
