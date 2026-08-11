from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest

from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.lichess_oauth import LichessOAuth


class FakeHTTP:
    def __init__(self, *, scopes="board:play"):
        self.scopes = scopes
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/token"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "access_token": "lio_test_token",
                    "expires_in": 31536000,
                },
            )
        if url.endswith("/api/token/test"):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "lio_test_token": {
                        "userId": "player",
                        "scopes": self.scopes,
                    }
                },
            )
        raise AssertionError(url)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"id": "player", "username": "Player"},
        )


class LichessOAuthTests(unittest.TestCase):
    def test_begin_uses_official_pkce_board_scope_and_loopback_callback(self):
        oauth = LichessOAuth(http=FakeHTTP())
        url = oauth.begin("http://127.0.0.1:8000/auth/lichess/callback")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).path, "/oauth")
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["board:play"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertGreater(len(query["code_challenge"][0]), 30)
        self.assertNotIn("code_verifier", query)

    def test_non_loopback_callback_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "loopback"):
            LichessOAuth(http=FakeHTTP()).begin(
                "http://192.168.1.5:8000/auth/lichess/callback"
            )

    def test_code_exchange_validates_account_and_scope(self):
        http = FakeHTTP()
        oauth = LichessOAuth(http=http)
        url = oauth.begin("http://127.0.0.1:8000/auth/lichess/callback")
        state = parse_qs(urlsplit(url).query)["state"][0]
        status = oauth.complete(code="code", state=state)
        self.assertTrue(status["connected"])
        self.assertEqual(status["username"], "Player")
        self.assertEqual(status["scopes"], ["board:play"])
        exchange = next(call for call in http.calls if call[1].endswith("/api/token"))
        self.assertIn("code_verifier", exchange[2]["data"])

    def test_state_is_single_use(self):
        oauth = LichessOAuth(http=FakeHTTP())
        url = oauth.begin("http://localhost:8000/auth/lichess/callback")
        state = parse_qs(urlsplit(url).query)["state"][0]
        oauth.complete(code="code", state=state)
        with self.assertRaisesRegex(ValidationError, "already used"):
            oauth.complete(code="code", state=state)

    def test_missing_board_scope_is_rejected(self):
        oauth = LichessOAuth(http=FakeHTTP(scopes="challenge:read"))
        url = oauth.begin("http://localhost:8000/auth/lichess/callback")
        state = parse_qs(urlsplit(url).query)["state"][0]
        with self.assertRaisesRegex(ConfigurationError, "board:play"):
            oauth.complete(code="code", state=state)

    def test_disconnect_forgets_token(self):
        oauth = LichessOAuth(http=FakeHTTP())
        url = oauth.begin("http://localhost:8000/auth/lichess/callback")
        state = parse_qs(urlsplit(url).query)["state"][0]
        oauth.complete(code="code", state=state)
        oauth.disconnect()
        self.assertFalse(oauth.status()["connected"])
        self.assertIsNone(oauth.token())


if __name__ == "__main__":
    unittest.main()
