from __future__ import annotations

import json
import unittest

from chess_gantry.errors import ValidationError
from chess_gantry.lichess_open import create_open_challenge


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self):
        return json.dumps(self.value).encode()


class OpenChallengeTests(unittest.TestCase):
    def test_anonymous_challenge_returns_fixed_color_lichess_urls(self):
        captured = {}

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response(
                {
                    "id": "OyJi5QSy",
                    "url": "https://lichess.org/OyJi5QSy",
                    "urlWhite": "https://lichess.org/OyJi5QSy?color=white",
                    "urlBlack": "https://lichess.org/OyJi5QSy?color=black",
                }
            )

        challenge = create_open_challenge(opener=opener)
        self.assertEqual(challenge.challenge_id, "OyJi5QSy")
        self.assertIn("color=white", challenge.white_url)
        self.assertIn("color=black", challenge.black_url)
        self.assertNotIn("Authorization", captured["request"].headers)
        body = captured["request"].data.decode()
        self.assertIn("rated=false", body)
        self.assertIn("variant=standard", body)
        self.assertNotIn("clock.limit", body)

    def test_invalid_non_lichess_urls_are_rejected(self):
        def opener(request, timeout):
            return Response(
                {
                    "id": "OyJi5QSy",
                    "url": "https://evil.example/game",
                    "urlWhite": "https://evil.example/white",
                    "urlBlack": "https://evil.example/black",
                }
            )

        with self.assertRaisesRegex(ValidationError, "invalid white URL"):
            create_open_challenge(opener=opener)
