"""Sign-in, first-login password creation, and password changes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..context import AppContext
from ..deps import (
    SESSION_COOKIE,
    clear_session_cookie,
    client_key,
    current_user,
    get_ctx,
    login_limiter,
    require_json,
    set_session_cookie,
)
from ..security import (
    MIN_PASSWORD_LENGTH,
    PasswordError,
    hash_password,
    hash_session_token,
    validate_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"], dependencies=[Depends(require_json)])


class LoginBody(BaseModel):
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=1024)


class SetPasswordBody(BaseModel):
    username: str = Field(default="", max_length=128)
    current_password: str = Field(default="", max_length=1024)
    new_password: str = Field(default="", max_length=1024)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(default="", max_length=1024)
    new_password: str = Field(default="", max_length=1024)


def _account(ctx: AppContext, username: str):
    """Return (config entry, db row) for a configured user, or (None, None)."""
    entry = ctx.config.user(username)
    if entry is None:
        return None, None
    ctx.db.ensure_users([entry.username])
    return entry, ctx.db.get_user(entry.username)


def _first_login_ok(ctx: AppContext, entry, supplied: str) -> bool:
    """Decide whether someone may claim an account that has no password yet."""
    if entry.initial_password:
        return verify_password_plain(supplied, entry.initial_password)
    return ctx.config.allow_blank_first_login


def verify_password_plain(supplied: str, expected: str) -> bool:
    # The initial password lives in the config file in the clear; a plain
    # comparison is all that is on offer, done in constant time.
    import hmac

    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


@router.post("/login")
def login(
    body: LoginBody,
    request: Request,
    response: Response,
    ctx: AppContext = Depends(get_ctx),
):
    username = (body.username or "").strip().lower()
    login_limiter.check(client_key(request, username))

    entry, row = _account(ctx, username)
    if entry is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    if not row or not row["password_hash"]:
        # First login: the account exists but has never been claimed.
        return {
            "status": "password_not_set",
            "username": entry.username,
            "needs_initial_password": bool(entry.initial_password),
            "min_password_length": MIN_PASSWORD_LENGTH,
        }

    if not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    login_limiter.reset(client_key(request, username))
    set_session_cookie(request, response, ctx, entry.username)
    return {"status": "ok", "username": entry.username}


@router.post("/set-password")
def set_password(
    body: SetPasswordBody,
    request: Request,
    response: Response,
    ctx: AppContext = Depends(get_ctx),
):
    """Claim an account on first login (or replace a known password)."""
    username = (body.username or "").strip().lower()
    login_limiter.check(client_key(request, username))

    entry, row = _account(ctx, username)
    if entry is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")

    if row and row["password_hash"]:
        if not verify_password(body.current_password, row["password_hash"]):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    elif not _first_login_ok(ctx, entry, body.current_password):
        detail = (
            "the initial password for this account is not correct"
            if entry.initial_password
            else "this account cannot be claimed; ask the administrator to set an initial password"
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)

    try:
        validate_password(body.new_password)
    except PasswordError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    ctx.db.set_password_hash(entry.username, hash_password(body.new_password))
    ctx.db.delete_user_sessions(entry.username)
    login_limiter.reset(client_key(request, username))
    set_session_cookie(request, response, ctx, entry.username)
    return {"status": "ok", "username": entry.username}


@router.post("/change-password")
def change_password(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    username: str = Depends(current_user),
    ctx: AppContext = Depends(get_ctx),
):
    login_limiter.check(client_key(request, username))
    row = ctx.db.get_user(username)
    if not row or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current password is not correct")
    try:
        validate_password(body.new_password)
    except PasswordError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    ctx.db.set_password_hash(username, hash_password(body.new_password))
    # Sign other devices out, but keep this one signed in.
    token = request.cookies.get(SESSION_COOKIE)
    ctx.db.delete_user_sessions(username, keep=hash_session_token(token) if token else None)
    return {"status": "ok"}


@router.post("/logout")
def logout(request: Request, response: Response, ctx: AppContext = Depends(get_ctx)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        ctx.db.delete_session(hash_session_token(token))
    clear_session_cookie(response)
    return {"status": "ok"}
