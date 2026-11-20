"""The local web server behind the dashboard.

One process, serving JSON and a static page from the same port. There is no separate
frontend build: the page is plain HTML, CSS and JavaScript, so installing Scout does
not drag in a Node toolchain, and `scout serve` is the whole story.

Live progress is read from the database rather than pushed from the worker. That is
the simpler design and it buys something real: a run started from the command line in
another terminal shows up in the dashboard too, because the database is the only thing
either of them trusts.

Nothing here is exposed beyond the loopback interface by default, and there is no
authentication, because this is a local tool reading a local file.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from scout.api import queries
from scout.config import VALID_DEPTHS, Settings, load_settings
from scout.db.session import init_db

STATIC_DIR = Path(__file__).resolve().parent / "static"

# How often the event stream re-reads the database. Fast enough that a step appears
# while you are still looking at the page, slow enough to be free.
POLL_SECONDS = 1.0


class RunRequest(BaseModel):
    domains: list[str]
    objectives: str | None = None
    icp: dict[str, Any] | None = None
    depth: str | None = None
    fields: str | None = None
    label: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings)

    app = FastAPI(title="Scout", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    # One research run at a time. The engine is sequential today, and starting a
    # second would have two workers writing the same SQLite file for no gain.
    app.state.worker: threading.Thread | None = None

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "database": str(settings.db_path),
            "model": settings.model,
            "engine": settings.engine,
            "depth": settings.depth,
            "api_key": bool(settings.api_key()),
            "running": _is_running(app),
        }

    @app.get("/api/totals")
    def totals() -> dict:
        return queries.totals(settings)

    @app.get("/api/runs")
    def runs(limit: int = 50) -> list[dict]:
        return queries.list_runs(settings, limit=limit)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int) -> dict:
        data = queries.get_run(settings, run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return data

    @app.get("/api/company-runs/{company_run_id}")
    def company_detail(company_run_id: int) -> dict:
        data = queries.get_company_run(settings, company_run_id)
        if data is None:
            raise HTTPException(
                status_code=404, detail=f"no company run {company_run_id}"
            )
        return data

    @app.get("/api/company-runs/{company_run_id}/steps")
    def company_steps(company_run_id: int) -> list[dict]:
        return queries.get_steps(settings, company_run_id)

    @app.post("/api/runs")
    def start_run(request: RunRequest) -> dict:
        """Start a research run in the background. Costs money, so it checks first."""
        if _is_running(app):
            raise HTTPException(status_code=409, detail="a run is already in progress")

        # The request is checked before the configuration, so a caller hears what is
        # wrong with what they asked for rather than what is wrong with the machine.
        domains = [d.strip() for d in request.domains if d.strip()]
        if not domains:
            raise HTTPException(status_code=400, detail="give at least one domain")
        if not request.objectives and not request.icp:
            raise HTTPException(
                status_code=400,
                detail=(
                    "this run has no goal. Give objectives or an ICP, or Scout will "
                    "research whatever it finds first."
                ),
            )
        run_settings = settings
        if request.depth:
            if request.depth not in VALID_DEPTHS:
                raise HTTPException(
                    status_code=400,
                    detail=f"depth must be one of {', '.join(VALID_DEPTHS)}",
                )
            run_settings = replace(settings, depth=request.depth)

        if not settings.api_key():
            raise HTTPException(
                status_code=400,
                detail="no ANTHROPIC_API_KEY set; add it to .env and restart",
            )

        run_id = _launch(app, run_settings, request, domains)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: int) -> StreamingResponse:
        """Server-sent events: the run as it changes, read from the database."""

        async def stream():
            last = None
            idle = 0
            while idle < 1800:  # give up after half an hour of nothing happening
                data = await asyncio.to_thread(queries.get_run, settings, run_id)
                if data is None:
                    yield _sse({"error": f"no run {run_id}"})
                    return
                payload = json.dumps(data, default=str)
                if payload != last:
                    last = payload
                    idle = 0
                    yield f"data: {payload}\n\n"
                else:
                    idle += 1
                if data["status"] in ("done", "failed") and not _is_running(app):
                    yield "event: end\ndata: {}\n\n"
                    return
                await asyncio.sleep(POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if STATIC_DIR.exists():
        app.mount(
            "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _is_running(app: FastAPI) -> bool:
    worker = getattr(app.state, "worker", None)
    return bool(worker and worker.is_alive())


def _launch(
    app: FastAPI, settings: Settings, request: RunRequest, domains: list[str]
) -> int:
    """Create the run row, then do the research on a background thread.

    The row is created here rather than in the thread so the caller gets an id to
    watch immediately, instead of polling for a run that may not exist yet.
    """
    from scout.agent.loop import create_run, finish_run, research_company
    from scout.agent.prompts import resolve_fields
    from anthropic import Anthropic

    fields = resolve_fields(request.fields)
    run_id = create_run(
        settings,
        label=request.label,
        objectives=request.objectives,
        requested_fields=fields,
        icp=request.icp,
    )

    def work() -> None:
        client = Anthropic(api_key=settings.api_key())
        for domain in domains:
            try:
                research_company(
                    client=client,
                    settings=settings,
                    run_id=run_id,
                    domain=domain,
                    name=None,
                    objectives=request.objectives,
                    requested_fields=fields,
                    icp=request.icp,
                )
            except Exception:
                # research_company records its own failure row; a crash here must
                # not take the rest of the batch with it.
                continue
        finish_run(settings, run_id)

    thread = threading.Thread(target=work, name=f"scout-run-{run_id}", daemon=True)
    app.state.worker = thread
    thread.start()
    return run_id
