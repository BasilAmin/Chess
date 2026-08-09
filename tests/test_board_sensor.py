from __future__ import annotations

from pathlib import Path
import json
import time
import unittest

import chess

from chess_gantry.board_sensor import (
    SyntheticBoardSensor,
    board_matrix,
    matrix_after_lift,
    validate_matrix,
)
from chess_gantry.errors import ValidationError


class SensorTests(unittest.TestCase):
    def wait_for(self, sensor: SyntheticBoardSensor, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = sensor.status()
            if predicate(status):
                return status
            time.sleep(0.005)
        self.fail(f"sensor condition did not arrive: {sensor.status()}")

    def test_standard_matrix_has_32_occupied_squares(self) -> None:
        matrix = board_matrix(chess.Board())
        self.assertEqual(sum(sum(row) for row in matrix), 32)
        self.assertEqual(matrix[0], (1,) * 8)
        self.assertEqual(matrix[7], (1,) * 8)

    def test_lift_then_drop_detects_legal_move_and_tracks_piece(self) -> None:
        sensor = SyntheticBoardSensor(sample_hz=300, stable_samples=2)
        try:
            board = chess.Board()
            sensor.set_matrix(matrix_after_lift(board, "e2e4"))
            lifted = self.wait_for(sensor, lambda value: value["state"] == "waiting")
            self.assertIsNone(lifted["last_move"])
            board.push_uci("e2e4")
            sensor.set_matrix(board_matrix(board))
            moved = self.wait_for(sensor, lambda value: value["last_move"] == "e2e4")
            self.assertEqual(moved["turn"], "black")
            pawn = next(piece for piece in moved["pieces"] if piece["square"] == "e4")
            self.assertEqual(pawn["type"], "pawn")
            self.assertEqual(pawn["color"], "white")
        finally:
            sensor.close()

    def test_capture_is_inferred_from_occupancy_and_legal_position(self) -> None:
        sensor = SyntheticBoardSensor(sample_hz=300, stable_samples=2)
        try:
            board = chess.Board()
            for uci in ("e2e4", "d7d5"):
                board.push_uci(uci)
                sensor.set_matrix(board_matrix(board))
                self.wait_for(
                    sensor, lambda value, target=uci: value["last_move"] == target
                )
            board.push_uci("e4d5")
            sensor.set_matrix(board_matrix(board))
            result = self.wait_for(sensor, lambda value: value["last_move"] == "e4d5")
            self.assertEqual(len(result["pieces"]), 31)
            pawn = next(piece for piece in result["pieces"] if piece["square"] == "d5")
            self.assertEqual(pawn["symbol"], "P")
        finally:
            sensor.close()

    def test_dataset_simulates_a_complete_move(self) -> None:
        sensor = SyntheticBoardSensor(sample_hz=300, stable_samples=2)
        try:
            sensor.apply_dataset("g1f3")
            result = self.wait_for(sensor, lambda value: value["last_move"] == "g1f3")
            knight = next(
                piece for piece in result["pieces"] if piece["square"] == "f3"
            )
            self.assertEqual(knight["type"], "knight")
        finally:
            sensor.close()

    def test_lichess_submitter_receives_detected_move(self) -> None:
        submitted = []
        sensor = SyntheticBoardSensor(
            sample_hz=300,
            stable_samples=2,
            move_submitter=lambda game_id, uci: submitted.append((game_id, uci)),
        )
        try:
            sensor.configure_lichess("game1234", True)
            sensor.apply_dataset("d2d4")
            self.wait_for(sensor, lambda value: value["last_move"] == "d2d4")
            self.assertEqual(submitted, [("game1234", "d2d4")])
        finally:
            sensor.close()

    def test_sampler_runs_close_to_300_hz(self) -> None:
        sensor = SyntheticBoardSensor(sample_hz=300, stable_samples=2)
        try:
            time.sleep(0.15)
            rate = sensor.status()["sample_hz_observed"]
            self.assertGreater(rate, 200)
            self.assertLess(rate, 400)
        finally:
            sensor.close()

    def test_matrix_validation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "8 rows"):
            validate_matrix([])
        invalid = [[0] * 8 for _ in range(8)]
        invalid[3][2] = 2
        with self.assertRaisesRegex(ValidationError, "must be 0 or 1"):
            validate_matrix(invalid)

    def test_example_matrix_datasets_match_expected_move_sequence(self) -> None:
        root = Path(__file__).resolve().parents[1] / "examples"
        standard = validate_matrix(
            json.loads((root / "sensor_matrix_standard.json").read_text())
        )
        lifted = validate_matrix(
            json.loads((root / "sensor_matrix_e2_lifted.json").read_text())
        )
        moved = validate_matrix(
            json.loads((root / "sensor_matrix_e2e4.json").read_text())
        )
        board = chess.Board()
        self.assertEqual(standard, board_matrix(board))
        self.assertEqual(lifted, matrix_after_lift(board, "e2e4"))
        board.push_uci("e2e4")
        self.assertEqual(moved, board_matrix(board))


if __name__ == "__main__":
    unittest.main()
