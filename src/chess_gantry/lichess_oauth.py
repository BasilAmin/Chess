from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

from .errors import ConfigurationError, ValidationError


class LichessOAuth:
    def __init__(
        self, *, client_id: str = "relay-chess-gantry", http: Any = None
    ) -> None:
        self.client_id = client_id
        self._http = http
        self._lock = threading.RLock()
        self._pending: dict[str, tuple[str, str, float]] = {}
        self._token = os.environ.get("LICHESS_TOKEN", "").strip() or None
        self._account: Optional[dict[str, Any]] = None
        self._scopes: tuple[str, ...] = ()
        self._expires_at: Optional[float] = None

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=15.0, follow_redirects=False)
        return self._http

    @staticmethod
    def _challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def begin(self, redirect_uri: str) -> str:
        if not redirect_uri.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValidationError("Lichess OAuth callback must use loopback HTTP")
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._pending[state] = (verifier, redirect_uri, time.monotonic() + 600)
        return "https://lichess.org/oauth?" + urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": "board:play",
                "code_challenge_method": "S256",
                "code_challenge": self._challenge(verifier),
                "state": state,
            }
        )

    def complete(self, *, code: str, state: str) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None:
            raise ValidationError(
                "Lichess OAuth state is missing, expired, or already used"
            )
        verifier, redirect_uri, expires = pending
        if time.monotonic() > expires:
            raise ValidationError("Lichess OAuth request expired; start again")
        response = self._client().post(
            "https://lichess.org/api/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise ConfigurationError(
                f"Lichess token exchange failed with HTTP {response.status_code}"
            )
        payload = response.json()
        token = str(payload.get("access_token", "")).strip()
        if not token:
            raise ConfigurationError("Lichess token exchange returned no access token")
        expires_in = int(payload.get("expires_in", 0) or 0)
        with self._lock:
            self._token = token
            self._expires_at = time.time() + expires_in if expires_in else None
        return self.validate()

    def validate(self) -> dict[str, Any]:
        token = self.token(optional=False)
        account_response = self._client().get(
            "https://lichess.org/api/account",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if account_response.status_code != 200:
            raise ConfigurationError(
                f"Lichess token validation failed with HTTP {account_response.status_code}"
            )
        account = account_response.json()
        test_response = self._client().post(
            "https://lichess.org/api/token/test",
            content=token,
            headers={"Content-Type": "text/plain", "Accept": "application/json"},
        )
        scopes: tuple[str, ...] = ()
        if test_response.status_code == 200:
            tested = test_response.json().get(token)
            if tested:
                scopes = tuple(str(tested.get("scopes", "")).split())
        if scopes and "board:play" not in scopes:
            raise ConfigurationError("Lichess token lacks required board:play scope")
        with self._lock:
            self._account = account
            self._scopes = scopes or ("board:play",)
        return self.status()

    def token(self, *, optional: bool = True) -> Optional[str]:
        with self._lock:
            if self._expires_at is not None and time.time() >= self._expires_at:
                self._token = None
                self._account = None
            token = self._token
        if token is None and not optional:
            raise ConfigurationError("connect a Lichess account with board:play access")
        return token

    def disconnect(self) -> None:
        with self._lock:
            self._token = None
            self._account = None
            self._scopes = ()
            self._expires_at = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connected": self._token is not None,
                "username": (
                    self._account.get("username") or self._account.get("id")
                    if self._account
                    else None
                ),
                "scopes": list(self._scopes),
                "expires_at": self._expires_at,
            }
