from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from chess_gantry.commissioning import CONFIRMATION, CommissioningStore
from chess_gantry.errors import ValidationError


class CommissioningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"machine": "test", "value": 1}))
        self.store = CommissioningStore(self.root / "commissioning.json", self.config)

    def tearDown(self):
        self.temporary.cleanup()

    def test_attestation_requires_exact_confirmation_and_persists(self):
        with self.assertRaises(ValidationError):
            self.store.attest("yes")
        status = self.store.attest(CONFIRMATION, git_commit="abc123")
        self.assertTrue(status["commissioned"])
        self.assertEqual(status["git_commit"], "abc123")
        self.assertTrue(
            CommissioningStore(self.store.path, self.config).status()["commissioned"]
        )

    def test_configuration_change_invalidates_attestation(self):
        self.store.attest(CONFIRMATION)
        self.config.write_text(json.dumps({"machine": "test", "value": 2}))
        status = self.store.status()
        self.assertFalse(status["commissioned"])
        self.assertEqual(status["reason"], "configuration_changed")

    def test_clear_removes_attestation(self):
        self.store.attest(CONFIRMATION)
        status = self.store.clear()
        self.assertFalse(status["commissioned"])
        self.assertEqual(status["reason"], "locally_decommissioned")

    def test_calibrated_config_defaults_to_commissioned_without_runtime_file(self):
        self.config.write_text(json.dumps({"safety": {"calibrated": True}}))
        status = CommissioningStore(self.store.path, self.config).status()
        self.assertTrue(status["commissioned"])
        self.assertEqual(status["reason"], "config_calibrated")


if __name__ == "__main__":
    unittest.main()
