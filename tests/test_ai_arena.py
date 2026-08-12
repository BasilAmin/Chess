from __future__ import annotations

import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
