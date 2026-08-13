#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export CHESS_GANTRY_STATION_AUTOSTART=1
export CHESS_GANTRY_PUBLIC_URL="${CHESS_GANTRY_PUBLIC_URL:-http://$(hostname -I | cut -d' ' -f1):8000}"

printf 'Station QR page: %s/station\n' "$CHESS_GANTRY_PUBLIC_URL"

exec uv run chess-gantry --config config.json web \
  --host 0.0.0.0 \
  --web-port 8000 \
  --allow-network \
  --no-browser
