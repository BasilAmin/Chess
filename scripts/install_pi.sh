#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Linux" ]]; then
  printf 'This installer requires Raspberry Pi OS or another Linux system.\n' >&2
  exit 2
fi

if ! command -v sudo > /dev/null 2>&1; then
  printf 'sudo is required to install and configure Docker.\n' >&2
  exit 2
fi

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64 | arm64) ;;

  armv7l | armv8l)
    printf 'Chess Gantry vision requires a 64-bit Raspberry Pi OS (aarch64); found %s.\n' "$ARCH" >&2
    exit 2
    ;;

  *)
    printf 'Warning: expected a Raspberry Pi ARM architecture, found %s.\n' "$ARCH"
    ;;
esac

if ! command -v docker > /dev/null 2>&1; then
  printf 'Installing Docker Engine...\n'
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  INSTALLER="$(mktemp)"
  trap 'rm -f "$INSTALLER"' EXIT
  curl -fsSL https://get.docker.com -o "$INSTALLER"
  sudo sh "$INSTALLER"
  rm -f "$INSTALLER"
  trap - EXIT
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
sudo usermod -aG dialout "$USER"

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
fi

mkdir -p data
if [[ ! -f data/board_state.json ]]; then
  cp examples/board_state.standard.json data/board_state.json
fi

SERIAL_DEVICE="${CHESS_GANTRY_SERIAL_PORT:-/dev/ttyUSB0}"

DOCKER=(docker)
if ! docker info > /dev/null 2>&1; then
  DOCKER=(sudo docker)
fi

printf 'Building the Chess Gantry container for %s. This can take several minutes on a Pi 3B+.\n' "$ARCH"
"${DOCKER[@]}" build -t "${CHESS_GANTRY_IMAGE:-chess:latest}" .

LAN_IP="$(hostname -I 2> /dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-RASPBERRY_PI_IP}"

cat << EOF

Chess Gantry Docker installation complete.

Detected serial device:
  $SERIAL_DEVICE

Start the dashboard with:
  ./run.sh

Dashboard address after run.sh starts:
  http://$LAN_IP/

The current user was added to the docker and dialout groups. Log out and back in
before using Docker without sudo. Export OPENAI_API_KEY before starting Sol
board recognition. The default phone source is snapshot:http://192.168.100.88:8080/shot.jpg.
EOF
