"""SQLite persistence: accounts, sessions, per-user settings, print sets, jobs.

Everything the server remembers between requests lives here. Connections are
opened per operation (cheap, and it keeps threads from sharing handles) with WAL
enabled so the download workers can write progress while requests are served.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    username   TEXT PRIMARY KEY,
    api_key    TEXT,             -- encrypted with the server secret
    prefs      TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_username ON sessions(username);

CREATE TABLE IF NOT EXISTS selection (
    username    TEXT NOT NULL,
    asset_id    TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    position    INTEGER NOT NULL,
    info        TEXT NOT NULL DEFAULT '{}',
    adjustments TEXT,
    PRIMARY KEY (username, asset_id)
);
CREATE INDEX IF NOT EXISTS selection_order ON selection(username, position);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    username    TEXT NOT NULL,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    detail      TEXT NOT NULL DEFAULT '{}',
    filename    TEXT,
    path        TEXT,
    size        INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_username ON jobs(username, created_at);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- accounts ----------

    def ensure_users(self, usernames: Iterable[str]) -> None:
        """Create a row for every configured user; never delete de-configured
        ones (their print set survives a temporary edit to the config file)."""
        now = iso(utcnow())
        with self.connect() as conn:
            for username in usernames:
                conn.execute(
                    "INSERT INTO users (username, password_hash, created_at, updated_at)"
                    " VALUES (?, NULL, ?, ?) ON CONFLICT(username) DO NOTHING",
                    (username, now, now),
                )

    def get_user(self, username: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def set_password_hash(self, username: str, password_hash: str) -> None:
        now = iso(utcnow())
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ?",
                (password_hash, now, username),
            )

    # ---------- sessions ----------

    def create_session(self, token_hash: str, username: str, days: int) -> datetime:
        now = utcnow()
        expires = now + timedelta(days=days)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, username, created_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (token_hash, username, iso(now), iso(expires)),
            )
        return expires

    def get_session(self, token_hash: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if row and parse_iso(row["expires_at"]) <= utcnow():
            self.delete_session(token_hash)
            return None
        return row

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_user_sessions(self, username: str, keep: Optional[str] = None) -> None:
        with self.connect() as conn:
            if keep:
                conn.execute(
                    "DELETE FROM sessions WHERE username = ? AND token_hash != ?",
                    (username, keep),
                )
            else:
                conn.execute("DELETE FROM sessions WHERE username = ?", (username,))

    def purge_expired_sessions(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(utcnow()),))

    # ---------- settings ----------

    def get_settings(self, username: str) -> Dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT api_key, prefs FROM settings WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return {"api_key": None, "prefs": {}}
        try:
            prefs = json.loads(row["prefs"] or "{}")
        except json.JSONDecodeError:
            prefs = {}
        return {"api_key": row["api_key"], "prefs": prefs}

    def save_settings(
        self,
        username: str,
        api_key: Optional[str] = None,
        prefs: Optional[Dict[str, Any]] = None,
        clear_api_key: bool = False,
    ) -> None:
        now = iso(utcnow())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings (username, api_key, prefs, updated_at)"
                " VALUES (?, NULL, '{}', ?) ON CONFLICT(username) DO NOTHING",
                (username, now),
            )
            if clear_api_key:
                conn.execute(
                    "UPDATE settings SET api_key = NULL, updated_at = ? WHERE username = ?",
                    (now, username),
                )
            elif api_key is not None:
                conn.execute(
                    "UPDATE settings SET api_key = ?, updated_at = ? WHERE username = ?",
                    (api_key, now, username),
                )
            if prefs is not None:
                conn.execute(
                    "UPDATE settings SET prefs = ?, updated_at = ? WHERE username = ?",
                    (json.dumps(prefs), now, username),
                )

    # ---------- selection (the print set) ----------

    def selection_ids(self, username: str) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_id FROM selection WHERE username = ? ORDER BY position",
                (username,),
            ).fetchall()
        return [row["asset_id"] for row in rows]

    def selection_items(self, username: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT asset_id, info, adjustments, added_at FROM selection"
                " WHERE username = ? ORDER BY position",
                (username,),
            ).fetchall()
        items = []
        for row in rows:
            try:
                info = json.loads(row["info"] or "{}")
            except json.JSONDecodeError:
                info = {}
            try:
                adjustments = json.loads(row["adjustments"]) if row["adjustments"] else None
            except json.JSONDecodeError:
                adjustments = None
            items.append(
                {
                    "asset_id": row["asset_id"],
                    "info": info,
                    "adjustments": adjustments,
                    "added_at": row["added_at"],
                }
            )
        return items

    def selection_count(self, username: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM selection WHERE username = ?", (username,)
            ).fetchone()["n"]

    def add_to_selection(self, username: str, assets: List[Dict[str, Any]]) -> int:
        """Add assets, keeping insertion order and ignoring duplicates."""
        if not assets:
            return 0
        now = iso(utcnow())
        added = 0
        with self._write_lock, self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) AS p FROM selection WHERE username = ?",
                (username,),
            ).fetchone()
            position = row["p"] + 1
            for asset in assets:
                asset_id = asset.get("id")
                if not asset_id:
                    continue
                cursor = conn.execute(
                    "INSERT INTO selection (username, asset_id, added_at, position, info)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT(username, asset_id) DO NOTHING",
                    (username, asset_id, now, position, json.dumps(asset)),
                )
                if cursor.rowcount:
                    position += 1
                    added += 1
        return added

    def remove_from_selection(self, username: str, asset_ids: List[str]) -> int:
        if not asset_ids:
            return 0
        removed = 0
        with self.connect() as conn:
            for asset_id in asset_ids:
                removed += conn.execute(
                    "DELETE FROM selection WHERE username = ? AND asset_id = ?",
                    (username, asset_id),
                ).rowcount
        return removed

    def clear_selection(self, username: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM selection WHERE username = ?", (username,))

    def set_adjustments(
        self, username: str, asset_ids: List[str], adjustments: Optional[Dict[str, Any]]
    ) -> int:
        payload = json.dumps(adjustments) if adjustments is not None else None
        updated = 0
        with self.connect() as conn:
            for asset_id in asset_ids:
                updated += conn.execute(
                    "UPDATE selection SET adjustments = ? WHERE username = ? AND asset_id = ?",
                    (payload, username, asset_id),
                ).rowcount
        return updated

    def get_selection_item(self, username: str, asset_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT asset_id, info, adjustments FROM selection"
                " WHERE username = ? AND asset_id = ?",
                (username, asset_id),
            ).fetchone()
        if not row:
            return None
        try:
            info = json.loads(row["info"] or "{}")
        except json.JSONDecodeError:
            info = {}
        try:
            adjustments = json.loads(row["adjustments"]) if row["adjustments"] else None
        except json.JSONDecodeError:
            adjustments = None
        return {"asset_id": row["asset_id"], "info": info, "adjustments": adjustments}

    # ---------- jobs ----------

    def create_job(self, job_id: str, username: str, total: int, ttl_hours: int = 12) -> None:
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, username, status, total, done, message, detail,"
                " created_at, updated_at, expires_at)"
                " VALUES (?, ?, 'pending', ?, 0, ?, '{}', ?, ?, ?)",
                (
                    job_id, username, total, "Queued",
                    iso(now), iso(now), iso(now + timedelta(hours=ttl_hours)),
                ),
            )

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "detail" in fields and not isinstance(fields["detail"], str):
            fields["detail"] = json.dumps(fields["detail"])
        fields["updated_at"] = iso(utcnow())
        columns = ", ".join("%s = ?" % key for key in fields)
        with self.connect() as conn:
            conn.execute(
                "UPDATE jobs SET %s WHERE id = ?" % columns, (*fields.values(), job_id)
            )

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def recent_jobs(self, username: str, limit: int = 10) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM jobs WHERE username = ? ORDER BY created_at DESC LIMIT ?",
                (username, limit),
            ).fetchall()

    def expired_jobs(self) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM jobs WHERE expires_at <= ?", (iso(utcnow()),)
            ).fetchall()

    def delete_job(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
