from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
import copy
import math
import re

from .errors import StateError, ValidationError

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_STATUS_BOARD = "board"
_STATUS_CAPTURED = "captured"
_STATUS_BUFFERED = "buffered"
_ALLOWED_STATUSES = {_STATUS_BOARD, _STATUS_CAPTURED, _STATUS_BUFFERED}


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer, got {value!r}")
    return value


def _valid_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValidationError(
            f"{name} must be 1-80 characters using letters, numbers, '.', '_', ':', or '-'"
        )
    return value


@dataclass(frozen=True, order=True)
class GridPosition:
    x: int
    y: int

    @classmethod
    def validated(
        cls, x: Any, y: Any, width: int, height: int, prefix: str = "position"
    ) -> "GridPosition":
        xi = _strict_int(x, f"{prefix}.x")
        yi = _strict_int(y, f"{prefix}.y")
        if not 0 <= xi < width:
            raise ValidationError(f"{prefix}.x must be in 0..{width - 1}, got {xi}")
        if not 0 <= yi < height:
            raise ValidationError(f"{prefix}.y must be in 0..{height - 1}, got {yi}")
        return cls(x=xi, y=yi)

    def to_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class CaptureSpec:
    piece_id: str
    position: GridPosition

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.piece_id, "x": self.position.x, "y": self.position.y}


@dataclass(frozen=True)
class MoveDelta:
    piece_id: str
    previous: GridPosition
    new: GridPosition
    event_id: Optional[str] = None
    capture: Optional[CaptureSpec] = None

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], width: int = 8, height: int = 8
    ) -> "MoveDelta":
        if not isinstance(raw, Mapping):
            raise ValidationError("move JSON must be an object")

        payload: Dict[str, Any] = dict(raw)
        nested = payload.get("position")
        if isinstance(nested, Mapping):
            unknown_root = set(payload) - {"position", "event_id"}
            if unknown_root:
                raise ValidationError(
                    "nested move JSON has unknown top-level field(s): "
                    + ", ".join(sorted(unknown_root))
                )
            nested_payload = dict(nested)
            if "event_id" in payload and "event_id" not in nested_payload:
                nested_payload["event_id"] = payload["event_id"]
            payload = nested_payload

        allowed = {"position", "id", "px", "py", "nx", "ny", "event_id", "capture"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError(
                "unknown move field(s): " + ", ".join(sorted(unknown))
            )

        position_id = (
            payload.get("position")
            if isinstance(payload.get("position"), str)
            else None
        )
        explicit_id = payload.get("id")
        if position_id is None and explicit_id is None:
            raise ValidationError(
                "move JSON needs a piece id in 'position' (original format) or 'id'"
            )
        if (
            position_id is not None
            and explicit_id is not None
            and position_id != explicit_id
        ):
            raise ValidationError("'position' and 'id' disagree")
        piece_id = _valid_identifier(
            explicit_id if explicit_id is not None else position_id, "piece id"
        )

        missing = [name for name in ("px", "py", "nx", "ny") if name not in payload]
        if missing:
            raise ValidationError("missing move field(s): " + ", ".join(missing))

        previous = GridPosition.validated(
            payload["px"], payload["py"], width, height, "previous"
        )
        new = GridPosition.validated(payload["nx"], payload["ny"], width, height, "new")
        if previous == new:
            raise ValidationError("previous and new positions are identical")

        event_value = payload.get("event_id")
        event_id = (
            None if event_value is None else _valid_identifier(event_value, "event_id")
        )

        capture_raw = payload.get("capture")
        capture = None
        if capture_raw is not None:
            if not isinstance(capture_raw, Mapping):
                raise ValidationError("capture must be an object with id, x, and y")
            capture_unknown = set(capture_raw) - {"id", "x", "y"}
            if capture_unknown:
                raise ValidationError(
                    "capture has unknown field(s): "
                    + ", ".join(sorted(capture_unknown))
                )
            missing_capture = [
                name for name in ("id", "x", "y") if name not in capture_raw
            ]
            if missing_capture:
                raise ValidationError(
                    "capture is missing field(s): " + ", ".join(missing_capture)
                )
            captured_id = _valid_identifier(capture_raw["id"], "capture.id")
            if captured_id == piece_id:
                raise ValidationError("a piece cannot capture itself")
            captured_position = GridPosition.validated(
                capture_raw["x"], capture_raw["y"], width, height, "capture"
            )
            if captured_position == previous:
                raise ValidationError(
                    "capture position cannot be the moving piece's source square"
                )
            capture = CaptureSpec(piece_id=captured_id, position=captured_position)

        return cls(
            piece_id=piece_id,
            previous=previous,
            new=new,
            event_id=event_id,
            capture=capture,
        )

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "position": self.piece_id,
            "px": self.previous.x,
            "py": self.previous.y,
            "nx": self.new.x,
            "ny": self.new.y,
        }
        if self.event_id is not None:
            result["event_id"] = self.event_id
        if self.capture is not None:
            result["capture"] = self.capture.to_dict()
        return result


@dataclass(frozen=True)
class PieceState:
    piece_id: str
    status: str = _STATUS_BOARD
    x: Optional[int] = None
    y: Optional[int] = None
    capture_slot: Optional[int] = None
    machine_x: Optional[float] = None
    machine_y: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        piece_id: str,
        raw: Mapping[str, Any],
        width: int,
        height: int,
    ) -> "PieceState":
        pid = _valid_identifier(piece_id, "piece id")
        if not isinstance(raw, Mapping):
            raise ValidationError(f"piece {pid!r} must be a JSON object")
        allowed = {
            "status",
            "x",
            "y",
            "capture_slot",
            "machine_x",
            "machine_y",
            "metadata",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(
                f"piece {pid!r} has unknown field(s): {', '.join(sorted(unknown))}"
            )

        status = raw.get("status", _STATUS_BOARD)
        if status not in _ALLOWED_STATUSES:
            raise ValidationError(
                f"piece {pid!r} status must be one of {sorted(_ALLOWED_STATUSES)}"
            )

        metadata = raw.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValidationError(f"piece {pid!r} metadata must be an object")
        metadata_copy = copy.deepcopy(dict(metadata))

        if status == _STATUS_BOARD:
            if "x" not in raw or "y" not in raw:
                raise ValidationError(f"piece {pid!r} on the board needs x and y")
            pos = GridPosition.validated(
                raw["x"], raw["y"], width, height, f"piece {pid}"
            )
            if raw.get("capture_slot") is not None:
                raise ValidationError(
                    f"piece {pid!r} on the board cannot have capture_slot"
                )
            if raw.get("machine_x") is not None or raw.get("machine_y") is not None:
                raise ValidationError(
                    f"piece {pid!r} on the board cannot have buffered machine coordinates"
                )
            return cls(
                piece_id=pid, status=status, x=pos.x, y=pos.y, metadata=metadata_copy
            )

        if raw.get("x") is not None or raw.get("y") is not None:
            raise ValidationError(
                f"off-board piece {pid!r} must use null/omitted x and y"
            )
        if status == _STATUS_BUFFERED:
            machine_x = raw.get("machine_x")
            machine_y = raw.get("machine_y")
            if isinstance(machine_x, bool) or not isinstance(machine_x, (int, float)):
                raise ValidationError(f"buffered piece {pid!r} needs numeric machine_x")
            if isinstance(machine_y, bool) or not isinstance(machine_y, (int, float)):
                raise ValidationError(f"buffered piece {pid!r} needs numeric machine_y")
            if not math.isfinite(float(machine_x)) or not math.isfinite(
                float(machine_y)
            ):
                raise ValidationError(
                    f"buffered piece {pid!r} machine coordinates must be finite"
                )
            if raw.get("capture_slot") is not None:
                raise ValidationError(f"buffered piece {pid!r} cannot use capture_slot")
            return cls(
                piece_id=pid,
                status=status,
                machine_x=float(machine_x),
                machine_y=float(machine_y),
                metadata=metadata_copy,
            )
        slot = _strict_int(raw.get("capture_slot"), f"piece {pid}.capture_slot")
        if slot < 0:
            raise ValidationError(f"piece {pid!r} capture_slot must be non-negative")
        if raw.get("machine_x") is not None or raw.get("machine_y") is not None:
            raise ValidationError(
                f"captured piece {pid!r} cannot have machine coordinates"
            )
        return cls(
            piece_id=pid,
            status=status,
            capture_slot=slot,
            metadata=metadata_copy,
        )

    @property
    def board_position(self) -> Optional[GridPosition]:
        if self.status != _STATUS_BOARD:
            return None
        assert self.x is not None and self.y is not None
        return GridPosition(self.x, self.y)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": self.status}
        if self.status == _STATUS_BOARD:
            result.update({"x": self.x, "y": self.y})
        else:
            result.update({"x": None, "y": None})
            if self.status == _STATUS_CAPTURED:
                result["capture_slot"] = self.capture_slot
            else:
                result.update(
                    {"machine_x": self.machine_x, "machine_y": self.machine_y}
                )
        if self.metadata:
            result["metadata"] = copy.deepcopy(dict(self.metadata))
        return result


@dataclass(frozen=True)
class BoardState:
    schema_version: int
    revision: int
    pieces: Mapping[str, PieceState]
    processed_events: Tuple[str, ...] = ()

    @classmethod
    def standard(cls, width: int = 8, height: int = 8) -> "BoardState":
        if width != 8 or height != 8:
            raise ValidationError(
                "automatic game initialization currently requires an 8x8 board"
            )
        pieces: Dict[str, PieceState] = {}
        back_rank = (
            ("rook", "a"),
            ("knight", "b"),
            ("bishop", "c"),
            ("queen", "d"),
            ("king", "e"),
            ("bishop", "f"),
            ("knight", "g"),
            ("rook", "h"),
        )
        for color, pawn_y, back_y in (("white", 1, 0), ("black", 6, 7)):
            for x, file_name in enumerate("abcdefgh"):
                piece_id = f"{color}_pawn_{file_name}"
                pieces[piece_id] = PieceState(
                    piece_id, x=x, y=pawn_y, metadata={"color": color, "kind": "pawn"}
                )
            for x, (kind, file_name) in enumerate(back_rank):
                piece_id = f"{color}_{kind}_{file_name}"
                pieces[piece_id] = PieceState(
                    piece_id, x=x, y=back_y, metadata={"color": color, "kind": kind}
                )
        return cls(schema_version=1, revision=0, pieces=pieces)

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], width: int = 8, height: int = 8
    ) -> "BoardState":
        if not isinstance(raw, Mapping):
            raise ValidationError("board state must be a JSON object")
        allowed = {"schema_version", "revision", "pieces", "processed_events"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValidationError(
                "board state has unknown field(s): " + ", ".join(sorted(unknown))
            )

        schema_version = _strict_int(raw.get("schema_version", 1), "schema_version")
        if schema_version not in {1, 2}:
            raise ValidationError(
                f"unsupported board state schema_version {schema_version}; expected 1 or 2"
            )
        revision = _strict_int(raw.get("revision", 0), "revision")
        if revision < 0:
            raise ValidationError("revision must be non-negative")

        pieces_raw = raw.get("pieces")
        if not isinstance(pieces_raw, Mapping):
            raise ValidationError(
                "board state 'pieces' must be an object keyed by piece id"
            )
        pieces: Dict[str, PieceState] = {}
        occupied: Dict[GridPosition, str] = {}
        capture_slots: Dict[int, str] = {}
        for piece_id, piece_raw in pieces_raw.items():
            piece = PieceState.from_mapping(str(piece_id), piece_raw, width, height)
            if piece.piece_id in pieces:
                raise ValidationError(f"duplicate piece id {piece.piece_id!r}")
            pieces[piece.piece_id] = piece
            if piece.status == _STATUS_BOARD:
                pos = piece.board_position
                assert pos is not None
                if pos in occupied:
                    raise ValidationError(
                        f"pieces {occupied[pos]!r} and {piece.piece_id!r} both occupy ({pos.x}, {pos.y})"
                    )
                occupied[pos] = piece.piece_id
            elif piece.status == _STATUS_CAPTURED:
                assert piece.capture_slot is not None
                if piece.capture_slot in capture_slots:
                    raise ValidationError(
                        f"pieces {capture_slots[piece.capture_slot]!r} and {piece.piece_id!r} both use "
                        f"capture slot {piece.capture_slot}"
                    )
                capture_slots[piece.capture_slot] = piece.piece_id
            else:
                if schema_version != 2:
                    raise ValidationError(
                        "buffered pieces require board state schema_version 2"
                    )
                if piece.machine_x is None or piece.machine_y is None:
                    raise ValidationError(
                        f"buffered piece {piece.piece_id!r} lacks machine coordinates"
                    )

        events_raw = raw.get("processed_events", [])
        if not isinstance(events_raw, list):
            raise ValidationError("processed_events must be an array")
        events = tuple(
            _valid_identifier(item, "processed event id") for item in events_raw
        )
        if len(events) != len(set(events)):
            raise ValidationError("processed_events contains duplicates")

        return cls(
            schema_version=schema_version,
            revision=revision,
            pieces=pieces,
            processed_events=events,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "pieces": {
                piece_id: self.pieces[piece_id].to_dict()
                for piece_id in sorted(self.pieces)
            },
            "processed_events": list(self.processed_events),
        }

    def piece_at(self, position: GridPosition) -> Optional[PieceState]:
        for piece in self.pieces.values():
            if piece.board_position == position:
                return piece
        return None

    def board_pieces(self) -> Iterable[PieceState]:
        return (
            piece for piece in self.pieces.values() if piece.status == _STATUS_BOARD
        )

    def used_capture_slots(self) -> Dict[int, str]:
        return {
            piece.capture_slot: piece.piece_id
            for piece in self.pieces.values()
            if piece.status == _STATUS_CAPTURED and piece.capture_slot is not None
        }

    def buffer_piece(
        self, piece_id: str, point: MachinePoint, event_id: Optional[str] = None
    ) -> "BoardState":
        if not math.isfinite(point.x) or not math.isfinite(point.y):
            raise StateError("castling buffer coordinates must be finite")
        piece = self.pieces.get(piece_id)
        if piece is None or piece.status != _STATUS_BOARD:
            raise StateError(f"piece {piece_id!r} is not available on the board")
        pieces = dict(self.pieces)
        pieces[piece_id] = replace(
            piece,
            status=_STATUS_BUFFERED,
            x=None,
            y=None,
            capture_slot=None,
            machine_x=point.x,
            machine_y=point.y,
        )
        events = list(self.processed_events)
        if event_id:
            events.append(_valid_identifier(event_id, "event_id"))
        return BoardState(
            2,
            self.revision + 1,
            pieces,
            tuple(events[-512:]),
        )

    def unbuffer_piece(
        self,
        piece_id: str,
        destination: GridPosition,
        event_id: Optional[str] = None,
    ) -> "BoardState":
        piece = self.pieces.get(piece_id)
        if piece is None or piece.status != _STATUS_BUFFERED:
            raise StateError(f"piece {piece_id!r} is not in the castling buffer")
        if self.piece_at(destination) is not None:
            raise StateError("castling buffer destination is occupied")
        pieces = dict(self.pieces)
        pieces[piece_id] = replace(
            piece,
            status=_STATUS_BOARD,
            x=destination.x,
            y=destination.y,
            machine_x=None,
            machine_y=None,
        )
        events = list(self.processed_events)
        if event_id:
            events.append(_valid_identifier(event_id, "event_id"))
        return BoardState(
            self.schema_version,
            self.revision + 1,
            pieces,
            tuple(events[-512:]),
        )

    def validate_move(self, move: MoveDelta) -> Optional[PieceState]:
        if move.event_id is not None and move.event_id in self.processed_events:
            raise StateError(f"event {move.event_id!r} has already been applied")
        moving = self.pieces.get(move.piece_id)
        if moving is None:
            raise StateError(f"piece id {move.piece_id!r} is not in board state")
        if moving.status != _STATUS_BOARD:
            raise StateError(f"piece {move.piece_id!r} is captured, not on the board")
        if moving.board_position != move.previous:
            actual = moving.board_position
            assert actual is not None
            raise StateError(
                f"piece {move.piece_id!r} is stored at ({actual.x}, {actual.y}), not "
                f"({move.previous.x}, {move.previous.y})"
            )
        source_occupant = self.piece_at(move.previous)
        if source_occupant is None or source_occupant.piece_id != move.piece_id:
            raise StateError("source square does not contain the stated piece")

        destination_occupant = self.piece_at(move.new)
        if move.capture is None:
            return destination_occupant

        captured = self.piece_at(move.capture.position)
        if captured is None:
            raise StateError(
                f"explicit capture square ({move.capture.position.x}, {move.capture.position.y}) is empty"
            )
        if captured.piece_id != move.capture.piece_id:
            raise StateError(
                f"explicit capture expected {move.capture.piece_id!r}, but square contains {captured.piece_id!r}"
            )
        if captured.piece_id == move.piece_id:
            raise StateError("a piece cannot capture itself")
        if move.capture.position != move.new and destination_occupant is not None:
            raise StateError(
                "off-destination capture requires an empty destination square; use a normal destination capture instead"
            )
        if move.capture.position == move.new and destination_occupant != captured:
            raise StateError(
                "explicit destination capture does not match destination occupant"
            )
        return captured

    def capture_piece(
        self,
        piece_id: str,
        capture_slot: int,
        event_id: Optional[str] = None,
        max_events: int = 512,
    ) -> "BoardState":
        piece = self.pieces.get(piece_id)
        if piece is None or piece.status != _STATUS_BOARD:
            raise StateError(f"capture target {piece_id!r} is not on the board")
        if capture_slot < 0 or capture_slot in self.used_capture_slots():
            raise StateError(f"capture slot {capture_slot} is not available")
        pieces = dict(self.pieces)
        pieces[piece_id] = replace(
            piece,
            status=_STATUS_CAPTURED,
            x=None,
            y=None,
            capture_slot=capture_slot,
        )
        events = list(self.processed_events)
        if event_id is not None:
            events.append(_valid_identifier(event_id, "event_id"))
        return BoardState(
            self.schema_version,
            self.revision + 1,
            pieces,
            tuple(events[-max_events:]),
        )

    def applied(
        self, move: MoveDelta, capture_slot: Optional[int], max_events: int = 512
    ) -> "BoardState":
        captured = self.validate_move(move)
        if captured is not None and capture_slot is None:
            raise StateError(
                "the move captures a piece, but no capture slot was assigned"
            )
        if captured is None and capture_slot is not None:
            raise StateError("capture slot was assigned for a non-capture move")

        new_pieces: Dict[str, PieceState] = dict(self.pieces)
        if captured is not None:
            assert capture_slot is not None
            new_pieces[captured.piece_id] = replace(
                captured,
                status=_STATUS_CAPTURED,
                x=None,
                y=None,
                capture_slot=capture_slot,
            )

        moving = new_pieces[move.piece_id]
        new_pieces[move.piece_id] = replace(
            moving,
            status=_STATUS_BOARD,
            x=move.new.x,
            y=move.new.y,
            capture_slot=None,
        )

        events = list(self.processed_events)
        if move.event_id is not None:
            events.append(move.event_id)
            events = events[-max_events:]

        return BoardState(
            schema_version=self.schema_version,
            revision=self.revision + 1,
            pieces=new_pieces,
            processed_events=tuple(events),
        )


@dataclass(frozen=True)
class MachinePoint:
    x: float
    y: float

    def to_dict(self) -> Dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True)
class PieceTransfer:
    piece_id: str
    purpose: str
    start: MachinePoint
    end: MachinePoint
    path: Tuple[MachinePoint, ...]
    capture_slot: Optional[int] = None

    def __post_init__(self) -> None:
        if self.purpose not in {"move", "capture"}:
            raise ValueError(f"invalid transfer purpose {self.purpose!r}")
        if len(self.path) < 2:
            raise ValueError("transfer path needs at least start and end")
        if self.path[0] != self.start or self.path[-1] != self.end:
            raise ValueError("transfer path endpoints do not match start/end")
