from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated, Self

from .errors import ConfigurationError, ValidationError
from .local_vision import (
    COLOR_TO_TYPE,
    ColorProfiles,
    annotate_local_board,
    board_piece_types,
    detect_aruco_references,
    detect_colored_board,
    infer_type_move,
)
from .persistence import atomic_write_json, read_json


MODEL = "gpt-5.6-sol"
DEFAULT_PHONE_SOURCE = "auto:http://192.168.100.88:8080"
PIECE_SYMBOLS = "PNBRQKpnbrqk"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RankString = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=8, max_length=8, pattern=r"^[PNBRQKpnbrqk.x?]{8}$"
    ),
]


class ImageRows(StrictModel):
    row_1: RankString
    row_2: RankString
    row_3: RankString
    row_4: RankString
    row_5: RankString
    row_6: RankString
    row_7: RankString
    row_8: RankString

    def values(self) -> tuple[str, ...]:
        return (
            self.row_1,
            self.row_2,
            self.row_3,
            self.row_4,
            self.row_5,
            self.row_6,
            self.row_7,
            self.row_8,
        )


class BoardConfidence(StrictModel):
    board_detection: Literal["high", "medium", "low", "none"]
    grid_mapping: Literal["high", "medium", "low", "none"]
    piece_recognition: Literal["high", "medium", "low", "none"]


class BoardTranscription(StrictModel):
    status: Literal["complete", "partial", "not_found", "unusable"]
    confidence: BoardConfidence
    rows: ImageRows
    problems: list[
        Literal[
            "blur",
            "glare",
            "shadow",
            "occlusion",
            "cropping",
            "perspective",
            "low_resolution",
            "ambiguous_pieces",
            "ambiguous_colors",
        ]
    ] = Field(max_length=9)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        encoded = "".join(self.rows.values())
        unresolved = any(value in encoded for value in "x?")
        if self.status == "complete":
            if unresolved:
                raise ValueError("complete transcription cannot contain x or ?")
            if "low" in {
                self.confidence.board_detection,
                self.confidence.grid_mapping,
                self.confidence.piece_recognition,
            } or "none" in {
                self.confidence.board_detection,
                self.confidence.grid_mapping,
                self.confidence.piece_recognition,
            }:
                raise ValueError(
                    "complete transcription requires medium or high confidence"
                )
        elif self.status in {"not_found", "unusable"} and encoded != "?" * 64:
            raise ValueError("not_found and unusable must mark all cells unknown")
        return self


SYSTEM_PROMPT = """You are a deterministic visual transcriber for one overhead physical chessboard image.
Return exactly one result matching the supplied schema.

Select the complete physical 8x8 chessboard occupying the largest image area. The rows are IMAGE SPACE: row_1 is the board's top edge, row_8 its bottom edge, and characters run image-left to image-right. Do not infer algebraic orientation; the host maps orientation.

Symbols: P N B R Q K are visually supported white pieces, p n b r q k black pieces, . confidently empty, x definitely occupied but type/color uncertain, ? occupancy uncertain.

Inspect all 64 cells independently. Use only visible evidence. Never repair the image using a starting-position assumption, material counts, chess legality, previous frames, king counts, or expected moves. Transcribe visually illegal positions exactly as visible.

complete means every cell is exact or confidently empty with reliable board, grid, and piece recognition. partial means the board/grid are reliable but at least one cell is x or ?. not_found means no qualifying board. unusable means a possible board cannot be transcribed reliably. For not_found or unusable return ???????? in all rows.
"""


def validate_calibration(corners: Any) -> tuple[tuple[float, float], ...]:
    cv2, numpy = _vision_modules()
    if not isinstance(corners, list) or len(corners) != 4:
        raise ValidationError(
            "board calibration needs top-left, top-right, bottom-right, and bottom-left"
        )
    parsed = []
    for value in corners:
        if not isinstance(value, dict):
            raise ValidationError("each calibration corner must be an object")
        x, y = value.get("x"), value.get("y")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise ValidationError("calibration coordinates must be numbers")
        if not 0 <= float(x) <= 1 or not 0 <= float(y) <= 1:
            raise ValidationError("calibration coordinates must be normalized 0..1")
        parsed.append((float(x), float(y)))
    points = numpy.float32(parsed)
    if not cv2.isContourConvex((points * 1000).astype(numpy.int32)):
        raise ValidationError("calibration corners must form a convex quadrilateral")
    if abs(float(cv2.contourArea(points))) < 0.1:
        raise ValidationError("calibrated board occupies too little of the frame")
    return tuple(parsed)


def warp_board_frame(frame: Any, corners: tuple[tuple[float, float], ...]) -> Any:
    cv2, numpy = _vision_modules()
    height, width = frame.shape[:2]
    source = numpy.float32([(x * width, y * height) for x, y in corners])
    destination = numpy.float32(((0, 0), (1023, 0), (1023, 1023), (0, 1023)))
    transform = cv2.getPerspectiveTransform(source, destination)
    return cv2.warpPerspective(frame, transform, (1024, 1024))


def _vision_modules() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy
    except ImportError as exc:
        raise ConfigurationError(
            "camera support requires opencv-python-headless and numpy"
        ) from exc
    return cv2, numpy


def encode_jpeg(frame: Any, *, max_width: int = 1280, quality: int = 88) -> bytes:
    cv2, _ = _vision_modules()
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, round(height * max_width / width)))
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValidationError("could not encode camera frame as JPEG")
    return encoded.tobytes()


def image_cell_to_square(row: int, column: int, orientation: str) -> int:
    import chess

    if not 0 <= row < 8 or not 0 <= column < 8:
        raise ValidationError("image board cell must be inside the 8x8 grid")
    if orientation not in {"white_bottom", "black_bottom"}:
        raise ValidationError("camera orientation must be white_bottom or black_bottom")
    if orientation == "white_bottom":
        file_index = column
        rank_index = 7 - row
    else:
        file_index = 7 - column
        rank_index = row
    return chess.square(file_index, rank_index)


def square_to_image_cell(square: int, orientation: str) -> tuple[int, int]:
    import chess

    if not 0 <= square < 64:
        raise ValidationError("chess square must be between 0 and 63")
    file_index = chess.square_file(square)
    rank_index = chess.square_rank(square)
    if orientation == "white_bottom":
        return 7 - rank_index, file_index
    if orientation == "black_bottom":
        return rank_index, 7 - file_index
    raise ValidationError("camera orientation must be white_bottom or black_bottom")


def image_rows_to_board(
    rows: tuple[str, ...], orientation: str = "white_bottom"
) -> dict[int, str]:
    result = {}
    for image_row, row in enumerate(rows):
        for image_column, symbol in enumerate(row):
            result[image_cell_to_square(image_row, image_column, orientation)] = symbol
    return result


def rotate_image_cell(row: int, column: int, rotation: int) -> tuple[int, int]:
    if not 0 <= row < 8 or not 0 <= column < 8:
        raise ValidationError("raw image board cell must be inside the 8x8 grid")
    if rotation == 0:
        return row, column
    if rotation == 90:
        return column, 7 - row
    if rotation == 180:
        return 7 - row, 7 - column
    if rotation == 270:
        return 7 - column, row
    raise ValidationError("rotation must be 0, 90, 180, or 270 degrees")


def canonical_rows(rows: tuple[str, ...], orientation: str) -> tuple[str, ...]:
    import chess

    observed = image_rows_to_board(rows, orientation)
    return tuple(
        "".join(observed[chess.square(file_index, rank)] for file_index in range(8))
        for rank in range(7, -1, -1)
    )


def board_symbols(board: Any) -> dict[int, str]:
    return {
        square: (piece.symbol() if piece is not None else ".")
        for square in range(64)
        for piece in [board.piece_at(square)]
    }


def infer_legal_move(board: Any, observed: dict[int, str]) -> Optional[Any]:
    if observed == board_symbols(board):
        return None
    matches = []
    for move in board.legal_moves:
        candidate = board.copy(stack=False)
        candidate.push(move)
        if board_symbols(candidate) == observed:
            matches.append(move)
    if len(matches) != 1:
        raise ValidationError(
            "camera position does not match exactly one legal move"
            if not matches
            else "camera position matches multiple promotion choices"
        )
    return matches[0]


def board_payload(
    rows: tuple[str, ...], orientation: str, geometry: Optional[Any] = None
) -> list[dict[str, Any]]:
    import chess

    from .kinematics import grid_to_machine
    from .models import GridPosition

    observed = image_rows_to_board(rows, orientation)
    payload = []
    for square, symbol in sorted(observed.items()):
        if symbol not in PIECE_SYMBOLS:
            continue
        file_index = chess.square_file(square)
        rank_index = chess.square_rank(square)
        item = {
            "square": chess.square_name(square),
            "symbol": symbol,
            "color": "white" if symbol.isupper() else "black",
            "type": chess.piece_name(chess.Piece.from_symbol(symbol).piece_type),
            "grid": {"x": file_index, "y": rank_index},
        }
        if geometry is not None:
            machine = grid_to_machine(GridPosition(file_index, rank_index), geometry)
            item["machine_mm"] = {"x": machine.x, "y": machine.y}
        payload.append(item)
    return payload


class SolTranscriber:
    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = MODEL,
        client: Any = None,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY", "").strip()
        if client is None and not key:
            raise ConfigurationError(
                "OPENAI_API_KEY is required for Sol board recognition"
            )
        if client is None:
            try:
                import httpx
                from openai import OpenAI
            except ImportError as exc:
                raise ConfigurationError(
                    "OpenAI vision dependencies are not installed; run uv sync"
                ) from exc
            client = OpenAI(
                api_key=key,
                timeout=httpx.Timeout(
                    35.0, connect=5.0, read=30.0, write=30.0, pool=5.0
                ),
                max_retries=1,
            )
        self.client = client
        self.model = model

    def transcribe(self, jpeg: bytes) -> BoardTranscription:
        data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "developer", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Transcribe this physical chessboard.",
                        },
                        {
                            "type": "input_image",
                            "image_url": data_url,
                            "detail": "original",
                        },
                    ],
                },
            ],
            text_format=BoardTranscription,
            reasoning={"effort": "none"},
            max_output_tokens=700,
            store=False,
        )
        parsed = response.output_parsed
        if response.status != "completed" or parsed is None:
            raise ValidationError(
                f"Sol did not return a complete board transcription: {response.status}"
            )
        return parsed


class _ImageSource:
    def __init__(self, path: Path) -> None:
        cv2, _ = _vision_modules()
        self.frame = cv2.imread(str(path))
        if self.frame is None:
            raise ValidationError(f"could not read camera image {path}")

    def read(self) -> Any:
        return self.frame.copy()

    def close(self) -> None:
        return None


class _SnapshotSource:
    def __init__(self, url: str) -> None:
        self.url = url

    def read(self) -> Any:
        cv2, numpy = _vision_modules()
        errors = []
        for attempt in range(3):
            request = Request(
                f"{self.url}{'&' if '?' in self.url else '?'}_={time.time_ns()}",
                headers={
                    "Cache-Control": "no-cache, no-store",
                    "Pragma": "no-cache",
                    "Connection": "close",
                    "User-Agent": "ChessGantry/1",
                },
            )
            try:
                with urlopen(request, timeout=5) as response:
                    content_type = response.headers.get_content_type()
                    payload = response.read(15_000_000)
                if content_type not in {"image/jpeg", "image/jpg", "image/png"}:
                    raise ValidationError(
                        f"snapshot endpoint returned {content_type}, not an image"
                    )
                frame = cv2.imdecode(
                    numpy.frombuffer(payload, dtype=numpy.uint8), cv2.IMREAD_COLOR
                )
                if frame is None:
                    raise ValidationError(
                        "snapshot endpoint did not return a supported image"
                    )
                return frame
            except Exception as exc:
                errors.append(str(exc))
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        raise ValidationError("snapshot failed after 3 attempts: " + " | ".join(errors))

    def close(self) -> None:
        return None


class _MjpegSource:
    def __init__(self, url: str, opener: Callable[..., Any] = urlopen) -> None:
        self.url = url
        self._opener = opener
        self._response: Any = None
        self._buffer = bytearray()

    def _connect(self) -> None:
        request = Request(
            self.url,
            headers={
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
                "User-Agent": "ChessGantry/1",
            },
        )
        response = self._opener(request, timeout=6)
        content_type = response.headers.get("Content-Type", "").lower()
        if "multipart/" not in content_type and "mjpeg" not in content_type:
            response.close()
            raise ValidationError(
                f"MJPEG endpoint returned {content_type or 'unknown content type'}"
            )
        self._response = response

    def read(self) -> Any:
        cv2, numpy = _vision_modules()
        if self._response is None:
            self._connect()
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            start = self._buffer.find(b"\xff\xd8")
            if start >= 0:
                end = self._buffer.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    payload = bytes(self._buffer[start : end + 2])
                    del self._buffer[: end + 2]
                    frame = cv2.imdecode(
                        numpy.frombuffer(payload, dtype=numpy.uint8), cv2.IMREAD_COLOR
                    )
                    if frame is not None:
                        return frame
            read_available = getattr(self._response, "read1", self._response.read)
            chunk = read_available(16_384)
            if not chunk:
                raise ValidationError("MJPEG stream ended before the next frame")
            self._buffer.extend(chunk)
            if len(self._buffer) > 20_000_000:
                del self._buffer[:-2_000_000]
        raise ValidationError(
            "MJPEG stream timed out waiting for a complete JPEG frame"
        )

    def close(self) -> None:
        if self._response is not None:
            self._response.close()
            self._response = None
        self._buffer.clear()


class _VideoSource:
    def __init__(self, source: str) -> None:
        cv2, _ = _vision_modules()
        value: Any = int(source) if source.isdigit() else source
        self.capture = cv2.VideoCapture()
        self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.capture.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        self.capture.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
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


class _AutoSource:
    def __init__(self, base_url: str) -> None:
        base = base_url.rstrip("/")
        self.candidates = (
            f"{base}/video",
            f"snapshot:{base}/shot.jpg",
        )
        self.reader: Any = None
        self.resolved_source: Optional[str] = None
        self._next_index = 0

    def read(self) -> Any:
        if self.reader is not None:
            try:
                return self.reader.read()
            except Exception:
                if self.resolved_source in self.candidates:
                    self._next_index = (
                        self.candidates.index(self.resolved_source) + 1
                    ) % len(self.candidates)
                self.reader.close()
                self.reader = None
                self.resolved_source = None
        errors = []
        ordered = (
            self.candidates[self._next_index :] + self.candidates[: self._next_index]
        )
        for candidate in ordered:
            try:
                reader = open_frame_source(candidate)
                frame = reader.read()
                self.reader = reader
                self.resolved_source = candidate
                self._next_index = self.candidates.index(candidate)
                return frame
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
        raise ValidationError("phone camera endpoints failed: " + " | ".join(errors))

    def close(self) -> None:
        if self.reader is not None:
            self.reader.close()
            self.reader = None


def open_frame_source(source: str) -> Any:
    normalized = source.strip()
    if not normalized:
        raise ValidationError("camera source is required")
    if normalized.startswith("auto:"):
        return _AutoSource(normalized[5:])
    if normalized.startswith("browser:"):
        raise ValidationError(
            "browser camera sources receive frames through the web UI"
        )
    if normalized.startswith("snapshot:"):
        return _SnapshotSource(normalized[9:])
    if normalized.startswith(("http://", "https://")):
        return _MjpegSource(normalized)
    path = Path(normalized)
    if path.is_file():
        return _ImageSource(path)
    return _VideoSource(normalized)


def probe_camera_source(
    source: str, source_factory: Callable[[str], Any] = open_frame_source
) -> dict[str, Any]:
    started = time.monotonic()
    probe_source = source
    if source.startswith("browser:"):
        probe_source = f"snapshot:{source[8:].rstrip('/')}/shot.jpg"
    reader = source_factory(probe_source)
    try:
        frame = reader.read()
        height, width = frame.shape[:2]
        jpeg = encode_jpeg(frame)
        return {
            "ok": True,
            "source": source,
            "resolved_source": getattr(reader, "resolved_source", probe_source),
            "width": width,
            "height": height,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "jpeg": jpeg,
        }
    finally:
        reader.close()


class SolVisionManager:
    def __init__(
        self,
        *,
        source: str = DEFAULT_PHONE_SOURCE,
        orientation: str = "white_bottom",
        rotation: int = 0,
        interval_s: float = 3.0,
        transcriber: Optional[Any] = None,
        move_handler: Optional[Callable[[str], None]] = None,
        geometry: Optional[Any] = None,
        calibration_path: Optional[Path] = None,
        color_profile_path: Optional[Path] = None,
        source_factory: Callable[[str], Any] = open_frame_source,
    ) -> None:
        if interval_s < 1:
            raise ConfigurationError("Sol scan interval must be at least one second")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._capture_wake = threading.Event()
        self._inference_wake = threading.Event()
        self._source = source
        self._orientation = orientation
        self._rotation = rotation
        self._interval = interval_s
        self._transcriber = transcriber
        self._move_handler = move_handler
        self._geometry = geometry
        self._calibration_path = calibration_path
        self._color_profiles = ColorProfiles(color_profile_path)
        self._source_factory = source_factory
        self._enabled = False
        self._running = False
        self._reader: Any = None
        self._active_source: Optional[str] = None
        self._resolved_source: Optional[str] = None
        self._raw_jpeg: Optional[bytes] = None
        self._plan_jpeg: Optional[bytes] = None
        self._latest_jpeg: Optional[bytes] = None
        self._capture_error: Optional[str] = None
        self._inference_error: Optional[str] = None
        self._transcription: Optional[BoardTranscription] = None
        self._candidate: Optional[tuple[str, ...]] = None
        self._stable = 0
        self._board: Any = None
        self._last_move: Optional[str] = None
        self._paused = False
        self._frames = 0
        self._requests = 0
        self._frame_generation = 0
        self._inferred_generation = 0
        self._last_frame_at: Optional[float] = None
        self._consecutive_errors = 0
        self._local_candidate: Optional[tuple[tuple[int, str], ...]] = None
        self._local_stable = 0
        self._local_confidence = 0.0
        self._local_unresolved: tuple[str, ...] = ()
        self._local_types: dict[int, str] = {}
        self._local_error: Optional[str] = None
        self._local_last_at: Optional[float] = None
        self._local_move: Optional[str] = None
        self._local_callback_pending = False
        self._local_frames = 0
        self._local_last_ms: Optional[float] = None
        self._local_started_at = time.monotonic()
        self._calibration: Optional[tuple[tuple[float, float], ...]] = None
        if calibration_path is not None and calibration_path.exists():
            raw = read_json(calibration_path)
            self._calibration = validate_calibration(raw.get("corners"))
            self._rotation = int(raw.get("rotation", self._rotation))
            self._orientation = str(raw.get("orientation", self._orientation))
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._inference_thread = threading.Thread(
            target=self._inference_loop, daemon=True
        )
        self._capture_thread.start()
        self._inference_thread.start()

    def configure(
        self,
        *,
        source: str,
        enabled: bool,
        orientation: Optional[str] = None,
        rotation: Optional[int] = None,
    ) -> dict[str, Any]:
        if enabled and not source.strip():
            raise ValidationError("camera source is required")
        if orientation is not None and orientation not in {
            "white_bottom",
            "black_bottom",
        }:
            raise ValidationError("orientation must be white_bottom or black_bottom")
        if rotation is not None and rotation not in {0, 90, 180, 270}:
            raise ValidationError("rotation must be 0, 90, 180, or 270 degrees")
        with self._lock:
            changed = source.strip() != self._source
            rotation_changed = rotation is not None and rotation != self._rotation
            self._source = source.strip()
            self._enabled = enabled
            if orientation is not None:
                self._orientation = orientation
            if rotation is not None:
                self._rotation = rotation
            if changed or rotation_changed:
                self._calibration = None
                if self._calibration_path is not None:
                    self._calibration_path.unlink(missing_ok=True)
            self._capture_error = None
            self._inference_error = None
        self._capture_wake.set()
        self._inference_wake.set()
        return self.status()

    def start_game(self, fen: Optional[str] = None) -> dict[str, Any]:
        import chess

        with self._lock:
            self._board = chess.Board(fen) if fen else chess.Board()
            self._candidate = None
            self._stable = 0
            self._local_candidate = None
            self._local_stable = 0
            self._last_move = None
            self._inference_error = None
        return self.status()

    def apply_expected_move(self, uci: str) -> dict[str, Any]:
        import chess

        with self._lock:
            if self._board is None:
                self._board = chess.Board()
            move = chess.Move.from_uci(uci)
            if move not in self._board.legal_moves:
                raise ValidationError(
                    f"expected move {uci} is illegal in the current camera game"
                )
            self._board.push(move)
            self._candidate = None
            self._stable = 0
            self._local_candidate = None
            self._local_stable = 0
        return self.status()

    def set_move_handler(self, handler: Optional[Callable[[str], None]]) -> None:
        with self._lock:
            self._move_handler = handler

    def pause_inference(self, paused: bool) -> None:
        with self._lock:
            self._paused = bool(paused)

    def preview_jpeg(self) -> bytes:
        with self._lock:
            if self._latest_jpeg is None:
                raise ValidationError("no camera frame is available")
            return self._latest_jpeg

    def raw_preview_jpeg(self) -> bytes:
        with self._lock:
            if self._raw_jpeg is None:
                raise ValidationError("no raw camera frame is available")
            return self._raw_jpeg

    def ingest_browser_jpeg(self, payload: bytes) -> dict[str, Any]:
        cv2, numpy = _vision_modules()
        if not payload or len(payload) > 8_000_000:
            raise ValidationError("browser camera JPEG must be between 1 byte and 8 MB")
        frame = cv2.imdecode(
            numpy.frombuffer(payload, dtype=numpy.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            raise ValidationError("browser camera upload is not a valid JPEG")
        self._store_frame(frame, already_rotated=True)
        return self.status()

    def probe(self, source: str) -> dict[str, Any]:
        result = probe_camera_source(source, self._source_factory)
        jpeg = result.pop("jpeg")
        with self._lock:
            self._raw_jpeg = jpeg
            self._plan_jpeg = jpeg
            self._latest_jpeg = jpeg
            self._last_frame_at = time.time()
            self._frames += 1
            self._frame_generation += 1
            self._capture_error = None
            self._resolved_source = result["resolved_source"]
        return result

    def calibrate_aruco(self) -> dict[str, Any]:
        cv2, numpy = _vision_modules()
        with self._lock:
            jpeg = self._raw_jpeg
        if jpeg is None:
            raise ValidationError(
                "no raw camera frame is available for ArUco calibration"
            )
        frame = cv2.imdecode(
            numpy.frombuffer(jpeg, dtype=numpy.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            raise ValidationError("raw camera frame cannot be decoded")
        return self.calibrate(detect_aruco_references(frame))

    def sample_piece_color(self, color: str, x: float, y: float) -> dict[str, Any]:
        cv2, numpy = _vision_modules()
        with self._lock:
            jpeg = self._plan_jpeg
        if jpeg is None:
            raise ValidationError("no plan-view frame is available for color sampling")
        frame = cv2.imdecode(
            numpy.frombuffer(jpeg, dtype=numpy.uint8), cv2.IMREAD_COLOR
        )
        if frame is None:
            raise ValidationError("plan-view frame cannot be decoded")
        return self._color_profiles.sample(frame, color, x, y)

    def calibrate(self, corners: Any) -> dict[str, Any]:
        parsed = validate_calibration(corners)
        with self._lock:
            self._calibration = parsed
            self._candidate = None
            self._stable = 0
            if self._calibration_path is not None:
                atomic_write_json(
                    self._calibration_path,
                    {
                        "schema_version": 1,
                        "orientation": self._orientation,
                        "rotation": self._rotation,
                        "corners": [{"x": x, "y": y} for x, y in parsed],
                    },
                )
        self._capture_wake.set()
        self._inference_wake.set()
        return self.status()

    def clear_calibration(self) -> dict[str, Any]:
        with self._lock:
            self._calibration = None
            self._candidate = None
            self._stable = 0
            if self._calibration_path is not None:
                self._calibration_path.unlink(missing_ok=True)
        self._capture_wake.set()
        self._inference_wake.set()
        return self.status()

    def _get_transcriber(self) -> Any:
        if self._transcriber is None:
            self._transcriber = SolTranscriber()
        return self._transcriber

    def _store_frame(self, frame: Any, *, already_rotated: bool = False) -> None:
        cv2, _ = _vision_modules()
        if not already_rotated:
            if self._rotation == 90:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            elif self._rotation == 180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            elif self._rotation == 270:
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        raw_jpeg = encode_jpeg(frame)
        plan_frame = (
            warp_board_frame(frame, self._calibration)
            if self._calibration is not None
            else frame
        )
        plan_jpeg = encode_jpeg(plan_frame)
        with self._lock:
            self._raw_jpeg = raw_jpeg
            self._plan_jpeg = plan_jpeg
            self._latest_jpeg = plan_jpeg
            self._frames += 1
            self._frame_generation += 1
            self._last_frame_at = time.time()
            self._consecutive_errors = 0
            self._running = True
            self._capture_error = None
            self._resolved_source = self._source
        self._process_local(plan_frame)
        self._inference_wake.set()

    def _process_local(self, plan_frame: Any) -> None:
        if self._calibration is None:
            return
        started = time.monotonic()
        try:
            observation = detect_colored_board(
                plan_frame, self._color_profiles, orientation=self._orientation
            )
            annotated = annotate_local_board(
                plan_frame, observation, orientation=self._orientation
            )
            annotated_jpeg = encode_jpeg(annotated)
            fingerprint = tuple(sorted(observation.types.items()))
            with self._lock:
                self._local_types = dict(observation.types)
                self._local_confidence = observation.confidence
                self._local_unresolved = observation.unresolved
                self._local_error = None
                self._local_last_at = time.time()
                self._latest_jpeg = annotated_jpeg
                self._local_frames += 1
                self._local_last_ms = round((time.monotonic() - started) * 1000, 2)
                if fingerprint == self._local_candidate:
                    self._local_stable += 1
                else:
                    self._local_candidate = fingerprint
                    self._local_stable = 1
                board = (
                    self._board.copy(stack=True) if self._board is not None else None
                )
                ready = (
                    board is not None
                    and self._color_profiles.ready
                    and self._local_stable >= 3
                    and observation.complete
                    and observation.confidence >= 0.45
                    and not self._paused
                    and not self._local_callback_pending
                )
            if ready:
                move = infer_type_move(board, observation.types)
                if move is not None:
                    self._dispatch_local_move(move.uci())
        except Exception as exc:
            with self._lock:
                self._local_error = str(exc)
                self._local_stable = 0

    def _dispatch_local_move(self, uci: str) -> None:
        with self._lock:
            if self._local_callback_pending:
                return
            self._local_callback_pending = True

        def run() -> None:
            try:
                handler = self._move_handler
                if handler is not None:
                    handler(uci)
                with self._lock:
                    if self._board is not None:
                        move = self._board.parse_uci(uci)
                        self._board.push(move)
                    self._local_move = uci
                    self._last_move = uci
                    self._local_candidate = None
                    self._local_stable = 0
            except Exception as exc:
                with self._lock:
                    self._local_error = str(exc)
            finally:
                with self._lock:
                    self._local_callback_pending = False

        threading.Thread(target=run, daemon=True).start()

    def _accept(self, result: BoardTranscription) -> None:
        rows = result.rows.values()
        if result.status != "complete":
            self._candidate = None
            self._stable = 0
            return
        if rows == self._candidate:
            self._stable += 1
        else:
            self._candidate = rows
            self._stable = 1
        if self._stable != 2 or self._board is None or self._paused:
            return
        observed = image_rows_to_board(rows, self._orientation)
        move = infer_legal_move(self._board, observed)
        if move is None:
            return
        uci = move.uci()
        if self._move_handler is not None:
            self._move_handler(uci)
        self._board.push(move)
        self._last_move = uci

    def _capture_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled = self._enabled
                source = self._source
            if not enabled:
                with self._lock:
                    self._running = False
                if self._reader is not None:
                    self._reader.close()
                    self._reader = None
                    self._active_source = None
                self._capture_wake.wait(0.5)
                self._capture_wake.clear()
                continue
            if source.startswith("browser:"):
                with self._lock:
                    self._running = (
                        self._last_frame_at is not None
                        and time.time() - self._last_frame_at <= 3.0
                    )
                    if not self._running and self._last_frame_at is not None:
                        self._capture_error = (
                            "browser camera bridge stopped sending frames"
                        )
                self._capture_wake.wait(0.5)
                self._capture_wake.clear()
                continue
            try:
                if self._reader is None or source != self._active_source:
                    if self._reader is not None:
                        self._reader.close()
                    self._reader = self._source_factory(source)
                    self._active_source = source
                frame = self._reader.read()
                self._store_frame(frame)
                with self._lock:
                    self._resolved_source = getattr(
                        self._reader, "resolved_source", source
                    )
            except Exception as exc:
                with self._lock:
                    self._capture_error = str(exc)
                    self._running = False
                    self._consecutive_errors += 1
                if self._reader is not None:
                    self._reader.close()
                    self._reader = None
                    self._active_source = None
                    self._resolved_source = None
                self._capture_wake.wait(min(5.0, 0.5 * self._consecutive_errors))
                self._capture_wake.clear()
                continue
            delay = 1.0 if source.startswith("snapshot:") else 0.03
            self._capture_wake.wait(delay)
            self._capture_wake.clear()
        if self._reader is not None:
            self._reader.close()

    def _inference_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled = self._enabled
                paused = self._paused
                generation = self._frame_generation
                jpeg = self._plan_jpeg
                local_ready = (
                    self._color_profiles.ready
                    and self._local_stable >= 2
                    and not self._local_unresolved
                    and self._local_confidence >= 0.45
                    and self._local_error is None
                )
            if (
                not enabled
                or paused
                or jpeg is None
                or generation == self._inferred_generation
                or local_ready
            ):
                self._inference_wake.wait(0.5)
                self._inference_wake.clear()
                continue
            if (
                self._transcriber is None
                and not os.environ.get("OPENAI_API_KEY", "").strip()
            ):
                with self._lock:
                    self._inference_error = "OPENAI_API_KEY is required for Sol recognition; camera preview is connected"
                self._inference_wake.wait(self._interval)
                self._inference_wake.clear()
                continue
            try:
                result = self._get_transcriber().transcribe(jpeg)
                with self._lock:
                    self._requests += 1
                    self._inferred_generation = generation
                    self._transcription = result
                    self._inference_error = None
                    local_authoritative = self._color_profiles.ready and (
                        self._local_move is not None
                        or (
                            self._local_stable >= 2
                            and not self._local_unresolved
                            and self._local_error is None
                        )
                    )
                    if not local_authoritative:
                        self._accept(result)
            except Exception as exc:
                with self._lock:
                    self._inferred_generation = generation
                    self._inference_error = str(exc)
            self._inference_wake.wait(self._interval)
            self._inference_wake.clear()

    def close(self) -> None:
        self._stop.set()
        self._capture_wake.set()
        self._inference_wake.set()
        self._capture_thread.join(timeout=6)
        self._inference_thread.join(timeout=40)

    def status(self) -> dict[str, Any]:
        import chess

        with self._lock:
            result = self._transcription
            rows = result.rows.values() if result is not None else None
            local_standard = (
                self._color_profiles.ready
                and self._local_types == board_piece_types(chess.Board())
            )
            effective_rows = rows
            if (
                effective_rows is None
                and self._board is not None
                and self._color_profiles.ready
                and self._local_stable >= 2
            ):
                effective_rows = tuple(
                    "".join(
                        (
                            self._board.piece_at(
                                chess.square(file_index, rank)
                            ).symbol()
                            if self._board.piece_at(chess.square(file_index, rank))
                            else "."
                        )
                        for file_index in range(8)
                    )
                    for rank in range(7, -1, -1)
                )
            elif effective_rows is None and local_standard:
                effective_rows = tuple(
                    "".join(
                        (
                            chess.Board()
                            .piece_at(chess.square(file_index, rank))
                            .symbol()
                            if chess.Board().piece_at(chess.square(file_index, rank))
                            else "."
                        )
                        for file_index in range(8)
                    )
                    for rank in range(7, -1, -1)
                )
            standard_match = False
            if rows and result is not None and result.status == "complete":
                standard_match = image_rows_to_board(
                    rows, self._orientation
                ) == board_symbols(chess.Board())
            return {
                "enabled": self._enabled,
                "running": self._running,
                "source": self._source,
                "orientation": self._orientation,
                "rotation": self._rotation,
                "model": MODEL,
                "error": self._capture_error or self._inference_error,
                "capture_error": self._capture_error,
                "inference_error": self._inference_error,
                "resolved_source": self._resolved_source,
                "status": (
                    "complete"
                    if self._color_profiles.ready
                    and self._local_stable >= 3
                    and not self._local_unresolved
                    and self._local_confidence >= 0.45
                    else result.status if result else "idle"
                ),
                "confidence": result.confidence.model_dump() if result else None,
                "problems": list(result.problems) if result else [],
                "image_rows": list(rows) if rows else None,
                "rows": (
                    list(canonical_rows(effective_rows, self._orientation))
                    if effective_rows
                    else None
                ),
                "pieces": (
                    board_payload(effective_rows, self._orientation, self._geometry)
                    if effective_rows
                    else []
                ),
                "stable_observations": max(self._stable, self._local_stable),
                "last_move": self._last_move,
                "fen": self._board.fen() if self._board is not None else None,
                "turn": (
                    ("white" if self._board.turn == chess.WHITE else "black")
                    if self._board
                    else None
                ),
                "matches_standard_position": standard_match or local_standard,
                "frames": self._frames,
                "requests": self._requests,
                "paused": self._paused,
                "last_frame_at": self._last_frame_at,
                "frame_age_s": (
                    round(max(0.0, time.time() - self._last_frame_at), 3)
                    if self._last_frame_at is not None
                    else None
                ),
                "stale": (
                    self._last_frame_at is None
                    or time.time() - self._last_frame_at > 3.0
                ),
                "consecutive_errors": self._consecutive_errors,
                "calibrated": self._calibration is not None,
                "calibration": (
                    [{"x": x, "y": y} for x, y in self._calibration]
                    if self._calibration
                    else None
                ),
                "detection_mode": (
                    "local"
                    if self._color_profiles.ready
                    and (
                        (self._local_stable >= 2 and not self._local_unresolved)
                        or self._local_move is not None
                    )
                    else (
                        "calibrate_colors"
                        if not self._color_profiles.ready
                        else "sol_fallback" if result is not None else "waiting_local"
                    )
                ),
                "local": {
                    "confidence": self._local_confidence,
                    "stable_frames": self._local_stable,
                    "unresolved": list(self._local_unresolved),
                    "error": self._local_error,
                    "last_at": self._local_last_at,
                    "last_move": self._local_move,
                    "frames": self._local_frames,
                    "last_ms": self._local_last_ms,
                    "hz": round(
                        self._local_frames
                        / max(0.001, time.monotonic() - self._local_started_at),
                        2,
                    ),
                    "profiles": self._color_profiles.status(),
                    "profiles_ready": self._color_profiles.ready,
                    "detected": {
                        chess.square_name(square): piece_type
                        for square, piece_type in self._local_types.items()
                    },
                },
            }


VisionManager = SolVisionManager
