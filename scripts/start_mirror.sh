#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GAME_ID="${1:-}"
if [[ -z "$GAME_ID" ]]; then
  printf 'Usage: %s GAME_ID\n' "$0" >&2
  exit 2
fi

exec uv run chess-gantry --config config.json lichess-mirror "$GAME_ID" \
  --execute \
  --stream-mode public \
  --confirm-motion \
  --confirm-standard-position \
  --confirm-clear-path \
  --confirm-capture-chutes \
  --confirm-high-speed
