from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import MutableMapping, Optional

from .errors import ConfigurationError


ALLOWED_LOCAL_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LICHESS_TOKEN",
    "CLERK_PUBLISHABLE_KEY",
    "CHESS_GANTRY_CAMERA_SOURCE",
}


def load_local_environment(
    path: Path = Path(".env.local"),
    environ: Optional[MutableMapping[str, str]] = None,
) -> dict[str, str]:
    target = os.environ if environ is None else environ
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigurationError(
            f"{path} contains credentials and must use mode 0600, not {mode:04o}"
        )
    loaded = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigurationError(f"{path}:{number} must use KEY=VALUE syntax")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in ALLOWED_LOCAL_KEYS:
            raise ConfigurationError(f"{path}:{number} contains unsupported key {key}")
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        if not value:
            raise ConfigurationError(f"{path}:{number} has an empty value for {key}")
        target[key] = value
        loaded[key] = value
    return loaded
