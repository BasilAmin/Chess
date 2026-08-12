from __future__ import annotations

from types import SimpleNamespace
import unittest

import chess

from chess_gantry.errors import ValidationError
from chess_gantry.openai_opponent import OpponentMove, SolChessOpponent


class FakeResponses:
    def __init__(self, move):
        self.move = move
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status="completed", output_parsed=self.move)


class OpenAIOpponentTests(unittest.TestCase):
    def test_move_is_selected_from_server_legal_allowlist(self):
        responses = FakeResponses(
            OpponentMove(
                uci="e2e4", rationale="Claims the center.", plan="Develop quickly."
            )
        )
        opponent = SolChessOpponent(client=SimpleNamespace(responses=responses))
        result = opponent.choose_move(chess.Board(), style="aggressive")
        self.assertEqual(result.uci, "e2e4")
        call = responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertIs(call["text_format"], OpponentMove)
        self.assertFalse(call["store"])
        prompt = call["input"][1]["content"]
        self.assertIn("Style: aggressive", prompt)
        self.assertIn("e2e4", prompt)

    def test_illegal_model_move_is_rejected_even_if_schema_is_valid(self):
        responses = FakeResponses(
            OpponentMove(uci="e2e5", rationale="Invalid.", plan="Invalid.")
        )
        opponent = SolChessOpponent(client=SimpleNamespace(responses=responses))
        with self.assertRaisesRegex(ValidationError, "allowlist"):
            opponent.choose_move(chess.Board())

    def test_game_over_position_is_rejected_without_api_call(self):
        board = chess.Board("7k/5Q2/7K/8/8/8/8/8 b - - 0 1")
        responses = FakeResponses(None)
        opponent = SolChessOpponent(client=SimpleNamespace(responses=responses))
        with self.assertRaisesRegex(ValidationError, "game is over"):
            opponent.choose_move(board)
        self.assertEqual(responses.calls, [])


if __name__ == "__main__":
    unittest.main()
