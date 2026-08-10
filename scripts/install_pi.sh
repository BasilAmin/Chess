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

sudo apt-get update
sudo apt-get install -y i2c-tools

BOOT_CONFIG=/boot/firmware/config.txt
if [[ ! -f $BOOT_CONFIG ]]; then
  BOOT_CONFIG=/boot/config.txt
fi
I2C_REBOOT_REQUIRED=0
if [[ -f $BOOT_CONFIG ]] && ! grep -qE '^dtparam=i2c_arm=on([[:space:]]|$)' "$BOOT_CONFIG"; then
  printf '\n# Chess Gantry MCP23017\ndtparam=i2c_arm=on\n' | sudo tee -a "$BOOT_CONFIG" > /dev/null
  printf 'Enabled Raspberry Pi I2C in %s. Reboot before starting the reed switch test.\n' "$BOOT_CONFIG"
  I2C_REBOOT_REQUIRED=1
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
sudo usermod -aG dialout "$USER"

if [[ $I2C_REBOOT_REQUIRED -eq 1 ]]; then
  printf 'Reboot now, then rerun ./scripts/install_pi.sh to build and start the container.\n'
  exit 0
fi

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
fi

mkdir -p data
if [[ ! -f data/board_state.json ]]; then
  cp examples/board_state.standard.json data/board_state.json
fi

SERIAL_DEVICE="${CHESS_GANTRY_SERIAL_PORT:-/dev/ttyUSB0}"
I2C_DEVICE="${CHESS_GANTRY_I2C_DEVICE:-/dev/i2c-1}"

if [[ -z "${CLERK_PUBLISHABLE_KEY:-}" ]]; then
  printf 'CLERK_PUBLISHABLE_KEY is not set. The dashboard authenticates with Clerk only.\n' >&2
  printf 'Export it before running this installer.\n' >&2
  exit 2
fi

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

Detected devices:
  serial: $SERIAL_DEVICE
  I2C:    $I2C_DEVICE

Start the dashboard with:
  ./run.sh

Dashboard address after run.sh starts:
  http://$LAN_IP/

The current user was added to the docker and dialout groups. Log out and back in
before using Docker without sudo. Export CLERK_PUBLISHABLE_KEY before running
run.sh; the deployment script contains no built-in Clerk credentials.
EOF
