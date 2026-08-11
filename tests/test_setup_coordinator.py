from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.controller import GantryController
from chess_gantry.errors import (
    ConfigurationError,
    PendingTransactionError,
    SerialProtocolError,
)
from chess_gantry.models import BoardState
from chess_gantry.persistence import atomic_write_json
from chess_gantry.serial_link import CommandResult, DemoMarlinSerial
from chess_gantry.service import GantryService


ROOT = Path(__file__).resolve().parents[1]


class SetupLink(DemoMarlinSerial):
    def __init__(self, settings, *, firmware="Relay Chess Gantry", fail_command=None):
        super().__init__(settings)
        self.identity = firmware
        self.fail_command = fail_command
        self.best_effort_programs = []

    @property
    def firmware_identity(self):
        return f"FIRMWARE_NAME:{self.identity}" if self.connected else None

    def send_command(self, command, timeout_s=None):
        if command == self.fail_command:
            raise SerialProtocolError(f"failed {command}")
        if command == "M115":
            self.commands.append(command)
            return CommandResult(command, (f"FIRMWARE_NAME:{self.identity}", "ok"))
        return super().send_command(command, timeout_s)

    def best_effort(self, commands):
        self.best_effort_programs.append(tuple(commands))


class SetupCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        raw = json.loads((ROOT / "config.json").read_text())
        raw["planner"]["kind"] = "direct"
        self.config = AppConfig.from_mapping(raw)
        state = self.root / "state.json"
        atomic_write_json(state, BoardState.standard().to_dict())
        self.service = GantryService(
            self.config, state, self.root / "pending.json", self.root / "audit.jsonl"
        )
        self.link = SetupLink(self.config.serial)
        self.controller = GantryController(
            self.config, self.service, link_factory=lambda settings: self.link
        )
        self.controller.connect()

    def tearDown(self):
        self.controller.disconnect()
        self.temporary.cleanup()

    def complete_through_home(self):
        self.controller.run_setup_diagnostics()
        self.controller.verify_setup_endstops()
        self.controller.home_xy()

    def test_diagnostics_require_relay_firmware_and_parse_required_endstops(self):
        status = self.controller.run_setup_diagnostics()
        self.assertTrue(status["setup"]["diagnostics"])
        result = status["setup"]["results"]["diagnostics"]
        self.assertIn("Relay Chess Gantry", result["firmware"])
        self.assertEqual(set(result["endstops"]), {"x_min", "y_max", "z_max"})

    def test_generic_marlin_identity_is_rejected(self):
        self.link.identity = "Marlin generic"
        with self.assertRaisesRegex(ConfigurationError, "Relay Chess"):
            self.controller.run_setup_diagnostics()

    def test_endstop_test_requires_diagnostics(self):
        with self.assertRaisesRegex(ConfigurationError, "diagnostics"):
            self.controller.verify_setup_endstops()

    def test_strict_home_verifies_expected_machine_position(self):
        self.complete_through_home()
        status = self.controller.status()
        self.assertTrue(status["homed"])
        self.assertTrue(status["setup"]["homed"])
        self.assertEqual(
            status["machine_position_mm"], {"x": 2.0, "y": 298.0, "z": 328.0}
        )

    def test_home_does_not_invent_position_when_m114_fails(self):
        self.controller.run_setup_diagnostics()
        self.controller.verify_setup_endstops()
        self.link.fail_command = "M114"
        with self.assertRaises(SerialProtocolError):
            self.controller.home_xy()
        self.assertFalse(self.controller.status()["homed"])
        self.assertEqual(
            self.controller.status()["position_mm"], {"x": None, "y": None}
        )

    def test_movement_test_requires_home_and_uses_no_g92_or_disabled_endstops(self):
        with self.assertRaisesRegex(ConfigurationError, "homing"):
            self.controller.run_setup_movement_test()
        self.complete_through_home()
        status = self.controller.run_setup_movement_test()
        program = status["setup"]["results"]["movement"]["program"]
        self.assertFalse(any(command.startswith("G92") for command in program))
        self.assertNotIn("M211 S0", program)
        self.assertIn("M211 S1", program)
        self.assertIn("G1 X2 Y298 Z323 F300", program)
        self.assertEqual(program[-2:], ["G1 X2 Y298 Z328 F300", "M400"])

    def test_movement_failure_forces_magnet_off_and_invalidates_home(self):
        self.complete_through_home()
        self.link.fail_command = "M114"
        with self.assertRaises(SerialProtocolError):
            self.controller.run_setup_movement_test()
        self.assertFalse(self.controller.status()["homed"])
        self.assertIn("M107 P0", self.link.best_effort_programs[-1])

    def test_magnet_test_requires_movement_and_is_exactly_one_second(self):
        self.complete_through_home()
        with self.assertRaisesRegex(ConfigurationError, "movement"):
            self.controller.run_setup_magnet_test()
        self.controller.run_setup_movement_test()
        status = self.controller.run_setup_magnet_test()
        program = status["setup"]["results"]["magnet"]["program"]
        self.assertIn("G4 P1000", program)
        self.assertEqual(program[-2:], ["M107 P0", "M400"])

    def test_center_test_visits_64_with_magnet_off_and_returns_verified_home(self):
        self.complete_through_home()
        self.controller.run_setup_movement_test()
        status = self.controller.run_setup_square_centers()
        self.assertEqual(status["setup"]["results"]["centers"]["square_count"], 64)
        latest = self.link.commands
        self.assertFalse(any(command.startswith("M106") for command in latest))
        self.assertNotIn("M211 S0", latest)
        self.assertIn("G1 X2.000 Y298.000 Z328.000 F1800", latest)

    def test_pending_transaction_blocks_every_actuator_setup_step(self):
        self.complete_through_home()
        self.service.journal.create({"status": "prepared"})
        for operation in (
            self.controller.home_xy,
            self.controller.run_setup_movement_test,
            self.controller.run_setup_magnet_test,
            self.controller.run_setup_square_centers,
        ):
            with self.assertRaises(PendingTransactionError):
                operation()

    def test_combined_setup_completes_all_six_steps(self):
        status = self.controller.run_setup_combined()
        for step in (
            "diagnostics",
            "endstops",
            "homed",
            "movement",
            "magnet",
            "centers",
        ):
            self.assertTrue(status["setup"][step])

    def test_disconnect_resets_setup_evidence(self):
        self.controller.run_setup_diagnostics()
        status = self.controller.disconnect()
        self.assertFalse(status["setup"]["diagnostics"])

    def test_setup_tests_reject_custom_unreviewed_parameters(self):
        self.complete_through_home()
        with self.assertRaisesRegex(Exception, "fixed"):
            self.controller.run_setup_movement_test(distance_mm=10)
        with self.assertRaisesRegex(Exception, "fixed"):
            self.controller.run_setup_square_centers(feed_mm_min=600)

    def test_rehoming_invalidates_downstream_setup_steps(self):
        self.complete_through_home()
        self.controller.run_setup_movement_test()
        self.controller.run_setup_magnet_test()
        self.controller.run_setup_square_centers()
        status = self.controller.home_xy()
        self.assertFalse(status["setup"]["movement"])
        self.assertFalse(status["setup"]["magnet"])
        self.assertFalse(status["setup"]["centers"])


if __name__ == "__main__":
    unittest.main()
