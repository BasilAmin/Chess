from __future__ import annotations

from dataclasses import replace
import math
import re
import threading
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .config import AppConfig, SerialSettings
from .errors import (
    ConfigurationError,
    GantryError,
    PendingTransactionError,
    SerialProtocolError,
    ValidationError,
)
from .models import MachinePoint, MoveDelta
from .serial_link import DemoMarlinSerial, MarlinSerial, PortInfo, discover_serial_ports
from .serial_link import parse_endstop_states
from .service import GantryService, MotionPlan


_POSITION_RE = re.compile(
    r"\bX:\s*(-?\d+(?:\.\d+)?)\s+Y:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_INNER_POSITION_RE = re.compile(
    r"\bZ:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)

MAX_RAW_COMMAND_CHARS = 256
MAX_RAW_PROGRAM_COMMANDS = 200
_REFERENCE_PREFIXES = ("G28", "G92")


def normalize_raw_command(command: Any) -> str:
    if not isinstance(command, str):
        raise ValidationError("a raw G-code command must be a string")
    if "\n" in command or "\r" in command:
        raise ValidationError("a raw G-code command cannot contain a newline")
    stripped = command.split(";", 1)[0].strip()
    if not stripped:
        raise ValidationError("a raw G-code command cannot be empty")
    if len(stripped) > MAX_RAW_COMMAND_CHARS:
        raise ValidationError(
            f"a raw G-code command cannot exceed {MAX_RAW_COMMAND_CHARS} characters"
        )
    try:
        stripped.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValidationError("a raw G-code command must be ASCII") from exc
    return stripped


def split_raw_program(source: Any) -> Tuple[str, ...]:
    if isinstance(source, str):
        candidates: List[Any] = list(source.splitlines())
    elif isinstance(source, Sequence):
        candidates = list(source)
    else:
        raise ValidationError("raw G-code must be a string or a list of strings")

    commands: List[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and not candidate.split(";", 1)[0].strip():
            continue
        commands.append(normalize_raw_command(candidate))
    if not commands:
        raise ValidationError("no G-code commands were supplied")
    if len(commands) > MAX_RAW_PROGRAM_COMMANDS:
        raise ValidationError(
            f"a raw G-code batch cannot exceed {MAX_RAW_PROGRAM_COMMANDS} commands"
        )
    return tuple(commands)


class GantryController:

    def __init__(
        self,
        config: AppConfig,
        service: GantryService,
        *,
        link_factory: Optional[Callable[[SerialSettings], Any]] = None,
        demo: bool = False,
    ) -> None:
        self.config = config
        self.service = service
        self.demo = demo
        self._link_factory = link_factory or (
            (lambda settings: DemoMarlinSerial(settings))
            if demo
            else (lambda settings: MarlinSerial(settings))
        )
        self._link: Optional[Any] = None
        self._operation_lock = threading.RLock()
        self._homed = False
        self._position: Optional[MachinePoint] = None
        self._machine_position: Optional[tuple[float, float, float]] = None
        self._last_error: Optional[str] = None
        self._setup = {
            "diagnostics": False,
            "endstops": False,
            "homed": False,
            "movement": False,
            "magnet": False,
            "centers": False,
            "last_step": None,
            "last_error": None,
            "results": {},
        }

    def _reset_setup(self) -> None:
        self._setup = {
            "diagnostics": False,
            "endstops": False,
            "homed": False,
            "movement": False,
            "magnet": False,
            "centers": False,
            "last_step": None,
            "last_error": None,
            "results": {},
        }

    def _setup_result(self, step: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        self._setup[step] = True
        self._setup["last_step"] = step
        self._setup["last_error"] = None
        self._setup["results"][step] = dict(payload)
        return self.status()

    def _setup_failure(self, step: str, exc: Exception) -> None:
        self._setup["last_step"] = step
        self._setup["last_error"] = str(exc)
        self._last_error = str(exc)

    @property
    def connected(self) -> bool:
        return bool(self._link is not None and getattr(self._link, "connected", False))

    def available_ports(self) -> tuple[PortInfo, ...]:
        if self.demo:
            return (PortInfo("DEMO", "Simulated Marlin controller", "DEMO", True),)
        return discover_serial_ports()

    def status(self) -> dict[str, Any]:
        link = self._link
        try:
            revision = self.service.store.load().revision
        except Exception:
            revision = None
        return {
            "connected": self.connected,
            "port": None if link is None else getattr(link, "active_port", None),
            "baudrate": (
                None if link is None else getattr(link, "active_baudrate", None)
            ),
            "firmware": (
                None if link is None else getattr(link, "firmware_identity", None)
            ),
            "homed": self._homed,
            "position_mm": (
                {"x": self._position.x, "y": self._position.y}
                if self._position is not None
                else {"x": None, "y": None}
            ),
            "machine_position_mm": (
                {
                    "x": self._machine_position[0],
                    "y": self._machine_position[1],
                    "z": self._machine_position[2],
                }
                if self._machine_position is not None
                else {"x": None, "y": None, "z": None}
            ),
            "workspace_mm": {
                "min_x": self.config.workspace.min_x_mm,
                "max_x": self.config.workspace.max_x_mm,
                "min_y": self.config.workspace.min_y_mm,
                "max_y": self.config.workspace.max_y_mm,
            },
            "max_manual_feed_mm_min": self.config.motion.travel_feed_mm_min,
            "calibrated": self.config.safety.calibrated,
            "home_before_execute": self.config.safety.home_before_execute,
            "pending_transaction": self.service.journal.exists(),
            "board_revision": revision,
            "last_error": self._last_error,
            "demo": self.demo,
            "setup": dict(self._setup),
        }

    def connect(
        self,
        *,
        port: Optional[str] = None,
        baudrate: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._operation_lock:
            if self.connected:
                return self.status()
            settings = self.config.serial
            if port:
                settings = replace(settings, port=port)
            if baudrate is not None:
                if baudrate <= 0:
                    raise ValidationError("baudrate must be a positive integer")
                settings = replace(settings, baudrate=baudrate, fallback_baudrates=())
            link = self._link_factory(settings)
            try:
                link.connect()
            except Exception as exc:
                self._last_error = str(exc)
                try:
                    link.close()
                except Exception:
                    pass
                raise
            self._link = link
            self._homed = False
            self._position = None
            self._machine_position = None
            self._last_error = None
            self._reset_setup()
            return self.status()

    def disconnect(self) -> dict[str, Any]:
        with self._operation_lock:
            link = self._link
            self._link = None
            if link is not None:
                link.close()
            self._homed = False
            self._position = None
            self._machine_position = None
            self._reset_setup()
            return self.status()

    def _require_link(self) -> Any:
        if not self.connected or self._link is None:
            raise SerialProtocolError("connect to the Marlin controller first")
        return self._link

    def _update_position(self, responses: tuple[str, ...]) -> Optional[MachinePoint]:
        for line in responses:
            match = _POSITION_RE.search(line)
            if match:
                inner_match = _INNER_POSITION_RE.search(line)
                self._position = MachinePoint(
                    (
                        float(inner_match.group(1))
                        if inner_match is not None
                        else float(match.group(1))
                    ),
                    float(match.group(2)),
                )
                self._machine_position = (
                    float(match.group(1)),
                    float(match.group(2)),
                    (
                        float(inner_match.group(1))
                        if inner_match is not None
                        else float(match.group(1))
                    ),
                )
                return self._position
        return None

    def query_position(self) -> dict[str, Any]:
        with self._operation_lock:
            link = self._require_link()
            result = link.send_command("M114", timeout_s=10.0)
            self._update_position(result.responses)
            return self.status()

    def jog(
        self, *, delta_x_mm: float, delta_y_mm: float, feed_mm_min: float
    ) -> dict[str, Any]:
        values = (delta_x_mm, delta_y_mm, feed_mm_min)
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            raise ValidationError("jog deltas and feed rate must be numbers")
        delta_x_mm, delta_y_mm, feed_mm_min = map(float, values)
        if not all(math.isfinite(value) for value in values):
            raise ValidationError("jog deltas and feed rate must be finite")
        if (delta_x_mm == 0) == (delta_y_mm == 0):
            raise ValidationError("a jog must move exactly one logical axis")
        if max(abs(delta_x_mm), abs(delta_y_mm)) > 20.0:
            raise ValidationError("a single jog cannot exceed 20 mm")
        with self._operation_lock:
            if not self._homed:
                raise ConfigurationError("home the gantry before using keyboard jog")
            if self._position is None:
                result = self._require_link().send_command("M114", timeout_s=10.0)
                if self._update_position(result.responses) is None:
                    raise SerialProtocolError(
                        "M114 did not return a parseable position"
                    )
            assert self._position is not None
            target_x = self._position.x + delta_x_mm
            target_y = self._position.y + delta_y_mm
            return self.move_to_mm(
                x_mm=target_x,
                y_mm=target_y,
                feed_mm_min=feed_mm_min,
            )

    def check_endstops(self) -> tuple[str, ...]:
        with self._operation_lock:
            result = self._require_link().send_command("M119", timeout_s=10.0)
            return result.responses

    def run_setup_diagnostics(self) -> dict[str, Any]:
        with self._operation_lock:
            link = self._require_link()
            try:
                firmware = link.send_command("M115", timeout_s=10.0)
                identity = " | ".join(firmware.responses)
                if not self.demo and "relay chess gantry" not in identity.lower():
                    raise ConfigurationError(
                        "M115 did not identify the Relay Chess Gantry firmware"
                    )
                endstop_result = link.send_command("M119", timeout_s=10.0)
                states = parse_endstop_states(endstop_result.responses)
                required = ("x_min", "y_max", "z_max")
                missing = [name for name in required if name not in states]
                if missing:
                    raise ConfigurationError(
                        "M119 did not report required endstops: " + ", ".join(missing)
                    )
                position_result = link.send_command("M114", timeout_s=10.0)
                if self._update_position(position_result.responses) is None:
                    raise SerialProtocolError(
                        "M114 did not return a parseable X/Y/Z position"
                    )
                return self._setup_result(
                    "diagnostics",
                    {
                        "firmware": identity,
                        "endstops": states,
                        "position": self.status()["machine_position_mm"],
                    },
                )
            except Exception as exc:
                self._setup_failure("diagnostics", exc)
                raise

    def verify_setup_endstops(self) -> dict[str, Any]:
        with self._operation_lock:
            if not self._setup["diagnostics"]:
                raise ConfigurationError("run diagnostics before the endstop test")
            try:
                result = self._require_link().send_command("M119", timeout_s=10.0)
                states = parse_endstop_states(result.responses)
                required = ("x_min", "y_max", "z_max")
                missing = [name for name in required if name not in states]
                if missing:
                    raise ConfigurationError(
                        "M119 did not report required endstops: " + ", ".join(missing)
                    )
                return self._setup_result("endstops", {"states": states})
            except Exception as exc:
                self._setup_failure("endstops", exc)
                raise

    def _resolve_raw_timeout(self, timeout_s: Optional[float]) -> Optional[float]:
        if timeout_s is None:
            return None
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValidationError("the command timeout must be a number")
        value = float(timeout_s)
        if not math.isfinite(value) or value <= 0:
            raise ValidationError("the command timeout must be greater than zero")
        limit = self.config.serial.command_timeout_s
        if value > limit:
            raise ValidationError(
                f"the command timeout cannot exceed serial.command_timeout_s ({limit:g} s)"
            )
        return value

    def send_raw_program(
        self,
        source: Any,
        *,
        timeout_s: Optional[float] = None,
        stop_on_error: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        commands = split_raw_program(source)
        timeout = self._resolve_raw_timeout(timeout_s)
        blocked = self.config.safety.emergency_stop_command.split(";", 1)[0].strip()
        for command in commands:
            if command.upper() == blocked.upper():
                raise ValidationError(
                    f"{command} halts the controller and needs a power cycle; "
                    "use the emergency stop control instead"
                )

        results: List[dict[str, Any]] = []
        with self._operation_lock:
            link = self._require_link()
            for command in commands:
                try:
                    result = link.send_command(command, timeout_s=timeout)
                except GantryError as exc:
                    self._last_error = str(exc)
                    results.append(
                        {"command": command, "responses": [], "error": str(exc)}
                    )
                    if stop_on_error:
                        break
                    continue
                self._update_position(result.responses)
                if command.upper().startswith(_REFERENCE_PREFIXES):
                    self._homed = True
                self._last_error = None
                results.append(
                    {
                        "command": command,
                        "responses": list(result.responses),
                        "error": None,
                    }
                )
        return tuple(results)

    def home_xy(self) -> dict[str, Any]:
        with self._operation_lock:
            if self.service.journal.exists():
                raise PendingTransactionError(
                    f"pending transaction exists at {self.service.journal.path}; reconcile it first"
                )
            link = self._require_link()
            try:
                self.service.home_with_link(link)
                result = link.send_command("M114", timeout_s=10.0)
                if self._update_position(result.responses) is None:
                    raise SerialProtocolError(
                        "M114 did not return a parseable position after homing"
                    )
                expected = (2.0, 298.0, 328.0)
                assert self._machine_position is not None
                if any(
                    abs(actual - target) > 0.25
                    for actual, target in zip(self._machine_position, expected)
                ):
                    raise ConfigurationError(
                        "post-home M114 does not match X2 Y298 Z328 within 0.25 mm"
                    )
                self._homed = True
                self._setup["movement"] = False
                self._setup["magnet"] = False
                self._setup["centers"] = False
                self._last_error = None
                return self._setup_result(
                    "homed",
                    {"machine_position_mm": self.status()["machine_position_mm"]},
                )
            except Exception as exc:
                self._homed = False
                self._position = None
                self._machine_position = None
                self._setup_failure("homed", exc)
                raise

    def move_to_mm(
        self,
        *,
        x_mm: float,
        y_mm: float,
        feed_mm_min: float,
    ) -> dict[str, Any]:
        values = (x_mm, y_mm, feed_mm_min)
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            raise ValidationError("X, Y, and feed rate must be numbers")
        x_mm, y_mm, feed_mm_min = map(float, values)
        if not all(math.isfinite(value) for value in (x_mm, y_mm, feed_mm_min)):
            raise ValidationError("X, Y, and feed rate must be finite")
        if feed_mm_min <= 0 or feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ValidationError(
                "manual feed rate must be greater than zero and no more than "
                f"{self.config.motion.travel_feed_mm_min:g} mm/min"
            )
        point = MachinePoint(x_mm, y_mm)
        if not self.config.workspace.contains(point):
            raise ValidationError(
                "manual coordinate is outside the configured workspace"
            )

        with self._operation_lock:
            if not self._homed:
                raise ConfigurationError(
                    "initialize the outer X/Y axes before commanding an absolute coordinate"
                )
            link = self._require_link()
            link.send_program(
                (
                    "G21",
                    "G90",
                    *self.config.magnet.off_commands,
                    (
                        f"G1 X{self.config.workspace.min_y_mm + self.config.workspace.max_y_mm - y_mm:.3f} "
                        f"Y{y_mm:.3f} Z{x_mm:.3f} F{feed_mm_min:.0f}"
                    ),
                    "M400",
                )
            )
            self._position = point
            try:
                result = link.send_command("M114", timeout_s=10.0)
                self._update_position(result.responses)
            except Exception:
                pass
            self._last_error = None
            return self.status()

    def run_setup_movement_test(
        self, *, distance_mm: float = 5.0, feed_mm_min: float = 300.0
    ) -> dict[str, Any]:
        if distance_mm != 5.0 or feed_mm_min != 300.0:
            raise ValidationError("setup movement test is fixed at 5 mm and 300 mm/min")
        with self._operation_lock:
            if self.service.journal.exists():
                raise PendingTransactionError("resolve the pending transaction first")
            if not self._setup["homed"] or not self._homed:
                raise ConfigurationError(
                    "complete verified homing before movement test"
                )
            link = self._require_link()
            start = self._machine_position
            if start is None:
                raise ConfigurationError("verified home position is unavailable")
            program = (
                *self.config.magnet.off_commands,
                "G21",
                "G90",
                "M211 S1",
                "G1 X2 Y298 Z323 F300",
                "M400",
                "G1 X2 Y298 Z328 F300",
                "M400",
                "G1 X7 Y293 Z328 F300",
                "M400",
                "G1 X2 Y298 Z328 F300",
                "M400",
            )
            try:
                link.send_program(program)
                result = link.send_command("M114", timeout_s=10.0)
                if self._update_position(result.responses) is None:
                    raise SerialProtocolError(
                        "M114 did not verify movement-test return"
                    )
                assert self._machine_position is not None
                if any(
                    abs(actual - target) > 0.25
                    for actual, target in zip(self._machine_position, start)
                ):
                    raise ConfigurationError(
                        "movement test did not return to verified home within 0.25 mm"
                    )
                return self._setup_result(
                    "movement", {"distance_mm": 5.0, "program": list(program)}
                )
            except BaseException as exc:
                link.best_effort((*self.config.magnet.off_commands, "M84"))
                self._homed = False
                self._setup_failure("movement", exc)
                raise

    def run_setup_magnet_test(self) -> dict[str, Any]:
        with self._operation_lock:
            if self.service.journal.exists():
                raise PendingTransactionError("resolve the pending transaction first")
            if not self._setup["movement"]:
                raise ConfigurationError("complete the 5 mm movement test first")
            link = self._require_link()
            program = self.service.magnet_test_program(1.0)
            try:
                link.send_program(program)
                return self._setup_result(
                    "magnet", {"duration_s": 1.0, "program": list(program)}
                )
            except BaseException as exc:
                link.best_effort(self.config.magnet.off_commands)
                self._setup_failure("magnet", exc)
                raise

    def run_setup_square_centers(
        self, *, feed_mm_min: float = 1800.0, dwell_ms: int = 150
    ) -> dict[str, Any]:
        from .kinematics import grid_to_machine
        from .models import GridPosition

        if feed_mm_min != 1800.0 or dwell_ms != 150:
            raise ValidationError("setup center test uses fixed 1800 mm/min and 150 ms")
        with self._operation_lock:
            if self.service.journal.exists():
                raise PendingTransactionError("resolve the pending transaction first")
            if not self._setup["movement"] or not self._homed:
                raise ConfigurationError(
                    "complete verified homing and movement test first"
                )
            link = self._require_link()
            points = []
            for y in range(self.config.board.height):
                columns = (
                    range(self.config.board.width - 1, -1, -1)
                    if y % 2 == 0
                    else range(self.config.board.width)
                )
                points.extend(
                    grid_to_machine(GridPosition(x, y), self.config.board)
                    for x in columns
                )
            mirror = self.config.workspace.min_y_mm + self.config.workspace.max_y_mm
            program = [*self.config.magnet.off_commands, "G21", "G90", "M211 S1"]
            for point in points:
                program.extend(
                    (
                        f"G1 X{mirror - point.y:.3f} Y{point.y:.3f} Z{point.x:.3f} F1800",
                        "M400",
                        "G4 P150",
                    )
                )
            program.extend(
                (
                    *self.config.magnet.off_commands,
                    "G1 X2.000 Y298.000 Z328.000 F1800",
                    "M400",
                )
            )
            try:
                link.send_program(tuple(program))
                result = link.send_command("M114", timeout_s=10.0)
                if self._update_position(result.responses) is None:
                    raise SerialProtocolError("M114 did not verify center-test return")
                assert self._machine_position is not None
                if any(
                    abs(actual - target) > 0.25
                    for actual, target in zip(
                        self._machine_position, (2.0, 298.0, 328.0)
                    )
                ):
                    raise ConfigurationError(
                        "center test did not return to verified home within 0.25 mm"
                    )
                return self._setup_result(
                    "centers", {"square_count": len(points), "magnet": "off"}
                )
            except BaseException as exc:
                link.best_effort((*self.config.magnet.off_commands, "M84"))
                self._homed = False
                self._setup_failure("centers", exc)
                raise

    def run_setup_combined(self) -> dict[str, Any]:
        self.run_setup_diagnostics()
        self.verify_setup_endstops()
        self.home_xy()
        self.run_setup_movement_test()
        self.run_setup_magnet_test()
        self.run_setup_square_centers()
        return self.status()

    def _move_from_mapping(self, raw_move: Mapping[str, Any]) -> MoveDelta:
        return MoveDelta.from_mapping(
            raw_move,
            self.config.board.width,
            self.config.board.height,
        )

    def plan_move(self, raw_move: Mapping[str, Any]) -> MotionPlan:
        move = self._move_from_mapping(raw_move)
        if self.service.journal.exists():
            raise PendingTransactionError(
                f"pending transaction exists at {self.service.journal.path}; reconcile it first"
            )
        with self.service.store.locked():
            return self.service.plan(move, self.service.store.load())

    def execute_move(
        self,
        raw_move: Mapping[str, Any],
        *,
        confirm_motion: bool,
    ) -> MotionPlan:
        if not confirm_motion:
            raise ConfigurationError("execution requires explicit motion confirmation")
        move = self._move_from_mapping(raw_move)
        with self._operation_lock:
            link = self._require_link()
            if not self.config.safety.home_before_execute and not self._homed:
                raise ConfigurationError(
                    "home X/Y first, or enable safety.home_before_execute"
                )
            plan = self.service.execute_with_link(move, link)
            if self.config.safety.home_before_execute:
                self._homed = True
            if self.config.motion.park_after_move:
                self._position = self.config.motion.park_position
            else:
                self._position = plan.transfers[-1].end
            self._last_error = None
            return plan

    def board_state(self) -> dict[str, Any]:
        return self.service.store.load().to_dict()

    def pending_transaction(self) -> Optional[dict[str, Any]]:
        if not self.service.journal.exists():
            return None
        return dict(self.service.journal.load())

    def emergency_stop(self) -> dict[str, Any]:
        link = self._link
        if link is None or not getattr(link, "connected", False):
            raise SerialProtocolError("the Marlin controller is not connected")
        try:
            self.service.emergency_stop_with_link(link)
        finally:
            try:
                link.close()
            finally:
                self._link = None
                self._homed = False
                self._position = None
                self._machine_position = None
                self._reset_setup()
                self._last_error = (
                    "Emergency stop sent. Reset or power-cycle the controller, "
                    "then reconnect and re-home."
                )
        return self.status()
