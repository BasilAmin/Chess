from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import base64
import unittest

import chess
from pydantic import ValidationError as PydanticValidationError

from chess_gantry.errors import ValidationError
from chess_gantry.vision import (
    BoardTranscription,
    SolTranscriber,
    SolVisionManager,
    board_payload,
    board_symbols,
    canonical_rows,
    image_rows_to_board,
    image_cell_to_square,
    infer_legal_move,
    rotate_image_cell,
    square_to_image_cell,
    validate_calibration,
    warp_board_frame,
)


def transcription(rows, status="complete"):
    confidence = {
        "board_detection": "high",
        "grid_mapping": "high",
        "piece_recognition": "high" if status == "complete" else "medium",
    }
    return BoardTranscription.model_validate(
        {
            "status": status,
            "confidence": confidence,
            "rows": {f"row_{index + 1}": row for index, row in enumerate(rows)},
            "problems": [],
        }
    )


def board_rows(board):
    return tuple(
        "".join(
            (
                board.piece_at(chess.square(file_index, rank)).symbol()
                if board.piece_at(chess.square(file_index, rank))
                else "."
            )
            for file_index in range(8)
        )
        for rank in range(7, -1, -1)
    )


class FakeResponses:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            status="completed",
            output_parsed=self.result,
        )


class VisionTests(unittest.TestCase):
    def test_complete_schema_rejects_unknowns_and_low_confidence(self) -> None:
        rows = list(board_rows(chess.Board()))
        rows[0] = "?" + rows[0][1:]
        with self.assertRaises(PydanticValidationError):
            transcription(tuple(rows))
        with self.assertRaises(PydanticValidationError):
            BoardTranscription.model_validate(
                {
                    "status": "complete",
                    "confidence": {
                        "board_detection": "high",
                        "grid_mapping": "high",
                        "piece_recognition": "low",
                    },
                    "rows": {
                        f"row_{index + 1}": row
                        for index, row in enumerate(board_rows(chess.Board()))
                    },
                    "problems": [],
                }
            )

    def test_partial_schema_allows_tentative_symbols_without_accepting_move(
        self,
    ) -> None:
        rows = board_rows(chess.Board())
        value = BoardTranscription.model_validate(
            {
                "status": "partial",
                "confidence": {
                    "board_detection": "high",
                    "grid_mapping": "medium",
                    "piece_recognition": "low",
                },
                "rows": {f"row_{index + 1}": row for index, row in enumerate(rows)},
                "problems": ["ambiguous_pieces"],
            }
        )
        self.assertEqual(value.status, "partial")
        moves = []
        manager = SolVisionManager(transcriber=object(), move_handler=moves.append)
        try:
            manager.start_game()
            manager._accept(value)
            manager._accept(value)
            self.assertEqual(moves, [])
        finally:
            manager.close()

    def test_orientation_maps_image_rows_to_algebraic_squares(self) -> None:
        rows = board_rows(chess.Board())
        white = image_rows_to_board(rows, "white_bottom")
        self.assertEqual(white[chess.A1], "R")
        self.assertEqual(white[chess.H8], "r")
        black = image_rows_to_board(rows, "black_bottom")
        self.assertEqual(black[chess.H8], "R")

    def test_all_64_image_cells_round_trip_for_both_orientations(self) -> None:
        for orientation in ("white_bottom", "black_bottom"):
            seen = set()
            for row in range(8):
                for column in range(8):
                    square = image_cell_to_square(row, column, orientation)
                    self.assertEqual(
                        square_to_image_cell(square, orientation), (row, column)
                    )
                    seen.add(square)
            self.assertEqual(seen, set(range(64)))

    def test_all_four_rotations_are_bijective_over_64_cells(self) -> None:
        for rotation in (0, 90, 180, 270):
            mapped = {
                rotate_image_cell(row, column, rotation)
                for row in range(8)
                for column in range(8)
            }
            self.assertEqual(
                mapped, {(row, column) for row in range(8) for column in range(8)}
            )
        self.assertEqual(rotate_image_cell(0, 0, 90), (0, 7))
        self.assertEqual(rotate_image_cell(0, 0, 180), (7, 7))
        self.assertEqual(rotate_image_cell(0, 0, 270), (7, 0))

    def test_canonical_rows_are_identical_for_either_camera_side(self) -> None:
        white_rows = board_rows(chess.Board())
        self.assertEqual(canonical_rows(white_rows, "white_bottom"), white_rows)
        black_image_rows = tuple(row[::-1] for row in white_rows[::-1])
        self.assertEqual(canonical_rows(black_image_rows, "black_bottom"), white_rows)

    def test_image_cell_mapping_rejects_invalid_coordinates(self) -> None:
        with self.assertRaisesRegex(ValidationError, "inside"):
            image_cell_to_square(8, 0, "white_bottom")
        with self.assertRaisesRegex(ValidationError, "orientation"):
            image_cell_to_square(0, 0, "sideways")

    def test_one_legal_changed_position_infers_move(self) -> None:
        board = chess.Board()
        moved = board.copy()
        moved.push_uci("e2e4")
        observed = image_rows_to_board(board_rows(moved))
        self.assertEqual(infer_legal_move(board, observed).uci(), "e2e4")
        self.assertIsNone(infer_legal_move(board, board_symbols(board)))

    def test_invalid_position_is_not_repaired_by_legality(self) -> None:
        board = chess.Board()
        observed = board_symbols(board)
        observed[chess.E2] = "."
        with self.assertRaisesRegex(ValidationError, "exactly one legal move"):
            infer_legal_move(board, observed)

    def test_piece_payload_lists_every_detected_piece(self) -> None:
        payload = board_payload(board_rows(chess.Board()), "white_bottom")
        self.assertEqual(len(payload), 32)
        self.assertIn(
            {
                "square": "e1",
                "symbol": "K",
                "color": "white",
                "type": "king",
                "grid": {"x": 4, "y": 0},
            },
            payload,
        )

    def test_piece_payload_uses_configured_machine_centers(self) -> None:
        import json
        from pathlib import Path
        from chess_gantry.config import AppConfig

        config = AppConfig.from_mapping(
            json.loads(
                (Path(__file__).resolve().parents[1] / "config.json").read_text()
            )
        )
        payload = {
            value["square"]: value
            for value in board_payload(
                board_rows(chess.Board()), "white_bottom", config.board
            )
        }
        self.assertEqual(payload["a1"]["machine_mm"], {"x": 40.0, "y": 298.0})
        self.assertEqual(payload["h1"]["machine_mm"], {"x": 320.0, "y": 298.0})
        self.assertEqual(payload["a8"]["machine_mm"], {"x": 40.0, "y": 18.0})
        self.assertEqual(payload["h8"]["machine_mm"], {"x": 320.0, "y": 18.0})

    def test_sol_request_uses_model_image_and_structured_output(self) -> None:
        result = transcription(board_rows(chess.Board()))
        responses = FakeResponses(result)
        client = SimpleNamespace(responses=responses)
        transcriber = SolTranscriber(client=client)
        self.assertEqual(transcriber.transcribe(b"jpeg"), result)
        call = responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertIs(call["text_format"], BoardTranscription)
        self.assertFalse(call["store"])
        image_url = call["input"][1]["content"][1]["image_url"]
        self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), b"jpeg")

    def test_two_matching_observations_emit_one_move(self) -> None:
        moves = []
        manager = SolVisionManager(transcriber=object(), move_handler=moves.append)
        try:
            manager.start_game()
            board = chess.Board()
            board.push_uci("e2e4")
            result = transcription(board_rows(board))
            manager._accept(result)
            self.assertEqual(moves, [])
            manager._accept(result)
            self.assertEqual(moves, ["e2e4"])
            manager._accept(result)
            self.assertEqual(moves, ["e2e4"])
            self.assertEqual(manager.status()["last_move"], "e2e4")
        finally:
            manager.close()

    def test_partial_result_never_emits_move(self) -> None:
        moves = []
        manager = SolVisionManager(transcriber=object(), move_handler=moves.append)
        try:
            manager.start_game()
            rows = list(board_rows(chess.Board()))
            rows[4] = rows[4][:4] + "?" + rows[4][5:]
            partial = transcription(tuple(rows), "partial")
            manager._accept(partial)
            manager._accept(partial)
            self.assertEqual(moves, [])
        finally:
            manager.close()

    def test_camera_rotation_validation_and_status(self) -> None:
        manager = SolVisionManager(transcriber=object())
        try:
            manager.configure(
                source="snapshot:http://phone/shot.jpg",
                enabled=False,
                rotation=90,
            )
            self.assertEqual(manager.status()["rotation"], 90)
            with self.assertRaisesRegex(ValidationError, "rotation"):
                manager.configure(
                    source="snapshot:http://phone/shot.jpg",
                    enabled=False,
                    rotation=45,
                )
        finally:
            manager.close()

    def test_board_calibration_validates_corners_and_warps_square(self) -> None:
        import numpy

        corners = validate_calibration(
            [
                {"x": 0.1, "y": 0.1},
                {"x": 0.9, "y": 0.1},
                {"x": 0.9, "y": 0.9},
                {"x": 0.1, "y": 0.9},
            ]
        )
        frame = numpy.zeros((1080, 1920, 3), dtype=numpy.uint8)
        warped = warp_board_frame(frame, corners)
        self.assertEqual(warped.shape, (1024, 1024, 3))

    def test_board_calibration_rejects_small_or_nonconvex_shape(self) -> None:
        with self.assertRaises(ValidationError):
            validate_calibration(
                [
                    {"x": 0.1, "y": 0.1},
                    {"x": 0.2, "y": 0.1},
                    {"x": 0.15, "y": 0.15},
                    {"x": 0.1, "y": 0.2},
                ]
            )

    def test_manager_calibration_round_trip(self) -> None:
        manager = SolVisionManager(transcriber=object())
        try:
            status = manager.calibrate(
                [
                    {"x": 0.1, "y": 0.1},
                    {"x": 0.9, "y": 0.1},
                    {"x": 0.9, "y": 0.9},
                    {"x": 0.1, "y": 0.9},
                ]
            )
            self.assertTrue(status["calibrated"])
            self.assertFalse(manager.clear_calibration()["calibrated"])
        finally:
            manager.close()

    def test_calibration_persists_and_invalidates_on_rotation(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "calibration.json"
            corners = [
                {"x": 0.1, "y": 0.1},
                {"x": 0.9, "y": 0.1},
                {"x": 0.9, "y": 0.9},
                {"x": 0.1, "y": 0.9},
            ]
            manager = SolVisionManager(transcriber=object(), calibration_path=path)
            try:
                manager.calibrate(corners)
                self.assertTrue(path.exists())
            finally:
                manager.close()
            restored = SolVisionManager(transcriber=object(), calibration_path=path)
            try:
                self.assertTrue(restored.status()["calibrated"])
                restored.configure(
                    source=restored.status()["source"],
                    enabled=False,
                    rotation=90,
                )
                self.assertFalse(restored.status()["calibrated"])
                self.assertFalse(path.exists())
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
