from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import threading
import time
from typing import Any, Callable, Mapping, Optional
from urllib.request import Request, urlopen

from .board_sensor import Matrix, board_matrix, validate_matrix
from .errors import ConfigurationError, ValidationError
from .lichess_pgn import lichess_client


REFERENCE_IDS = (0, 1, 2, 3)
REFERENCE_POINTS = ((-0.7, -0.7), (8.7, -0.7), (8.7, 8.7), (-0.7, 8.7))
PIECE_MARKER_START = 10


def _vision_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise ConfigurationError(
            "vision requires opencv-python-headless and numpy; run uv sync"
        ) from exc
    if not hasattr(cv2, "aruco") or not hasattr(cv2.aruco, "ArucoDetector"):
        raise ConfigurationError("installed OpenCV does not provide ArUco detection")
    return cv2, numpy


def aruco_dictionary() -> Any:
    cv2, _ = _vision_modules()
    dictionary_id = getattr(cv2.aruco, "DICT_ARUCO_MIP_36h12", None)
    if dictionary_id is None:
        dictionary_id = cv2.aruco.DICT_APRILTAG_36h11
    return cv2.aruco.getPredefinedDictionary(dictionary_id)


def standard_piece_markers() -> dict[int, dict[str, str]]:
    import chess

    board = chess.Board()
    markers: dict[int, dict[str, str]] = {}
    for offset, square in enumerate(sorted(board.piece_map())):
        piece = board.piece_at(square)
        assert piece is not None
        file_name = chess.FILE_NAMES[chess.square_file(square)]
        color = "white" if piece.color else "black"
        if piece.piece_type == chess.PAWN:
            identity = f"{color}_pawn_{file_name}"
        elif piece.piece_type == chess.KING:
            identity = f"{color}_king"
        elif piece.piece_type == chess.QUEEN:
            identity = f"{color}_queen"
        else:
            identity = f"{color}_{chess.piece_name(piece.piece_type)}_{file_name}"
        markers[PIECE_MARKER_START + offset] = {
            "piece_id": identity,
            "color": color,
            "type": chess.piece_name(piece.piece_type),
            "start_square": chess.square_name(square),
        }
    return markers


def marker_manifest() -> dict[str, Any]:
    return {
        "dictionary": "DICT_ARUCO_MIP_36h12",
        "orientation": "camera view: a8 at top-left, h1 at bottom-right",
        "board_reference_markers": {
            "0": "top-left",
            "1": "top-right",
            "2": "bottom-right",
            "3": "bottom-left",
        },
        "pieces": {str(key): value for key, value in standard_piece_markers().items()},
    }


def generate_marker_pack(output_dir: Path, marker_pixels: int = 240) -> dict[str, Any]:
    if marker_pixels < 80:
        raise ValidationError("marker size must be at least 80 pixels")
    cv2, _ = _vision_modules()
    output_dir.mkdir(parents=True, exist_ok=True)
    dictionary = aruco_dictionary()
    manifest = marker_manifest()
    marker_ids = [*REFERENCE_IDS, *standard_piece_markers().keys()]
    for marker_id in marker_ids:
        image = cv2.aruco.generateImageMarker(dictionary, marker_id, marker_pixels)
        target = output_dir / f"marker-{marker_id:03d}.png"
        if not cv2.imwrite(str(target), image):
            raise ConfigurationError(f"could not write {target}")
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def _paste_marker(
    canvas: Any, marker: Any, center: tuple[int, int], backing: int
) -> None:
    cv2, _ = _vision_modules()
    size = marker.shape[0]
    outer = size + backing * 2
    x0 = center[0] - outer // 2
    y0 = center[1] - outer // 2
    canvas[y0 : y0 + outer, x0 : x0 + outer] = 255
    resized = cv2.cvtColor(marker, cv2.COLOR_GRAY2BGR)
    canvas[
        y0 + backing : y0 + backing + size,
        x0 + backing : x0 + backing + size,
    ] = resized


def synthetic_board_frame(
    *,
    moves: tuple[str, ...] = (),
    image_size: int = 1440,
    perspective: bool = False,
) -> Any:
    import chess

    cv2, numpy = _vision_modules()
    if image_size < 960:
        raise ValidationError("synthetic vision frame must be at least 960 pixels")
    canvas = numpy.full((image_size, image_size, 3), 232, dtype=numpy.uint8)
    board_start = int(image_size * 0.14)
    board_end = image_size - board_start
    square_size = (board_end - board_start) / 8
    for row in range(8):
        for column in range(8):
            color = (218, 225, 230) if (row + column) % 2 == 0 else (82, 112, 128)
            x0 = round(board_start + column * square_size)
            y0 = round(board_start + row * square_size)
            x1 = round(board_start + (column + 1) * square_size)
            y1 = round(board_start + (row + 1) * square_size)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), color, -1)
    dictionary = aruco_dictionary()
    reference_centers = (
        (
            round(board_start - 0.7 * square_size),
            round(board_start - 0.7 * square_size),
        ),
        (round(board_end + 0.7 * square_size), round(board_start - 0.7 * square_size)),
        (round(board_end + 0.7 * square_size), round(board_end + 0.7 * square_size)),
        (round(board_start - 0.7 * square_size), round(board_end + 0.7 * square_size)),
    )
    reference_size = max(70, round(square_size * 0.56))
    for marker_id, center in zip(REFERENCE_IDS, reference_centers):
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, reference_size)
        _paste_marker(canvas, marker, center, max(10, reference_size // 4))
    board = chess.Board()
    identities = {
        chess.parse_square(value["start_square"]): marker_id
        for marker_id, value in standard_piece_markers().items()
    }
    for uci in moves:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValidationError(f"illegal synthetic vision move: {uci}")
        marker_id = identities.pop(move.from_square)
        capture_square = move.to_square
        if board.is_en_passant(move):
            capture_square += -8 if board.turn else 8
        identities.pop(capture_square, None)
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            if chess.square_file(move.to_square) > chess.square_file(move.from_square):
                rook_from = chess.square(7, rank)
                rook_to = chess.square(5, rank)
            else:
                rook_from = chess.square(0, rank)
                rook_to = chess.square(3, rank)
            identities[rook_to] = identities.pop(rook_from)
        identities[move.to_square] = marker_id
        board.push(move)
    piece_size = max(54, round(square_size * 0.43))
    for square, marker_id in identities.items():
        column = chess.square_file(square)
        row = 7 - chess.square_rank(square)
        center = (
            round(board_start + (column + 0.5) * square_size),
            round(board_start + (row + 0.5) * square_size),
        )
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, piece_size)
        _paste_marker(canvas, marker, center, max(10, piece_size // 4))
    if perspective:
        source = numpy.float32(
            [
                [0, 0],
                [image_size - 1, 0],
                [image_size - 1, image_size - 1],
                [0, image_size - 1],
            ]
        )
        destination = numpy.float32(
            [
                [image_size * 0.06, image_size * 0.02],
                [image_size * 0.95, image_size * 0.08],
                [image_size * 0.98, image_size * 0.94],
                [image_size * 0.03, image_size * 0.98],
            ]
        )
        transform = cv2.getPerspectiveTransform(source, destination)
        canvas = cv2.warpPerspective(canvas, transform, (image_size, image_size))
    return canvas


@dataclass(frozen=True)
class VisionEvent:
    timestamp: str
    kind: str
    message: str
    uci: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "message": self.message,
            "uci": self.uci,
        }


class VisionBoardTracker:
    def __init__(
        self,
        *,
        stable_frames: int = 3,
        min_marker_side_px: float = 24.0,
        move_submitter: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        if stable_frames < 1:
            raise ConfigurationError("vision stable frame count must be positive")
        if min_marker_side_px < 8:
            raise ConfigurationError("minimum marker side must be at least 8 pixels")
        import chess

        self.stable_frames = stable_frames
        self.min_marker_side_px = float(min_marker_side_px)
        self._move_submitter = move_submitter or self._submit_to_lichess
        self._lock = threading.RLock()
        self._board = chess.Board()
        self._piece_markers = standard_piece_markers()
        self._positions = {
            value["piece_id"]: chess.parse_square(value["start_square"])
            for value in self._piece_markers.values()
        }
        self._piece_types = {
            value["piece_id"]: value["type"] for value in self._piece_markers.values()
        }
        self._candidate: Optional[tuple[tuple[str, int], ...]] = None
        self._blocked_write_candidate: Optional[tuple[tuple[str, int], ...]] = None
        self._stable_count = 0
        self._state = "waiting"
        self._error: Optional[str] = None
        self._last_move: Optional[str] = None
        self._last_move_detail: Optional[dict[str, Any]] = None
        self._candidates: tuple[str, ...] = ()
        self._events: list[VisionEvent] = []
        self._last_observed: dict[str, int] = {}
        self._reference_error: Optional[float] = None
        self._board_area_ratio: Optional[float] = None
        self._marker_sides: dict[int, float] = {}
        self._frames = 0
        self._accepted_frames = 0
        self._started = time.monotonic()
        self._last_frame_at: Optional[str] = None
        self._game_id: Optional[str] = None
        self._write_lichess = False
        self._fusion_enabled = False
        self._aux_matrix: Optional[Matrix] = None

    @staticmethod
    def _submit_to_lichess(game_id: str, uci: str) -> None:
        import os

        token = os.environ.get("LICHESS_TOKEN", "").strip()
        if not token:
            raise ConfigurationError(
                "LICHESS_TOKEN is required to submit vision moves through the Lichess Board API"
            )
        lichess_client(token).board.make_move(game_id, uci)

    def _event(self, kind: str, message: str, uci: Optional[str] = None) -> None:
        self._events.append(
            VisionEvent(datetime.now(timezone.utc).isoformat(), kind, message, uci)
        )
        self._events = self._events[-100:]

    def configure_lichess(self, game_id: Optional[str], write: bool) -> dict[str, Any]:
        normalized = game_id.strip() if isinstance(game_id, str) else ""
        if normalized and (not normalized.isalnum() or not 8 <= len(normalized) <= 12):
            raise ValidationError("Lichess game ID must be 8-12 letters or digits")
        if write and not normalized:
            raise ValidationError("a Lichess game ID is required when write is enabled")
        with self._lock:
            self._game_id = normalized or None
            self._write_lichess = bool(write)
            self._event(
                "config",
                f"Vision Lichess writing {'enabled' if write else 'disabled'}"
                + (f" for {normalized}" if normalized else ""),
            )
        return self.status()

    def configure_fusion(self, enabled: bool, matrix: Any = None) -> dict[str, Any]:
        parsed = validate_matrix(matrix) if matrix is not None else None
        with self._lock:
            self._fusion_enabled = bool(enabled)
            self._aux_matrix = parsed
            self._event(
                "fusion", f"Occupancy fusion {'enabled' if enabled else 'disabled'}"
            )
        return self.status()

    def set_aux_matrix(self, matrix: Any) -> None:
        parsed = validate_matrix(matrix)
        with self._lock:
            self._aux_matrix = parsed

    def reset(self) -> dict[str, Any]:
        import chess

        with self._lock:
            self._board = chess.Board()
            self._positions = {
                value["piece_id"]: chess.parse_square(value["start_square"])
                for value in self._piece_markers.values()
            }
            self._piece_types = {
                value["piece_id"]: value["type"]
                for value in self._piece_markers.values()
            }
            self._candidate = None
            self._blocked_write_candidate = None
            self._stable_count = 0
            self._state = "waiting"
            self._error = None
            self._last_move = None
            self._last_move_detail = None
            self._candidates = ()
            self._last_observed = {}
            self._events = []
            self._event("reset", "Vision tracker reset to standard position")
        return self.status()

    def retry(self) -> dict[str, Any]:
        with self._lock:
            if self._state != "error" or not self._candidates:
                raise ValidationError("there is no failed vision move to retry")
            self._blocked_write_candidate = None
            self._candidate = None
            self._stable_count = 0
            self._state = "waiting"
            self._error = None
            self._event(
                "retry", "Operator requested one retry of the failed vision move"
            )
        return self.status()

    def _detect(self, frame: Any) -> tuple[dict[str, int], dict[str, Any]]:
        import chess

        cv2, numpy = _vision_modules()
        if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
            raise ValidationError("camera frame is empty")
        gray = (
            cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
        )
        parameters = cv2.aruco.DetectorParameters()
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        parameters.errorCorrectionRate = 0.4
        detector = cv2.aruco.ArucoDetector(aruco_dictionary(), parameters)
        corners, ids, rejected = detector.detectMarkers(gray)
        if ids is None:
            raise ValidationError("no ArUco markers were detected")
        detected: dict[int, Any] = {}
        marker_sides: dict[int, float] = {}
        for marker_corners, marker_id_value in zip(corners, ids.flatten().tolist()):
            marker_id = int(marker_id_value)
            points = marker_corners.reshape(4, 2)
            side_lengths = [
                float(numpy.linalg.norm(points[(index + 1) % 4] - points[index]))
                for index in range(4)
            ]
            side = min(side_lengths)
            if side < self.min_marker_side_px:
                continue
            if marker_id in detected:
                raise ValidationError(f"marker {marker_id} was detected more than once")
            detected[marker_id] = points
            marker_sides[marker_id] = side
        missing_references = [value for value in REFERENCE_IDS if value not in detected]
        if missing_references:
            joined = ", ".join(str(value) for value in missing_references)
            raise ValidationError(f"missing board reference marker(s): {joined}")
        image_points = numpy.float32(
            [detected[marker_id].mean(axis=0) for marker_id in REFERENCE_IDS]
        )
        if not cv2.isContourConvex(image_points.astype(numpy.int32)):
            raise ValidationError(
                "board reference markers do not form a convex quadrilateral"
            )
        frame_area = float(frame.shape[0] * frame.shape[1])
        board_area_ratio = abs(float(cv2.contourArea(image_points))) / frame_area
        if board_area_ratio < 0.15:
            raise ValidationError(
                "board reference markers cover too little of the camera image"
            )
        board_points = numpy.float32(REFERENCE_POINTS)
        homography, _ = cv2.findHomography(image_points, board_points, cv2.RANSAC)
        if homography is None:
            raise ValidationError("could not calculate board homography")
        projected = cv2.perspectiveTransform(
            image_points.reshape(1, -1, 2), homography
        )[0]
        reference_error = float(numpy.sqrt(numpy.mean((projected - board_points) ** 2)))
        observed: dict[str, int] = {}
        unknown: list[int] = []
        boundaries: list[int] = []
        for marker_id, points in detected.items():
            if marker_id in REFERENCE_IDS:
                continue
            definition = self._piece_markers.get(marker_id)
            if definition is None:
                unknown.append(marker_id)
                continue
            center = points.mean(axis=0).reshape(1, 1, 2).astype(numpy.float32)
            board_center = cv2.perspectiveTransform(center, homography)[0][0]
            column = int(numpy.floor(board_center[0]))
            row = int(numpy.floor(board_center[1]))
            fractional = board_center - numpy.floor(board_center)
            if not 0 <= column < 8 or not 0 <= row < 8:
                raise ValidationError(f"piece marker {marker_id} is outside the board")
            if (
                min(
                    float(fractional[0]),
                    float(fractional[1]),
                    1 - float(fractional[0]),
                    1 - float(fractional[1]),
                )
                < 0.12
            ):
                boundaries.append(marker_id)
                continue
            square = chess.square(column, 7 - row)
            piece_id = definition["piece_id"]
            if square in observed.values():
                raise ValidationError(
                    f"more than one piece marker occupies {chess.square_name(square)}"
                )
            observed[piece_id] = square
        if unknown:
            raise ValidationError(
                "unknown marker ID(s): "
                + ", ".join(str(value) for value in sorted(unknown))
            )
        if boundaries:
            raise ValidationError(
                "marker center is too close to a square boundary: "
                + ", ".join(str(value) for value in sorted(boundaries))
            )
        return observed, {
            "detected_markers": len(detected),
            "rejected_candidates": len(rejected),
            "reference_error": reference_error,
            "board_area_ratio": board_area_ratio,
            "marker_sides": marker_sides,
        }

    def _expected_after(self, move: Any) -> dict[str, int]:
        import chess

        expected = dict(self._positions)
        moving_id = next(
            (
                piece_id
                for piece_id, square in expected.items()
                if square == move.from_square
            ),
            None,
        )
        if moving_id is None:
            return {}
        capture_square = move.to_square
        if self._board.is_en_passant(move):
            capture_square += -8 if self._board.turn else 8
        captured_id = next(
            (
                piece_id
                for piece_id, square in expected.items()
                if square == capture_square
            ),
            None,
        )
        if captured_id is not None:
            del expected[captured_id]
        expected[moving_id] = move.to_square
        if self._board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            if chess.square_file(move.to_square) > chess.square_file(move.from_square):
                rook_from = chess.square(7, rank)
                rook_to = chess.square(5, rank)
            else:
                rook_from = chess.square(0, rank)
                rook_to = chess.square(3, rank)
            rook_id = next(
                (
                    piece_id
                    for piece_id, square in expected.items()
                    if square == rook_from
                ),
                None,
            )
            if rook_id is None:
                return {}
            expected[rook_id] = rook_to
        return expected

    @staticmethod
    def _square_detail(square: Optional[int]) -> Optional[dict[str, Any]]:
        if square is None:
            return None
        import chess

        x = chess.square_file(square)
        y = chess.square_rank(square)
        return {
            "square": chess.square_name(square),
            "coordinate": {"x": x, "y": y},
            "camera_cell": {"row": 7 - y, "column": x},
        }

    def _move_detail(self, move: Any) -> dict[str, Any]:
        import chess

        moving_id = next(
            piece_id
            for piece_id, square in self._positions.items()
            if square == move.from_square
        )
        capture_square = move.to_square
        if self._board.is_en_passant(move):
            capture_square += -8 if self._board.turn else 8
        captured_id = next(
            (
                piece_id
                for piece_id, square in self._positions.items()
                if square == capture_square
            ),
            None,
        )
        detail = {
            "uci": move.uci(),
            "piece_id": moving_id,
            "from": self._square_detail(move.from_square),
            "to": self._square_detail(move.to_square),
            "capture": (
                {
                    "piece_id": captured_id,
                    "at": self._square_detail(capture_square),
                    "en_passant": self._board.is_en_passant(move),
                }
                if captured_id is not None
                else None
            ),
            "promotion": (chess.piece_name(move.promotion) if move.promotion else None),
            "rook_transfer": None,
        }
        if self._board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            king_side = chess.square_file(move.to_square) > chess.square_file(
                move.from_square
            )
            rook_from = chess.square(7 if king_side else 0, rank)
            rook_to = chess.square(5 if king_side else 3, rank)
            rook_id = next(
                piece_id
                for piece_id, square in self._positions.items()
                if square == rook_from
            )
            detail["rook_transfer"] = {
                "piece_id": rook_id,
                "from": self._square_detail(rook_from),
                "to": self._square_detail(rook_to),
            }
        return detail

    @staticmethod
    def _matrix_from_positions(positions: Mapping[str, int]) -> Matrix:
        import chess

        rows = [[0 for _ in range(8)] for _ in range(8)]
        for square in positions.values():
            rows[7 - chess.square_rank(square)][chess.square_file(square)] = 1
        return tuple(tuple(row) for row in rows)

    def _process_stable(self, observed: dict[str, int]) -> None:
        if self._fusion_enabled:
            if self._aux_matrix is None:
                self._state = "conflict"
                self._error = (
                    "occupancy fusion is enabled but no 8 x 8 matrix is available"
                )
                return
            if self._matrix_from_positions(observed) != self._aux_matrix:
                self._state = "conflict"
                self._error = (
                    "vision occupancy disagrees with the auxiliary occupancy matrix"
                )
                self._event("conflict", self._error)
                return
        if observed == self._positions:
            self._state = "ready"
            self._error = None
            self._candidates = ()
            return
        matches = []
        for move in self._board.legal_moves:
            if self._expected_after(move) == observed:
                matches.append(move)
        if len(matches) > 1:
            promotion_matches = [move for move in matches if move.promotion]
            same_transfer = (
                len(promotion_matches) == len(matches)
                and len({(move.from_square, move.to_square) for move in matches}) == 1
            )
            if same_transfer:
                import chess

                matches = [
                    move for move in promotion_matches if move.promotion == chess.QUEEN
                ]
                self._event(
                    "promotion",
                    "Vision could not distinguish promotion type; applied queen policy",
                    matches[0].uci(),
                )
        if len(matches) > 1:
            self._state = "ambiguous"
            self._candidates = tuple(move.uci() for move in matches)
            self._error = "observed pieces match more than one legal move"
            self._event("ambiguous", self._error)
            return
        if not matches:
            expected_count = len(self._positions)
            if len(observed) < expected_count - 1:
                self._state = "moving"
                self._error = "piece markers are occluded or currently being moved"
                return
            self._state = "illegal"
            self._error = (
                "stable exact-piece position is not the current board or one legal move"
            )
            self._event("illegal", self._error)
            return
        move = matches[0]
        uci = move.uci()
        move_detail = self._move_detail(move)
        try:
            if self._write_lichess:
                if self._game_id is None:
                    raise ConfigurationError(
                        "set a Lichess game ID before enabling writes"
                    )
                self._move_submitter(self._game_id, uci)
                self._event(
                    "lichess", f"Submitted vision move {uci} to {self._game_id}", uci
                )
            self._positions = self._expected_after(move)
            if move.promotion:
                import chess

                self._piece_types[move_detail["piece_id"]] = chess.piece_name(
                    move.promotion
                )
            self._board.push(move)
            self._last_move = uci
            self._last_move_detail = move_detail
            self._candidates = (uci,)
            self._state = "move"
            self._error = None
            self._event("move", f"Detected exact-piece move {uci}", uci)
        except Exception as exc:
            self._blocked_write_candidate = tuple(sorted(observed.items()))
            self._state = "error"
            self._error = str(exc)
            self._candidates = (uci,)
            self._event("error", f"Detected {uci} but submission failed: {exc}", uci)

    def process_frame(self, frame: Any) -> dict[str, Any]:
        with self._lock:
            self._frames += 1
            self._last_frame_at = datetime.now(timezone.utc).isoformat()
            try:
                observed, diagnostics = self._detect(frame)
                fingerprint = tuple(sorted(observed.items()))
                self._reference_error = diagnostics["reference_error"]
                self._board_area_ratio = diagnostics["board_area_ratio"]
                self._marker_sides = diagnostics["marker_sides"]
                self._last_observed = observed
                self._accepted_frames += 1
                if fingerprint == self._candidate:
                    self._stable_count += 1
                else:
                    self._candidate = fingerprint
                    self._stable_count = 1
                if fingerprint == self._blocked_write_candidate:
                    self._state = "error"
                    self._error = (
                        "vision move write outcome is uncertain; verify the remote game "
                        "before requesting one retry"
                    )
                    return self.status()
                if self._stable_count == self.stable_frames:
                    self._process_stable(observed)
            except ValidationError as exc:
                self._candidate = None
                self._stable_count = 0
                self._state = "waiting"
                self._error = str(exc)
            return self.status()

    def status(self) -> dict[str, Any]:
        import chess

        with self._lock:
            elapsed = max(time.monotonic() - self._started, 0.001)
            piece_comparison = []
            for marker_id, definition in sorted(self._piece_markers.items()):
                piece_id = definition["piece_id"]
                expected_square = self._positions.get(piece_id)
                observed_square = self._last_observed.get(piece_id)
                if expected_square is None and observed_square is None:
                    piece_state = "captured"
                elif expected_square is None:
                    piece_state = "unexpected"
                elif observed_square is None:
                    piece_state = "missing"
                elif expected_square == observed_square:
                    piece_state = "matched"
                else:
                    piece_state = "moved"
                expected = self._square_detail(expected_square)
                observed = self._square_detail(observed_square)
                piece_comparison.append(
                    {
                        "marker_id": marker_id,
                        "piece_id": piece_id,
                        "color": definition["color"],
                        "type": self._piece_types[piece_id],
                        "starting_type": definition["type"],
                        "state": piece_state,
                        "expected_square": expected["square"] if expected else None,
                        "expected_coordinate": (
                            expected["coordinate"] if expected else None
                        ),
                        "observed_square": observed["square"] if observed else None,
                        "observed_coordinate": (
                            observed["coordinate"] if observed else None
                        ),
                        "camera_cell": observed["camera_cell"] if observed else None,
                        "marker_side_px": (
                            round(self._marker_sides[marker_id], 1)
                            if marker_id in self._marker_sides
                            else None
                        ),
                    }
                )
            observed = [
                {
                    "marker_id": value["marker_id"],
                    "piece_id": value["piece_id"],
                    "color": value["color"],
                    "type": value["type"],
                    "square": value["observed_square"],
                    "coordinate": value["observed_coordinate"],
                    "camera_cell": value["camera_cell"],
                    "marker_side_px": value["marker_side_px"],
                }
                for value in piece_comparison
                if value["observed_square"] is not None
            ]
            return {
                "state": self._state,
                "error": self._error,
                "last_move": self._last_move,
                "last_move_detail": self._last_move_detail,
                "candidates": list(self._candidates),
                "fen": self._board.fen(),
                "turn": "white" if self._board.turn else "black",
                "stable_frames": self._stable_count,
                "stable_frames_required": self.stable_frames,
                "frames": self._frames,
                "accepted_frames": self._accepted_frames,
                "frame_hz_observed": round(self._frames / elapsed, 2),
                "last_frame_at": self._last_frame_at,
                "reference_error": self._reference_error,
                "board_area_ratio": self._board_area_ratio,
                "smallest_marker_px": (
                    round(min(self._marker_sides.values()), 1)
                    if self._marker_sides
                    else None
                ),
                "observed_pieces": observed,
                "piece_comparison": piece_comparison,
                "piece_disagreements": sum(
                    value["state"] not in {"matched", "captured"}
                    for value in piece_comparison
                ),
                "expected_piece_count": len(self._positions),
                "observed_piece_count": len(self._last_observed),
                "fusion_enabled": self._fusion_enabled,
                "fusion_matrix_available": self._aux_matrix is not None,
                "game_id": self._game_id,
                "write_lichess": self._write_lichess,
                "events": [event.as_dict() for event in self._events],
            }


class _DemoFrameSource:
    def __init__(self, moves: tuple[str, ...] = ()) -> None:
        self.frame = synthetic_board_frame(moves=moves, perspective=True)

    def read(self) -> Any:
        return self.frame.copy()

    def close(self) -> None:
        return None


class _ImageFrameSource:
    def __init__(self, path: Path) -> None:
        cv2, _ = _vision_modules()
        self.frame = cv2.imread(str(path))
        if self.frame is None:
            raise ValidationError(f"could not read camera image {path}")

    def read(self) -> Any:
        return self.frame.copy()

    def close(self) -> None:
        return None


class _SnapshotFrameSource:
    def __init__(self, url: str) -> None:
        self.url = url

    def read(self) -> Any:
        cv2, numpy = _vision_modules()
        request = Request(
            self.url,
            headers={"Cache-Control": "no-cache", "User-Agent": "ChessGantry/0.2"},
        )
        with urlopen(request, timeout=3) as response:
            payload = response.read(12_000_000)
        frame = cv2.imdecode(
            numpy.frombuffer(payload, dtype=numpy.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            raise ValidationError("phone snapshot endpoint did not return a JPEG image")
        return frame

    def close(self) -> None:
        return None


class _VideoFrameSource:
    def __init__(self, source: str) -> None:
        cv2, _ = _vision_modules()
        value: Any = int(source) if source.isdigit() else source
        self.capture = cv2.VideoCapture()
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000)
        self.capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000)
        if not self.capture.open(value):
            self.capture.release()
            raise ValidationError(f"could not open camera source {source}")

    def read(self) -> Any:
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise ValidationError("camera source did not return a frame")
        return frame

    def close(self) -> None:
        self.capture.release()


def open_frame_source(source: str) -> Any:
    normalized = source.strip()
    if not normalized:
        raise ValidationError("camera source is required")
    if normalized == "demo":
        return _DemoFrameSource()
    if normalized.startswith("demo:"):
        moves = tuple(item for item in normalized[5:].split(",") if item)
        return _DemoFrameSource(moves)
    if normalized.startswith("snapshot:"):
        return _SnapshotFrameSource(normalized[len("snapshot:") :])
    path = Path(normalized)
    if path.is_file():
        return _ImageFrameSource(path)
    return _VideoFrameSource(normalized)


class VisionManager:
    def __init__(
        self,
        *,
        source: str = "",
        enabled: bool = False,
        frame_hz: float = 5.0,
        tracker: Optional[VisionBoardTracker] = None,
        occupancy_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        if not 0.2 <= frame_hz <= 30:
            raise ConfigurationError("vision frame rate must be between 0.2 and 30 Hz")
        self.tracker = tracker or VisionBoardTracker()
        self._occupancy_provider = occupancy_provider
        self.frame_hz = float(frame_hz)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._source = source.strip()
        self._enabled = bool(enabled and self._source)
        self._running = False
        self._source_error: Optional[str] = None
        self._latest_jpeg: Optional[bytes] = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def configure(self, source: str, enabled: bool) -> dict[str, Any]:
        normalized = source.strip() if isinstance(source, str) else ""
        if enabled and not normalized:
            raise ValidationError("camera source is required when vision is enabled")
        if len(normalized) > 2048:
            raise ValidationError("camera source is too long")
        with self._lock:
            self._source = normalized
            self._enabled = bool(enabled)
            self._source_error = None
        self._wake.set()
        return self.status()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)

    def preview_jpeg(self) -> bytes:
        with self._lock:
            if self._latest_jpeg is None:
                raise ValidationError("no camera preview frame is available")
            return self._latest_jpeg

    def _loop(self) -> None:
        active_source = ""
        reader: Any = None
        while not self._stop.is_set():
            with self._lock:
                enabled = self._enabled
                source = self._source
            if not enabled:
                if reader is not None:
                    reader.close()
                    reader = None
                active_source = ""
                with self._lock:
                    self._running = False
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                if reader is None or source != active_source:
                    if reader is not None:
                        reader.close()
                    reader = open_frame_source(source)
                    active_source = source
                with self._lock:
                    self._running = True
                    self._source_error = None
                frame = reader.read()
                cv2, _ = _vision_modules()
                height, width = frame.shape[:2]
                preview = frame
                if width > 960:
                    preview = cv2.resize(frame, (960, round(height * 960 / width)))
                ok, encoded = cv2.imencode(
                    ".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 78]
                )
                if ok:
                    with self._lock:
                        self._latest_jpeg = encoded.tobytes()
                if self._occupancy_provider is not None:
                    self.tracker.set_aux_matrix(self._occupancy_provider())
                self.tracker.process_frame(frame)
                self._wake.wait(1.0 / self.frame_hz)
                self._wake.clear()
            except Exception as exc:
                if reader is not None:
                    reader.close()
                    reader = None
                active_source = ""
                with self._lock:
                    self._running = False
                    self._source_error = str(exc)
                self._wake.wait(1.0)
                self._wake.clear()
        if reader is not None:
            reader.close()
        with self._lock:
            self._running = False

    def status(self) -> dict[str, Any]:
        with self._lock:
            value = self.tracker.status()
            value.update(
                {
                    "enabled": self._enabled,
                    "running": self._running,
                    "source": self._source,
                    "source_error": self._source_error,
                    "frame_hz_target": self.frame_hz,
                    "method": "unique ArUco MIP 36h12 piece tags with four board references",
                }
            )
            return value
