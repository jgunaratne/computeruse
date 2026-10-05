"""FastAPI application factory.

Serves the JSON/WebSocket API under /api and the built web UI (web/dist) for
every other path, so a single `computeruse serve` process is the whole product.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from computeruse import __version__
from computeruse.config import Settings, get_settings
from computeruse.server.orchestrator import Orchestrator
from computeruse.server.routes import router

log = logging.getLogger("computeruse.app")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.orch = orch = Orchestrator(settings)
        app.state.background = set()
        log.info("computeruse %s listening on http://%s:%s (data_dir=%s)", __version__, settings.host,
                 settings.port, settings.data_dir.resolve())
        if settings.model_discovery != "off" and orch.catalog.any_available():
            # Verify which models the credentials can really call; the picker shows suggestions until this lands.
            task = asyncio.create_task(orch.catalog.discover(), name="model-discovery-startup")
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)
        try:
            yield
        finally:
            for task in list(app.state.background):
                task.cancel()
            await app.state.orch.shutdown()

    app = FastAPI(title="computeruse", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(router)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    dist = Path(settings.web_dist)
    index = dist / "index.html"
    if index.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = dist / path
            if path and candidate.is_file() and candidate.resolve().is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(index)
    else:
        @app.get("/", include_in_schema=False)
        async def no_ui() -> JSONResponse:
            return JSONResponse({
                "message": "web UI not built; run `cd web && npm install && npm run build`",
                "api_docs": "/api/docs",
            })

    return app


app = create_app()
