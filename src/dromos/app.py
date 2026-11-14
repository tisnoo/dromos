"""The web app: health check, dashboard, MCP endpoint (/mcp) with its OAuth routes, and the
login page.

Runs the background jobs in-process, so it must be served by a single worker process.
DROMOS_BASE_URL is the public URL (e.g. https://dromos.example.com); OAuth metadata and
redirects are built from it.
"""

import html
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.transport_security import TransportSecuritySettings

from dromos import dashboard, db
from dromos.auth import LOGIN_PATH, DromosOAuthProvider, LoginError
from dromos.mcp_server import create_mcp
from dromos.scheduler import add_jobs

log = logging.getLogger(__name__)

LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dromos · connect</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 22rem; margin: 15vh auto;
         padding: 0 1rem; color: #1a1a1a; background: #fafafa; }}
  input, button {{ font: inherit; width: 100%; padding: .6rem; margin-top: .5rem;
                   box-sizing: border-box; border-radius: 6px; border: 1px solid #bbb; }}
  button {{ background: #1a1a1a; color: #fff; border: 0; cursor: pointer; }}
  .error {{ color: #b00020; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #eee; background: #161616; }}
    button {{ background: #eee; color: #161616; }}
  }}
</style></head><body>
<h1>Dromos</h1>
<p><strong>{client}</strong> wants to access your training data.</p>
{error}
<form method="post" action="{action}">
  <input type="hidden" name="request" value="{request}">
  <label>Password<input type="password" name="password" autofocus required></label>
  <button type="submit">Allow access</button>
</form>
</body></html>"""

PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _login_page(request_id: str, client: str, error: str = "", status: int = 200):
    body = LOGIN_PAGE.format(
        client=html.escape(client),
        error=f'<p class="error">{html.escape(error)}</p>' if error else "",
        action=LOGIN_PATH,
        request=html.escape(request_id),
    )
    return HTMLResponse(body, status_code=status, headers=PAGE_HEADERS)


def create_app(base_url: str | None = None) -> FastAPI:
    base_url = (base_url or os.getenv("DROMOS_BASE_URL") or "http://localhost:8000").rstrip("/")
    oauth = DromosOAuthProvider(base_url)
    mcp = create_mcp(base_url, oauth)
    # Stateless + JSON: no server-pushed messages are needed, and sessions survive redeploys.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlparse(base_url).netloc],
            allowed_origins=[base_url, "https://claude.ai"],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
        db.connect().close()  # apply migrations before anything else touches the DB
        scheduler = add_jobs(BackgroundScheduler())
        scheduler.start()
        async with mcp.session_manager.run():
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

    @app.get(LOGIN_PATH)
    def login_form(request: str = "") -> HTMLResponse:
        client = oauth.pending_client_name(request)
        if client is None:
            return HTMLResponse(
                "This login link has expired. Start again from your app.",
                status_code=400,
                headers=PAGE_HEADERS,
            )
        return _login_page(request, client)

    @app.post(LOGIN_PATH)
    async def login_submit(request: Request):
        form = parse_qs((await request.body()).decode())
        request_id = form.get("request", [""])[0]
        password = form.get("password", [""])[0]
        try:
            redirect = oauth.complete_login(request_id, password)
        except LoginError as error:
            client = oauth.pending_client_name(request_id)
            if client is None:
                return HTMLResponse(str(error), status_code=400, headers=PAGE_HEADERS)
            return _login_page(request_id, client, str(error), status=401)
        return RedirectResponse(redirect, status_code=303, headers={"Cache-Control": "no-store"})

    app.include_router(dashboard.create_router(base_url))

    # Last, so the routes above win: /mcp plus the OAuth endpoints (/authorize, /token,
    # /register, /revoke, /.well-known/...) at the root.
    app.mount("/", mcp_app)
    return app


app = create_app()
