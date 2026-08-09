from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from .errors import ConfigurationError, ValidationError
from .lichess_pgn import lichess_client


Matrix = tuple[tuple[int, ...], ...]


def validate_matrix(value: Any) -> Matrix:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValidationError("sensor matrix must be an array of 8 rows")
    if len(value) != 8:
        raise ValidationError("sensor matrix must contain exactly 8 rows")
    rows = []
    for row_index, row in enumerate(value):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ValidationError(f"sensor matrix row {row_index} must be an array")
        if len(row) != 8:
            raise ValidationError(
                f"sensor matrix row {row_index} must contain 8 values"
            )
        parsed = []
        for column_index, item in enumerate(row):
            if (
                isinstance(item, bool)
                or not isinstance(item, int)
                or item not in {0, 1}
            ):
                raise ValidationError(
                    f"sensor matrix [{row_index}][{column_index}] must be 0 or 1"
                )
            parsed.append(item)
        rows.append(tuple(parsed))
    return tuple(rows)


def board_matrix(board: Any) -> Matrix:
    import chess

    return tuple(
        tuple(
            1 if board.piece_at(chess.square(file_index, 7 - row)) else 0
            for file_index in range(8)
        )
        for row in range(8)
    )


def matrix_after_lift(board: Any, uci: str) -> Matrix:
    import chess

    move = chess.Move.from_uci(uci)
    values = [list(row) for row in board_matrix(board)]
    row = 7 - chess.square_rank(move.from_square)
    column = chess.square_file(move.from_square)
    values[row][column] = 0
    return tuple(tuple(row_values) for row_values in values)


@dataclass(frozen=True)
class SensorEvent:
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


class SyntheticBoardSensor:
    def __init__(
        self,
        *,
        sample_hz: float = 300.0,
        stable_samples: int = 3,
        move_submitter: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        if sample_hz <= 0:
            raise ConfigurationError("sensor sample rate must be positive")
        if stable_samples < 1:
            raise ConfigurationError("sensor stable sample count must be positive")
        import chess

        self.sample_hz = float(sample_hz)
        self.stable_samples = stable_samples
        self._move_submitter = move_submitter or self._submit_to_lichess
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._board = chess.Board()
        self._input = board_matrix(self._board)
        self._last_sample: Optional[Matrix] = None
        self._stable_count = 0
        self._processed: Optional[Matrix] = self._input
        self._samples = 0
        self._started = time.monotonic()
        self._state = "ready"
        self._last_move: Optional[str] = None
        self._candidates: tuple[str, ...] = ()
        self._error: Optional[str] = None
        self._events: list[SensorEvent] = []
        self._game_id: Optional[str] = None
        self._write_lichess = False
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    @staticmethod
    def _submit_to_lichess(game_id: str, uci: str) -> None:
        token = os.environ.get("LICHESS_TOKEN", "").strip()
        if not token:
            raise ConfigurationError(
                "LICHESS_TOKEN is required to submit moves through the Lichess Board API"
            )
        client = lichess_client(token)
        client.board.make_move(game_id, uci)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)

    def _event(self, kind: str, message: str, uci: Optional[str] = None) -> None:
        self._events.append(
            SensorEvent(datetime.now(timezone.utc).isoformat(), kind, message, uci)
        )
        self._events = self._events[-100:]

    def _sample_loop(self) -> None:
        interval = 1.0 / self.sample_hz
        deadline = time.monotonic()
        while not self._stop.is_set():
            deadline += interval
            with self._lock:
                current = self._input
                self._samples += 1
                if current == self._last_sample:
                    self._stable_count += 1
                else:
                    self._last_sample = current
                    self._stable_count = 1
                if (
                    self._stable_count >= self.stable_samples
                    and current != self._processed
                ):
                    self._processed = current
                    self._process_stable(current)
            wait = deadline - time.monotonic()
            if wait > 0:
                self._stop.wait(wait)
            else:
                deadline = time.monotonic()

    def _matching_moves(self, matrix: Matrix) -> tuple[Any, ...]:
        matches = []
        for move in self._board.legal_moves:
            candidate = self._board.copy(stack=False)
            candidate.push(move)
            if board_matrix(candidate) == matrix:
                matches.append(move)
        if not matches:
            return ()
        by_squares: dict[tuple[int, int], list[Any]] = {}
        for move in matches:
            by_squares.setdefault((move.from_square, move.to_square), []).append(move)
        normalized = []
        for variants in by_squares.values():
            queen = next((move for move in variants if move.promotion == 5), None)
            normalized.append(queen or variants[0])
        return tuple(normalized)

    def _process_stable(self, matrix: Matrix) -> None:
        if matrix == board_matrix(self._board):
            self._state = "ready"
            self._candidates = ()
            self._error = None
            return
        matches = self._matching_moves(matrix)
        if not matches:
            self._state = "waiting"
            self._candidates = ()
            self._error = None
            self._event(
                "matrix", "Stable occupancy does not yet match a legal completed move"
            )
            return
        if len(matches) > 1:
            self._state = "ambiguous"
            self._candidates = tuple(move.uci() for move in matches)
            self._error = "occupancy alone matches more than one legal move"
            self._event("ambiguous", self._error)
            return
        move = matches[0]
        uci = move.uci()
        try:
            if self._write_lichess:
                if self._game_id is None:
                    raise ConfigurationError(
                        "set a Lichess game ID before enabling move writes"
                    )
                self._move_submitter(self._game_id, uci)
                self._event(
                    "lichess", f"Submitted {uci} to Lichess game {self._game_id}", uci
                )
            self._board.push(move)
            self._state = "move"
            self._last_move = uci
            self._candidates = (uci,)
            self._error = None
            self._event("move", f"Detected legal move {uci}", uci)
            if move.promotion:
                self._event(
                    "promotion",
                    "Occupancy cannot identify promotion type; selected queen by default",
                    uci,
                )
        except Exception as exc:
            self._state = "error"
            self._error = str(exc)
            self._candidates = (uci,)
            self._event(
                "error", f"Move {uci} was detected but not submitted: {exc}", uci
            )

    def set_matrix(self, value: Any) -> dict[str, Any]:
        matrix = validate_matrix(value)
        with self._lock:
            self._input = matrix
        return self.status()

    def configure_lichess(self, game_id: Optional[str], write: bool) -> dict[str, Any]:
        normalized = game_id.strip() if isinstance(game_id, str) else ""
        if normalized and not normalized.isalnum():
            raise ValidationError(
                "Lichess game ID must contain only letters and digits"
            )
        if write and not normalized:
            raise ValidationError("a Lichess game ID is required when write is enabled")
        with self._lock:
            self._game_id = normalized or None
            self._write_lichess = bool(write)
            self._event(
                "config",
                f"Lichess writing {'enabled' if write else 'disabled'}"
                + (f" for {normalized}" if normalized else ""),
            )
        return self.status()

    def reset(self) -> dict[str, Any]:
        import chess

        with self._lock:
            self._board = chess.Board()
            self._input = board_matrix(self._board)
            self._last_sample = None
            self._stable_count = 0
            self._processed = self._input
            self._state = "ready"
            self._last_move = None
            self._candidates = ()
            self._error = None
            self._events = []
            self._event("reset", "Synthetic board reset to the standard position")
        return self.status()

    def retry(self) -> dict[str, Any]:
        with self._lock:
            self._processed = None
        return self.status()

    def apply_dataset(self, name: str) -> dict[str, Any]:
        import chess

        datasets = {
            "e2e4": "e2e4",
            "d2d4": "d2d4",
            "g1f3": "g1f3",
            "b1c3": "b1c3",
        }
        try:
            uci = datasets[name]
        except KeyError as exc:
            raise ValidationError(f"unknown sensor dataset: {name}") from exc
        self.reset()
        board = chess.Board()
        self.set_matrix(matrix_after_lift(board, uci))
        time.sleep(max(0.02, self.stable_samples / self.sample_hz * 1.5))
        board.push_uci(uci)
        self.set_matrix(board_matrix(board))
        return self.status()

    def datasets(self) -> list[dict[str, str]]:
        return [
            {"id": "e2e4", "label": "White pawn e2 to e4"},
            {"id": "d2d4", "label": "White pawn d2 to d4"},
            {"id": "g1f3", "label": "White knight g1 to f3"},
            {"id": "b1c3", "label": "White knight b1 to c3"},
        ]

    def status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = max(time.monotonic() - self._started, 0.001)
            pieces = []
            import chess

            for square, piece in self._board.piece_map().items():
                pieces.append(
                    {
                        "square": chess.square_name(square),
                        "symbol": piece.symbol(),
                        "type": chess.piece_name(piece.piece_type),
                        "color": "white" if piece.color else "black",
                    }
                )
            return {
                "matrix": [list(row) for row in self._input],
                "expected_matrix": [list(row) for row in board_matrix(self._board)],
                "fen": self._board.fen(),
                "turn": "white" if self._board.turn else "black",
                "pieces": sorted(pieces, key=lambda item: item["square"]),
                "state": self._state,
                "last_move": self._last_move,
                "candidates": list(self._candidates),
                "error": self._error,
                "sample_hz_target": self.sample_hz,
                "sample_hz_observed": round(self._samples / elapsed, 1),
                "samples": self._samples,
                "stable_samples": self.stable_samples,
                "game_id": self._game_id,
                "write_lichess": self._write_lichess,
                "events": [event.as_dict() for event in self._events],
                "datasets": self.datasets(),
            }
