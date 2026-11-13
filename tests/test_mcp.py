"""The OAuth flow claude.ai goes through, the Hermes bearer token, and MCP tool calls."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import dromos.app as appmod
from dromos import auth

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
FORM = {"content-type": "application/x-www-form-urlencoded"}
MCP_ACCEPT = "application/json, text/event-stream"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DROMOS_DB", str(tmp_path / "dromos.db"))
    monkeypatch.setenv("DROMOS_OWNER_PASSWORD", "owner-pw")
    monkeypatch.setenv("DROMOS_API_TOKEN", "hermes-token")
    monkeypatch.setattr(appmod, "add_jobs", lambda scheduler: scheduler)  # no Garmin sync
    app = appmod.create_app("http://localhost:8000")
    with TestClient(app, base_url="http://localhost:8000", follow_redirects=False) as c:
        yield c


def start_login(client) -> tuple[str, str]:
    """Register a client and start authorization; returns (client_id, login request id)."""
    registered = client.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": "Claude <b>",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert registered.status_code == 201
    client_id = registered.json()["client_id"]
    response = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "state-1",
        },
    )
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    assert location.path == auth.LOGIN_PATH
    return client_id, parse_qs(location.query)["request"][0]


def login(client, request_id: str, password: str):
    return client.post(
        auth.LOGIN_PATH, content=f"request={request_id}&password={password}", headers=FORM
    )


def get_tokens(client) -> dict:
    client_id, request_id = start_login(client)
    approved = login(client, request_id, "owner-pw")
    assert approved.status_code == 303
    query = parse_qs(urlparse(approved.headers["location"]).query)
    assert query["state"] == ["state-1"]
    response = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": VERIFIER,
        },
    )
    assert response.status_code == 200
    return {"client_id": client_id, **response.json()}


def call(client, token: str, method: str, params: dict | None = None):
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {token}", "Accept": MCP_ACCEPT},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def test_mcp_requires_auth(client):
    response = client.post("/mcp", json={}, headers={"Accept": MCP_ACCEPT})
    assert response.status_code == 401
    assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]
    assert call(client, "made-up", "tools/list").status_code == 401


def test_login_page_escapes_client_name(client):
    _, request_id = start_login(client)
    page = client.get(auth.LOGIN_PATH, params={"request": request_id})
    assert "Claude &lt;b&gt;" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


def test_wrong_password_is_rejected_and_locks_out(client):
    _, request_id = start_login(client)
    for _ in range(auth.MAX_FAILED_LOGINS):
        assert login(client, request_id, "nope").status_code == 401
    locked = login(client, request_id, "owner-pw")
    assert locked.status_code == 401
    assert "Too many failed attempts" in locked.text


def test_no_owner_password_means_no_login(client, monkeypatch):
    monkeypatch.delenv("DROMOS_OWNER_PASSWORD")
    _, request_id = start_login(client)
    assert login(client, request_id, "").status_code == 401


def test_oauth_flow_then_tools(client):
    tokens = get_tokens(client)
    tools = call(client, tokens["access_token"], "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "get_training_status",
        "get_activities",
        "get_plan",
        "compare_plan_vs_actual",
    }
    assert all(t["annotations"]["readOnlyHint"] for t in tools)

    result = call(
        client,
        tokens["access_token"],
        "tools/call",
        {"name": "get_activities", "arguments": {"start_date": "2026-11-01"}},
    ).json()["result"]
    assert result["structuredContent"] == {"runs": []}


def test_refresh_rotates_tokens(client):
    tokens = get_tokens(client)
    refreshed = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
        },
    )
    assert refreshed.status_code == 200
    assert call(client, refreshed.json()["access_token"], "tools/list").status_code == 200
    assert call(client, tokens["access_token"], "tools/list").status_code == 401


def test_hermes_static_token(client, monkeypatch):
    assert call(client, "hermes-token", "tools/list").status_code == 200
    monkeypatch.delenv("DROMOS_API_TOKEN")
    assert call(client, "hermes-token", "tools/list").status_code == 401


def test_tokens_are_stored_hashed(client, tmp_path):
    import sqlite3

    tokens = get_tokens(client)
    stored = (
        sqlite3.connect(tmp_path / "dromos.db")
        .execute("SELECT token_hash FROM oauth_tokens")
        .fetchall()
    )
    assert stored and all(tokens["access_token"] not in row[0] for row in stored)
