from __future__ import annotations

from pathlib import Path
import hashlib
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware" / "relay-chess-v422-stm32f103ret6.bin"
CHECKSUM = ROOT / "firmware" / "relay-chess-v422-stm32f103ret6.bin.sha256"


class FirmwareConfigurationTests(unittest.TestCase):
    def test_versioned_firmware_matches_checksum(self) -> None:
        expected, filename = CHECKSUM.read_text(encoding="utf-8").split()
        self.assertEqual(filename, FIRMWARE.name)
        self.assertEqual(hashlib.sha256(FIRMWARE.read_bytes()).hexdigest(), expected)

    def test_example_requires_calibration_and_homing(self) -> None:
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertFalse(config["safety"]["calibrated"])
        self.assertTrue(config["safety"]["home_before_execute"])
        self.assertEqual(config["serial"]["port"], "auto")

    def test_example_workspace_stays_inside_firmware_limits(self) -> None:
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        self.assertLessEqual(config["workspace"]["max_y_mm"], 350.0)
        self.assertLessEqual(config["workspace"]["max_x_mm"], 350.0)
