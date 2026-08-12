from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
import math

from .config import AppConfig
from .errors import (
    ConfigurationError,
    PendingTransactionError,
    PlanningError,
    StateError,
    ValidationError,
)
from .gcode import GCodeGenerator, GCodeProgram
from .kinematics import grid_to_machine, validate_board_inside_workspace
from .models import (
    BoardState,
    GridPosition,
    MachinePoint,
    MoveDelta,
    PieceState,
    PieceTransfer,
)
from .path_planning import plan_path, safest_path_to_any_goal
from .persistence import (
    AuditLog,
    BoardStore,
    JournalStore,
    atomic_write_json,
    utc_now,
)
from .serial_link import MarlinSerial, parse_endstop_states


@dataclass(frozen=True)
class MotionPlan:
    move: MoveDelta
    base_revision: int
    captured_piece_id: Optional[str]
    capture_slot: Optional[int]
    transfers: Tuple[PieceTransfer, ...]
    program: GCodeProgram
    next_state: BoardState

    def journal_payload(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "prepared",
            "base_revision": self.base_revision,
            "next_revision": self.next_state.revision,
            "move": self.move.to_dict(),
            "captured_piece_id": self.captured_piece_id,
            "capture_slot": self.capture_slot,
            "transfers": [
                {
                    "piece_id": transfer.piece_id,
                    "purpose": transfer.purpose,
                    "start": transfer.start.to_dict(),
                    "end": transfer.end.to_dict(),
                    "path": [point.to_dict() for point in transfer.path],
                    "capture_slot": transfer.capture_slot,
                }
                for transfer in self.transfers
            ],
            "gcode_sha256": self.program.digest,
            "gcode": list(self.program.lines),
            "next_state": self.next_state.to_dict(),
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "piece_id": self.move.piece_id,
            "from": self.move.previous.to_dict(),
            "to": self.move.new.to_dict(),
            "capture": self.captured_piece_id,
            "capture_slot": self.capture_slot,
            "base_revision": self.base_revision,
            "next_revision": self.next_state.revision,
            "transfer_count": len(self.transfers),
            "gcode_command_count": len(self.program.commands),
            "gcode_sha256": self.program.digest,
        }


class GantryService:
    def __init__(
        self,
        config: AppConfig,
        state_path: Union[Path, str],
        journal_path: Union[Path, str],
        audit_path: Union[Path, str],
        link_factory: Optional[Callable[[Any], MarlinSerial]] = None,
    ) -> None:
        self.config = config
        validate_board_inside_workspace(config.board, config.workspace)
        self.store = BoardStore(state_path, config.board.width, config.board.height)
        self.journal = JournalStore(journal_path)
        self.audit = AuditLog(audit_path)
        self._link_factory = link_factory or (lambda settings: MarlinSerial(settings))
        self._generator = GCodeGenerator(config)

    def _capture_slot_for(self, state: BoardState) -> int:
        if not self.config.capture.enabled:
            raise PlanningError(
                "the destination is occupied, but captures are disabled. Configure real off-board capture slots first"
            )
        used = state.used_capture_slots()
        if self.config.capture.mode == "eject":
            return next(value for value in range(4096) if value not in used)
        for slot in range(len(self.config.capture.slots)):
            if slot not in used:
                return slot
        raise PlanningError("all configured capture slots are already occupied")

    def _capture_point_for(
        self, captured_position: MachinePoint, capture_slot: int
    ) -> MachinePoint:
        if self.config.capture.mode == "eject":
            return min(
                self.config.capture.eject_points,
                key=lambda point: math.hypot(
                    point.x - captured_position.x,
                    point.y - captured_position.y,
                ),
            )
        return self.config.capture.slots[capture_slot]

    def _capture_ejection_path(
        self,
        start: MachinePoint,
        obstacles: Sequence[MachinePoint],
    ) -> Tuple[MachinePoint, ...]:
        settings = replace(
            self.config.planner,
            kind="astar",
            obstacle_keepout_mm=self.config.capture.magnetic_keepout_mm,
        )
        return safest_path_to_any_goal(
            start,
            self.config.capture.eject_points,
            obstacles,
            self.config.workspace,
            settings,
        )

    def _physical_obstacles(
        self,
        state: BoardState,
        exclude_piece_ids: Iterable[str] = (),
        extra_capture_slots: Iterable[int] = (),
    ) -> Tuple[MachinePoint, ...]:
        excluded = set(exclude_piece_ids)
        points: List[MachinePoint] = []
        for piece in state.pieces.values():
            if piece.piece_id in excluded:
                continue
            if piece.status == "board":
                position = piece.board_position
                assert position is not None
                points.append(grid_to_machine(position, self.config.board))
            elif piece.capture_slot is not None:
                if (
                    not self.config.capture.enabled
                    or self.config.capture.mode == "eject"
                ):
                    continue
                if piece.capture_slot >= len(self.config.capture.slots):
                    raise StateError(
                        f"piece {piece.piece_id!r} uses capture slot {piece.capture_slot}, but only "
                        f"{len(self.config.capture.slots)} slot(s) are configured"
                    )
                points.append(self.config.capture.slots[piece.capture_slot])
            elif piece.status == "buffered":
                assert piece.machine_x is not None and piece.machine_y is not None
                points.append(MachinePoint(piece.machine_x, piece.machine_y))
        for slot in extra_capture_slots:
            if not 0 <= slot < len(self.config.capture.slots):
                raise StateError(f"capture slot {slot} is outside configured slots")
            points.append(self.config.capture.slots[slot])
        return tuple(points)

    def plan(self, move: MoveDelta, state: Optional[BoardState] = None) -> MotionPlan:
        board_state = state if state is not None else self.store.load()
        captured = board_state.validate_move(move)
        transfers: List[PieceTransfer] = []
        capture_slot: Optional[int] = None

        if captured is not None:
            capture_slot = self._capture_slot_for(board_state)
            captured_position = captured.board_position
            assert captured_position is not None
            capture_start = grid_to_machine(captured_position, self.config.board)
            capture_obstacles = self._physical_obstacles(
                board_state,
                exclude_piece_ids={captured.piece_id},
            )
            if self.config.capture.mode == "eject":
                capture_path = self._capture_ejection_path(
                    capture_start, capture_obstacles
                )
                capture_end = capture_path[-1]
            else:
                capture_end = self._capture_point_for(capture_start, capture_slot)
                capture_path = plan_path(
                    capture_start,
                    capture_end,
                    capture_obstacles,
                    self.config.workspace,
                    self.config.planner,
                )
            transfers.append(
                PieceTransfer(
                    piece_id=captured.piece_id,
                    purpose="capture",
                    start=capture_start,
                    end=capture_end,
                    path=capture_path,
                    capture_slot=capture_slot,
                )
            )

        move_start = grid_to_machine(move.previous, self.config.board)
        move_end = grid_to_machine(move.new, self.config.board)
        exclusions = {move.piece_id}
        if captured is not None:
            exclusions.add(captured.piece_id)
        move_obstacles = self._physical_obstacles(
            board_state,
            exclude_piece_ids=exclusions,
            extra_capture_slots=(
                (capture_slot,)
                if capture_slot is not None and self.config.capture.mode == "slots"
                else ()
            ),
        )
        move_path = plan_path(
            move_start,
            move_end,
            move_obstacles,
            self.config.workspace,
            self.config.planner,
        )
        transfers.append(
            PieceTransfer(
                piece_id=move.piece_id,
                purpose="move",
                start=move_start,
                end=move_end,
                path=move_path,
            )
        )

        next_state = board_state.applied(move, capture_slot)
        program = self._generator.generate(transfers)
        return MotionPlan(
            move=move,
            base_revision=board_state.revision,
            captured_piece_id=None if captured is None else captured.piece_id,
            capture_slot=capture_slot,
            transfers=tuple(transfers),
            program=program,
            next_state=next_state,
        )

    def plan_capture_ejection(
        self, move: MoveDelta, state: Optional[BoardState] = None
    ) -> MotionPlan:
        board_state = state if state is not None else self.store.load()
        captured = board_state.validate_move(move)
        if captured is None or self.config.capture.mode != "eject":
            raise PlanningError("capture ejection requires an ejection-mode capture")
        capture_slot = self._capture_slot_for(board_state)
        captured_position = captured.board_position
        assert captured_position is not None
        start = grid_to_machine(captured_position, self.config.board)
        obstacles = self._physical_obstacles(
            board_state, exclude_piece_ids={captured.piece_id}
        )
        path = self._capture_ejection_path(start, obstacles)
        end = path[-1]
        transfer = PieceTransfer(
            piece_id=captured.piece_id,
            purpose="capture",
            start=start,
            end=end,
            path=path,
            capture_slot=capture_slot,
        )
        next_state = board_state.capture_piece(captured.piece_id, capture_slot)
        return MotionPlan(
            move=move,
            base_revision=board_state.revision,
            captured_piece_id=captured.piece_id,
            capture_slot=capture_slot,
            transfers=(transfer,),
            program=self._generator.generate((transfer,)),
            next_state=next_state,
        )

    def plan_buffer_out(
        self,
        move: MoveDelta,
        buffer_point: MachinePoint,
        state: Optional[BoardState] = None,
    ) -> MotionPlan:
        board_state = state if state is not None else self.store.load()
        if buffer_point not in self.config.capture.buffer_points:
            raise PlanningError("castling buffer point is not configured")
        if board_state.validate_move(move) is not None:
            raise PlanningError("castling buffer transfer cannot capture")
        start = grid_to_machine(move.previous, self.config.board)
        obstacles = self._physical_obstacles(
            board_state, exclude_piece_ids={move.piece_id}
        )
        path = plan_path(
            start,
            buffer_point,
            obstacles,
            self.config.workspace,
            self.config.planner,
        )
        transfer = PieceTransfer(
            piece_id=move.piece_id,
            purpose="move",
            start=start,
            end=buffer_point,
            path=path,
        )
        next_state = board_state.buffer_piece(
            move.piece_id, buffer_point, move.event_id
        )
        return MotionPlan(
            move=move,
            base_revision=board_state.revision,
            captured_piece_id=None,
            capture_slot=None,
            transfers=(transfer,),
            program=self._generator.generate((transfer,)),
            next_state=next_state,
        )

    def plan_buffer_in(
        self,
        move: MoveDelta,
        destination: GridPosition,
        state: Optional[BoardState] = None,
    ) -> MotionPlan:
        board_state = state if state is not None else self.store.load()
        piece = board_state.pieces.get(move.piece_id)
        if piece is None or piece.status != "buffered":
            raise StateError(f"piece {move.piece_id!r} is not buffered")
        assert piece.machine_x is not None and piece.machine_y is not None
        start = MachinePoint(piece.machine_x, piece.machine_y)
        end = grid_to_machine(destination, self.config.board)
        obstacles = self._physical_obstacles(
            board_state, exclude_piece_ids={move.piece_id}
        )
        path = plan_path(
            start,
            end,
            obstacles,
            self.config.workspace,
            self.config.planner,
        )
        transfer = PieceTransfer(
            piece_id=move.piece_id,
            purpose="move",
            start=start,
            end=end,
            path=path,
        )
        next_state = board_state.unbuffer_piece(
            move.piece_id, destination, move.event_id
        )
        return MotionPlan(
            move=move,
            base_revision=board_state.revision,
            captured_piece_id=None,
            capture_slot=None,
            transfers=(transfer,),
            program=self._generator.generate((transfer,)),
            next_state=next_state,
        )

    def execute_plan_with_link(self, plan: MotionPlan, link: Any) -> MotionPlan:
        self._require_execution_unlocked()
        if (
            self.config.capture.mode == "eject"
            and len(plan.transfers) > 1
            and any(transfer.purpose == "capture" for transfer in plan.transfers)
        ):
            raise ConfigurationError(
                "compound ejection plans must execute through execute_with_link"
            )
        if not getattr(link, "connected", False):
            raise ConfigurationError(
                "cannot execute: the supplied Marlin link is not connected"
            )
        with self.store.locked():
            if self.journal.exists():
                raise PendingTransactionError(
                    f"pending transaction exists at {self.journal.path}; inspect and reconcile it first"
                )
            current = self.store.load()
            if current.revision != plan.base_revision:
                raise StateError(
                    f"custom plan revision {plan.base_revision} does not match current {current.revision}"
                )
            self.journal.create(plan.journal_payload())
            self.audit.append({"status": "prepared", **plan.summary()})
            try:
                self._stream_plan(plan, link)
                return self._complete_transaction(plan)
            except Exception as exc:
                self._fail_transaction(plan, exc)
                raise

    def commit_observed_moves(self, moves: tuple[MoveDelta, ...]) -> BoardState:
        if not moves:
            raise ValidationError("at least one observed move is required")
        if self.journal.exists():
            raise PendingTransactionError(
                f"pending transaction exists at {self.journal.path}; reconcile it first"
            )
        with self.store.locked():
            state = self.store.load()
            for move in moves:
                captured = state.validate_move(move)
                capture_slot = None
                if captured is not None:
                    used = set(state.used_capture_slots())
                    capture_slot = next(
                        value for value in range(64) if value not in used
                    )
                state = state.applied(move, capture_slot)
            self.store.save(state)
            self.audit.append(
                {
                    "event": "camera_observed",
                    "revision": state.revision,
                    "moves": [move.to_dict() for move in moves],
                }
            )
            return state

    def _require_execution_unlocked(self) -> None:
        if not self.config.safety.calibrated:
            raise ConfigurationError(
                "hardware execution is locked because safety.calibrated is false. Dry-run, measure, home, "
                "and verify coordinates before changing it to true"
            )

    def _prepare_transaction(self, move: MoveDelta) -> MotionPlan:
        if self.journal.exists():
            raise PendingTransactionError(
                f"pending transaction exists at {self.journal.path}; inspect and reconcile it first"
            )
        state = self.store.load()
        plan = self.plan(move, state)
        self.journal.create(plan.journal_payload())
        self.audit.append({"status": "prepared", **plan.summary()})
        return plan

    def _stream_plan(self, plan: MotionPlan, link: Any) -> None:
        if self.config.safety.preflight_commands:
            link.send_program(self.config.safety.preflight_commands)
        if self.config.safety.home_before_execute:
            link.send_program(self.config.safety.home_commands)
        try:
            link.send_program(plan.program.commands)
        except Exception:
            link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
            raise

    def _complete_transaction(self, plan: MotionPlan) -> MotionPlan:
        self.store.save(plan.next_state)
        self.audit.append({"status": "completed", **plan.summary()})
        self.journal.clear()
        return plan

    def _fail_transaction(self, plan: MotionPlan, exc: Exception) -> None:
        self.journal.mark_failed(str(exc))
        self.audit.append(
            {"status": "failed_or_unknown", "error": str(exc), **plan.summary()}
        )

    def execute(self, move: MoveDelta) -> MotionPlan:
        self._require_execution_unlocked()
        link = self._link_factory(self.config.serial)
        with link:
            return self.execute_with_link(move, link)

    def execute_with_link(self, move: MoveDelta, link: Any) -> MotionPlan:
        self._require_execution_unlocked()
        if not getattr(link, "connected", False):
            raise ConfigurationError(
                "cannot execute: the supplied Marlin link is not connected"
            )
        state = self.store.load()
        captured = None
        if self.config.capture.mode == "eject":
            if move.capture is not None:
                specified = state.pieces.get(move.capture.piece_id)
                if (
                    specified is not None
                    and specified.status == "captured"
                    and state.piece_at(move.capture.position) is None
                ):
                    move = replace(move, capture=None)
                else:
                    captured = state.validate_move(move)
            else:
                captured = state.validate_move(move)
            if captured is not None:
                ejection = self.plan_capture_ejection(move, state)
                self.execute_plan_with_link(ejection, link)
                move = replace(move, capture=None)
        with self.store.locked():
            plan = self._prepare_transaction(move)
            try:
                self._stream_plan(plan, link)
                return self._complete_transaction(plan)
            except Exception as exc:
                self._fail_transaction(plan, exc)
                raise

    def home_with_link(self, link: Any) -> None:
        if not getattr(link, "connected", False):
            raise ConfigurationError(
                "cannot home: the supplied Marlin link is not connected"
            )
        link.send_program(self.config.magnet.off_commands)
        link.send_program(self.config.safety.home_commands)

    def _gantry_home_reference(self) -> Tuple[float, float, float]:
        workspace = self.config.workspace
        return workspace.min_y_mm, workspace.max_y_mm, workspace.max_x_mm

    def _gantry_logical_home(self) -> MachinePoint:
        _, y, e = self._gantry_home_reference()
        return MachinePoint(e, y)

    def home(self) -> None:
        link = self._link_factory(self.config.serial)
        with link:
            self.home_with_link(link)

    def reference_gantry_with_link(self, link: Any) -> Tuple[str, ...]:
        if not getattr(link, "connected", False):
            raise ConfigurationError(
                "cannot reference gantry: the supplied Marlin link is not connected"
            )
        result = link.send_command("M119", timeout_s=10.0)
        states = parse_endstop_states(result.responses)
        required = ("x_min", "y_max", "z_max")
        missing = [name for name in required if name not in states]
        open_switches = [name for name in required if not states.get(name, False)]
        if missing:
            raise ConfigurationError(
                "cannot reference gantry: M119 did not report " + ", ".join(missing)
            )
        if open_switches:
            raise ConfigurationError(
                "cannot reference gantry: these switches are not triggered: "
                + ", ".join(open_switches)
            )
        home_x, home_y, home_e = self._gantry_home_reference()
        program = (
            *self.config.magnet.off_commands,
            "G90",
            (f"G92 X{home_x:g} Y{home_y:g} Z{home_e:g}"),
            "M400",
        )
        link.send_program(program)
        return program

    def reference_gantry(self) -> Tuple[str, ...]:
        link = self._link_factory(self.config.serial)
        with link:
            return self.reference_gantry_with_link(link)

    def home_gantry(
        self,
        record_path: Path,
    ) -> Mapping[str, Any]:
        self._require_execution_unlocked()
        link = self._link_factory(self.config.serial)
        with link:
            try:
                link.send_program(
                    (
                        *self.config.magnet.off_commands,
                        "G21",
                        *self.config.safety.home_commands,
                    )
                )
                endstop_result = link.send_command("M119", timeout_s=10.0)
                position_result = link.send_command("M114", timeout_s=10.0)
            except Exception:
                link.best_effort(
                    (
                        *self.config.magnet.off_commands,
                        "M84",
                    )
                )
                raise
            info = getattr(link, "connection_info", None)
            record = {
                "schema_version": 1,
                "homed_at": utc_now(),
                "method": "marlin_g28",
                "port": getattr(info, "port", None),
                "baudrate": getattr(info, "baudrate", None),
                "endstop_response": list(endstop_result.responses),
                "position_response": list(position_result.responses),
            }
            atomic_write_json(record_path, record)
            self.audit.append({"status": "gantry_homed", **record})
            return record

    def workspace_test_program(
        self,
        feed_mm_min: float = 1200.0,
        margin_mm: float = 20.0,
        columns: int = 8,
        rows: int = 8,
        dwell_ms: int = 100,
    ) -> Tuple[str, ...]:
        if not math.isfinite(feed_mm_min) or feed_mm_min <= 0:
            raise ConfigurationError(
                "workspace-test feed rate must be finite and greater than zero"
            )
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "workspace-test feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        if not math.isfinite(margin_mm) or margin_mm < 0:
            raise ConfigurationError(
                "workspace-test margin must be finite and non-negative"
            )
        if columns < 2 or rows < 2:
            raise ConfigurationError(
                "workspace-test requires at least two columns and two rows"
            )
        if dwell_ms < 0 or dwell_ms > 5000:
            raise ConfigurationError(
                "workspace-test dwell must be between zero and 5000 ms"
            )
        min_x = self.config.workspace.min_x_mm + margin_mm
        max_x = self.config.workspace.max_x_mm - margin_mm
        min_y = self.config.workspace.min_y_mm + margin_mm
        max_y = self.config.workspace.max_y_mm - margin_mm
        if min_x >= max_x or min_y >= max_y:
            raise ConfigurationError(
                "workspace-test margin leaves no usable movement area"
            )
        x_values = tuple(
            min_x + (max_x - min_x) * index / (columns - 1) for index in range(columns)
        )
        y_values = tuple(
            min_y + (max_y - min_y) * index / (rows - 1) for index in range(rows)
        )
        points = []
        for row, y in enumerate(y_values):
            values = x_values if row % 2 == 0 else tuple(reversed(x_values))
            points.extend(MachinePoint(x, y) for x in values)
        mirror_origin = self.config.workspace.min_y_mm + self.config.workspace.max_y_mm
        home_x, home_y, home_e = self._gantry_home_reference()

        def number(value: float) -> str:
            return f"{value:.3f}".rstrip("0").rstrip(".")

        def move(point: MachinePoint) -> str:
            return (
                f"G1 X{number(mirror_origin - point.y)} Y{number(point.y)} "
                f"Z{number(point.x)} F{number(feed_mm_min)}"
            )

        program = [
            "G21",
            "G90",
            *self.config.magnet.off_commands,
            "M92 X80 Y80 Z80",
            "M203 X200 Y200 Z50",
            "M201 X500 Y500 Z300",
            "M205 X5 Y5 Z5",
            "M211 S0",
            f"G92 X{home_x:g} Y{home_y:g} Z{home_e:g}",
            "M400",
        ]
        for point in points:
            program.extend((move(point), "M400"))
            if dwell_ms:
                program.append(f"G4 P{dwell_ms}")
        program.extend(
            (
                f"G1 X{home_x:g} Y{home_y:g} Z{home_e:g} F{feed_mm_min:g}",
                "M400",
                *self.config.magnet.off_commands,
                "M211 S1",
                "M84",
            )
        )
        return tuple(program)

    def workspace_test(
        self,
        feed_mm_min: float = 1200.0,
        margin_mm: float = 20.0,
        columns: int = 8,
        rows: int = 8,
        dwell_ms: int = 100,
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.workspace_test_program(
            feed_mm_min, margin_mm, columns, rows, dwell_ms
        )
        link = self._link_factory(self.config.serial)
        with link:
            try:
                self.reference_gantry_with_link(link)
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M211 S1", "M84"))
                raise
        self.audit.append(
            {
                "status": "workspace_test_completed",
                "feed_mm_min": feed_mm_min,
                "margin_mm": margin_mm,
                "columns": columns,
                "rows": rows,
                "commands": list(program),
            }
        )
        return program

    def motor_test_program(
        self,
        distance_mm: float = 20.0,
        feed_mm_min: float = 600.0,
        magnet_on: bool = False,
        presentation_loops: int = 0,
    ) -> Tuple[str, ...]:
        if distance_mm <= 0:
            raise ConfigurationError("motor-test distance must be greater than zero")
        if feed_mm_min <= 0:
            raise ConfigurationError("motor-test feed rate must be greater than zero")
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "motor-test feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        if presentation_loops < 0:
            raise ConfigurationError("motor-test presentation loops cannot be negative")
        if presentation_loops and not magnet_on:
            raise ConfigurationError(
                "motor-test presentation loops require the electromagnet to be on"
            )
        pickup_duration_s = distance_mm / feed_mm_min * 120.0
        presentation_duration_s = pickup_duration_s * 2.0 * presentation_loops
        if presentation_loops and presentation_duration_s > 30.0:
            raise ConfigurationError(
                "motor-test presentation would energize the electromagnet for more "
                "than 30 seconds; reduce loops or distance, or increase feed rate"
            )
        if magnet_on and not presentation_loops and pickup_duration_s > 5.0:
            raise ConfigurationError(
                "motor-test pickup movement would energize the electromagnet "
                "for more than 5 seconds; reduce distance or increase feed rate"
            )
        origin = self._gantry_logical_home()
        inner_direction = -1.0 if origin.x == self.config.workspace.max_x_mm else 1.0
        outer_direction = -1.0 if origin.y == self.config.workspace.max_y_mm else 1.0
        inner_target = MachinePoint(origin.x + inner_direction * distance_mm, origin.y)
        outer_target = MachinePoint(origin.x, origin.y + outer_direction * distance_mm)
        for point in (origin, inner_target, outer_target):
            if not self.config.workspace.contains(point):
                raise ConfigurationError(
                    f"motor-test distance {distance_mm:g} mm exceeds the configured workspace"
                )
        mirror_origin = self.config.workspace.min_y_mm + self.config.workspace.max_y_mm
        home_x, home_y, home_e = self._gantry_home_reference()
        mirror_target = mirror_origin - outer_target.y
        program = [
            "G21",
            "G90",
            *self.config.magnet.off_commands,
            "M92 X80 Y80 Z80",
            "M203 X200 Y200 Z50",
            "M201 X500 Y500 Z300",
            "M205 X5 Y5 Z5",
            "M211 S0",
            f"G92 X{mirror_origin - origin.y:g} Y{origin.y:g} Z{origin.x:g}",
            "M400",
        ]
        moves = (
            f"G1 Z{inner_target.x:g} F{feed_mm_min:g}",
            f"G1 X{mirror_target:g} Y{outer_target.y:g} F{feed_mm_min:g}",
            f"G1 Z{origin.x:g} F{feed_mm_min:g}",
            f"G1 X{mirror_origin - origin.y:g} Y{origin.y:g} F{feed_mm_min:g}",
        )
        if magnet_on:
            program.extend(self.config.magnet.on_commands)
            if self.config.motion.magnet_on_dwell_ms:
                program.append(f"G4 P{self.config.motion.magnet_on_dwell_ms}")
        if presentation_loops:
            for _ in range(presentation_loops):
                for move in moves:
                    program.extend(self.config.magnet.on_commands)
                    program.extend((move, "M400"))
        else:
            for index, move in enumerate(moves):
                program.extend((move, "M400"))
                if magnet_on and index == 1:
                    program.extend(self.config.magnet.off_commands)
                    if self.config.motion.magnet_off_dwell_ms:
                        program.append(f"G4 P{self.config.motion.magnet_off_dwell_ms}")
        program.extend((*self.config.magnet.off_commands, "M211 S1", "M84"))
        if any(command.strip().upper().startswith("G28") for command in program):
            raise ConfigurationError("motor-test must never issue a homing command")
        return tuple(program)

    def motor_test(
        self,
        distance_mm: float = 20.0,
        feed_mm_min: float = 600.0,
        magnet_on: bool = False,
        presentation_loops: int = 0,
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.motor_test_program(
            distance_mm, feed_mm_min, magnet_on, presentation_loops
        )
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                raise
        self.audit.append(
            {
                "status": "motor_test_completed",
                "magnet_on": magnet_on,
                "presentation_loops": presentation_loops,
                "commands": list(program),
            }
        )
        return program

    def piece_demo_program(
        self, distance_mm: float = 20.0, feed_mm_min: float = 1200.0
    ) -> Tuple[str, ...]:
        return self.motor_test_program(
            distance_mm=distance_mm,
            feed_mm_min=feed_mm_min,
            magnet_on=True,
        )

    def piece_demo(
        self, distance_mm: float = 20.0, feed_mm_min: float = 1200.0
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.piece_demo_program(distance_mm, feed_mm_min)
        link = self._link_factory(self.config.serial)
        with link:
            try:
                self.reference_gantry_with_link(link)
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M211 S1", "M84"))
                raise
        self.audit.append(
            {
                "status": "piece_demo_completed",
                "distance_mm": distance_mm,
                "feed_mm_min": feed_mm_min,
                "commands": list(program),
            }
        )
        return program

    def magnet_test_program(self, duration_s: float = 1.0) -> Tuple[str, ...]:
        if not math.isfinite(duration_s) or duration_s <= 0 or duration_s > 5.0:
            raise ConfigurationError(
                "magnet-test duration must be greater than zero and no more than 5 seconds"
            )
        duration_ms = round(duration_s * 1000.0)
        return (
            *self.config.magnet.off_commands,
            "M400",
            *self.config.magnet.on_commands,
            f"G4 P{duration_ms}",
            *self.config.magnet.off_commands,
            "M400",
        )

    def magnet_test(self, duration_s: float = 1.0) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.magnet_test_program(duration_s)
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort(self.config.magnet.off_commands)
                raise
        self.audit.append(
            {"status": "magnet_test_completed", "commands": list(program)}
        )
        return program

    def circle_demo_program(
        self,
        diameter_mm: float = 200.0,
        feed_mm_min: float = 1800.0,
        segments: int = 72,
    ) -> Tuple[str, ...]:
        if not math.isfinite(diameter_mm) or diameter_mm <= 0:
            raise ConfigurationError(
                "circle-demo diameter must be finite and greater than zero"
            )
        if not math.isfinite(feed_mm_min) or feed_mm_min <= 0:
            raise ConfigurationError(
                "circle-demo feed rate must be finite and greater than zero"
            )
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "circle-demo feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        if segments < 12 or segments > 360:
            raise ConfigurationError("circle-demo segments must be between 12 and 360")
        workspace = self.config.workspace
        radius = diameter_mm / 2.0
        if diameter_mm > min(
            workspace.max_x_mm - workspace.min_x_mm,
            workspace.max_y_mm - workspace.min_y_mm,
        ):
            raise ConfigurationError(
                "circle-demo diameter exceeds the configured workspace"
            )
        center = MachinePoint(workspace.max_x_mm - radius, workspace.max_y_mm - radius)
        if (
            center.x - radius < workspace.min_x_mm
            or center.y - radius < workspace.min_y_mm
        ):
            raise ConfigurationError(
                "circle-demo circle does not fit inside the configured workspace"
            )
        home = self._gantry_logical_home()
        start_angle = math.pi / 4.0
        points = tuple(
            MachinePoint(
                center.x
                + radius * math.cos(start_angle + 2.0 * math.pi * index / segments),
                center.y
                + radius * math.sin(start_angle + 2.0 * math.pi * index / segments),
            )
            for index in range(segments + 1)
        )
        approach_mm = math.hypot(points[0].x - home.x, points[0].y - home.y)
        energized_s = (approach_mm + math.pi * diameter_mm) / feed_mm_min * 60.0
        if energized_s > 30.0:
            raise ConfigurationError(
                "circle-demo would energize the electromagnet for more than 30 seconds; "
                "increase feed rate or reduce diameter"
            )
        mirror_origin = workspace.min_y_mm + workspace.max_y_mm

        def number(value: float) -> str:
            return f"{value:.3f}".rstrip("0").rstrip(".")

        def movement(point: MachinePoint) -> str:
            return (
                f"G1 X{number(mirror_origin - point.y)} Y{number(point.y)} "
                f"Z{number(point.x)} F{number(feed_mm_min)}"
            )

        home_x, home_y, home_z = self._gantry_home_reference()

        program = [
            *self.config.magnet.off_commands,
            "G21",
            "G90",
            *self.config.safety.home_commands,
            "M211 S1",
            *self.config.magnet.on_commands,
        ]
        if self.config.motion.magnet_on_dwell_ms:
            program.append(f"G4 P{self.config.motion.magnet_on_dwell_ms}")
        program.extend((movement(points[0]), "M400"))
        for point in points[1:]:
            program.append(movement(point))
        program.extend(("M400", *self.config.magnet.off_commands))
        if self.config.motion.magnet_off_dwell_ms:
            program.append(f"G4 P{self.config.motion.magnet_off_dwell_ms}")
        program.extend(
            (
                f"G1 X{number(home_x)} Y{number(home_y)} Z{number(home_z)} "
                f"F{number(feed_mm_min)}",
                "M400",
                "M84",
            )
        )
        return tuple(program)

    def circle_demo(
        self,
        diameter_mm: float = 200.0,
        feed_mm_min: float = 1800.0,
        segments: int = 72,
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.circle_demo_program(diameter_mm, feed_mm_min, segments)
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M84"))
                raise
        self.audit.append(
            {
                "status": "circle_demo_completed",
                "diameter_mm": diameter_mm,
                "feed_mm_min": feed_mm_min,
                "segments": segments,
                "commands": list(program),
            }
        )
        return program

    def perimeter_demo_program(
        self,
        width_mm: float = 330.0,
        height_mm: float = 300.0,
        feed_mm_min: float = 1800.0,
        magnet_on: bool = False,
    ) -> Tuple[str, ...]:
        if not math.isfinite(width_mm) or width_mm <= 0:
            raise ConfigurationError(
                "perimeter-demo width must be finite and greater than zero"
            )
        if not math.isfinite(height_mm) or height_mm <= 0:
            raise ConfigurationError(
                "perimeter-demo height must be finite and greater than zero"
            )
        if not math.isfinite(feed_mm_min) or feed_mm_min <= 0:
            raise ConfigurationError(
                "perimeter-demo feed rate must be finite and greater than zero"
            )
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "perimeter-demo feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        workspace = self.config.workspace
        if width_mm > workspace.max_x_mm - workspace.min_x_mm:
            raise ConfigurationError(
                "perimeter-demo width exceeds the configured workspace"
            )
        if height_mm > workspace.max_y_mm - workspace.min_y_mm:
            raise ConfigurationError(
                "perimeter-demo height exceeds the configured workspace"
            )
        perimeter_mm = 2.0 * (width_mm + height_mm)
        energized_s = perimeter_mm / feed_mm_min * 60.0
        if magnet_on and energized_s > 30.0:
            raise ConfigurationError(
                "perimeter-demo would energize the electromagnet for more than 30 seconds; "
                "increase feed rate, reduce dimensions, or disable the magnet"
            )
        home = self._gantry_logical_home()
        inner_direction = -1.0 if home.x == workspace.max_x_mm else 1.0
        outer_direction = -1.0 if home.y == workspace.max_y_mm else 1.0
        inner_end = home.x + inner_direction * width_mm
        outer_end = home.y + outer_direction * height_mm
        corners = (
            home,
            MachinePoint(inner_end, home.y),
            MachinePoint(inner_end, outer_end),
            MachinePoint(home.x, outer_end),
            home,
        )
        mirror_origin = workspace.min_y_mm + workspace.max_y_mm

        def number(value: float) -> str:
            return f"{value:.3f}".rstrip("0").rstrip(".")

        def movement(point: MachinePoint) -> str:
            return (
                f"G1 X{number(mirror_origin - point.y)} Y{number(point.y)} "
                f"Z{number(point.x)} F{number(feed_mm_min)}"
            )

        program = [
            *self.config.magnet.off_commands,
            "G21",
            "G90",
            *self.config.safety.home_commands,
            "M211 S1",
        ]
        if magnet_on:
            program.extend(self.config.magnet.on_commands)
            if self.config.motion.magnet_on_dwell_ms:
                program.append(f"G4 P{self.config.motion.magnet_on_dwell_ms}")
        for point in corners[1:]:
            program.append(movement(point))
        program.extend(("M400", *self.config.magnet.off_commands))
        if magnet_on and self.config.motion.magnet_off_dwell_ms:
            program.append(f"G4 P{self.config.motion.magnet_off_dwell_ms}")
        program.extend(("M84",))
        return tuple(program)

    def perimeter_demo(
        self,
        width_mm: float = 330.0,
        height_mm: float = 300.0,
        feed_mm_min: float = 1800.0,
        magnet_on: bool = False,
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.perimeter_demo_program(
            width_mm, height_mm, feed_mm_min, magnet_on
        )
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M84"))
                raise
        self.audit.append(
            {
                "status": "perimeter_demo_completed",
                "width_mm": width_mm,
                "height_mm": height_mm,
                "feed_mm_min": feed_mm_min,
                "magnet_on": magnet_on,
                "commands": list(program),
            }
        )
        return program

    def square_center_demo_program(
        self,
        feed_mm_min: float = 1800.0,
        dwell_ms: int = 150,
        magnet_on: bool = False,
    ) -> Tuple[str, ...]:
        if not math.isfinite(feed_mm_min) or feed_mm_min <= 0:
            raise ConfigurationError(
                "square-center-demo feed rate must be finite and greater than zero"
            )
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "square-center-demo feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        if dwell_ms < 0 or dwell_ms > 5000:
            raise ConfigurationError(
                "square-center-demo dwell must be between zero and 5000 ms"
            )
        geometry = self.config.board
        logical_rows = []
        for y in range(geometry.height):
            columns = (
                range(geometry.width - 1, -1, -1)
                if y % 2 == 0
                else range(geometry.width)
            )
            logical_rows.extend(
                grid_to_machine(GridPosition(x, y), geometry) for x in columns
            )
        points = tuple(logical_rows)
        if len(points) != geometry.width * geometry.height:
            raise ConfigurationError("square-center-demo did not generate every square")
        for point in points:
            if not self.config.workspace.contains(point):
                raise ConfigurationError(
                    "square-center-demo center is outside the configured workspace"
                )
        traversal_mm = sum(
            math.hypot(second.x - first.x, second.y - first.y)
            for first, second in zip(points, points[1:])
        )
        energized_s = (
            traversal_mm / feed_mm_min * 60.0
            + geometry.width * geometry.height * dwell_ms / 1000.0
            + self.config.motion.magnet_on_dwell_ms / 1000.0
        )
        if magnet_on and energized_s > 120.0:
            raise ConfigurationError(
                "square-center-demo would energize the electromagnet for more than 120 seconds; "
                "increase feed rate or disable the magnet"
            )
        mirror_origin = self.config.workspace.min_y_mm + self.config.workspace.max_y_mm

        def number(value: float) -> str:
            return f"{value:.3f}".rstrip("0").rstrip(".")

        def movement(point: MachinePoint) -> str:
            return (
                f"G1 X{number(mirror_origin - point.y)} Y{number(point.y)} "
                f"Z{number(point.x)} F{number(feed_mm_min)}"
            )

        home_x, home_y, home_z = self._gantry_home_reference()
        program = [
            *self.config.magnet.off_commands,
            "G21",
            "G90",
            *self.config.safety.home_commands,
            "M211 S1",
            movement(points[0]),
            "M400",
        ]
        if magnet_on:
            program.extend(self.config.magnet.on_commands)
            if self.config.motion.magnet_on_dwell_ms:
                program.append(f"G4 P{self.config.motion.magnet_on_dwell_ms}")
        if dwell_ms:
            program.append(f"G4 P{dwell_ms}")
        for point in points[1:]:
            program.extend((movement(point), "M400"))
            if dwell_ms:
                program.append(f"G4 P{dwell_ms}")
        program.extend(("M400", *self.config.magnet.off_commands))
        if magnet_on and self.config.motion.magnet_off_dwell_ms:
            program.append(f"G4 P{self.config.motion.magnet_off_dwell_ms}")
        program.extend(
            (
                f"G1 X{number(home_x)} Y{number(home_y)} Z{number(home_z)} "
                f"F{number(feed_mm_min)}",
                "M400",
                "M84",
            )
        )
        return tuple(program)

    def square_center_demo(
        self,
        feed_mm_min: float = 1800.0,
        dwell_ms: int = 150,
        magnet_on: bool = False,
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.square_center_demo_program(feed_mm_min, dwell_ms, magnet_on)
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M84"))
                raise
        self.audit.append(
            {
                "status": "square_center_demo_completed",
                "feed_mm_min": feed_mm_min,
                "dwell_ms": dwell_ms,
                "magnet_on": magnet_on,
                "commands": list(program),
            }
        )
        return program

    def board_sweep_program(
        self, feed_mm_min: float = 1800.0, magnet_on: bool = False
    ) -> Tuple[str, ...]:
        if not math.isfinite(feed_mm_min) or feed_mm_min <= 0:
            raise ConfigurationError(
                "board-sweep feed rate must be finite and greater than zero"
            )
        if feed_mm_min > self.config.motion.travel_feed_mm_min:
            raise ConfigurationError(
                "board-sweep feed rate cannot exceed motion.travel_feed_mm_min "
                f"({self.config.motion.travel_feed_mm_min:g} mm/min)"
            )
        positions = []
        for y in range(self.config.board.height):
            columns = (
                range(self.config.board.width)
                if y % 2 == 0
                else range(self.config.board.width - 1, -1, -1)
            )
            positions.extend(
                grid_to_machine(GridPosition(x, y), self.config.board) for x in columns
            )
        if not positions:
            raise ConfigurationError("board-sweep requires at least one board square")
        for point in positions:
            if not self.config.workspace.contains(point):
                raise ConfigurationError(
                    "board-sweep square center is outside the configured workspace"
                )
        mirror_origin = self.config.workspace.min_y_mm + self.config.workspace.max_y_mm
        home_x, home_y, home_e = self._gantry_home_reference()

        def movement(command: str, point: MachinePoint) -> str:
            return (
                f"{command} X{mirror_origin - point.y:g} Y{point.y:g} "
                f"Z{point.x:g} F{feed_mm_min:g}"
            )

        program = [
            "G21",
            "G90",
            *self.config.magnet.off_commands,
            "M92 X80 Y80 Z80",
            "M203 X200 Y200 Z50",
            "M201 X500 Y500 Z300",
            "M205 X5 Y5 Z5",
            "M211 S0",
            f"G92 X{home_x:g} Y{home_y:g} Z{home_e:g}",
            movement("G0", positions[0]),
            "M400",
        ]
        for index, point in enumerate(positions):
            if magnet_on:
                program.extend(self.config.magnet.on_commands)
                if self.config.motion.magnet_on_dwell_ms:
                    program.append(f"G4 P{self.config.motion.magnet_on_dwell_ms}")
                program.extend(self.config.magnet.off_commands)
                if self.config.motion.magnet_off_dwell_ms:
                    program.append(f"G4 P{self.config.motion.magnet_off_dwell_ms}")
            if index + 1 < len(positions):
                program.extend((movement("G1", positions[index + 1]), "M400"))
        program.extend(self.config.magnet.off_commands)
        program.extend(("M211 S1", "M84"))
        return tuple(program)

    def board_sweep(
        self, feed_mm_min: float = 1800.0, magnet_on: bool = False
    ) -> Tuple[str, ...]:
        self._require_execution_unlocked()
        program = self.board_sweep_program(feed_mm_min, magnet_on)
        link = self._link_factory(self.config.serial)
        with link:
            if self.config.safety.preflight_commands:
                link.send_program(self.config.safety.preflight_commands)
            try:
                link.send_program(program)
            except Exception:
                link.best_effort((*self.config.magnet.off_commands, "M211 S1", "M84"))
                raise
        self.audit.append(
            {
                "status": "board_sweep_completed",
                "feed_mm_min": feed_mm_min,
                "magnet_on": magnet_on,
                "commands": list(program),
            }
        )
        return program

    def reset_state(self) -> BoardState:
        initial = BoardState.standard(self.config.board.width, self.config.board.height)
        self.store.initialize(initial, overwrite=True)
        if self.journal.exists():
            self.journal.clear()
        self.audit.append(
            {
                "status": "state_reset",
                "revision": initial.revision,
            }
        )
        return initial

    def emergency_stop_with_link(self, link: Any) -> None:
        if not getattr(link, "connected", False):
            raise ConfigurationError(
                "cannot stop: the supplied Marlin link is not connected"
            )
        link.emergency_stop(self.config.safety.emergency_stop_command)

    def emergency_stop(self) -> None:
        link = self._link_factory(self.config.serial)
        if isinstance(link, MarlinSerial):
            link.settings = replace(link.settings, startup_wait_s=0.0)
        with link:
            link.emergency_stop(self.config.safety.emergency_stop_command)

    def reconcile_mark_applied(self) -> BoardState:
        if not self.journal.exists():
            raise PendingTransactionError("there is no pending transaction")
        document = self.journal.load()
        try:
            base_revision = int(document["base_revision"])
            next_raw = document["next_state"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(
                "pending journal is missing valid revision/state data"
            ) from exc
        next_state = BoardState.from_mapping(
            next_raw,
            self.config.board.width,
            self.config.board.height,
        )
        with self.store.locked():
            current = self.store.load()
            if current.to_dict() == next_state.to_dict():
                pass
            elif current.revision == base_revision:
                self.store.save(next_state)
            else:
                raise StateError(
                    f"cannot apply journal based on revision {base_revision}; current revision is {current.revision}"
                )
            self.audit.append(
                {
                    "status": "reconciled_applied",
                    "base_revision": base_revision,
                    "next_revision": next_state.revision,
                }
            )
            self.journal.clear()
            return next_state

    def reconcile_discard(self) -> None:
        if not self.journal.exists():
            raise PendingTransactionError("there is no pending transaction")
        document = self.journal.load()
        self.audit.append(
            {
                "status": "reconciled_discarded",
                "base_revision": document.get("base_revision"),
                "next_revision": document.get("next_revision"),
            }
        )
        self.journal.clear()
