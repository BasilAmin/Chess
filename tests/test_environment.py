from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from chess_gantry.environment import load_local_environment
from chess_gantry.errors import ConfigurationError


class EnvironmentTests(unittest.TestCase):
    def test_missing_file_is_a_noop(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(
                load_local_environment(Path(directory) / "missing", {}), {}
            )

    def test_secure_file_loads_supported_keys(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text(
                "OPENAI_API_KEY='secret'\n"
                "CHESS_GANTRY_CAMERA_SOURCE=snapshot:http://phone/shot.jpg\n"
            )
            os.chmod(path, 0o600)
            target = {}
            loaded = load_local_environment(path, target)
            self.assertEqual(target["OPENAI_API_KEY"], "secret")
            self.assertEqual(
                target["CHESS_GANTRY_CAMERA_SOURCE"],
                "snapshot:http://phone/shot.jpg",
            )
            self.assertEqual(loaded, target)

    def test_secure_project_file_overrides_stale_inherited_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("OPENAI_API_KEY=file-value\n")
            os.chmod(path, 0o600)
            target = {"OPENAI_API_KEY": "exported-value"}
            self.assertEqual(
                load_local_environment(path, target),
                {"OPENAI_API_KEY": "file-value"},
            )
            self.assertEqual(target["OPENAI_API_KEY"], "file-value")

    def test_group_or_world_access_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("OPENAI_API_KEY=secret\n")
            os.chmod(path, 0o644)
            with self.assertRaisesRegex(ConfigurationError, "0600"):
                load_local_environment(path, {})

    def test_unknown_key_and_invalid_line_are_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env.local"
            path.write_text("UNKNOWN_KEY=value\n")
            os.chmod(path, 0o600)
            with self.assertRaisesRegex(ConfigurationError, "unsupported"):
                load_local_environment(path, {})
            path.write_text("not-an-assignment\n")
            with self.assertRaisesRegex(ConfigurationError, "KEY=VALUE"):
                load_local_environment(path, {})


if __name__ == "__main__":
    unittest.main()
