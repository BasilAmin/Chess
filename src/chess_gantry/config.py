from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union
import json
import math

from .errors import ConfigurationError
from .models import MachinePoint


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{name} must be a JSON object")
    return value


def _unknown(raw: Mapping[str, Any], allowed: Iterable[str], name: str) -> None:
    extra = set(raw) - set(allowed)
    if extra:
        raise ConfigurationError(
            f"{name} has unknown field(s): {', '.join(sorted(extra))}"
        )


def _number(
    value: Any, name: str, *, positive: bool = False, non_negative: bool = False
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    if positive and result <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    if non_negative and result < 0:
        raise ConfigurationError(f"{name} must be non-negative")
    return result


def _integer(
    value: Any, name: str, *, positive: bool = False, non_negative: bool = False
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if positive and value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    if non_negative and value < 0:
        raise ConfigurationError(f"{name} must be non-negative")
    return value


def _integer_sequence(value: Any, name: str) -> Tuple[int, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{name} must be an array of positive integers")
    result = []
    for index, item in enumerate(value):
        parsed = _integer(item, f"{name}[{index}]", positive=True)
        if parsed not in result:
            result.append(parsed)
    return tuple(result)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be true or false")
    return value


def _string(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _commands(value: Any, name: str, *, allow_empty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigurationError(f"{name} must be an array of G-code strings")
    if not value and not allow_empty:
        raise ConfigurationError(f"{name} cannot be empty")
    commands = []
    for index, command in enumerate(value):
        text = _string(command, f"{name}[{index}]")
        if "\n" in text or "\r" in text:
            raise ConfigurationError(f"{name}[{index}] cannot contain a newline")
        if text.startswith(";"):
            raise ConfigurationError(
                f"{name}[{index}] must be a command, not only a comment"
            )
        commands.append(text)
    return tuple(commands)


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int
    fallback_baudrates: Tuple[int, ...]
    read_timeout_s: float
    write_timeout_s: float
    command_timeout_s: float
    startup_wait_s: float
    verify_marlin: bool
    handshake_timeout_s: float

    @property
    def auto_detect(self) -> bool:
        return self.port.strip().lower() in {"auto", "detect", ""}

    @property
    def candidate_baudrates(self) -> Tuple[int, ...]:
        ordered = [self.baudrate, *self.fallback_baudrates]
        return tuple(dict.fromkeys(ordered))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SerialSettings":
        _unknown(
            raw,
            {
                "port",
                "baudrate",
                "fallback_baudrates",
                "read_timeout_s",
                "write_timeout_s",
                "command_timeout_s",
                "startup_wait_s",
                "verify_marlin",
                "handshake_timeout_s",
            },
            "serial",
        )
        return cls(
            port=_string(raw.get("port", "auto"), "serial.port"),
            baudrate=_integer(
                raw.get("baudrate", 115200), "serial.baudrate", positive=True
            ),
            fallback_baudrates=_integer_sequence(
                raw.get("fallback_baudrates", [115200, 250000]),
                "serial.fallback_baudrates",
            ),
            read_timeout_s=_number(
                raw.get("read_timeout_s", 0.25), "serial.read_timeout_s", positive=True
            ),
            write_timeout_s=_number(
                raw.get("write_timeout_s", 2.0), "serial.write_timeout_s", positive=True
            ),
            command_timeout_s=_number(
                raw.get("command_timeout_s", 300.0),
                "serial.command_timeout_s",
                positive=True,
            ),
            startup_wait_s=_number(
                raw.get("startup_wait_s", 2.5),
                "serial.startup_wait_s",
                non_negative=True,
            ),
            verify_marlin=_boolean(
                raw.get("verify_marlin", True), "serial.verify_marlin"
            ),
            handshake_timeout_s=_number(
                raw.get("handshake_timeout_s", 5.0),
                "serial.handshake_timeout_s",
                positive=True,
            ),
        )


@dataclass(frozen=True)
class BoardGeometry:
    width: int
    height: int
    square_size_mm: float
    origin_x_mm: float
    origin_y_mm: float
    flip_x: bool
    flip_y: bool
    swap_xy: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BoardGeometry":
        _unknown(
            raw,
            {
                "width",
                "height",
                "square_size_mm",
                "origin_x_mm",
                "origin_y_mm",
                "flip_x",
                "flip_y",
                "swap_xy",
            },
            "board",
        )
        width = _integer(raw.get("width", 8), "board.width", positive=True)
        height = _integer(raw.get("height", 8), "board.height", positive=True)
        if width > 64 or height > 64:
            raise ConfigurationError("board dimensions are unreasonably large")
        return cls(
            width=width,
            height=height,
            square_size_mm=_number(
                raw.get("square_size_mm"), "board.square_size_mm", positive=True
            ),
            origin_x_mm=_number(raw.get("origin_x_mm"), "board.origin_x_mm"),
            origin_y_mm=_number(raw.get("origin_y_mm"), "board.origin_y_mm"),
            flip_x=_boolean(raw.get("flip_x", False), "board.flip_x"),
            flip_y=_boolean(raw.get("flip_y", False), "board.flip_y"),
            swap_xy=_boolean(raw.get("swap_xy", False), "board.swap_xy"),
        )


@dataclass(frozen=True)
class Workspace:
    min_x_mm: float
    max_x_mm: float
    min_y_mm: float
    max_y_mm: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Workspace":
        _unknown(raw, {"min_x_mm", "max_x_mm", "min_y_mm", "max_y_mm"}, "workspace")
        result = cls(
            min_x_mm=_number(raw.get("min_x_mm"), "workspace.min_x_mm"),
            max_x_mm=_number(raw.get("max_x_mm"), "workspace.max_x_mm"),
            min_y_mm=_number(raw.get("min_y_mm"), "workspace.min_y_mm"),
            max_y_mm=_number(raw.get("max_y_mm"), "workspace.max_y_mm"),
        )
        if result.max_x_mm <= result.min_x_mm or result.max_y_mm <= result.min_y_mm:
            raise ConfigurationError("workspace maximums must be greater than minimums")
        return result

    def contains(self, point: MachinePoint, tolerance: float = 1e-6) -> bool:
        return (
            self.min_x_mm - tolerance <= point.x <= self.max_x_mm + tolerance
            and self.min_y_mm - tolerance <= point.y <= self.max_y_mm + tolerance
        )


@dataclass(frozen=True)
class MotionSettings:
    travel_feed_mm_min: float
    drag_feed_mm_min: float
    magnet_on_dwell_ms: int
    magnet_off_dwell_ms: int
    park_after_move: bool
    park_position: Optional[MachinePoint]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MotionSettings":
        _unknown(
            raw,
            {
                "travel_feed_mm_min",
                "drag_feed_mm_min",
                "magnet_on_dwell_ms",
                "magnet_off_dwell_ms",
                "park_after_move",
                "park_x_mm",
                "park_y_mm",
            },
            "motion",
        )
        park_after = _boolean(
            raw.get("park_after_move", True), "motion.park_after_move"
        )
        park_x = raw.get("park_x_mm")
        park_y = raw.get("park_y_mm")
        if park_after and (park_x is None or park_y is None):
            raise ConfigurationError(
                "motion.park_x_mm and motion.park_y_mm are required when parking is enabled"
            )
        if not park_after and (park_x is None) != (park_y is None):
            raise ConfigurationError(
                "motion park coordinates must be provided together"
            )
        park = None
        if park_x is not None and park_y is not None:
            park = MachinePoint(
                _number(park_x, "motion.park_x_mm"), _number(park_y, "motion.park_y_mm")
            )
        return cls(
            travel_feed_mm_min=_number(
                raw.get("travel_feed_mm_min", 4000.0),
                "motion.travel_feed_mm_min",
                positive=True,
            ),
            drag_feed_mm_min=_number(
                raw.get("drag_feed_mm_min", 900.0),
                "motion.drag_feed_mm_min",
                positive=True,
            ),
            magnet_on_dwell_ms=_integer(
                raw.get("magnet_on_dwell_ms", 250),
                "motion.magnet_on_dwell_ms",
                non_negative=True,
            ),
            magnet_off_dwell_ms=_integer(
                raw.get("magnet_off_dwell_ms", 250),
                "motion.magnet_off_dwell_ms",
                non_negative=True,
            ),
            park_after_move=park_after,
            park_position=park,
        )


@dataclass(frozen=True)
class MagnetSettings:
    on_commands: Tuple[str, ...]
    move_on_commands: Tuple[str, ...]
    capture_on_commands: Tuple[str, ...]
    off_commands: Tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MagnetSettings":
        _unknown(
            raw,
            {
                "on_commands",
                "move_on_commands",
                "capture_on_commands",
                "off_commands",
            },
            "magnet",
        )
        on_commands = _commands(
            raw.get("on_commands", ["M106 P0 S255"]),
            "magnet.on_commands",
        )
        return cls(
            on_commands=on_commands,
            move_on_commands=_commands(
                raw.get("move_on_commands", on_commands),
                "magnet.move_on_commands",
            ),
            capture_on_commands=_commands(
                raw.get("capture_on_commands", on_commands),
                "magnet.capture_on_commands",
            ),
            off_commands=_commands(
                raw.get("off_commands", ["M107 P0"]),
                "magnet.off_commands",
            ),
        )


@dataclass(frozen=True)
class PlannerSettings:
    kind: str
    grid_step_mm: float
    obstacle_keepout_mm: float
    allow_diagonal: bool
    simplify_path: bool
    max_expanded_nodes: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PlannerSettings":
        _unknown(
            raw,
            {
                "kind",
                "grid_step_mm",
                "obstacle_keepout_mm",
                "allow_diagonal",
                "simplify_path",
                "max_expanded_nodes",
            },
            "planner",
        )
        kind = _string(raw.get("kind", "astar"), "planner.kind").lower()
        if kind not in {"astar", "direct"}:
            raise ConfigurationError("planner.kind must be 'astar' or 'direct'")
        return cls(
            kind=kind,
            grid_step_mm=_number(
                raw.get("grid_step_mm", 5.0), "planner.grid_step_mm", positive=True
            ),
            obstacle_keepout_mm=_number(
                raw.get("obstacle_keepout_mm", 18.0),
                "planner.obstacle_keepout_mm",
                non_negative=True,
            ),
            allow_diagonal=_boolean(
                raw.get("allow_diagonal", True), "planner.allow_diagonal"
            ),
            simplify_path=_boolean(
                raw.get("simplify_path", True), "planner.simplify_path"
            ),
            max_expanded_nodes=_integer(
                raw.get("max_expanded_nodes", 100000),
                "planner.max_expanded_nodes",
                positive=True,
            ),
        )


@dataclass(frozen=True)
class CaptureSettings:
    enabled: bool
    slots: Tuple[MachinePoint, ...]
    mode: str
    eject_points: Tuple[MachinePoint, ...]
    buffer_points: Tuple[MachinePoint, ...]
    magnetic_keepout_mm: float
    planner_grid_step_mm: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CaptureSettings":
        _unknown(
            raw,
            {
                "enabled",
                "slots",
                "mode",
                "eject_points",
                "buffer_points",
                "magnetic_keepout_mm",
                "planner_grid_step_mm",
            },
            "capture",
        )
        enabled = _boolean(raw.get("enabled", False), "capture.enabled")
        mode = _string(raw.get("mode", "slots"), "capture.mode").lower()
        if mode not in {"slots", "eject"}:
            raise ConfigurationError("capture.mode must be 'slots' or 'eject'")
        raw_slots = raw.get("slots", [])
        if not isinstance(raw_slots, list):
            raise ConfigurationError("capture.slots must be an array of [x_mm, y_mm]")
        slots = []
        for index, item in enumerate(raw_slots):
            if not isinstance(item, list) or len(item) != 2:
                raise ConfigurationError(f"capture.slots[{index}] must be [x_mm, y_mm]")
            slots.append(
                MachinePoint(
                    _number(item[0], f"capture.slots[{index}][0]"),
                    _number(item[1], f"capture.slots[{index}][1]"),
                )
            )

        def points(name: str) -> Tuple[MachinePoint, ...]:
            value = raw.get(name, [])
            if not isinstance(value, list):
                raise ConfigurationError(
                    f"capture.{name} must be an array of [x_mm, y_mm]"
                )
            result = []
            for index, item in enumerate(value):
                if not isinstance(item, list) or len(item) != 2:
                    raise ConfigurationError(
                        f"capture.{name}[{index}] must be [x_mm, y_mm]"
                    )
                result.append(
                    MachinePoint(
                        _number(item[0], f"capture.{name}[{index}][0]"),
                        _number(item[1], f"capture.{name}[{index}][1]"),
                    )
                )
            return tuple(result)

        eject_points = points("eject_points")
        buffer_points = points("buffer_points")
        if enabled and mode == "slots" and not slots:
            raise ConfigurationError(
                "capture.enabled is true, but no capture slots are configured"
            )
        if enabled and mode == "eject" and not eject_points:
            raise ConfigurationError(
                "capture eject mode requires at least one eject point"
            )
        if enabled and mode == "eject" and not buffer_points:
            raise ConfigurationError(
                "capture eject mode requires at least one castling buffer point"
            )
        return cls(
            enabled=enabled,
            slots=tuple(slots),
            mode=mode,
            eject_points=eject_points,
            buffer_points=buffer_points,
            magnetic_keepout_mm=_number(
                raw.get("magnetic_keepout_mm", 30.0),
                "capture.magnetic_keepout_mm",
                positive=True,
            ),
            planner_grid_step_mm=_number(
                raw.get("planner_grid_step_mm", 15.0),
                "capture.planner_grid_step_mm",
                positive=True,
            ),
        )


@dataclass(frozen=True)
class SafetySettings:
    calibrated: bool
    home_before_execute: bool
    home_commands: Tuple[str, ...]
    preflight_commands: Tuple[str, ...]
    emergency_stop_command: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SafetySettings":
        _unknown(
            raw,
            {
                "calibrated",
                "home_before_execute",
                "home_commands",
                "preflight_commands",
                "emergency_stop_command",
            },
            "safety",
        )
        emergency = _string(
            raw.get("emergency_stop_command", "M112"), "safety.emergency_stop_command"
        )
        if "\n" in emergency or "\r" in emergency:
            raise ConfigurationError(
                "safety.emergency_stop_command cannot contain a newline"
            )
        return cls(
            calibrated=_boolean(raw.get("calibrated", False), "safety.calibrated"),
            home_before_execute=_boolean(
                raw.get("home_before_execute", False), "safety.home_before_execute"
            ),
            home_commands=_commands(
                raw.get(
                    "home_commands",
                    [
                        "M92 X80 Y80 Z80",
                        "M203 X50 Y50 Z50",
                        "M201 X500 Y500 Z500",
                        "M205 X5 Y5 Z5",
                        "G28 X Y Z",
                        "M400",
                    ],
                ),
                "safety.home_commands",
            ),
            preflight_commands=_commands(
                raw.get("preflight_commands", ["M115"]),
                "safety.preflight_commands",
                allow_empty=True,
            ),
            emergency_stop_command=emergency,
        )


@dataclass(frozen=True)
class AppConfig:
    serial: SerialSettings
    board: BoardGeometry
    workspace: Workspace
    motion: MotionSettings
    magnet: MagnetSettings
    planner: PlannerSettings
    capture: CaptureSettings
    safety: SafetySettings

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AppConfig":
        _unknown(
            raw,
            {
                "serial",
                "board",
                "workspace",
                "motion",
                "magnet",
                "planner",
                "capture",
                "safety",
            },
            "config",
        )
        result = cls(
            serial=SerialSettings.from_mapping(
                _mapping(raw.get("serial", {}), "serial")
            ),
            board=BoardGeometry.from_mapping(_mapping(raw.get("board", {}), "board")),
            workspace=Workspace.from_mapping(
                _mapping(raw.get("workspace", {}), "workspace")
            ),
            motion=MotionSettings.from_mapping(
                _mapping(raw.get("motion", {}), "motion")
            ),
            magnet=MagnetSettings.from_mapping(
                _mapping(raw.get("magnet", {}), "magnet")
            ),
            planner=PlannerSettings.from_mapping(
                _mapping(raw.get("planner", {}), "planner")
            ),
            capture=CaptureSettings.from_mapping(
                _mapping(raw.get("capture", {}), "capture")
            ),
            safety=SafetySettings.from_mapping(
                _mapping(raw.get("safety", {}), "safety")
            ),
        )
        result._validate_cross_fields()
        return result

    @classmethod
    def load(cls, path: Union[Path, str]) -> "AppConfig":
        config_path = Path(path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(
                f"configuration file not found: {config_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"invalid JSON in {config_path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        return cls.from_mapping(_mapping(raw, "config"))

    def _validate_cross_fields(self) -> None:
        if self.motion.park_position is not None and not self.workspace.contains(
            self.motion.park_position
        ):
            raise ConfigurationError("motion park position is outside the workspace")

        physical_x_count = self.board.height if self.board.swap_xy else self.board.width
        physical_y_count = self.board.width if self.board.swap_xy else self.board.height
        half_square = self.board.square_size_mm / 2.0
        board_min_x = self.board.origin_x_mm - half_square
        board_max_x = (
            self.board.origin_x_mm
            + (physical_x_count - 0.5) * self.board.square_size_mm
        )
        board_min_y = self.board.origin_y_mm - half_square
        board_max_y = (
            self.board.origin_y_mm
            + (physical_y_count - 0.5) * self.board.square_size_mm
        )

        seen_slots = set()
        physical_capture_points = (
            self.capture.slots
            if self.capture.mode == "slots"
            else self.capture.eject_points
        ) + self.capture.buffer_points
        for index, slot in enumerate(physical_capture_points):
            if not self.workspace.contains(slot):
                raise ConfigurationError(
                    f"capture point {index} is outside the workspace"
                )
            key = (round(slot.x, 6), round(slot.y, 6))
            if key in seen_slots:
                raise ConfigurationError(
                    f"capture point {index} duplicates another capture point"
                )
            seen_slots.add(key)
            if (
                board_min_x <= slot.x <= board_max_x
                and board_min_y <= slot.y <= board_max_y
            ):
                raise ConfigurationError(
                    f"capture point {index} lies inside the playing-board footprint"
                )

        if self.motion.park_position is not None:
            park_key = (
                round(self.motion.park_position.x, 6),
                round(self.motion.park_position.y, 6),
            )
            if park_key in seen_slots:
                raise ConfigurationError("motion park position overlaps a capture slot")

        if self.capture.enabled and self.capture.mode == "eject":
            if self.capture.magnetic_keepout_mm < self.planner.obstacle_keepout_mm:
                raise ConfigurationError(
                    "capture.magnetic_keepout_mm must be at least planner.obstacle_keepout_mm"
                )
            if self.capture.magnetic_keepout_mm >= self.board.square_size_mm:
                raise ConfigurationError(
                    "capture.magnetic_keepout_mm must be smaller than one square"
                )
            if self.capture.planner_grid_step_mm > self.board.square_size_mm / 2:
                raise ConfigurationError(
                    "capture.planner_grid_step_mm must not exceed half a square"
                )

        if self.magnet.on_commands == self.magnet.off_commands:
            raise ConfigurationError(
                "magnet on_commands and off_commands must not be identical"
            )

        if self.planner.grid_step_mm > max(
            self.workspace.max_x_mm - self.workspace.min_x_mm,
            self.workspace.max_y_mm - self.workspace.min_y_mm,
        ):
            raise ConfigurationError(
                "planner.grid_step_mm is larger than the workspace"
            )
