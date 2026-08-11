from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import chess

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError
from chess_gantry.game_coordinator import GameCoordinator, pgn_uci_moves


ROOT = Path(__file__).resolve().parents[1]


class FakeVision:
    def __init__(self):
        self.handler = None
        self.expected = []
        self.paused = []

    def start_game(self, fen=None):
        return {}

    def set_move_handler(self, handler):
        self.handler = handler

    def apply_expected_move(self, uci):
        self.expected.append(uci)
        return {}

    def pause_inference(self, paused):
        self.paused.append(paused)

    def status(self):
        return {
            "status": "complete",
            "stable_observations": 2,
            "matches_standard_position": True,
        }


class FakeBoardAPI:
    def __init__(self):
        self.moves = []

    def make_move(self, game_id, uci):
        self.moves.append((game_id, uci))

    def stream_game_state(self, game_id):
        yield {
            "type": "gameFull",
            "state": {"moves": "", "status": "started"},
        }
        yield {"type": "gameState", "moves": "e2e4", "status": "started"}


class FakeClient:
    def __init__(self):
        self.board = FakeBoardAPI()


def pgn(*moves):
    board = chess.Board()
    result = []
    for uci in moves:
        move = chess.Move.from_uci(uci)
        result.append(board.san(move))
        board.push(move)
    return (
        '[Result "*"]\n\n'
        + " ".join(
            (f"{index // 2 + 1}. " if index % 2 == 0 else "") + san
            for index, san in enumerate(result)
        )
        + " *"
    )


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        raw = json.loads((ROOT / "config.demo.json").read_text())
        self.config = AppConfig.from_mapping(raw)
        self.vision = FakeVision()
        self.client = FakeClient()
        self.coordinator = GameCoordinator(
            self.root,
            self.config,
            self.vision,
            demo=True,
            client_factory=lambda token: self.client,
        )

    def tearDown(self):
        if self.coordinator.running():
            self.coordinator.stop()
        self.temporary.cleanup()

    def test_pgn_parser_returns_uci_sequence(self):
        self.assertEqual(pgn_uci_moves(pgn("e2e4", "e7e5")), ("e2e4", "e7e5"))

    def test_local_camera_move_updates_state_without_gantry_execution(self):
        self.coordinator.start(
            mode="local", game_id=None, local_color=None, confirm_motion=False
        )
        self.coordinator.on_camera_move("e2e4")
        status = self.coordinator.status()["status"]
        self.assertEqual(status["confirmed_ply"], 1)
        self.assertEqual(status["executed_count"], 0)
        board = chess.Board(status["fen"])
        self.assertEqual(board.piece_at(chess.E4).symbol(), "P")

    def test_camera_player_can_register_manual_capture_without_gantry_storage(self):
        self.coordinator.start(
            mode="local", game_id=None, local_color=None, confirm_motion=False
        )
        for uci in ("e2e4", "d7d5", "e4d5"):
            self.coordinator.on_camera_move(uci)
        status = self.coordinator.status()["status"]
        self.assertEqual(status["confirmed_ply"], 3)
        self.assertEqual(status["executed_count"], 0)
        state = self.coordinator._service.store.load()
        self.assertEqual(state.pieces["white_pawn_e"].board_position.x, 3)
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")

    def test_camera_move_submits_once_and_echo_is_not_executed(self):
        self.coordinator._mode = "lichess"
        self.coordinator._game_id = "game1234"
        self.coordinator._local_color = "white"
        self.coordinator._client = self.client
        self.coordinator._service = self.coordinator._new_service("game1234")
        self.coordinator._board = chess.Board()
        self.coordinator.on_camera_move("e2e4")
        self.assertEqual(self.client.board.moves, [("game1234", "e2e4")])
        self.assertEqual(self.coordinator.status()["status"]["executed_count"], 0)
        self.coordinator._pending_echo = None
        self.assertEqual(self.coordinator._confirmed_ply, 1)

    def test_remote_move_generates_physical_demo_plan(self):
        self.coordinator._mode = "mirror"
        self.coordinator._game_id = "game1234"
        self.coordinator._service = self.coordinator._new_service("game1234")
        self.coordinator._board = chess.Board()
        self.coordinator._execute_remote("e2e4")
        status = self.coordinator.status()["status"]
        self.assertEqual(status["executed_count"], 1)
        self.assertEqual(status["confirmed_ply"], 1)
        self.assertEqual(self.vision.expected, ["e2e4"])
        state = self.coordinator._service.store.load()
        self.assertEqual(state.pieces["white_pawn_e"].y, 3)

    def test_authenticated_board_stream_executes_new_remote_move(self):
        self.coordinator._mode = "mirror"
        self.coordinator._game_id = "game1234"
        self.coordinator._client = self.client
        self.coordinator._service = self.coordinator._new_service("game1234")
        self.coordinator._board = chess.Board()
        self.coordinator._follow_lichess()
        self.assertEqual(self.coordinator._confirmed_ply, 1)
        self.assertEqual(self.coordinator._executed, 1)
        self.assertEqual(self.vision.expected, ["e2e4"])

    def test_board_stream_rejects_game_that_already_has_moves(self):
        self.client.board.stream_game_state = lambda game_id: iter(
            [
                {
                    "type": "gameFull",
                    "state": {"moves": "e2e4", "status": "started"},
                }
            ]
        )
        self.coordinator._mode = "mirror"
        self.coordinator._game_id = "game1234"
        self.coordinator._client = self.client
        self.coordinator._service = self.coordinator._new_service("game1234")
        self.coordinator._board = chess.Board()
        with self.assertRaisesRegex(ConfigurationError, "before the first"):
            self.coordinator._follow_lichess()

    def test_selected_serial_port_and_baud_are_preserved_for_game(self):
        self.coordinator._token_provider = lambda: "token"
        self.coordinator.start(
            mode="mirror",
            game_id="game1234",
            local_color=None,
            confirm_motion=True,
            serial_port="/dev/ttyUSB9",
            serial_baudrate=250000,
        )
        self.assertEqual(self.coordinator._serial_settings.port, "/dev/ttyUSB9")
        self.assertEqual(self.coordinator._serial_settings.baudrate, 250000)
        self.assertEqual(self.coordinator._serial_settings.fallback_baudrates, ())

    def test_capture_is_blocked_without_storage(self):
        self.coordinator._mode = "mirror"
        self.coordinator._game_id = "game1234"
        self.coordinator._service = self.coordinator._new_service("game1234")
        self.coordinator._board = chess.Board()
        for uci in ("e2e4", "d7d5"):
            self.coordinator._execute_remote(uci)
        with self.assertRaisesRegex(ConfigurationError, "capture storage"):
            self.coordinator._execute_remote("e4d5")

    def test_global_pending_transaction_blocks_game_start(self):
        (self.root / "data" / "pending_move.json").write_text("{}")
        with self.assertRaisesRegex(ConfigurationError, "reconcile"):
            self.coordinator.start(
                mode="local", game_id=None, local_color=None, confirm_motion=False
            )

    def test_camera_game_rejects_unstable_or_misoriented_starting_board(self):
        self.vision.status = lambda: {
            "status": "complete",
            "stable_observations": 2,
            "matches_standard_position": False,
        }
        with self.assertRaisesRegex(ConfigurationError, "standard starting"):
            self.coordinator.start(
                mode="local", game_id=None, local_color=None, confirm_motion=False
            )


if __name__ == "__main__":
    unittest.main()
