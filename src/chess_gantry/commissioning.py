from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any
import json

from .errors import ConfigurationError, ValidationError
from .persistence import atomic_write_json, read_json


CONFIRMATION = "I CONFIRM PHYSICAL SETUP IS SAFE"


class CommissioningStore:
    def __init__(self, path: Path, config_path: Path) -> None:
        self.path = path
        self.config_path = config_path

    def _fingerprint(self) -> str:
        if not self.config_path.exists():
            raise ConfigurationError(
                f"configuration file does not exist: {self.config_path}"
            )
        parsed = json.loads(self.config_path.read_text(encoding="utf-8"))
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
        return sha256(canonical).hexdigest()

    def attest(
        self, confirmation: str, *, git_commit: str = "unknown"
    ) -> dict[str, Any]:
        if confirmation != CONFIRMATION:
            raise ValidationError(f"type exactly: {CONFIRMATION}")
        value = {
            "schema_version": 1,
            "commissioned": True,
            "confirmed_at": datetime.now(timezone.utc).isoformat(),
            "config_sha256": self._fingerprint(),
            "git_commit": git_commit,
        }
        atomic_write_json(self.path, value)
        return self.status()

    def clear(self) -> dict[str, Any]:
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "commissioned": False,
                "confirmed_at": None,
                "config_sha256": self._fingerprint(),
                "git_commit": "local-decommission",
            },
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            calibrated = bool(config.get("safety", {}).get("calibrated"))
            return {
                "commissioned": calibrated,
                "reason": "config_calibrated" if calibrated else "not_attested",
            }
        value = read_json(self.path)
        if value.get("config_sha256") != self._fingerprint():
            return {
                "commissioned": False,
                "reason": "configuration_changed",
                "confirmed_at": value.get("confirmed_at"),
            }
        commissioned = bool(value.get("commissioned"))
        return {
            "commissioned": commissioned,
            "reason": None if commissioned else "locally_decommissioned",
            "confirmed_at": value.get("confirmed_at"),
            "git_commit": value.get("git_commit"),
            "config_sha256": value.get("config_sha256"),
        }
