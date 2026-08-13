from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import time
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.game_replay import load_replay_game
from chess_gantry.station_game import STATION_CONFIRMATION, StationGame, qr_svg
from chess_gantry.station_game import STATION_RESUME_CONFIRMATION


ROOT = Path(__file__).resolve().parents[1]


class StationGameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = AppConfig.from_mapping(
            json.loads((ROOT / "config.demo.json").read_text())
        )
        self.station = StationGame(self.root, config, demo=True)

    def tearDown(self) -> None:
        if self.station.active():
            self.station.stop()
        self.temporary.cleanup()

    def create(self):
        return self.station.create(
            base_url="http://station.local:8000",
            confirmation=STATION_CONFIRMATION,
        )

    def tokens(self, created):
        return {
            color: url.rsplit("#", 1)[1] for color, url in created["join_urls"].items()
        }

    def wait_playing(self):
        deadline = time.monotonic() + 3
        while (
            self.station.admin_status()["state"] == "homing"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertEqual(self.station.admin_status()["state"], "playing")

    def test_two_scans_start_unlimited_game_and_execute_legal_moves(self) -> None:
        created = self.create()
        tokens = self.tokens(created)
        white = self.station.join(tokens["white"])
        self.assertEqual(white["state"], "waiting_players")
        self.station.join(tokens["black"])
        self.wait_playing()
        moved = self.station.move(tokens["white"], "e2e4")
        self.assertEqual(moved["turn"], "black")
        self.assertIsNone(moved["clock"])
        self.assertIsNone(moved["expires_at"])
        self.station.move(tokens["black"], "e7e5")
        reconnected = self.station.player_status(tokens["white"])
        self.assertEqual(
            [item["uci"] for item in reconnected["history"]], ["e2e4", "e7e5"]
        )
        self.assertEqual(reconnected["state"], "playing")

    def test_lost_response_retry_is_idempotent_and_never_replays_motion(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        first = self.station.move(tokens["white"], "e2e4", expected_ply=0)
        revision = self.station._mirror.service.store.load().revision
        retried = self.station.move(tokens["white"], "e2e4", expected_ply=0)
        self.assertEqual(retried["history"], first["history"])
        self.assertEqual(self.station._mirror.service.store.load().revision, revision)
        with self.assertRaisesRegex(ValidationError, "conflicts"):
            self.station.move(tokens["white"], "d2d4", expected_ply=0)

    def test_group_can_leave_then_next_group_gets_new_qr_tokens(self) -> None:
        first = self.create()
        first_tokens = self.tokens(first)
        self.station.join(first_tokens["white"])
        self.station.join(first_tokens["black"])
        self.wait_playing()
        left = self.station.leave(first_tokens["white"])
        self.assertEqual(left["state"], "finished")
        self.assertTrue(left["left"]["white"])
        second = self.create()
        second_tokens = self.tokens(second)
        self.assertNotEqual(first["game_id"], second["game_id"])
        self.assertNotEqual(first_tokens, second_tokens)
        with self.assertRaisesRegex(ValidationError, "invalid station seat"):
            self.station.join(first_tokens["black"])

    def test_leave_before_second_scan_finishes_game_and_allows_turnover(self) -> None:
        first = self.create()
        tokens = self.tokens(first)
        self.station.join(tokens["white"])
        status = self.station.leave(tokens["white"])
        self.assertEqual(status["result"], "0-1")
        self.assertEqual(self.create()["state"], "waiting_players")

    def test_station_reaches_checkmate_and_closes_connection(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        for color, move in (
            ("white", "f2f3"),
            ("black", "e7e5"),
            ("white", "g2g4"),
            ("black", "d8h4"),
        ):
            status = self.station.move(tokens[color], move)
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["result"], "0-1")
        self.assertIsNone(self.station._link)

    def test_station_mirrors_en_passant_and_castling_without_duration_limit(
        self,
    ) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        self.station._started_at = 1.0
        for color, move in (
            ("white", "e2e4"),
            ("black", "a7a6"),
            ("white", "e4e5"),
            ("black", "d7d5"),
            ("white", "e5d6"),
            ("black", "e7d6"),
            ("white", "g1f3"),
            ("black", "b8c6"),
            ("white", "f1e2"),
            ("black", "g8f6"),
            ("white", "e1g1"),
        ):
            status = self.station.move(tokens[color], move)
        self.assertEqual(status["state"], "playing")
        self.assertIsNone(status["clock"])
        self.assertIsNone(status["expires_at"])
        state = self.station._mirror.service.store.load()
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")
        self.assertEqual(state.pieces["white_king_e"].x, 6)
        self.assertEqual(state.pieces["white_rook_h"].x, 5)

    def test_full_opera_game_is_mirrored_to_checkmate_without_cutoff(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        game = load_replay_game(ROOT / "examples" / "replays" / "opera-game.pgn")
        for expected_ply, uci in enumerate(game.moves):
            color = "white" if expected_ply % 2 == 0 else "black"
            status = self.station.move(tokens[color], uci, expected_ply=expected_ply)
        self.assertEqual(len(status["history"]), 33)
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["result"], "1-0")
        self.assertFalse(self.station._mirror.journal_path.exists())

    def test_capture_promotion_uses_proxy_and_finishes_by_resignation(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        game = load_replay_game(ROOT / "examples" / "replays" / "capture-promotion.pgn")
        for expected_ply, uci in enumerate(game.moves):
            color = "white" if expected_ply % 2 == 0 else "black"
            self.station.move(tokens[color], uci, expected_ply=expected_ply)
        state = self.station._mirror.service.store.load()
        self.assertEqual(state.pieces["white_pawn_a"].x, 0)
        self.assertEqual(state.pieces["white_pawn_a"].y, 7)
        self.assertEqual(state.pieces["black_rook_a"].status, "captured")
        finished = self.station.resign(tokens["black"])
        self.assertEqual(finished["result"], "1-0")

    def test_wrong_turn_illegal_move_and_invalid_token_are_rejected(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        with self.assertRaisesRegex(ValidationError, "not your turn"):
            self.station.move(tokens["black"], "e7e5")
        with self.assertRaisesRegex(ValidationError, "illegal move"):
            self.station.move(tokens["white"], "e2e5")
        with self.assertRaisesRegex(ValidationError, "invalid station seat"):
            self.station.player_status("invalid")

    def test_tokens_are_only_in_fragment_urls_and_not_persisted_plaintext(self) -> None:
        created = self.create()
        tokens = self.tokens(created)
        for url in created["join_urls"].values():
            self.assertIn("/station/play#", url)
            self.assertNotIn("?token=", url)
        metadata = next((self.root / "data" / "station-games").glob("*/station.json"))
        raw = metadata.read_text()
        self.assertNotIn(tokens["white"], raw)
        self.assertNotIn(tokens["black"], raw)
        self.assertIn("token_sha256", raw)

    def test_resignation_finishes_without_waiting_or_timeout(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        status = self.station.resign(tokens["white"])
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["result"], "0-1")

    def test_draw_requires_both_players_and_finishes_cleanly(self) -> None:
        tokens = self.tokens(self.create())
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        offered = self.station.offer_draw(tokens["white"])
        self.assertEqual(offered["draw_offer"], "white")
        accepted = self.station.offer_draw(tokens["black"])
        self.assertEqual(accepted["state"], "finished")
        self.assertEqual(accepted["result"], "1/2-1/2")
        self.assertIsNone(self.station._link)

    def test_create_requires_exact_confirmation_and_one_active_game(self) -> None:
        with self.assertRaisesRegex(ValidationError, "STATION BOARD"):
            self.station.create(base_url="http://station", confirmation="yes")
        self.create()
        with self.assertRaisesRegex(ConfigurationError, "already active"):
            self.create()

    def test_pending_station_transaction_blocks_new_game(self) -> None:
        created = self.create()
        self.station.stop()
        game_id = created["game_id"]
        pending = self.root / "data" / "station-games" / game_id / "pending_move.json"
        pending.write_text("{}\n")
        with self.assertRaisesRegex(ConfigurationError, "pending station transaction"):
            self.create()

    def test_process_restart_restores_midgame_without_motion_until_confirmed(
        self,
    ) -> None:
        created = self.create()
        tokens = self.tokens(created)
        self.station.join(tokens["white"])
        self.station.join(tokens["black"])
        self.wait_playing()
        self.station.move(tokens["white"], "e2e4", expected_ply=0)
        self.station._close_link()
        restarted = StationGame(self.root, self.station.config, demo=True)
        self.station = restarted
        status = restarted.admin_status()
        self.assertEqual(status["state"], "resume_required")
        self.assertEqual(status["history"][0]["uci"], "e2e4")
        self.assertIsNone(restarted._link)
        self.assertEqual(restarted.player_status(tokens["black"])["turn"], "black")
        with self.assertRaisesRegex(ValidationError, "MATCHES SAVED"):
            restarted.resume("yes")
        restarted.resume(STATION_RESUME_CONFIRMATION)
        self.wait_playing()
        restarted.move(tokens["black"], "e7e5", expected_ply=1)

    def test_qr_is_local_svg_and_contains_no_remote_dependency(self) -> None:
        svg = qr_svg("http://station.local/station/play#secret")
        self.assertIn(b"<svg", svg)
        self.assertNotIn(b"http://api", svg)
