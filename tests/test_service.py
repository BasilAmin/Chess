from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import json
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError, PlanningError, SerialProtocolError
from chess_gantry.kinematics import grid_to_machine
from chess_gantry.models import BoardState, GridPosition, MoveDelta
from chess_gantry.path_planning import segment_is_clear
from chess_gantry.persistence import atomic_write_json, read_json
from chess_gantry.serial_link import CommandResult
from chess_gantry.service import GantryService

ROOT = Path(__file__).resolve().parents[1]


def test_config(
    *, calibrated: bool = True, capture: bool = False, eject: bool = False
) -> AppConfig:
    raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    raw["planner"]["kind"] = "direct"
    raw["capture"]["enabled"] = capture
    if capture:
        if eject:
            raw["planner"]["kind"] = "astar"
            raw["capture"] = {
                "enabled": True,
                "mode": "eject",
                "slots": [],
                "eject_points": [[0.0, 0.0], [0.0, 300.0]],
                "buffer_points": [[10.0, 0.0], [10.0, 300.0]],
                "magnetic_keepout_mm": 30.0,
                "planner_grid_step_mm": 15.0,
            }
        else:
            raw["capture"]["mode"] = "slots"
            raw["capture"]["slots"] = [[5.0, 5.0], [5.0, 25.0]]
    raw["safety"]["calibrated"] = calibrated
    raw["safety"]["home_before_execute"] = False
    raw["safety"]["preflight_commands"] = []
    return AppConfig.from_mapping(raw)


class FakeLink:
    def __init__(
        self, fail: bool = False, endstops=None, endstop_sequence=None
    ) -> None:
        self.fail = fail
        self.connected = True
        self.programs = []
        self.best_effort_programs = []
        self.stopped = False
        self.endstops = endstops or {
            "x_min": True,
            "y_max": True,
            "z_max": True,
        }
        self.endstop_sequence = list(endstop_sequence or [])
        self.connection_info = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def send_program(self, commands):
        commands = tuple(commands)
        self.programs.append(commands)
        if self.fail:
            raise SerialProtocolError("simulated serial failure")
        return ()

    def send_command(self, command, timeout_s=None):
        if command == "M119":
            if self.endstop_sequence:
                self.endstops = self.endstop_sequence.pop(0)
            responses = tuple(
                f"{name}: {'TRIGGERED' if triggered else 'open'}"
                for name, triggered in self.endstops.items()
            )
            return CommandResult(command, (*responses, "ok"))
        if command == "M114":
            return CommandResult(command, ("X:0.00 Y:300.00 Z:330.00 E:0.00", "ok"))
        return CommandResult(command, ("ok",))

    def best_effort(self, commands):
        self.best_effort_programs.append(tuple(commands))

    def emergency_stop(self, command):
        self.stopped = True


class ServiceTests(unittest.TestCase):
    def minimal_state(self) -> BoardState:
        return BoardState.from_mapping(
            {
                "schema_version": 1,
                "revision": 0,
                "pieces": {
                    "white_pawn_e": {"status": "board", "x": 4, "y": 1},
                },
                "processed_events": [],
            }
        )

    def capture_state(self) -> BoardState:
        return BoardState.from_mapping(
            {
                "schema_version": 1,
                "revision": 0,
                "pieces": {
                    "white_pawn_e": {"status": "board", "x": 4, "y": 3},
                    "black_pawn_d": {"status": "board", "x": 3, "y": 4},
                },
                "processed_events": [],
            }
        )

    def paths(self, temp: Path):
        return (
            temp / "board.json",
            temp / "pending.json",
            temp / "audit.jsonl",
        )

    def test_plan_does_not_change_state(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            state = self.minimal_state()
            atomic_write_json(state_path, state.to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            move = MoveDelta.from_mapping(
                {"position": "white_pawn_e", "px": 4, "py": 1, "nx": 4, "ny": 3}
            )
            plan = service.plan(move)
            self.assertEqual(plan.next_state.revision, 1)
            self.assertEqual(service.store.load().revision, 0)
            self.assertFalse(journal_path.exists())
            commands = plan.program.commands
            self.assertIn("M106 P0 S160", commands)
            self.assertIn("M107 P0", commands)
            self.assertLess(
                commands.index("M106 P0 S160"),
                commands.index("G1 X122 Y178 Z200 F3000"),
            )

    def test_reference_gantry_requires_all_three_endstops(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(endstops={"x_min": True, "y_max": True, "z_max": False})
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            with self.assertRaisesRegex(ConfigurationError, "z_max"):
                service.reference_gantry()
            self.assertEqual(fake.programs, [])

    def test_reference_gantry_assigns_mirrored_xye_origin(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.reference_gantry()
            self.assertIn("G92 X0 Y300 Z330", program)
            self.assertEqual(fake.programs, [program])

    def test_home_gantry_runs_marlin_g28_and_saves_responses(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            record_path = temp / "gantry_home.json"
            record = service.home_gantry(record_path)
            self.assertEqual(
                fake.programs,
                [
                    (
                        "M107 P0",
                        "G21",
                        "G28 X Y Z",
                        "M400",
                        "G92 X2 Y298 Z328",
                        "M400",
                    )
                ],
            )
            self.assertEqual(record["method"], "marlin_g28")
            saved = read_json(record_path)
            self.assertIn("x_min: TRIGGERED", saved["endstop_response"])
            self.assertIn("X:0.00 Y:300.00 Z:330.00 E:0.00", saved["position_response"])

    def test_home_gantry_failure_does_not_save_record(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(fail=True)
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            record_path = temp / "gantry_home.json"
            with self.assertRaises(SerialProtocolError):
                service.home_gantry(record_path)
            self.assertFalse(record_path.exists())
            self.assertEqual(
                fake.best_effort_programs[-1],
                ("M107 P0", "M84"),
            )

    def test_workspace_test_visits_grid_and_returns_to_reference(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.workspace_test(1200.0, 20.0, 8, 8, 100)
            moves = tuple(command for command in program if command.startswith("G1 "))
            self.assertEqual(len(moves), 65)
            self.assertEqual(moves[0], "G1 X280 Y20 Z20 F1200")
            self.assertEqual(moves[7], "G1 X280 Y20 Z310 F1200")
            self.assertEqual(moves[8], "G1 X242.857 Y57.143 Z310 F1200")
            self.assertEqual(moves[-1], "G1 X0 Y300 Z330 F1200")
            self.assertFalse(any(command.startswith("M106") for command in program))
            self.assertEqual(len(fake.programs), 2)

    def test_capture_generates_two_transfers_and_updates_expected_state(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.capture_state().to_dict())
            service = GantryService(
                test_config(capture=True), state_path, journal_path, audit_path
            )
            move = MoveDelta.from_mapping(
                {"position": "white_pawn_e", "px": 4, "py": 3, "nx": 3, "ny": 4}
            )
            plan = service.plan(move)
            self.assertEqual(
                [transfer.purpose for transfer in plan.transfers], ["capture", "move"]
            )
            self.assertEqual(plan.captured_piece_id, "black_pawn_d")
            self.assertEqual(plan.next_state.pieces["black_pawn_d"].capture_slot, 0)
            self.assertEqual(
                plan.next_state.pieces["white_pawn_e"].board_position,
                GridPosition(3, 4),
            )

    def test_en_passant_capture_starts_at_explicit_capture_square(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            state = BoardState.from_mapping(
                {
                    "schema_version": 1,
                    "revision": 0,
                    "pieces": {
                        "white_pawn_e": {"status": "board", "x": 4, "y": 4},
                        "black_pawn_d": {"status": "board", "x": 3, "y": 4},
                    },
                    "processed_events": [],
                }
            )
            atomic_write_json(state_path, state.to_dict())
            service = GantryService(
                test_config(capture=True), state_path, journal_path, audit_path
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 4,
                    "nx": 3,
                    "ny": 5,
                    "capture": {"id": "black_pawn_d", "x": 3, "y": 4},
                }
            )
            plan = service.plan(move)
            self.assertEqual(plan.transfers[0].purpose, "capture")
            self.assertEqual(plan.transfers[0].start.x, 160.0)
            self.assertEqual(plan.transfers[0].start.y, 138.0)
            self.assertEqual(plan.transfers[1].end.x, 160.0)
            self.assertEqual(plan.transfers[1].end.y, 98.0)

    def test_eject_capture_moves_piece_to_nearest_edge_and_removes_obstacle(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.capture_state().to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {"position": "white_pawn_e", "px": 4, "py": 3, "nx": 3, "ny": 4}
            )
            plan = service.plan(move)
            capture = plan.transfers[0]
            self.assertEqual(capture.purpose, "capture")
            self.assertEqual(capture.end.x, 0.0)
            self.assertIn(capture.end.y, {0.0, 300.0})
            self.assertEqual(plan.next_state.pieces["black_pawn_d"].status, "captured")
            self.assertNotIn(
                capture.end,
                service._physical_obstacles(plan.next_state),
            )
            commands = plan.program.commands
            edge_move_index = next(
                index
                for index, command in enumerate(commands)
                if command.startswith("G1 ")
                and "Z0" in command
                and ("Y0" in command or "Y300" in command)
            )
            self.assertIn(
                f"F{service.config.motion.capture_drag_feed_mm_min:g}",
                commands[edge_move_index],
            )
            self.assertIn("M107 P0", commands[edge_move_index + 1 :])

    def test_capture_ejection_keeps_energized_path_clear_of_every_piece(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            state = BoardState.from_mapping(
                {
                    "schema_version": 1,
                    "revision": 0,
                    "pieces": {
                        "white_pawn_e": {"status": "board", "x": 4, "y": 3},
                        "black_pawn_d": {"status": "board", "x": 3, "y": 4},
                        "guard_c4": {"status": "board", "x": 2, "y": 3},
                        "guard_c5": {"status": "board", "x": 2, "y": 4},
                        "guard_c6": {"status": "board", "x": 2, "y": 5},
                        "guard_b3": {"status": "board", "x": 1, "y": 2},
                        "guard_b6": {"status": "board", "x": 1, "y": 5},
                    },
                    "processed_events": [],
                }
            )
            atomic_write_json(state_path, state.to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 3,
                    "nx": 3,
                    "ny": 4,
                    "capture": {"id": "black_pawn_d", "x": 3, "y": 4},
                }
            )
            plan = service.plan_capture_ejection(move)
            path = plan.transfers[0].path
            obstacles = tuple(
                grid_to_machine(piece.board_position, service.config.board)
                for piece in state.pieces.values()
                if piece.piece_id != "black_pawn_d"
            )
            self.assertGreater(len(path), 2)
            for start, end in zip(path, path[1:]):
                self.assertTrue(
                    segment_is_clear(
                        start,
                        end,
                        obstacles,
                        service.config.capture.magnetic_keepout_mm,
                    )
                )

    def test_capture_planning_latency_stays_below_one_second(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.capture_state().to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 3,
                    "nx": 3,
                    "ny": 4,
                    "capture": {"id": "black_pawn_d", "x": 3, "y": 4},
                }
            )
            started = time.perf_counter()
            service.plan_capture_ejection(move)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 1.5)

    def test_capture_ejection_forces_astar_when_normal_moves_are_direct(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            state = BoardState.from_mapping(
                {
                    "schema_version": 1,
                    "revision": 0,
                    "pieces": {
                        "capturer": {"status": "board", "x": 4, "y": 3},
                        "captured": {"status": "board", "x": 3, "y": 4},
                        "blocker": {"status": "board", "x": 2, "y": 4},
                    },
                    "processed_events": [],
                }
            )
            atomic_write_json(state_path, state.to_dict())
            raw = json.loads((ROOT / "config.example.json").read_text())
            raw["planner"]["kind"] = "direct"
            service = GantryService(
                AppConfig.from_mapping(raw), state_path, journal_path, audit_path
            )
            plan = service.plan_capture_ejection(
                MoveDelta.from_mapping(
                    {
                        "position": "capturer",
                        "px": 4,
                        "py": 3,
                        "nx": 3,
                        "ny": 4,
                        "capture": {"id": "captured", "x": 3, "y": 4},
                    }
                )
            )
            self.assertGreater(len(plan.transfers[0].path), 2)

    def test_fully_enclosed_capture_fails_before_serial_or_state_change(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            pieces = {
                "capturer": {"status": "board", "x": 4, "y": 3},
                "captured": {"status": "board", "x": 3, "y": 4},
            }
            for name, x, y in (
                ("c4", 2, 3),
                ("d4", 3, 3),
                ("c5", 2, 4),
                ("e5", 4, 4),
                ("c6", 2, 5),
                ("d6", 3, 5),
                ("e6", 4, 5),
            ):
                pieces[name] = {"status": "board", "x": x, "y": y}
            state = BoardState.from_mapping(
                {
                    "schema_version": 1,
                    "revision": 0,
                    "pieces": pieces,
                    "processed_events": [],
                }
            )
            atomic_write_json(state_path, state.to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "capturer",
                    "px": 4,
                    "py": 3,
                    "nx": 3,
                    "ny": 4,
                    "capture": {"id": "captured", "x": 3, "y": 4},
                }
            )
            link = FakeLink()
            with self.assertRaisesRegex(
                PlanningError, "magnetically clear capture-ejection route"
            ):
                service.execute_with_link(move, link)
            self.assertEqual(link.programs, [])
            self.assertFalse(journal_path.exists())
            self.assertEqual(service.store.load().to_dict(), state.to_dict())

    def test_ejection_commits_before_attacker_move_and_recovers_independently(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.capture_state().to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 3,
                    "nx": 3,
                    "ny": 4,
                    "capture": {"id": "black_pawn_d", "x": 3, "y": 4},
                    "event_id": "capture.recovery",
                }
            )

            class FailSecondProgram(FakeLink):
                def send_program(self, commands):
                    commands = tuple(commands)
                    self.programs.append(commands)
                    if len(self.programs) == 2:
                        raise SerialProtocolError("attacker transfer interrupted")
                    return ()

            link = FailSecondProgram()
            with self.assertRaisesRegex(SerialProtocolError, "attacker transfer"):
                service.execute_with_link(move, link)
            persisted = service.store.load()
            self.assertEqual(persisted.pieces["black_pawn_d"].status, "captured")
            self.assertEqual(
                persisted.pieces["white_pawn_e"].board_position,
                GridPosition(4, 3),
            )
            service.reconcile_discard()
            persisted = service.store.load()
            self.assertEqual(persisted.pieces["black_pawn_d"].status, "captured")
            self.assertEqual(
                persisted.pieces["white_pawn_e"].board_position,
                GridPosition(4, 3),
            )
            service.execute_with_link(move, FakeLink())
            persisted = service.store.load()
            self.assertEqual(
                persisted.pieces["white_pawn_e"].board_position,
                GridPosition(3, 4),
            )

    def test_implicit_capture_uses_independently_committed_ejection(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.capture_state().to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            move = MoveDelta.from_mapping(
                {
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 3,
                    "nx": 3,
                    "ny": 4,
                }
            )

            class FailSecondProgram(FakeLink):
                def send_program(self, commands):
                    commands = tuple(commands)
                    self.programs.append(commands)
                    if len(self.programs) == 2:
                        raise SerialProtocolError("attacker transfer interrupted")
                    return ()

            with self.assertRaisesRegex(SerialProtocolError, "attacker transfer"):
                service.execute_with_link(move, FailSecondProgram())
            persisted = service.store.load()
            self.assertEqual(persisted.pieces["black_pawn_d"].status, "captured")
            self.assertEqual(
                persisted.pieces["white_pawn_e"].board_position,
                GridPosition(4, 3),
            )

    def test_castling_buffer_plans_out_and_back_with_persisted_state(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            state = BoardState.standard()
            pieces = dict(state.pieces)
            pieces.pop("white_knight_g")
            pieces.pop("white_bishop_f")
            state = BoardState(state.schema_version, state.revision, pieces)
            atomic_write_json(state_path, state.to_dict())
            service = GantryService(
                test_config(capture=True, eject=True),
                state_path,
                journal_path,
                audit_path,
            )
            rook_move = MoveDelta.from_mapping(
                {
                    "position": "white_rook_h",
                    "px": 7,
                    "py": 0,
                    "nx": 5,
                    "ny": 0,
                    "event_id": "castle.rook",
                }
            )
            buffer = service.config.capture.buffer_points[1]
            out = service.plan_buffer_out(rook_move, buffer)
            self.assertEqual(out.transfers[0].end, buffer)
            self.assertEqual(out.next_state.pieces["white_rook_h"].status, "buffered")
            service.store.save(out.next_state)
            back = service.plan_buffer_in(
                rook_move, GridPosition(5, 0), service.store.load()
            )
            self.assertEqual(back.transfers[0].start, buffer)
            self.assertEqual(
                back.next_state.pieces["white_rook_h"].board_position,
                GridPosition(5, 0),
            )

    def test_successful_execute_commits_state_and_clears_journal(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            move = MoveDelta.from_mapping(
                {
                    "event_id": "event-1",
                    "position": "white_pawn_e",
                    "px": 4,
                    "py": 1,
                    "nx": 4,
                    "ny": 3,
                }
            )
            service.execute(move)
            stored = service.store.load()
            self.assertEqual(stored.revision, 1)
            self.assertEqual(
                stored.pieces["white_pawn_e"].board_position, GridPosition(4, 3)
            )
            self.assertIn("event-1", stored.processed_events)
            self.assertFalse(journal_path.exists())
            self.assertEqual(len(fake.programs), 1)

    def test_failed_execute_keeps_state_and_leaves_reconcilable_journal(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(fail=True)
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            move = MoveDelta.from_mapping(
                {"position": "white_pawn_e", "px": 4, "py": 1, "nx": 4, "ny": 3}
            )
            with self.assertRaisesRegex(SerialProtocolError, "simulated"):
                service.execute(move)
            self.assertEqual(service.store.load().revision, 0)
            self.assertTrue(journal_path.exists())
            self.assertEqual(service.journal.load()["status"], "failed_or_unknown")
            self.assertEqual(
                fake.best_effort_programs,
                [("M107 P0", "M211 S1")],
            )

            reconciled = service.reconcile_mark_applied()
            self.assertEqual(reconciled.revision, 1)
            self.assertEqual(
                service.store.load().pieces["white_pawn_e"].board_position,
                GridPosition(4, 3),
            )
            self.assertFalse(journal_path.exists())

    def test_execute_is_locked_until_calibrated(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(
                test_config(calibrated=False), state_path, journal_path, audit_path
            )
            move = MoveDelta.from_mapping(
                {"position": "white_pawn_e", "px": 4, "py": 1, "nx": 4, "ny": 3}
            )
            with self.assertRaisesRegex(ConfigurationError, "calibrated is false"):
                service.execute(move)
            self.assertFalse(journal_path.exists())

    def test_motor_test_runs_without_homing_and_does_not_change_board_state(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.motor_test()
            self.assertEqual(fake.programs, [program])
            self.assertNotIn("M82", program)
            self.assertFalse(any(command.startswith("M302") for command in program))
            self.assertIn("M92 X80 Y80 Z80", program)
            self.assertIn("M203 X200 Y200 Z50", program)
            self.assertIn("M201 X500 Y500 Z300", program)
            self.assertIn("M205 X5 Y5 Z5", program)
            self.assertIn("G92 X0 Y300 Z330", program)
            self.assertFalse(any(command.startswith("G28") for command in program))
            self.assertTrue(any(" Z" in command for command in program))
            self.assertFalse(any(" E" in command for command in program))
            self.assertIn("G1 Z310 F600", program)
            self.assertIn("G1 X20 Y280 F600", program)
            self.assertIn("G1 Z330 F600", program)
            self.assertIn("G1 X0 Y300 F600", program)
            self.assertEqual(program[-2:], ("M211 S1", "M84"))
            self.assertEqual(program[-1], "M84")
            self.assertEqual(service.store.load().revision, 0)
            self.assertFalse(journal_path.exists())

    def test_motor_test_requires_workspace_and_calibration(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            locked = GantryService(
                test_config(calibrated=False), state_path, journal_path, audit_path
            )
            with self.assertRaisesRegex(ConfigurationError, "calibrated is false"):
                locked.motor_test()

    def test_motor_test_rejects_unsafe_distance_and_feed(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            with self.assertRaisesRegex(ConfigurationError, "distance"):
                service.motor_test_program(500.0, 600.0)
            with self.assertRaisesRegex(ConfigurationError, "cannot exceed"):
                service.motor_test_program(20.0, 50000.0)
            with self.assertRaisesRegex(ConfigurationError, "more than 5 seconds"):
                service.motor_test_program(100.0, 600.0, magnet_on=True)

    def test_motor_test_pulses_fan_one_during_each_move(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.motor_test(20.0, 1200.0, magnet_on=True)
            self.assertEqual(program.count("M106 P0 S255"), 1)
            self.assertEqual(program.count("G4 P300"), 2)
            first_on = program.index("M106 P0 S255")
            self.assertEqual(
                program[first_on : first_on + 4],
                ("M106 P0 S255", "G4 P300", "G1 Z310 F1200", "M400"),
            )
            self.assertLess(
                program.index("G1 X20 Y280 F1200"), program.index("M107 P0", first_on)
            )
            self.assertEqual(program[-3:], ("M107 P0", "M211 S1", "M84"))
            self.assertEqual(fake.programs, [program])

    def test_motor_presentation_keeps_full_power_until_all_loops_finish(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            program = service.motor_test_program(
                20.0, 1200.0, magnet_on=True, presentation_loops=3
            )
            moves = [command for command in program if command.startswith("G1 ")]
            first_on = program.index("M106 P0 S255")
            first_off = program.index("M107 P0", first_on)
            self.assertEqual(len(moves), 12)
            self.assertEqual(program.count("M106 P0 S255"), 13)
            self.assertGreater(first_off, program.index(moves[-1]))
            self.assertEqual(program[first_off], "M107 P0")

    def test_motor_presentation_rejects_unbounded_power_duration(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            with self.assertRaisesRegex(ConfigurationError, "more than 30 seconds"):
                service.motor_test_program(
                    100.0, 600.0, magnet_on=True, presentation_loops=2
                )

    def test_piece_demo_requires_three_endstops_before_movement(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(endstops={"x_min": True, "y_max": False, "z_max": True})
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            with self.assertRaisesRegex(ConfigurationError, "y_max"):
                service.piece_demo(20.0, 1200.0)
            self.assertEqual(fake.programs, [])

    def test_piece_demo_holds_magnet_outbound_then_releases_and_returns(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.piece_demo(20.0, 1200.0)
            on_index = program.index("M106 P0 S255")
            inner_out = program.index("G1 Z310 F1200")
            outer_out = program.index("G1 X20 Y280 F1200")
            release = program.index("M107 P0", on_index)
            inner_return = program.index("G1 Z330 F1200")
            outer_return = program.index("G1 X0 Y300 F1200")
            self.assertLess(on_index, inner_out)
            self.assertLess(inner_out, outer_out)
            self.assertLess(outer_out, release)
            self.assertLess(release, inner_return)
            self.assertLess(inner_return, outer_return)
            self.assertEqual(len(fake.programs), 2)
            self.assertEqual(service.store.load().revision, 0)

    def test_magnet_test_uses_configured_fan_output(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.magnet_test(1.5)
            self.assertEqual(
                program,
                (
                    "M107 P0",
                    "M400",
                    "M106 P0 S255",
                    "G4 P1500",
                    "M107 P0",
                    "M400",
                ),
            )
            self.assertEqual(fake.programs, [program])

    def test_magnet_test_rejects_long_pulse_and_attempts_shutoff(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(fail=True)
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            with self.assertRaisesRegex(ConfigurationError, "no more than 5"):
                service.magnet_test_program(5.1)
            with self.assertRaisesRegex(ConfigurationError, "no more than 5"):
                service.magnet_test_program(float("nan"))
            with self.assertRaises(SerialProtocolError):
                service.magnet_test(1.0)
            self.assertEqual(fake.best_effort_programs, [("M107 P0",)])

    def test_circle_demo_homes_holds_magnet_for_circle_and_returns(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.circle_demo(200.0, 1800.0, 72)
            moves = tuple(command for command in program if command.startswith("G1 "))
            home_index = program.index("G28 X Y Z")
            on_index = program.index("M106 P0 S255")
            off_index = program.index("M107 P0", on_index)
            self.assertEqual(len(moves), 74)
            self.assertEqual(moves[0], "G1 X29.289 Y270.711 Z300.711 F1800")
            self.assertEqual(moves[-2], moves[0])
            self.assertEqual(moves[-1], "G1 X0 Y300 Z330 F1800")
            self.assertLess(home_index, on_index)
            self.assertGreater(off_index, program.index(moves[-2]))
            self.assertLess(off_index, program.index(moves[-1]))
            self.assertEqual(program[-1], "M84")
            self.assertEqual(fake.programs[-1], program)

    def test_circle_demo_rejects_unsafe_geometry_and_duration(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            with self.assertRaisesRegex(ConfigurationError, "diameter exceeds"):
                service.circle_demo_program(400.0, 1800.0, 72)
            with self.assertRaisesRegex(ConfigurationError, "more than 30 seconds"):
                service.circle_demo_program(200.0, 1200.0, 72)
            with self.assertRaisesRegex(ConfigurationError, "segments"):
                service.circle_demo_program(200.0, 1800.0, 8)

    def test_perimeter_demo_homes_traces_rectangle_and_returns(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.perimeter_demo(330.0, 300.0, 3000.0, magnet_on=True)
            moves = tuple(command for command in program if command.startswith("G1 "))
            on_index = program.index("M106 P0 S255")
            off_index = program.index("M107 P0", on_index)
            self.assertEqual(
                moves,
                (
                    "G1 X0 Y300 Z0 F3000",
                    "G1 X300 Y0 Z0 F3000",
                    "G1 X300 Y0 Z330 F3000",
                    "G1 X0 Y300 Z330 F3000",
                ),
            )
            self.assertLess(on_index, program.index(moves[0]))
            self.assertGreater(off_index, program.index(moves[-1]))
            self.assertEqual(program[-1], "M84")
            self.assertEqual(fake.programs[-1], program)

    def test_perimeter_demo_rejects_invalid_margin_and_long_magnet_hold(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            with self.assertRaisesRegex(ConfigurationError, "width exceeds"):
                service.perimeter_demo_program(400.0, 300.0, 1800.0)
            with self.assertRaisesRegex(ConfigurationError, "more than 30 seconds"):
                service.perimeter_demo_program(330.0, 300.0, 1200.0, magnet_on=True)

    def test_square_center_demo_visits_all_measured_centers_in_40mm_steps(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.square_center_demo(1800.0, 150, magnet_on=True)
            moves = tuple(command for command in program if command.startswith("G1 "))
            centers = moves[:-1]
            self.assertEqual(len(centers), 64)
            self.assertEqual(centers[0], "G1 X2 Y298 Z320 F1800")
            self.assertEqual(centers[7], "G1 X2 Y298 Z40 F1800")
            self.assertEqual(centers[8], "G1 X42 Y258 Z40 F1800")
            self.assertEqual(centers[15], "G1 X42 Y258 Z320 F1800")
            self.assertEqual(centers[-1], "G1 X282 Y18 Z320 F1800")
            logical = []
            for command in centers:
                words = command.split()
                logical.append((float(words[3][1:]), float(words[2][1:])))
            self.assertEqual(len(set(logical)), 64)
            for first, second in zip(logical, logical[1:]):
                self.assertAlmostEqual(
                    abs(second[0] - first[0]) + abs(second[1] - first[1]),
                    40.0,
                )
            on_index = program.index("M106 P0 S255")
            off_index = program.index("M107 P0", on_index)
            self.assertLess(on_index, program.index(centers[1]))
            self.assertGreater(off_index, program.index(centers[-1]))
            self.assertEqual(moves[-1], "G1 X0 Y300 Z330 F1800")
            self.assertEqual(program[-1], "M84")
            self.assertEqual(fake.programs[-1], program)

    def test_square_center_demo_rejects_excessive_magnet_duration(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            service = GantryService(test_config(), state_path, journal_path, audit_path)
            with self.assertRaisesRegex(ConfigurationError, "more than 120 seconds"):
                service.square_center_demo_program(1200.0, 1000, magnet_on=True)

    def test_board_sweep_visits_every_square_and_controls_magnet(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink()
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            program = service.board_sweep(1800.0, magnet_on=True)
            moves = tuple(
                command for command in program if command.startswith(("G0 ", "G1 "))
            )
            self.assertEqual(len(moves), 64)
            self.assertEqual(moves[0], "G0 X2 Y298 Z40 F1800")
            self.assertEqual(moves[7], "G1 X2 Y298 Z320 F1800")
            self.assertEqual(moves[8], "G1 X42 Y258 Z320 F1800")
            self.assertEqual(moves[15], "G1 X42 Y258 Z40 F1800")
            self.assertEqual(moves[-1], "G1 X282 Y18 Z40 F1800")
            on_index = program.index("M106 P0 S255")
            final_off = len(program) - 1 - program[::-1].index("M107 P0")
            self.assertEqual(program.count("M106 P0 S255"), 64)
            self.assertLess(on_index, program.index(moves[1]))
            self.assertGreater(final_off, program.index(moves[-1]))
            self.assertEqual(program[-2:], ("M211 S1", "M84"))
            self.assertEqual(fake.programs, [program])

    def test_board_sweep_rejects_excess_speed_and_shuts_down_on_failure(self) -> None:
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            state_path, journal_path, audit_path = self.paths(temp)
            atomic_write_json(state_path, self.minimal_state().to_dict())
            fake = FakeLink(fail=True)
            service = GantryService(
                test_config(),
                state_path,
                journal_path,
                audit_path,
                link_factory=lambda settings: fake,
            )
            with self.assertRaisesRegex(ConfigurationError, "cannot exceed"):
                service.board_sweep_program(50000.0)
            with self.assertRaises(SerialProtocolError):
                service.board_sweep(1800.0, magnet_on=True)
            self.assertEqual(
                fake.best_effort_programs,
                [("M107 P0", "M211 S1", "M84")],
            )


if __name__ == "__main__":
    unittest.main()
