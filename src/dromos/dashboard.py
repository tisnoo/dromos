"""The dashboard (/dashboard): server-rendered pages over the database, behind Google sign-in.

Only one Google account gets in: DROMOS_DASHBOARD_EMAIL. Sign-in is the OAuth code flow with
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET; the ID token comes straight from Google's token
endpoint over TLS, so its claims are checked but its signature isn't (OpenID Connect Core
3.1.3.7). The session is a cookie signed with DROMOS_SESSION_SECRET. Without all four
settings the dashboard stays closed.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from dromos import db, replan, training

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
SESSION_COOKIE = "dromos_session"
STATE_COOKIE = "dromos_oauth_state"
SESSION_SECONDS = 30 * 24 * 3600
STATE_SECONDS = 600
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


@dataclass(frozen=True)
class Config:
    client_id: str
    client_secret: str
    email: str
    secret: bytes

    @classmethod
    def from_env(cls) -> "Config | None":
        values = [
            os.getenv(name)
            for name in (
                "GOOGLE_CLIENT_ID",
                "GOOGLE_CLIENT_SECRET",
                "DROMOS_DASHBOARD_EMAIL",
                "DROMOS_SESSION_SECRET",
            )
        ]
        if not all(values):
            return None
        client_id, client_secret, email, secret = values
        return cls(client_id, client_secret, email.strip().lower(), secret.encode())


def sign(payload: dict[str, Any], secret: bytes) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    mac = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def unsign(token: str | None, secret: bytes) -> dict[str, Any] | None:
    """The payload of a token signed with `secret` that hasn't expired, else None."""
    body, _, mac = (token or "").partition(".")
    expected = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    if not body or not hmac.compare_digest(mac, expected):
        return None
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return payload if payload.get("exp", 0) > time.time() else None


def allowed_email(claims: dict[str, Any], config: Config) -> str | None:
    """The signed-in email if these ID token claims are for the dashboard's account."""
    email = str(claims.get("email", "")).lower()
    ok = (
        claims.get("iss") in GOOGLE_ISSUERS
        and claims.get("aud") == config.client_id
        and claims.get("exp", 0) > time.time()
        and claims.get("email_verified") is True
        and hmac.compare_digest(email, config.email)
    )
    return email if ok else None


async def exchange_code(code: str, config: Config, redirect_uri: str) -> dict[str, Any]:
    """Swap an authorization code for Google's ID token claims."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    response.raise_for_status()
    body = response.json()["id_token"].split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


# Charts are drawn server-side as SVG, all the same size so their text renders the same:
# a 480 x 200 plot (about the width of a half-width card) with room for axis labels.
WIDTH, HEIGHT, LEFT, BOTTOM, TOP = 480, 200, 32, 24, 10


def _bar_path(x: float, y: float, width: float, base: float, radius: float = 4) -> str:
    """A bar rounded at its data end only, anchored to the baseline."""
    r = min(radius, width / 2, max(base - y, 0))
    return (
        f"M{x:.1f},{base:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
        f"H{x + width - r:.1f}Q{x + width:.1f},{y:.1f} {x + width:.1f},{y + r:.1f}V{base:.1f}Z"
    )


def _scale(top_value: float) -> tuple[float, list[dict[str, Any]]]:
    """A round axis maximum and three gridlines for values up to top_value."""
    step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 100) if top_value / s <= 4) if top_value else 1
    maximum = step * max(1, -(-top_value // step))
    plot = HEIGHT - BOTTOM - TOP
    ticks = [
        {"y": TOP + plot * (1 - v / maximum), "label": f"{v:g}"}
        for v in [step * i for i in range(int(maximum // step) + 1)]
    ]
    return maximum, ticks


def weekly_chart(weeks: list[dict[str, Any]], today: date) -> dict[str, Any]:
    maximum, ticks = _scale(max([w["planned_km"] for w in weeks] + [w["actual_km"] for w in weeks]))
    plot, base = HEIGHT - BOTTOM - TOP, HEIGHT - BOTTOM
    slot = (WIDTH - LEFT) / max(len(weeks), 1)
    bars = []
    for i, w in enumerate(weeks):
        x = LEFT + i * slot + slot * 0.2
        width = slot * 0.6
        future = date.fromisoformat(w["start"]) > today
        actual_y = base - plot * w["actual_km"] / maximum
        bars.append(
            {
                "path": None
                if future or not w["actual_km"]
                else _bar_path(x, actual_y, width, base),
                "plan_y": base - plot * w["planned_km"] / maximum,
                "x": x,
                "width": width,
                "slot_x": LEFT + i * slot,
                "slot": slot,
                "label": f"W{w['week']}",
                "current": date.fromisoformat(w["start"]) <= today <= date.fromisoformat(w["end"]),
                "title": f"Week {w['week']} ({w['start']}): "
                + (f"{w['actual_km']:g} of " if not future else "")
                + f"{w['planned_km']:g} km planned",
            }
        )
    return {"bars": bars, "ticks": ticks, "base": base}


def sleep_chart(nights: list[dict[str, Any]], start: date, days: int) -> dict[str, Any]:
    by_date = {n["date"]: n for n in nights}
    maximum, ticks = 100, _scale(100)[1]
    plot, base = HEIGHT - BOTTOM - TOP, HEIGHT - BOTTOM
    slot = (WIDTH - LEFT) / days
    bars = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        night = by_date.get(day)
        x = LEFT + i * slot + slot * 0.2
        score = night["score"] if night and night["score"] is not None else None
        bars.append(
            {
                "path": _bar_path(x, base - plot * score / maximum, slot * 0.6, base)
                if score is not None
                else None,
                "slot_x": LEFT + i * slot,
                "slot": slot,
                "label": str(int(day[8:])),
                "title": f"{day}: score {score}, {night['hours']} h"
                if night
                else f"{day}: no data",
            }
        )
    return {"bars": bars, "ticks": ticks, "base": base}


def hrv_chart(nights: list[dict[str, Any]], start: date, days: int) -> dict[str, Any]:
    by_date = {n["date"]: n for n in nights}
    values = [
        v
        for n in nights
        for v in (n["avg_overnight_hrv"], *(n["hrv_baseline"] or []))
        if v is not None
    ]
    maximum, ticks = _scale(max(values, default=100))
    plot, base = HEIGHT - BOTTOM - TOP, HEIGHT - BOTTOM
    slot = (WIDTH - LEFT) / days

    def y(v: float) -> float:
        return base - plot * v / maximum

    points, band_top, band_bottom, columns, segments = [], [], [], [], [[]]
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        night = by_date.get(day)
        cx = LEFT + i * slot + slot / 2
        hrv = night["avg_overnight_hrv"] if night else None
        if night and night["hrv_baseline"]:
            low, high = night["hrv_baseline"]
            band_top.append(f"{cx:.1f},{y(high):.1f}")
            band_bottom.append(f"{cx:.1f},{y(low):.1f}")
        if hrv is not None:
            points.append({"x": cx, "y": y(hrv)})
            segments[-1].append(f"{cx:.1f},{y(hrv):.1f}")
        elif segments[-1]:
            segments.append([])  # no line across nights without data
        columns.append(
            {
                "slot_x": LEFT + i * slot,
                "slot": slot,
                "label": str(int(day[8:])),
                "title": f"{day}: HRV {hrv:g} ms"
                + (
                    f" (baseline {night['hrv_baseline'][0]:g}-{night['hrv_baseline'][1]:g})"
                    if night["hrv_baseline"]
                    else ""
                )
                if hrv is not None
                else f"{day}: no data",
            }
        )
    band = " ".join(band_top + band_bottom[::-1]) if len(band_top) > 1 else None
    lines = [" ".join(s) for s in segments if len(s) > 1]
    return {"points": points, "lines": lines, "band": band, "columns": columns, "ticks": ticks}


def dashboard_data(conn, today: date) -> dict[str, Any]:
    current = training.week_number_for(conn, today)
    race = training.race_date(conn)
    weeks = []
    if current:
        for number in range(max(1, current - 5), current + 4):
            try:
                weeks.append(training.compare_week(conn, number, today))
            except ValueError:
                break
    this_week = next((w for w in weeks if w["week"] == current), None)
    sleep_start = today - timedelta(days=13)
    nights = training.sleep(conn, sleep_start, today)
    upcoming = [
        {**s, "weekday": date.fromisoformat(s["date"]).strftime("%a %d %b")}
        for w in training.plan(conn, today, today + timedelta(days=7))
        for s in w["sessions"]
        if today.isoformat() <= s["date"] <= (today + timedelta(days=7)).isoformat()
    ]
    return {
        "today": today,
        "race": race,
        "days_to_race": (race - today).days if race else None,
        "this_week": this_week,
        "last_night": nights[-1] if nights and nights[-1]["date"] == today.isoformat() else None,
        "latest_night": nights[-1] if nights else None,
        "targets": training.heart_rate_targets(conn),
        "weekly": weekly_chart(weeks, today) if weeks else None,
        "weeks": weeks,
        "sleep": sleep_chart(nights, sleep_start, 14),
        "hrv": hrv_chart(nights, sleep_start, 14),
        "nights": nights[::-1],
        "runs": training.activities(conn, today - timedelta(days=27), today)[::-1],
        "upcoming": upcoming,
        "changes": [c for c in replan.changes(conn, 5) if "changes" in c],
        "easy_labels": {"RECOVERY", "AEROBIC_BASE"},
    }


def fmt_duration(minutes: float) -> str:
    """54.8 → '54:48', 95.5 → '1:35:30'."""
    hours, rest = divmod(round(minutes * 60), 3600)
    mins, secs = divmod(rest, 60)
    return f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"


def _page(name: str, status: int = 200, **context: Any) -> HTMLResponse:
    body = templates.get_template(name).render(**context)
    return HTMLResponse(body, status_code=status, headers=HEADERS)


def create_router(base_url: str) -> APIRouter:
    router = APIRouter()
    secure = base_url.startswith("https://")
    redirect_uri = f"{base_url}/dashboard/auth/callback"

    def signed_in(request: Request, config: Config) -> bool:
        session = unsign(request.cookies.get(SESSION_COOKIE), config.secret)
        return bool(session) and hmac.compare_digest(session.get("email", ""), config.email)

    @router.get("/dashboard")
    def dashboard(request: Request) -> Response:
        config = Config.from_env()
        if config is None:
            return _page("message.html", 503, message="The dashboard isn't set up yet.")
        if not signed_in(request, config):
            return RedirectResponse("/dashboard/login", status_code=303, headers=HEADERS)
        conn = db.connect()
        try:
            data = dashboard_data(conn, date.today())
        finally:
            conn.close()
        return _page("dashboard.html", **data, fmt_duration=fmt_duration)

    @router.get("/dashboard/login")
    def login() -> Response:
        config = Config.from_env()
        if config is None:
            return _page("message.html", 503, message="The dashboard isn't set up yet.")
        state = secrets.token_urlsafe(24)
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email",
            "state": state,
            "login_hint": config.email,
            "prompt": "select_account",
        }
        response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=303)
        response.set_cookie(
            STATE_COOKIE,
            sign({"state": state, "exp": time.time() + STATE_SECONDS}, config.secret),
            max_age=STATE_SECONDS,
            path="/dashboard",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response

    @router.get("/dashboard/auth/callback")
    async def callback(request: Request, code: str = "", state: str = "") -> Response:
        config = Config.from_env()
        if config is None:
            return _page("message.html", 503, message="The dashboard isn't set up yet.")
        expected = unsign(request.cookies.get(STATE_COOKIE), config.secret)
        if not code or not expected or not hmac.compare_digest(state, expected["state"]):
            return _page("message.html", 400, message="Sign-in expired. Try again.", retry=True)
        try:
            claims = await exchange_code(code, config, redirect_uri)
        except (httpx.HTTPError, KeyError, ValueError):
            return _page(
                "message.html", 502, message="Google sign-in failed. Try again.", retry=True
            )
        email = allowed_email(claims, config)
        if email is None:
            return _page(
                "message.html", 403, message="This Google account can't view this dashboard."
            )
        response = RedirectResponse("/dashboard", status_code=303, headers=HEADERS)
        response.set_cookie(
            SESSION_COOKIE,
            sign({"email": email, "exp": time.time() + SESSION_SECONDS}, config.secret),
            max_age=SESSION_SECONDS,
            path="/dashboard",
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        response.delete_cookie(STATE_COOKIE, path="/dashboard")
        return response

    @router.post("/dashboard/logout")
    def logout() -> Response:
        response = _page("message.html", message="Signed out.", retry=True)
        response.delete_cookie(SESSION_COOKIE, path="/dashboard")
        return response

    return router
