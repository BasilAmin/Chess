#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  printf 'Usage: %s PGN [replay-game options]\n' "$0" >&2
  printf 'Example: %s examples/replays/en-passant-castling.pgn --demo --no-screen\n' "$0" >&2
  exit 2
fi

PGN="$1"
shift

if [[ " ${*:-} " == *" --demo "* || " ${*:-} " == *" --status "* ]]; then
  CONFIG="config.json"
  if [[ " ${*:-} " == *" --demo "* ]]; then
    CONFIG="config.demo.json"
  fi
  exec uv run chess-gantry --config "$CONFIG" replay-game "$PGN" "$@"
fi

if [[ " ${*:-} " == *" --mark-applied "* || " ${*:-} " == *" --discard-pending "* ]]; then
  read -r -p 'Type REPLAY PHYSICAL STATE VERIFIED to reconcile: ' recovery
  if [[ "$recovery" != "REPLAY PHYSICAL STATE VERIFIED" ]]; then
    printf 'Reconciliation cancelled.\n' >&2
    exit 1
  fi
  exec uv run chess-gantry --config config.json replay-game "$PGN" \
    --confirm-physical-state "$@"
fi

printf 'This will home the gantry and replay %s from the standard position.\n' "$PGN"
printf 'Install the capture tray and clear both chutes and castling buffers.\n'
read -r -p 'Type REPLAY BOARD AND CHUTES READY to continue: ' confirmation
if [[ "$confirmation" != "REPLAY BOARD AND CHUTES READY" ]]; then
  printf 'Replay cancelled.\n' >&2
  exit 1
fi

exec uv run chess-gantry --config config.json replay-game "$PGN" \
  --execute \
  --confirm-motion \
  --confirm-standard-position \
  --confirm-clear-path \
  --confirm-capture-chutes \
  --confirm-high-speed \
  "$@"
