#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p data/demo
cp examples/board_state.standard.json data/demo/board_state.json
rm -f data/demo/pending_move.json data/demo/audit.jsonl

./scripts/check.sh
npm run check
uv run chess-gantry \
  --config config.demo.json \
  --state data/demo/board_state.json \
  --journal data/demo/pending_move.json \
  --audit data/demo/audit.jsonl \
  plan examples/move_e2_e4.json \
  --summary-json \
  --output data/demo/e2e4.gcode
uv run chess-gantry \
  --config config.demo.json \
  --state data/demo/board_state.json \
  --journal data/demo/pending_move.json \
  --audit data/demo/audit.jsonl \
  magnet-test --duration-s 1 --confirm-motion --demo
uv run chess-gantry \
  --config config.demo.json \
  --state data/demo/board_state.json \
  --journal data/demo/pending_move.json \
  --audit data/demo/audit.jsonl \
  board-sweep --feed-mm-min 1800 --magnet-on --confirm-motion --demo \
  --output data/demo/board-sweep.gcode
printf 'Demo readiness checks passed. No physical serial port was opened.\n'
