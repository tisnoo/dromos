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
        "get_sleep",
        "get_plan_changes",
        "record_threshold_test",
        "propose_replan",
        "propose_plan_change",
        "apply_plan_change",
    }
    writes = {t["name"] for t in tools if not t["annotations"]["readOnlyHint"]}
    assert writes == {
        "propose_replan",
        "propose_plan_change",
        "apply_plan_change",
        "record_threshold_test",
    }

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


def tool(client, name: str, arguments: dict) -> dict:
    response = call(client, "hermes-token", "tools/call", {"name": name, "arguments": arguments})
    result = response.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_propose_and_apply_plan_changes(client, tmp_path):
    from datetime import date, timedelta

    from test_replan import make_plan

    from dromos import db, plan

    monday = date.today() + timedelta(days=7 - date.today().weekday())
    conn = db.connect(str(tmp_path / "dromos.db"))
    plan.import_plan(conn, make_plan(monday), reason="test")
    conn.close()

    first = tool(client, "propose_replan", {"reason": "no recent runs"})
    assert first["status"] == "proposed", first
    assert first["notes"][0].startswith("recent load 10.0 km/week")
    replanned = tool(client, "propose_replan", {"reason": "same again"})
    assert tool(client, "apply_plan_change", {"change_id": replanned["change_id"]}) == {
        "change_id": replanned["change_id"],
        "status": "applied",
        "weeks": [1, 2, 3, 4, 5],
    }
    week_1 = tool(client, "get_plan", {"start_date": monday.isoformat()})["weeks"][0]
    assert week_1["target_km"] <= 10 * 1.15 + 0.25

    # Drop Wednesday's run: the long run stays, so later weeks are still within the rules.
    wednesday = (monday + timedelta(days=2)).isoformat()
    keep = [s for s in week_1["sessions"] if s["date"] != wednesday]
    edit = tool(
        client,
        "propose_plan_change",
        {"weeks": [{"week": 1, "sessions": keep}], "reason": "busy Wednesday"},
    )
    assert edit["status"] == "proposed", edit
    assert [r["date"] for r in edit["changes"][0]["runs"]] == [wednesday]
    tool(client, "apply_plan_change", {"change_id": edit["change_id"]})

    changes = tool(client, "get_plan_changes", {})["changes"]
    assert [c["status"] for c in changes[:4]] == ["applied", "applied", "rejected", "applied"]
    assert changes[0]["source"] == "hermes"
    assert changes[3]["summary"]["action"] == "import"


def test_record_threshold_test_sets_targets(client):
    result = tool(
        client,
        "record_threshold_test",
        {"lthr_bpm": 175, "tested_on": "2026-11-20", "lt_pace": "5:30"},
    )
    targets = result["heart_rate_targets"]
    assert (targets["easy_max_bpm"], targets["lt_pace"]) == (156, "5:30/km")
    assert tool(client, "get_training_status", {})["heart_rate_targets"] == targets

    rejected = call(
        client,
        "hermes-token",
        "tools/call",
        {"name": "record_threshold_test", "arguments": {"lthr_bpm": 40}},
    ).json()["result"]
    assert rejected["isError"]
