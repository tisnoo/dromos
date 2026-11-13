"""The web app: health check now; MCP endpoint and dashboard come later.

Runs the background jobs in-process, so it must be served by a single worker process.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from dromos import db
from dromos.scheduler import add_jobs

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    db.connect().close()  # apply migrations before anything else touches the DB
    scheduler = add_jobs(BackgroundScheduler())
    scheduler.start()
    yield
    scheduler.shutdown()


# No /docs or /openapi.json: the app is public and the API is not meant to be browsed.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> JSONResponse:
    try:
        conn = db.connect()
        try:
            conn.execute("SELECT 1 FROM activities LIMIT 1")
        finally:
            conn.close()
    except Exception:
        log.exception("Health check failed")
        return JSONResponse({"status": "error"}, status_code=503)
    return JSONResponse({"status": "ok"})
