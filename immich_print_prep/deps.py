"""Request dependencies: sessions, the signed-in user, and their Immich client."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Depends, HTTPException, Request, Response, status

from .context import AppContext
from .immich import ImmichClient
from .security import hash_session_token, new_session_token

SESSION_COOKIE = "ipp_session"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def current_user(request: Request, ctx: AppContext = Depends(get_ctx)) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = ctx.db.get_session(hash_session_token(token))
        if session and ctx.config.user(session["username"]):
            return session["username"]
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not signed in")


def require_json(request: Request) -> None:
    """Reject cross-site form posts.

    The session cookie is SameSite=Lax, so a cross-origin page cannot read the
    response; requiring a JSON content type also stops it from making a simple
    (preflight-free) request in the first place.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected a JSON request body"
        )


def immich_client(
    username: str = Depends(current_user), ctx: AppContext = Depends(get_ctx)
) -> ImmichClient:
    """An Immich client bound to the signed-in user's API key.

    The client is pooled in the app context and closed at shutdown, so handlers
    must not close it. 409 (rather than 401) tells the UI to open the settings
    panel instead of bouncing the user back to the login screen.
    """
    client = ctx.client(username)
    if client is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "no Immich API key configured")
    return client


def set_session_cookie(request: Request, response: Response, ctx: AppContext, username: str) -> str:
    token, token_hash = new_session_token()
    expires = ctx.db.create_session(token_hash, username, ctx.config.session_days)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ctx.config.session_days * 24 * 3600,
        expires=int(expires.timestamp()),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return token


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _is_https(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return (forwarded or request.url.scheme) == "https"


class RateLimiter:
    """Small in-process limiter for the credential endpoints."""

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, wait a minute"
            )
        hits.append(now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = RateLimiter(limit=10, window_seconds=60.0)


def client_key(request: Request, username: str = "") -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    host = forwarded or (request.client.host if request.client else "?")
    return "%s|%s" % (host, username)
