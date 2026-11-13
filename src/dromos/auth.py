"""Single-user OAuth for the MCP endpoint.

claude.ai registers itself (dynamic client registration), sends the owner to /oauth/login,
and the owner approves with DROMOS_OWNER_PASSWORD. Hermes skips OAuth and sends
DROMOS_API_TOKEN as a bearer token. Issued tokens are stored as SHA-256 hashes, so a leaked
database doesn't leak working tokens. With neither secret set, nothing can authenticate.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from dromos import db

ACCESS_TOKEN_TTL = 60 * 60
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30
AUTH_CODE_TTL = 5 * 60
LOGIN_REQUEST_TTL = 10 * 60
MAX_FAILED_LOGINS = 5
FAILED_LOGIN_WINDOW = 15 * 60
LOGIN_PATH = "/oauth/login"
HERMES_CLIENT_ID = "hermes"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _matches(given: str, expected: str | None) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


@dataclass
class PendingLogin:
    client: OAuthClientInformationFull
    params: AuthorizationParams
    expires_at: float


class LoginError(Exception):
    pass


class DromosOAuthProvider:
    """OAuthAuthorizationServerProvider backed by SQLite (clients, tokens) and memory
    (login requests and authorization codes, which only live for minutes)."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._pending: dict[str, PendingLogin] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failed_logins: deque[float] = deque()

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT info FROM oauth_clients WHERE client_id = ?", (client_id,)
            ).fetchone()
        finally:
            conn.close()
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO oauth_clients (client_id, info, created_at) "
                    "VALUES (?, ?, ?)",
                    (client_info.client_id, client_info.model_dump_json(), _now()),
                )
        finally:
            conn.close()

    # Authorization: send the owner to the login page, then back to the client with a code

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        self._expire()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = PendingLogin(client, params, time.time() + LOGIN_REQUEST_TTL)
        return f"{self.base_url}{LOGIN_PATH}?request={request_id}"

    def pending_client_name(self, request_id: str) -> str | None:
        self._expire()
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        return pending.client.client_name or pending.client.client_id

    def complete_login(self, request_id: str, password: str) -> str:
        """Check the owner password and return the client redirect URL carrying a code."""
        self._expire()
        if len(self._failed_logins) >= MAX_FAILED_LOGINS:
            raise LoginError("Too many failed attempts. Try again in 15 minutes.")
        pending = self._pending.get(request_id)
        if pending is None:
            raise LoginError("This login request has expired. Start again from your app.")
        if not _matches(password, os.getenv("DROMOS_OWNER_PASSWORD")):
            self._failed_logins.append(time.time())
            raise LoginError("Wrong password.")

        del self._pending[request_id]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=pending.client.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject="owner",
        )
        return construct_redirect_uri(
            str(pending.params.redirect_uri), code=code, state=pending.params.state
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        self._expire()
        code = self._codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # Tokens

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._token_row(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._revoke(refresh_token.token)  # rotate: the old pair stops working
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if _matches(token, os.getenv("DROMOS_API_TOKEN")):
            return AccessToken(token=token, client_id=HERMES_CLIENT_ID, scopes=[], subject="owner")
        row = self._token_row(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=json.loads(row["scopes"]),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._revoke(token.token)

    # Internals

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        grant_id = secrets.token_hex(8)
        now = int(time.time())
        conn = db.connect()
        try:
            with conn:
                conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (now,))
                for token, kind, ttl in (
                    (access, "access", ACCESS_TOKEN_TTL),
                    (refresh, "refresh", REFRESH_TOKEN_TTL),
                ):
                    conn.execute(
                        """
                        INSERT INTO oauth_tokens (token_hash, kind, grant_id, client_id, scopes,
                                                  resource, expires_at, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _hash(token),
                            kind,
                            grant_id,
                            client_id,
                            json.dumps(scopes),
                            resource,
                            now + ttl,
                            _now(),
                        ),
                    )
        finally:
            conn.close()
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    def _token_row(self, token: str, kind: str):
        conn = db.connect()
        try:
            return conn.execute(
                "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                (_hash(token), kind, int(time.time())),
            ).fetchone()
        finally:
            conn.close()

    def _revoke(self, token: str) -> None:
        """Revoke a token and the other half of its access/refresh pair."""
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM oauth_tokens WHERE grant_id IN "
                    "(SELECT grant_id FROM oauth_tokens WHERE token_hash = ?)",
                    (_hash(token),),
                )
        finally:
            conn.close()

    def _expire(self) -> None:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        while self._failed_logins and self._failed_logins[0] < now - FAILED_LOGIN_WINDOW:
            self._failed_logins.popleft()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
