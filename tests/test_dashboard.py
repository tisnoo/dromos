"""Dashboard: Google sign-in for one account, and the page itself."""

import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import raw_run, raw_sleep
from fastapi.testclient import TestClient
from test_replan import make_plan

import dromos.app as appmod
from dromos import dashboard, db, plan
from dromos.garmin import normalise, normalise_sleep

OWNER = "runner@example.com"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("DROMOS_DASHBOARD_EMAIL", "Runner@Example.com")
    monkeypatch.setenv("DROMOS_SESSION_SECRET", "s" * 32)
    monkeypatch.setattr(appmod, "add_jobs", lambda scheduler: scheduler)
    app = appmod.create_app("https://dromos.example.com")
    with TestClient(app, base_url="https://dromos.example.com", follow_redirects=False) as c:
        yield c


def claims(**overrides) -> dict:
    return {
        "iss": "https://accounts.google.com",
        "aud": "client-id",
        "exp": time.time() + 300,
        "email": OWNER,
        "email_verified": True,
        **overrides,
    }


def sign_in(client, monkeypatch, id_claims: dict):
    async def fake_exchange(code, config, redirect_uri):
        assert (code, redirect_uri) == (
            "the-code",
            "https://dromos.example.com/dashboard/auth/callback",
        )
        return id_claims

    monkeypatch.setattr(dashboard, "exchange_code", fake_exchange)
    login = client.get("/dashboard/login")
    assert login.status_code == 303
    google = urlparse(login.headers["location"])
    params = parse_qs(google.query)
    assert google.netloc == "accounts.google.com"
    assert params["login_hint"] == ["runner@example.com"]
    assert params["scope"] == ["openid email"]
    return client.get(
        "/dashboard/auth/callback", params={"code": "the-code", "state": params["state"][0]}
    )


def test_dashboard_needs_sign_in(client):
    response = client.get("/dashboard")
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/login"


def test_dashboard_closed_without_config(client, monkeypatch):
    monkeypatch.delenv("DROMOS_SESSION_SECRET")
    assert client.get("/dashboard").status_code == 503
    assert client.get("/dashboard/login").status_code == 503


def test_owner_signs_in_and_sees_dashboard(client, monkeypatch):
    callback = sign_in(client, monkeypatch, claims())
    assert callback.status_code == 303
    cookie = callback.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/dashboard" in cookie

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert "Sign out" in page.text

    signed_out = client.post("/dashboard/logout")
    assert signed_out.status_code == 200
    assert client.get("/dashboard").status_code == 303


@pytest.mark.parametrize(
    "bad",
    [
        {"email": "someone@example.com"},
        {"email_verified": False},
        {"aud": "another-client"},
        {"iss": "https://evil.example.com"},
        {"exp": time.time() - 10},
    ],
)
def test_other_accounts_and_bad_tokens_are_refused(client, monkeypatch, bad):
    callback = sign_in(client, monkeypatch, claims(**bad))
    assert callback.status_code == 403
    assert "set-cookie" not in callback.headers
    assert client.get("/dashboard").status_code == 303


def test_callback_needs_matching_state(client, monkeypatch):
    client.get("/dashboard/login")
    response = client.get("/dashboard/auth/callback", params={"code": "c", "state": "forged"})
    assert response.status_code == 400


def test_tampered_or_expired_session_is_refused(client):
    secret = b"s" * 32
    forged = dashboard.sign({"email": OWNER, "exp": time.time() + 60}, b"wrong secret")
    expired = dashboard.sign({"email": OWNER, "exp": time.time() - 1}, secret)
    other = dashboard.sign({"email": "someone@example.com", "exp": time.time() + 60}, secret)
    for token in (forged, expired, other, forged.replace(".", "x.", 1)):
        client.cookies.set(dashboard.SESSION_COOKIE, token, path="/dashboard")
        assert client.get("/dashboard").status_code == 303


def test_dashboard_renders_runs_sleep_and_plan(client, monkeypatch, tmp_path):
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    conn = db.connect(str(tmp_path / "dromos.db"))
    plan.import_plan(conn, make_plan(monday - timedelta(weeks=1)), reason="test")
    raw = raw_run(
        1,
        f"{today.isoformat()} 07:00:00",
        trainingEffectLabel="AEROBIC_BASE",
        aerobicTrainingEffect=2.8,
    )
    db.upsert_activity(conn, normalise(raw), raw)
    night = raw_sleep(today.isoformat(), score=77)
    db.upsert_sleep(conn, normalise_sleep(night), night)
    db.add_threshold(conn, today, 180)
    conn.close()

    sign_in(client, monkeypatch, claims())
    page = client.get("/dashboard").text
    assert "Weekly distance" in page and '<path class="bar"' in page
    assert "Aerobic base" in page and 'class="ok"' in page
    assert ">77 <small>7.5 h" in page
    assert ">160 <small>bpm" in page
