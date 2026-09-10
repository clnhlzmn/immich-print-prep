"""Server-wide configuration: the Immich instance and the list of users.

There is deliberately no sign-up flow. Accounts exist because an administrator
put a username in this file; the account gets its password the first time that
person logs in.
"""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .imaging import Adjustments, ImagingError

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")

DEFAULT_CONFIG_PATH = "/config/config.yaml"
DEFAULT_DATA_DIR = "/data"


class ConfigError(Exception):
    """Raised for a malformed or unusable configuration file."""


@dataclass(frozen=True)
class UserConfig:
    username: str
    initial_password: Optional[str] = None


@dataclass(frozen=True)
class Config:
    immich_url: str
    users: List[UserConfig]
    data_dir: Path
    secret_key: str
    session_days: int = 30
    verify_tls: bool = True
    # With no initial_password set for an account, the first login claims it
    # with any password. Turn this off to require an initial_password instead.
    allow_blank_first_login: bool = True
    site_title: str = "Immich Print Prep"
    defaults: Adjustments = field(default_factory=Adjustments)
    source_path: Optional[Path] = None

    def user(self, username: str) -> Optional[UserConfig]:
        username = (username or "").strip().lower()
        for entry in self.users:
            if entry.username == username:
                return entry
        return None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "print-prep.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


def _normalise_url(raw: Any) -> str:
    url = str(raw or "").strip().rstrip("/")
    if not url:
        raise ConfigError("immich_url is required")
    if not url.startswith(("http://", "https://")):
        raise ConfigError("immich_url must start with http:// or https://")
    # Tolerate someone pasting the API base rather than the site root.
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url


def _parse_users(raw: Any) -> List[UserConfig]:
    if not raw:
        raise ConfigError("at least one user must be configured under 'users'")
    if not isinstance(raw, list):
        raise ConfigError("'users' must be a list")

    users: List[UserConfig] = []
    seen = set()
    for entry in raw:
        if isinstance(entry, str):
            username, initial = entry, None
        elif isinstance(entry, dict):
            username = entry.get("username") or entry.get("name") or ""
            initial = entry.get("initial_password") or entry.get("password")
            if initial is not None:
                initial = str(initial)
        else:
            raise ConfigError("each user must be a name or a mapping with 'username'")

        username = str(username).strip().lower()
        if not USERNAME_RE.match(username):
            raise ConfigError(
                "invalid username %r (letters, digits and . _ @ + - only)" % username
            )
        if username in seen:
            raise ConfigError("duplicate username %r" % username)
        seen.add(username)
        users.append(UserConfig(username=username, initial_password=initial))
    return users


def _resolve_secret_key(raw: Optional[str], data_dir: Path) -> str:
    """Use the configured key, else keep a generated one beside the database.

    Sessions and the stored Immich API keys are tied to this value, so it has to
    survive a container restart.
    """
    key = (raw or "").strip()
    if key:
        return key
    key_file = data_dir / "secret.key"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    key = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key + "\n", encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def _parse_defaults(raw: Any) -> Adjustments:
    if raw in (None, {}):
        return Adjustments()
    if not isinstance(raw, dict):
        raise ConfigError("'defaults' must be a mapping")
    try:
        return Adjustments.from_dict(raw)
    except ImagingError as exc:
        raise ConfigError("invalid 'defaults': %s" % exc) from exc


def load_config(path: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Config:
    env = dict(os.environ if env is None else env)
    config_path = Path(path or env.get("IPP_CONFIG") or DEFAULT_CONFIG_PATH)

    raw: Dict[str, Any] = {}
    if config_path.exists():
        try:
            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError("could not parse %s: %s" % (config_path, exc)) from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigError("%s must contain a mapping at the top level" % config_path)
        raw = loaded
    elif path or env.get("IPP_CONFIG"):
        raise ConfigError(
            "config file not found: %s - mount one (see config.example.yaml)" % config_path
        )
    else:
        raise ConfigError(
            "no config file at %s - mount one (see config.example.yaml) or set IPP_CONFIG"
            % config_path
        )

    data_dir = Path(env.get("IPP_DATA_DIR") or raw.get("data_dir") or DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)

    session_days = raw.get("session_days", 30)
    try:
        session_days = int(session_days)
    except (TypeError, ValueError) as exc:
        raise ConfigError("'session_days' must be a number") from exc
    if not 1 <= session_days <= 365:
        raise ConfigError("'session_days' must be between 1 and 365")

    return Config(
        immich_url=_normalise_url(env.get("IPP_IMMICH_URL") or raw.get("immich_url")),
        users=_parse_users(raw.get("users")),
        data_dir=data_dir,
        secret_key=_resolve_secret_key(
            env.get("IPP_SECRET_KEY") or raw.get("secret_key"), data_dir
        ),
        session_days=session_days,
        verify_tls=bool(raw.get("verify_tls", True)),
        allow_blank_first_login=bool(raw.get("allow_blank_first_login", True)),
        site_title=str(raw.get("site_title") or "Immich Print Prep"),
        defaults=_parse_defaults(raw.get("defaults")),
        source_path=config_path,
    )
