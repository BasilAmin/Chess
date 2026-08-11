#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${CHESS_GANTRY_WEB_PORT:-8000}"

printf 'Starting the Chess Gantry UI on every interface, port %s.\n' "$PORT"
printf 'WARNING: authentication is disabled; anyone who can route to this host can control the gantry.\n'

exec uv run chess-gantry --config config.json web \
  --host 0.0.0.0 \
  --allow-network \
  --web-port "$PORT" \
  --no-browser
