from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

import chess

from chess_gantry.board_sensor import board_matrix
from chess_gantry.errors import ValidationError
from chess_gantry.vision import (
    VisionBoardTracker,
    VisionManager,
    generate_marker_pack,
    marker_manifest,
    synthetic_board_frame,
)


class VisionTests(unittest.TestCase):
    def test_manifest_assigns_unique_ids_to_all_standard_pieces(self) -> None:
        manifest = marker_manifest()
        pieces = manifest["pieces"]
        self.assertEqual(len(pieces), 32)
        self.assertEqual(len({value["piece_id"] for value in pieces.values()}), 32)
        self.assertEqual(manifest["board_reference_markers"]["0"], "top-left")

    def test_generated_markers_are_detectable_and_include_manifest(self) -> None:
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            generate_marker_pack(output, marker_pixels=100)
            self.assertEqual(len(list(output.glob("marker-*.png"))), 36)
            self.assertTrue((output / "manifest.json").is_file())

    def test_standard_and_perspective_frames_detect_every_piece(self) -> None:
        for perspective in (False, True):
            tracker = VisionBoardTracker(stable_frames=2)
            frame = synthetic_board_frame(perspective=perspective)
            tracker.process_frame(frame)
            result = tracker.process_frame(frame)
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["observed_piece_count"], 32)
            self.assertEqual(result["piece_disagreements"], 0)
            self.assertEqual(len(result["piece_comparison"]), 32)
            self.assertLess(result["reference_error"], 0.01)
            self.assertGreater(result["board_area_ratio"], 0.4)
            self.assertGreater(result["smallest_marker_px"], 35)
            pawn = next(
                value
                for value in result["piece_comparison"]
                if value["piece_id"] == "white_pawn_e"
            )
            self.assertEqual(pawn["observed_square"], "e2")
            self.assertEqual(pawn["observed_coordinate"], {"x": 4, "y": 1})
            self.assertEqual(pawn["camera_cell"], {"row": 6, "column": 4})
            self.assertEqual(pawn["state"], "matched")

    def test_exact_piece_move_and_capture_are_inferred(self) -> None:
        tracker = VisionBoardTracker(stable_frames=1)
        tracker.process_frame(synthetic_board_frame())
        first = tracker.process_frame(synthetic_board_frame(moves=("e2e4",)))
        self.assertEqual(first["last_move"], "e2e4")
        self.assertEqual(first["last_move_detail"]["piece_id"], "white_pawn_e")
        self.assertEqual(first["last_move_detail"]["from"]["square"], "e2")
        self.assertEqual(
            first["last_move_detail"]["from"]["coordinate"], {"x": 4, "y": 1}
        )
        self.assertEqual(first["last_move_detail"]["to"]["square"], "e4")
        self.assertEqual(
            first["last_move_detail"]["to"]["coordinate"], {"x": 4, "y": 3}
        )
        second = tracker.process_frame(synthetic_board_frame(moves=("e2e4", "d7d5")))
        self.assertEqual(second["last_move"], "d7d5")
        capture = tracker.process_frame(
            synthetic_board_frame(moves=("e2e4", "d7d5", "e4d5"))
        )
        self.assertEqual(capture["last_move"], "e4d5")
        self.assertEqual(
            capture["last_move_detail"]["capture"]["piece_id"], "black_pawn_d"
        )
        self.assertEqual(capture["last_move_detail"]["capture"]["at"]["square"], "d5")
        self.assertEqual(capture["expected_piece_count"], 31)
        pawn = next(
            value
            for value in capture["observed_pieces"]
            if value["piece_id"] == "white_pawn_e"
        )
        self.assertEqual(pawn["square"], "d5")

    def test_castling_moves_two_exact_piece_ids(self) -> None:
        moves = ("e2e4", "e7e5", "g1f3", "b8c6", "f1e2", "g8f6")
        tracker = VisionBoardTracker(stable_frames=1)
        tracker.process_frame(synthetic_board_frame())
        for index in range(1, len(moves) + 1):
            tracker.process_frame(synthetic_board_frame(moves=moves[:index]))
        castled = tracker.process_frame(synthetic_board_frame(moves=(*moves, "e1g1")))
        self.assertEqual(castled["last_move"], "e1g1")
        observed = {
            value["piece_id"]: value["square"] for value in castled["observed_pieces"]
        }
        self.assertEqual(observed["white_king"], "g1")
        self.assertEqual(observed["white_rook_h"], "f1")
        self.assertEqual(
            castled["last_move_detail"]["rook_transfer"]["piece_id"],
            "white_rook_h",
        )
        self.assertEqual(
            castled["last_move_detail"]["rook_transfer"]["to"]["coordinate"],
            {"x": 5, "y": 0},
        )

    def test_illegal_rearrangement_reports_each_piece_disagreement(self) -> None:
        tracker = VisionBoardTracker(stable_frames=1)
        tracker.process_frame(synthetic_board_frame())
        frame = synthetic_board_frame(moves=("e2e4",))
        tracker.process_frame(frame)
        illegal = VisionBoardTracker(stable_frames=1)
        result = illegal.process_frame(frame)
        self.assertEqual(result["state"], "move")
        comparison = {value["piece_id"]: value for value in result["piece_comparison"]}
        self.assertEqual(comparison["white_pawn_e"]["observed_square"], "e4")
        self.assertEqual(
            comparison["white_pawn_e"]["observed_coordinate"], {"x": 4, "y": 3}
        )

    def test_fusion_agreement_accepts_and_disagreement_vetoes(self) -> None:
        tracker = VisionBoardTracker(stable_frames=1)
        tracker.configure_fusion(True, board_matrix(chess.Board()))
        ready = tracker.process_frame(synthetic_board_frame())
        self.assertEqual(ready["state"], "ready")
        moved_board = chess.Board()
        moved_board.push_uci("e2e4")
        tracker.set_aux_matrix(board_matrix(moved_board))
        moved = tracker.process_frame(synthetic_board_frame(moves=("e2e4",)))
        self.assertEqual(moved["last_move"], "e2e4")
        tracker.set_aux_matrix(board_matrix(chess.Board()))
        conflict = tracker.process_frame(synthetic_board_frame(moves=("e2e4", "e7e5")))
        self.assertEqual(conflict["state"], "conflict")
        self.assertIn("disagrees", conflict["error"])

    def test_lichess_submission_occurs_once_after_stability(self) -> None:
        submitted = []
        tracker = VisionBoardTracker(
            stable_frames=2,
            move_submitter=lambda game_id, uci: submitted.append((game_id, uci)),
        )
        tracker.configure_lichess("game1234", True)
        standard = synthetic_board_frame()
        tracker.process_frame(standard)
        tracker.process_frame(standard)
        moved = synthetic_board_frame(moves=("e2e4",))
        tracker.process_frame(moved)
        tracker.process_frame(moved)
        tracker.process_frame(moved)
        self.assertEqual(submitted, [("game1234", "e2e4")])

    def test_failed_lichess_write_does_not_repeat_without_operator_retry(self) -> None:
        attempts = []

        def fail(game_id, uci):
            attempts.append((game_id, uci))
            raise ValidationError("uncertain network result")

        tracker = VisionBoardTracker(stable_frames=2, move_submitter=fail)
        tracker.configure_lichess("game1234", True)
        moved = synthetic_board_frame(moves=("e2e4",))
        for _ in range(8):
            result = tracker.process_frame(moved)
        self.assertEqual(result["state"], "error")
        self.assertEqual(attempts, [("game1234", "e2e4")])
        tracker.process_frame(synthetic_board_frame()[:100, :100])
        for _ in range(4):
            result = tracker.process_frame(moved)
        self.assertEqual(result["state"], "error")
        self.assertEqual(attempts, [("game1234", "e2e4")])
        tracker.retry()
        tracker.process_frame(moved)
        result = tracker.process_frame(moved)
        self.assertEqual(result["state"], "error")
        self.assertEqual(attempts, [("game1234", "e2e4"), ("game1234", "e2e4")])

    def test_promoted_pawn_keeps_identity_and_changes_tracked_type(self) -> None:
        moves = (
            "a2a4",
            "b7b5",
            "a4b5",
            "h7h6",
            "b5b6",
            "h6h5",
            "b6b7",
            "h5h4",
            "b7a8q",
        )
        tracker = VisionBoardTracker(stable_frames=1)
        tracker.process_frame(synthetic_board_frame())
        for index in range(1, len(moves) + 1):
            result = tracker.process_frame(synthetic_board_frame(moves=moves[:index]))
        promoted = next(
            value
            for value in result["piece_comparison"]
            if value["piece_id"] == "white_pawn_a"
        )
        self.assertEqual(result["last_move"], "b7a8q")
        self.assertEqual(result["last_move_detail"]["promotion"], "queen")
        self.assertEqual(promoted["starting_type"], "pawn")
        self.assertEqual(promoted["type"], "queen")
        self.assertEqual(promoted["observed_square"], "a8")

    def test_missing_reference_never_commits(self) -> None:
        frame = synthetic_board_frame()
        frame[:220, :220] = 255
        result = VisionBoardTracker(stable_frames=1).process_frame(frame)
        self.assertEqual(result["state"], "waiting")
        self.assertIn("reference marker", result["error"])

    def test_manager_runs_demo_source_and_stops_cleanly(self) -> None:
        manager = VisionManager(source="demo:e2e4", enabled=True, frame_hz=20)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = manager.status()
                if status["last_move"] == "e2e4":
                    break
                time.sleep(0.02)
            self.assertEqual(status["last_move"], "e2e4")
            self.assertTrue(manager.preview_jpeg().startswith(b"\xff\xd8"))
        finally:
            manager.close()

    def test_invalid_camera_configuration_is_rejected(self) -> None:
        manager = VisionManager()
        try:
            with self.assertRaisesRegex(ValidationError, "source"):
                manager.configure("", True)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
