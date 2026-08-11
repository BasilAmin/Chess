from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import contextlib
import json
import unittest

from chess_gantry.cli import run

ROOT = Path(__file__).resolve().parents[1]


class RunCommandTests(unittest.TestCase):
    def args(self, *command: str) -> list[str]:
        self.addCleanup(self._temporary.cleanup)
        return [
            "--config",
            str(ROOT / "config.example.json"),
            "--state",
            str(ROOT / "examples/board_state.standard.json"),
            "--journal",
            str(Path(self._temporary.name) / "pending.json"),
            "--audit",
            str(Path(self._temporary.name) / "audit.jsonl"),
            *command,
        ]

    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()

    def test_run_dry_run_prints_gcode_from_example_json(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "run",
                    str(ROOT / "examples/move_e2_e4.json"),
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("G21", output)
        self.assertIn("G90", output)
        self.assertIn("G0", output)

    def test_run_summary_json(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "run",
                    str(ROOT / "examples/move_e2_e4.json"),
                    "--summary-json",
                )
            )
        self.assertEqual(code, 0)
        summary = json.loads(buffer.getvalue())
        self.assertEqual(summary["piece_id"], "white_pawn_e")
        self.assertEqual(summary["from"], {"x": 4, "y": 1})
        self.assertEqual(summary["to"], {"x": 4, "y": 3})

    def test_json_path_module_shortcut(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    str(ROOT / "examples/move_e2_e4.json"),
                )
            )
        self.assertEqual(code, 0)
        self.assertIn("G21", buffer.getvalue())

    def test_endstop_watch_demo_prints_initial_states(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(self.args("endstop-watch", "--demo", "--samples", "1"))
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Watching endstops on DEMO", output)
        self.assertIn("INITIAL x_min TRIGGERED", output)
        self.assertIn("INITIAL y_max TRIGGERED", output)
        self.assertIn("INITIAL z_max TRIGGERED", output)

    def test_removed_reed_and_marker_commands_are_absent(self) -> None:
        from chess_gantry.cli import _parser

        help_text = _parser().format_help()
        self.assertNotIn("reed-test", help_text)
        self.assertNotIn("reed-bank-test", help_text)
        self.assertNotIn("vision-markers", help_text)
        self.assertIn("vision-test", help_text)

    def test_web_defaults_to_unauthenticated_loopback(self) -> None:
        from chess_gantry.cli import _parser

        args = _parser().parse_args(["web"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertFalse(args.allow_network)
        self.assertFalse(args.require_clerk)

    def test_workspace_test_dry_run_generates_full_grid(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "workspace-test",
                    "--feed-mm-min",
                    "1200",
                    "--margin-mm",
                    "20",
                    "--columns",
                    "8",
                    "--rows",
                    "8",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertEqual(output.count("G1 "), 65)
        self.assertNotIn("M106", output)

    def test_home_gantry_requires_both_physical_confirmations(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("home-gantry"))
        self.assertIn("--confirm-motion", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("home-gantry", "--confirm-motion"))
        self.assertIn("--confirm-clear-path", errors.getvalue())

    def test_piece_demo_dry_run_prints_pickup_transfer_and_return(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "piece-demo",
                    "--distance-mm",
                    "20",
                    "--feed-mm-min",
                    "1200",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertIn("M106 P0 S255", output)
        self.assertIn("G1 Z330 F1200", output)
        self.assertIn("G1 X20 Y280 F1200", output)
        self.assertIn("G1 X0 Y300 F1200", output)

    def test_motor_test_without_confirmation_is_gcode_only(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(self.args("motor-test"))
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertIn("G92 X0 Y300 Z330", output)
        self.assertNotIn("G28", output)
        self.assertNotIn("M82", output)
        self.assertNotIn("M302", output)
        self.assertIn("M92 X80 Y80 Z80", output)
        self.assertIn("M203 X200 Y200 Z50", output)
        self.assertIn("M201 X500 Y500 Z300", output)
        self.assertIn("M205 X5 Y5 Z5", output)
        self.assertIn("G1 Z310 F600", output)
        self.assertIn("G1 X20 Y280 F600", output)
        self.assertIn("G1 Z330 F600", output)
        self.assertIn("G1 X0 Y300 F600", output)
        self.assertNotIn("M302", output)

    def test_motor_test_accepts_safe_distance_and_feed(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "motor-test",
                    "--distance-mm",
                    "10",
                    "--feed-mm-min",
                    "300",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("G1 Z320 F300", output)
        self.assertIn("G1 X10 Y290 F300", output)

    def test_motor_test_with_magnet_prints_fixed_fan_one_gcode(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "motor-test",
                    "--distance-mm",
                    "20",
                    "--feed-mm-min",
                    "1200",
                    "--magnet-on",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertEqual(output.count("M106 P0 S255"), 1)
        self.assertEqual(output.count("G4 P300"), 2)
        self.assertIn("M106 P0 S255\nG4 P300\nG1 Z310 F1200", output)
        self.assertIn("G1 X20 Y280 F1200\nM400\nM107 P0", output)

    def test_physical_motor_test_with_magnet_requires_confirmation(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "motor-test",
                    "--distance-mm",
                    "20",
                    "--feed-mm-min",
                    "1200",
                    "--magnet-on",
                    "--confirm-motion",
                )
            )
        self.assertIn("--confirm-magnet", errors.getvalue())

    def test_magnet_test_dry_run_uses_fan_one_and_turns_off(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(self.args("magnet-test", "--duration-s", "1.5"))
        self.assertEqual(code, 0)
        commands = [
            line
            for line in buffer.getvalue().splitlines()
            if line and not line.startswith(";")
        ]
        self.assertEqual(
            commands,
            [
                "M107 P0",
                "M400",
                "M106 P0 S255",
                "G4 P1500",
                "M107 P0",
                "M400",
            ],
        )

    def test_magnet_test_demo_streams_without_hardware(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "magnet-test",
                    "--duration-s",
                    "0.25",
                    "--confirm-motion",
                    "--demo",
                )
            )
        self.assertEqual(code, 0)
        self.assertIn("DEMO ONLY", buffer.getvalue())
        self.assertIn("G4 P250", buffer.getvalue())

    def test_circle_demo_dry_run_generates_200_mm_circle(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "circle-demo",
                    "--diameter-mm",
                    "200",
                    "--feed-mm-min",
                    "1800",
                    "--segments",
                    "72",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertIn("G28 X Y Z", output)
        self.assertIn("M106 P0 S255", output)
        self.assertEqual(output.count("G1 "), 74)
        self.assertIn("G1 X0 Y300 Z330 F1800", output)

    def test_physical_circle_demo_requires_both_confirmations(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("circle-demo", "--confirm-motion"))
        self.assertIn("--confirm-clear-workspace", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "circle-demo",
                    "--confirm-motion",
                    "--confirm-clear-workspace",
                )
            )
        self.assertIn("--confirm-magnet", errors.getvalue())

    def test_perimeter_demo_dry_run_generates_closed_rectangle(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "perimeter-demo",
                    "--width-mm",
                    "330",
                    "--height-mm",
                    "300",
                    "--feed-mm-min",
                    "1800",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertIn("G28 X Y Z", output)
        self.assertEqual(output.count("G1 "), 4)
        self.assertIn("G1 X0 Y300 Z0 F1800", output)
        self.assertIn("G1 X300 Y0 Z0 F1800", output)
        self.assertIn("G1 X300 Y0 Z330 F1800", output)
        self.assertIn("G1 X0 Y300 Z330 F1800", output)
        self.assertNotIn("M106", output)

    def test_physical_perimeter_demo_requires_confirmations(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("perimeter-demo", "--confirm-motion"))
        self.assertIn("--confirm-clear-workspace", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "perimeter-demo",
                    "--confirm-motion",
                    "--confirm-clear-workspace",
                    "--magnet-on",
                    "--feed-mm-min",
                    "3000",
                )
            )
        self.assertIn("--confirm-magnet", errors.getvalue())

    def test_square_center_demo_dry_run_uses_measured_corner_centers(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "square-center-demo",
                    "--feed-mm-min",
                    "1800",
                    "--dwell-ms",
                    "150",
                    "--magnet-on",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertEqual(output.count("G1 "), 65)
        self.assertIn("G1 X2 Y298 Z320 F1800", output)
        self.assertIn("G1 X282 Y18 Z320 F1800", output)
        self.assertIn("M106 P0 S255", output)
        self.assertIn("G1 X0 Y300 Z330 F1800", output)

    def test_physical_square_center_demo_requires_confirmations(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("square-center-demo", "--confirm-motion"))
        self.assertIn("--confirm-clear-workspace", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "square-center-demo",
                    "--confirm-motion",
                    "--confirm-clear-workspace",
                    "--magnet-on",
                )
            )
        self.assertIn("--confirm-magnet", errors.getvalue())

    def test_board_sweep_dry_run_visits_all_squares_with_fan_one(self) -> None:
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "board-sweep",
                    "--feed-mm-min",
                    "1800",
                    "--magnet-on",
                )
            )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("DRY RUN ONLY", output)
        self.assertEqual(output.count("G0 ") + output.count("G1 "), 64)
        self.assertIn("G0 X2 Y298 Z40 F1800", output)
        self.assertIn("G1 X2 Y298 Z320 F1800", output)
        self.assertIn("G1 X42 Y258 Z320 F1800", output)
        self.assertEqual(output.count("M106 P0 S255"), 64)
        self.assertGreater(output.rfind("M107 P0"), output.rfind("M106 P0 S255"))

    def test_board_sweep_demo_streams_and_writes_gcode(self) -> None:
        output_path = Path(self._temporary.name) / "sweep.gcode"
        buffer = StringIO()
        with contextlib.redirect_stdout(buffer):
            code = run(
                self.args(
                    "board-sweep",
                    "--feed-mm-min",
                    "1800",
                    "--magnet-on",
                    "--confirm-motion",
                    "--demo",
                    "--output",
                    str(output_path),
                )
            )
        self.assertEqual(code, 0)
        self.assertIn("DEMO ONLY", buffer.getvalue())
        self.assertEqual(output_path.read_text(encoding="ascii").count("G1 "), 63)

    def test_physical_board_sweep_requires_safety_confirmations(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("board-sweep", "--confirm-motion", "--magnet-on"))
        self.assertIn("--confirm-empty-board", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "board-sweep",
                    "--confirm-motion",
                    "--confirm-empty-board",
                    "--magnet-on",
                )
            )
        self.assertIn("--confirm-origin", errors.getvalue())

        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(
                self.args(
                    "board-sweep",
                    "--confirm-motion",
                    "--confirm-empty-board",
                    "--confirm-origin",
                    "--magnet-on",
                )
            )
        self.assertIn("--confirm-magnet", errors.getvalue())

    def test_reset_state_requires_confirmation(self) -> None:
        errors = StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            run(self.args("reset-state"))
        self.assertIn("--confirm-standard-position", errors.getvalue())

    def test_reset_state_restores_standard_board_and_clears_journal(self) -> None:
        state_path = Path(self._temporary.name) / "state.json"
        journal_path = Path(self._temporary.name) / "pending.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "revision": 4,
                    "pieces": {"white_pawn_e": {"status": "board", "x": 4, "y": 3}},
                    "processed_events": ["old-event"],
                }
            ),
            encoding="ascii",
        )
        journal_path.write_text("{}", encoding="ascii")
        args = self.args("reset-state", "--confirm-standard-position")
        args[args.index("--state") + 1] = str(state_path)
        args[args.index("--journal") + 1] = str(journal_path)
        code = run(args)
        self.assertEqual(code, 0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["revision"], 0)
        self.assertEqual(len(state["pieces"]), 32)
        self.assertFalse(journal_path.exists())


if __name__ == "__main__":
    unittest.main()
