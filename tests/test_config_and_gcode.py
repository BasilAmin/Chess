from __future__ import annotations

from pathlib import Path
import json
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError
from chess_gantry.gcode import GCodeGenerator
from chess_gantry.models import MachinePoint, PieceTransfer

ROOT = Path(__file__).resolve().parents[1]


class ConfigAndGCodeTests(unittest.TestCase):
    def raw_config(self):
        return json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))

    def test_example_config_drives_creality_fan_zero(self) -> None:
        config = AppConfig.from_mapping(self.raw_config())
        self.assertTrue(config.safety.calibrated)
        self.assertFalse(config.safety.home_before_execute)
        self.assertEqual(config.magnet.on_commands, ("M106 P0 S255",))
        self.assertEqual(config.magnet.off_commands, ("M107 P0",))
        self.assertEqual(config.serial.command_timeout_s, 300.0)

    def test_machine_and_playing_board_dimensions(self) -> None:
        config = AppConfig.from_mapping(self.raw_config())
        self.assertEqual(config.workspace.max_x_mm, 330.0)
        self.assertEqual(config.workspace.max_y_mm, 300.0)
        self.assertEqual(config.board.square_size_mm, 40.0)
        self.assertEqual(config.board.origin_x_mm, 40.0)
        self.assertEqual(config.board.origin_y_mm, 18.0)

    def test_rejects_capture_slot_outside_workspace(self) -> None:
        raw = self.raw_config()
        raw["capture"] = {"enabled": True, "slots": [[999.0, 999.0]]}
        with self.assertRaisesRegex(ConfigurationError, "outside the workspace"):
            AppConfig.from_mapping(raw)

    def test_rejects_capture_slot_inside_board(self) -> None:
        raw = self.raw_config()
        raw["capture"] = {"enabled": True, "slots": [[50.0, 50.0]]}
        with self.assertRaisesRegex(ConfigurationError, "playing-board footprint"):
            AppConfig.from_mapping(raw)

    def test_rejects_duplicate_capture_slot(self) -> None:
        raw = self.raw_config()
        raw["capture"] = {"enabled": True, "slots": [[5.0, 5.0], [5.0, 5.0]]}
        with self.assertRaisesRegex(ConfigurationError, "duplicates"):
            AppConfig.from_mapping(raw)

    def test_eject_capture_requires_edge_and_buffer_points(self) -> None:
        raw = self.raw_config()
        raw["capture"] = {
            "enabled": True,
            "mode": "eject",
            "slots": [],
            "eject_points": [],
            "buffer_points": [[10.0, 0.0]],
        }
        with self.assertRaisesRegex(ConfigurationError, "eject point"):
            AppConfig.from_mapping(raw)
        raw["capture"]["eject_points"] = [[0.0, 0.0]]
        raw["capture"]["buffer_points"] = []
        with self.assertRaisesRegex(ConfigurationError, "buffer point"):
            AppConfig.from_mapping(raw)

    def test_eject_capture_requires_valid_magnetic_keepout(self) -> None:
        raw = self.raw_config()
        raw["capture"]["magnetic_keepout_mm"] = 10.0
        with self.assertRaisesRegex(ConfigurationError, "at least planner"):
            AppConfig.from_mapping(raw)
        raw["capture"]["magnetic_keepout_mm"] = 40.0
        with self.assertRaisesRegex(ConfigurationError, "smaller than one square"):
            AppConfig.from_mapping(raw)
        raw = self.raw_config()
        raw["capture"]["planner_grid_step_mm"] = 21.0
        with self.assertRaisesRegex(ConfigurationError, "half a square"):
            AppConfig.from_mapping(raw)

    def test_eject_and_buffer_points_must_be_outside_board_and_inside_workspace(
        self,
    ) -> None:
        raw = self.raw_config()
        raw["capture"] = {
            "enabled": True,
            "mode": "eject",
            "slots": [],
            "eject_points": [[999.0, 0.0]],
            "buffer_points": [[10.0, 0.0]],
        }
        with self.assertRaisesRegex(ConfigurationError, "outside the workspace"):
            AppConfig.from_mapping(raw)
        raw["capture"]["eject_points"] = [[40.0, 40.0]]
        with self.assertRaisesRegex(ConfigurationError, "playing-board footprint"):
            AppConfig.from_mapping(raw)

    def test_gcode_waits_before_magnet_transitions(self) -> None:
        raw = self.raw_config()
        raw["motion"]["park_after_move"] = False
        raw["motion"].pop("park_x_mm")
        raw["motion"].pop("park_y_mm")
        config = AppConfig.from_mapping(raw)
        transfer = PieceTransfer(
            piece_id="piece-1",
            purpose="move",
            start=MachinePoint(10.0, 10.0),
            end=MachinePoint(30.0, 30.0),
            path=(
                MachinePoint(10.0, 10.0),
                MachinePoint(20.0, 15.0),
                MachinePoint(30.0, 30.0),
            ),
        )
        commands = GCodeGenerator(config).generate([transfer]).commands
        on_index = commands.index("M106 P0 S255")
        self.assertNotIn("M82", commands)
        self.assertFalse(any(command.startswith("M302") for command in commands))
        self.assertIn("G0 X290 Y10 Z10 F12000", commands)
        first_drag = commands.index("G1 X285 Y15 Z20 F3000")
        final_drag = commands.index("G1 X270 Y30 Z30 F3000")
        off_after_drag = next(
            index
            for index in range(final_drag + 1, len(commands))
            if commands[index] == "M107 P0"
        )
        self.assertEqual(commands[on_index - 1], "M400")
        self.assertLess(on_index, first_drag)
        self.assertEqual(commands[off_after_drag - 1], "M400")
        self.assertEqual(commands[-1], "M211 S1")
        self.assertTrue(any(" Z" in command for command in commands))
        self.assertFalse(any(" E" in command for command in commands))
        for command in commands:
            if command.startswith(("G0 ", "G1 ")) and " Y" in command:
                x_word = next(word for word in command.split() if word.startswith("X"))
                y_word = next(word for word in command.split() if word.startswith("Y"))
                self.assertAlmostEqual(float(x_word[1:]) + float(y_word[1:]), 300.0)


if __name__ == "__main__":
    unittest.main()
