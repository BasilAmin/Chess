from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from chess_gantry.ai_arena import AIArena
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.openai_opponent import OpponentMove


class FakeProvider:
    def __init__(self):
        self.calls = []

    def choose_move(self, board, *, style="balanced"):
        self.calls.append((board.fen(), style))
        move = next(iter(board.legal_moves))
        return OpponentMove(
            uci=move.uci(),
            rationale="A legal move.",
            plan="Continue development.",
        )


class SequenceProvider:
    def __init__(self, moves):
        self.moves = list(moves)

    def choose_move(self, board, *, style="balanced"):
        move = self.moves.pop(0)
        return OpponentMove(
            uci=move,
            rationale="Sequence move.",
            plan="Continue.",
        )


class AIArenaTests(unittest.TestCase):
    def setUp(self):
        self.chatgpt = FakeProvider()
        self.claude = FakeProvider()
        self.arena = AIArena(self.chatgpt, self.claude)

    def tearDown(self):
        if self.arena.running():
            self.arena.stop()

    def wait_for_finish(self):
        deadline = time.monotonic() + 3
        while self.arena.running() and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_claude_and_chatgpt_alternate_legal_moves(self):
        self.arena.start(delay_s=0, max_plies=6)
        self.wait_for_finish()
        status = self.arena.status()
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["ply"], 6)
        self.assertEqual(
            [move["actor"] for move in status["history"]],
            ["chatgpt", "claude", "chatgpt", "claude", "chatgpt", "claude"],
        )
        self.assertEqual(len(self.chatgpt.calls), 3)
        self.assertEqual(len(self.claude.calls), 3)

    def test_side_assignment_can_be_reversed(self):
        self.arena.start(white="claude", black="chatgpt", delay_s=0, max_plies=2)
        self.wait_for_finish()
        history = self.arena.status()["history"]
        self.assertEqual([value["actor"] for value in history], ["claude", "chatgpt"])

    def test_invalid_provider_assignment_and_limits_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.arena.start(white="chatgpt", black="chatgpt")
        with self.assertRaises(ValidationError):
            self.arena.start(delay_s=-1)
        with self.assertRaises(ValidationError):
            self.arena.start(max_plies=0)

    def test_double_start_is_rejected_and_stop_is_clean(self):
        self.arena.start(delay_s=1, max_plies=20)
        with self.assertRaises(ConfigurationError):
            self.arena.start()
        status = self.arena.stop()
        self.assertIn(status["state"], {"stopped", "finished"})

    def test_physical_demo_executes_normal_moves_and_tracks_state(self):
        from chess_gantry.config import AppConfig

        with TemporaryDirectory() as directory:
            config = AppConfig.from_mapping(
                json.loads(
                    (
                        Path(__file__).resolve().parents[1] / "config.demo.json"
                    ).read_text()
                )
            )
            arena = AIArena(
                SequenceProvider(["e2e4"]),
                SequenceProvider(["e7e5"]),
                config=config,
                root=Path(directory),
                demo=True,
            )
            arena.start(delay_s=0, max_plies=2, physical=True)
            deadline = time.monotonic() + 4
            while arena.running() and time.monotonic() < deadline:
                time.sleep(0.01)
            status = arena.status()
            self.assertEqual(status["state"], "finished")
            self.assertEqual(status["ply"], 2)
            self.assertTrue(status["physical"])
            state = arena._service.store.load()
            self.assertEqual(state.pieces["white_pawn_e"].y, 3)
            self.assertEqual(state.pieces["black_pawn_e"].y, 4)

    def test_physical_capture_pauses_for_manual_confirmation_then_commits(self):
        from chess_gantry.config import AppConfig

        with TemporaryDirectory() as directory:
            config = AppConfig.from_mapping(
                json.loads(
                    (
                        Path(__file__).resolve().parents[1] / "config.demo.json"
                    ).read_text()
                )
            )
            arena = AIArena(
                SequenceProvider(["e2e4", "e4d5"]),
                SequenceProvider(["d7d5"]),
                config=config,
                root=Path(directory),
                demo=True,
            )
            arena.start(delay_s=0, max_plies=3, physical=True)
            deadline = time.monotonic() + 4
            while (
                arena.status()["state"] != "waiting_manual"
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            pending = arena.status()["manual_action"]
            self.assertEqual(pending["uci"], "e4d5")
            self.assertTrue(pending["capture"])
            arena.confirm_manual_action()
            deadline = time.monotonic() + 4
            while arena.running() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(arena.status()["state"], "finished")
            state = arena._service.store.load()
            self.assertEqual(state.pieces["white_pawn_e"].x, 3)
            self.assertEqual(state.pieces["white_pawn_e"].y, 4)
            self.assertEqual(state.pieces["black_pawn_d"].status, "captured")


if __name__ == "__main__":
    unittest.main()
