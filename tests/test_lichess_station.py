from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.lichess_open import OpenChallenge
from chess_gantry.lichess_station import LichessStation, STATION_CONFIRMATION
from tests.test_lichess_mirror import FakeClient, pgn


ROOT = Path(__file__).resolve().parents[1]


class LichessStationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = AppConfig.from_mapping(
            json.loads((ROOT / "config.demo.json").read_text())
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_token_free_station_mirrors_long_update_and_finishes(self):
        moves = (
            "e2e4",
            "e7e5",
            "g1f3",
            "b8c6",
            "f1e2",
            "g8f6",
            "e1g1",
            "f8e7",
            "d2d3",
            "e8g8",
            "b1c3",
            "d7d6",
        )
        challenge = OpenChallenge(
            "game1234",
            "https://lichess.org/game1234?color=white",
            "https://lichess.org/game1234?color=black",
            "https://lichess.org/game1234",
        )
        client = FakeClient(
            [{"type": "gameState", "moves": " ".join(moves), "status": "mate"}]
        )
        station = LichessStation(
            self.root,
            self.config,
            demo=True,
            challenge_factory=lambda **kwargs: challenge,
            client=client,
            pgn_fetcher=lambda *args, **kwargs: pgn(),
            sleep=lambda value: None,
        )
        created = station.create(base_url="http://unused", confirmation=STATION_CONFIRMATION)
        self.assertEqual(created["join_urls"]["white"], challenge.white_url)
        deadline = time.monotonic() + 5
        while station.admin_status()["state"] not in {"finished", "failed"} and time.monotonic() < deadline:
            time.sleep(0.01)
        status = station.admin_status()
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["history"], list(moves))
        self.assertEqual(len(status["history"]), 12)

    def test_finished_station_can_create_fresh_qr_pair(self):
        challenges = iter(
            (
                OpenChallenge(
                    "game1234",
                    "https://lichess.org/game1234?color=white",
                    "https://lichess.org/game1234?color=black",
                    "https://lichess.org/game1234",
                ),
                OpenChallenge(
                    "game5678",
                    "https://lichess.org/game5678?color=white",
                    "https://lichess.org/game5678?color=black",
                    "https://lichess.org/game5678",
                ),
            )
        )
        station = LichessStation(
            self.root,
            self.config,
            demo=True,
            challenge_factory=lambda **kwargs: next(challenges),
            client=FakeClient([]),
            pgn_fetcher=lambda *args, **kwargs: pgn(),
            sleep=lambda value: time.sleep(0.01),
        )
        first = station.create(base_url="unused", confirmation=STATION_CONFIRMATION)
        station.stop()
        second = station.create(base_url="unused", confirmation=STATION_CONFIRMATION)
        self.assertNotEqual(first["game_id"], second["game_id"])
        self.assertNotEqual(first["join_urls"], second["join_urls"])
        station.stop()
