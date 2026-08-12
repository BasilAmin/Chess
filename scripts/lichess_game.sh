#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ACTION="${1:-dry-run}"
GAME_ID="${2:-6RkOwfp1}"
GAME_DIR="data/lichess/$GAME_ID"
STATE="$GAME_DIR/board_state.json"
JOURNAL="$GAME_DIR/pending_move.json"
AUDIT="$GAME_DIR/audit.jsonl"

mkdir -p "$GAME_DIR"

base=(
  uv run chess-gantry
  --config config.json
  --state "$STATE"
  --journal "$JOURNAL"
  --audit "$AUDIT"
)

demo_base=(
  uv run chess-gantry
  --config config.demo.json
  --state "$STATE"
  --journal "$JOURNAL"
  --audit "$AUDIT"
)

initialize() {
  if [[ ! -f "$STATE" ]]; then
    "${base[@]}" init-state examples/board_state.standard.json
  fi
}

case "$ACTION" in
  check)
    initialize
    ./scripts/demo_check.sh
    "${demo_base[@]}" lichess-pgn "$GAME_ID" --output-dir "$GAME_DIR/replay"
    ;;
  dry-run)
    initialize
    "${demo_base[@]}" lichess-follow "$GAME_ID" \
      --output-dir "$GAME_DIR/dry-run" \
      --session "$GAME_DIR/dry-run.session.json" \
      --interval 2
    ;;
  play)
    exec ./scripts/mirror_lichess.sh "$GAME_ID"
    ;;
  reset)
    initialize
    "${base[@]}" reset-state --confirm-standard-position
    rm -f "$GAME_DIR/dry-run.session.json" "$GAME_DIR/physical.session.json"
    ;;
  status)
    initialize
    "${base[@]}" show-state
    ;;
  reconcile)
    initialize
    "${base[@]}" reconcile
    ;;
  home)
    uv run chess-gantry --config config.json home-gantry \
      --record "$GAME_DIR/gantry_home.json" \
      --confirm-motion --confirm-clear-path
    ;;
  *)
    printf 'Usage: %s {check|dry-run|home|play|reset|status|reconcile} [GAME_ID]\n' "$0" >&2
    exit 2
    ;;
esac
