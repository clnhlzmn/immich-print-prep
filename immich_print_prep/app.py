"""FastAPI application wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, load_config
from .context import AppContext
from .deps import SESSION_COOKIE
from .routes import auth, browse, jobs, selection, settings
from .security import hash_session_token

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
HOUSEKEEPING_SECONDS = 600


def create_app(config: Optional[Config] = None) -> FastAPI:
    config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = AppContext(config)
        app.state.ctx = ctx
        ctx.sweep_jobs()
        housekeeper = asyncio.ensure_future(_housekeeping(ctx))
        log.info(
            "immich-print-prep ready: immich=%s users=%d data=%s",
            config.immich_url, len(config.users), config.data_dir,
        )
        try:
            yield
        finally:
            housekeeper.cancel()
            for task in list(ctx.tasks.values()):
                task.cancel()
            await ctx.aclose()

    app = FastAPI(
        title="immich-print-prep",
        description="Select photos from Immich and prepare them for print ordering.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.include_router(auth.router)
    app.include_router(settings.router)
    app.include_router(browse.router)
    app.include_router(selection.router)
    app.include_router(jobs.router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _signed_in(request: Request) -> bool:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return False
        session = request.app.state.ctx.db.get_session(hash_session_token(token))
        return bool(session and config.user(session["username"]))

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        if not _signed_in(request):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        if _signed_in(request):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    return app


async def _housekeeping(ctx: AppContext) -> None:
    """Expire sessions, drop stale downloads, keep the thumbnail cache bounded."""
    while True:
        try:
            await asyncio.sleep(HOUSEKEEPING_SECONDS)
            ctx.db.purge_expired_sessions()
            ctx.sweep_jobs()
            ctx.sweep_cache()
            await ctx.close_retired_clients()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - housekeeping must not kill the server
            log.exception("housekeeping pass failed")
