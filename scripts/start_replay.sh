#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec uv run chess-gantry --config config.json replay-game \
  examples/replays/clear-lanes-no-knights.pgn \
  --execute \
  --reset-session \
  --discard-pending-on-reset \
  --confirm-motion \
  --confirm-clear-path \
  --confirm-capture-chutes \
  --confirm-high-speed \
  --physical-confirmation "REPLAY BOARD AND CHUTES READY"
