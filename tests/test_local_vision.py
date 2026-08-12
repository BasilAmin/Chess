from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

import chess
import numpy

from chess_gantry.errors import ValidationError
from chess_gantry.local_vision import (
    COLOR_TO_TYPE,
    TYPE_TO_COLOR,
    ColorProfiles,
    annotate_local_board,
    board_piece_types,
    detect_aruco_references,
    detect_colored_board,
    generate_reference_markers,
    infer_type_move,
)
from chess_gantry.vision import SolVisionManager


def color_bgr(color):
    import cv2

    profile = ColorProfiles().values[color]
    hsv = numpy.uint8(
        [
            [
                [
                    profile["hue"],
                    max(180, profile["sat_min"] + 40),
                    min(220, profile["val_max"]),
                ]
            ]
        ]
    )
    return tuple(int(value) for value in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def plan_frame(board, orientation="white_bottom"):
    import cv2

    frame = numpy.full((1024, 1024, 3), 210, dtype=numpy.uint8)
    for row in range(8):
        for column in range(8):
            shade = 185 if (row + column) % 2 else 225
            frame[row * 128 : (row + 1) * 128, column * 128 : (column + 1) * 128] = (
                shade
            )
    for square, piece in board.piece_map().items():
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        if orientation == "white_bottom":
            row, column = 7 - rank_index, file_index
        else:
            row, column = rank_index, 7 - file_index
        piece_type = chess.piece_name(piece.piece_type)
        cv2.circle(
            frame,
            (column * 128 + 64, row * 128 + 64),
            31,
            color_bgr(TYPE_TO_COLOR[piece_type]),
            -1,
        )
    return frame


def ready_profiles(path=None):
    profiles = ColorProfiles(path)
    profiles.sampled = set(COLOR_TO_TYPE)
    return profiles


class LocalVisionTests(unittest.TestCase):
    def test_all_six_colors_classify_standard_piece_types_for_both_sides(self):
        board = chess.Board()
        observation = detect_colored_board(
            plan_frame(board), ready_profiles(), orientation="white_bottom"
        )
        self.assertEqual(observation.types, board_piece_types(board))
        self.assertTrue(observation.complete)
        self.assertGreater(observation.confidence, 0.7)
        self.assertEqual(observation.types[chess.E1], "king")
        self.assertEqual(observation.types[chess.E8], "king")

    def test_black_bottom_orientation_maps_to_same_algebraic_position(self):
        board = chess.Board()
        observation = detect_colored_board(
            plan_frame(board, "black_bottom"),
            ready_profiles(),
            orientation="black_bottom",
        )
        self.assertEqual(observation.types, board_piece_types(board))

    def test_type_layout_infers_pawn_move_capture_and_knight_move(self):
        board = chess.Board()
        for uci in ("e2e4", "d7d5", "e4d5", "g8f6"):
            moved = board.copy()
            moved.push_uci(uci)
            observed = detect_colored_board(
                plan_frame(moved), ready_profiles(), orientation="white_bottom"
            )
            self.assertEqual(infer_type_move(board, observed.types).uci(), uci)
            board.push_uci(uci)

    def test_illegal_or_incomplete_type_layout_is_rejected(self):
        board = chess.Board()
        observed = board_piece_types(board)
        del observed[chess.E2]
        with self.assertRaisesRegex(ValidationError, "exactly one legal"):
            infer_type_move(board, observed)

    def test_profile_sampling_persists_all_six_colors(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "colors.json"
            profiles = ColorProfiles(path)
            for color in COLOR_TO_TYPE:
                frame = numpy.full((300, 300, 3), color_bgr(color), dtype=numpy.uint8)
                result = profiles.sample(frame, color, 0.5, 0.5)
                self.assertEqual(result["type"], COLOR_TO_TYPE[color])
            self.assertTrue(profiles.ready)
            restored = ColorProfiles(path)
            self.assertTrue(restored.ready)
            self.assertEqual(set(restored.status()), set(COLOR_TO_TYPE))

    def test_aruco_reference_markers_define_playing_area_corners(self):
        import cv2

        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        frame = numpy.full((1000, 1000, 3), 255, dtype=numpy.uint8)
        placements = {0: (40, 40), 1: (760, 40), 2: (760, 760), 3: (40, 760)}
        for marker_id, (x, y) in placements.items():
            marker = cv2.aruco.generateImageMarker(dictionary, marker_id, 200)
            frame[y : y + 200, x : x + 200] = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
        corners = detect_aruco_references(frame)
        expected = ((0.239, 0.239), (0.76, 0.239), (0.76, 0.76), (0.239, 0.76))
        for actual, target in zip(corners, expected):
            self.assertAlmostEqual(actual["x"], target[0], places=2)
            self.assertAlmostEqual(actual["y"], target[1], places=2)

    def test_missing_aruco_reference_is_rejected(self):
        frame = numpy.full((500, 500, 3), 255, dtype=numpy.uint8)
        with self.assertRaisesRegex(ValidationError, "no ArUco|missing ArUco"):
            detect_aruco_references(frame)

    def test_annotation_draws_grid_piece_labels_and_status(self):
        board = chess.Board()
        frame = plan_frame(board)
        observation = detect_colored_board(
            frame, ready_profiles(), orientation="white_bottom"
        )
        annotated = annotate_local_board(frame, observation, orientation="white_bottom")
        self.assertEqual(annotated.shape, frame.shape)
        self.assertFalse(numpy.array_equal(annotated, frame))

    def test_reference_marker_pack_is_generated(self):
        with TemporaryDirectory() as directory:
            paths = generate_reference_markers(Path(directory), pixels=200)
            self.assertEqual(len(paths), 4)
            self.assertTrue(all(path.exists() for path in paths))

    def test_manager_emits_local_move_after_three_stable_frames(self):
        moves = []
        manager = SolVisionManager(transcriber=object(), move_handler=moves.append)
        try:
            manager._calibration = ((0, 0), (1, 0), (1, 1), (0, 1))
            manager._color_profiles = ready_profiles()
            manager.start_game()
            board = chess.Board()
            board.push_uci("e2e4")
            frame = plan_frame(board)
            started = time.monotonic()
            for _ in range(3):
                manager._process_local(frame)
            deadline = time.monotonic() + 1
            while not moves and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(moves, ["e2e4"])
            self.assertLess(time.monotonic() - started, 1.0)
            status = manager.status()
            self.assertEqual(status["detection_mode"], "local")
            self.assertLess(status["local"]["last_ms"], 100)
        finally:
            manager.close()

    def test_annotation_does_not_replace_clean_sol_or_sampling_frame(self):
        manager = SolVisionManager(transcriber=object())
        try:
            manager._calibration = ((0, 0), (1, 0), (1, 1), (0, 1))
            manager._color_profiles = ready_profiles()
            frame = plan_frame(chess.Board())
            manager._store_frame(frame, already_rotated=True)
            clean = manager._plan_jpeg
            manager._process_local(frame)
            self.assertEqual(manager._plan_jpeg, clean)
            self.assertNotEqual(manager._latest_jpeg, clean)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
