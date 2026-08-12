#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  printf 'Usage: %s GAME_ID [options] | %s {status|reconcile} GAME_ID\n' "$0" "$0" >&2
  exit 2
fi

if [[ "$1" == "status" || "$1" == "reconcile" ]]; then
  ACTION="$1"
  GAME_ID="${2:-}"
  if [[ -z "$GAME_ID" ]]; then
    printf 'Usage: %s %s GAME_ID\n' "$0" "$ACTION" >&2
    exit 2
  fi
  DIR="data/lichess-mirror/$GAME_ID/physical"
  if [[ "$ACTION" == "status" ]]; then
    printf '%s\n' '--- Mirror cursor ---'
    test -f "$DIR/cursor.json" && cat "$DIR/cursor.json" || printf 'No physical mirror cursor.\n'
    printf '%s\n' '--- Physical board state ---'
    exec uv run chess-gantry --config config.json \
      --state "$DIR/board_state.json" \
      --journal "$DIR/pending_move.json" \
      --audit "$DIR/audit.jsonl" \
      show-state
  fi
  exec uv run chess-gantry --config config.json \
    --state "$DIR/board_state.json" \
    --journal "$DIR/pending_move.json" \
    --audit "$DIR/audit.jsonl" \
    reconcile "${@:3}"
fi

GAME_ID="$1"
shift

if [[ " ${*:-} " == *" --demo "* ]]; then
  exec uv run chess-gantry --config config.demo.json \
    lichess-mirror "$GAME_ID" --execute --demo --confirm-high-speed "$@"
fi

printf 'This will home the gantry and use the X0 capture chutes and X10 castling buffers.\n'
printf 'Install the collection tray and clear both chute and buffer paths before continuing.\n'
if [[ " ${*:-} " == *" --configured-speed "* ]]; then
  printf 'Motion profile: calibrated feeds from config.json.\n'
else
  printf 'Fast profile: travel 12000 mm/min, drag 3000 mm/min.\n'
fi
read -r -p 'Type MIRROR BOARD AND CHUTES READY to continue: ' confirmation
if [[ "$confirmation" != "MIRROR BOARD AND CHUTES READY" ]]; then
  printf 'Confirmation did not match; nothing moved.\n' >&2
  exit 2
fi

exec uv run chess-gantry --config config.json \
  lichess-mirror "$GAME_ID" \
  --execute \
  --confirm-motion \
  --confirm-standard-position \
  --confirm-clear-path \
  --confirm-capture-chutes \
  --confirm-high-speed \
  "$@"
