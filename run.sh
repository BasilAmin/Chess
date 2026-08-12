#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -f .env.local ]]; then
  mode="$(stat -c '%a' .env.local)"
  if [[ "$mode" != "600" ]]; then
    printf '.env.local must use mode 600, not %s. Run: chmod 600 .env.local\n' "$mode" >&2
    exit 2
  fi
  set -a
  source .env.local
  set +a
fi

CLERK_PUBLISHABLE_KEY="${CLERK_PUBLISHABLE_KEY:-}"

IMAGE="${CHESS_GANTRY_IMAGE:-chess:latest}"
CONTAINER="${CHESS_GANTRY_CONTAINER:-chess-gantry}"
MDNS_NAME="${CHESS_GANTRY_MDNS_NAME:-chess.local}"
HTTP_PORT="${CHESS_GANTRY_HTTP_PORT:-80}"
ALT_PORT="${CHESS_GANTRY_ALT_PORT:-8000}"
APP_PORT=8000
SERIAL_DEVICE="${CHESS_GANTRY_SERIAL_PORT:-/dev/ttyUSB0}"
CAMERA_SOURCE="${CHESS_GANTRY_CAMERA_SOURCE:-}"
VIDEO_DEVICE="${CHESS_GANTRY_VIDEO_DEVICE:-}"
APP_UID="${CHESS_GANTRY_APP_UID:-65532}"
APP_GID="${CHESS_GANTRY_APP_GID:-65532}"

if [[ "$(uname -s)" == Linux ]] && command -v sudo > /dev/null 2>&1; then
  printf '==> Preparing the Docker service; sudo will ask for your password\n'
  sudo usermod -aG docker "$USER" || printf '    usermod skipped; is Docker installed?\n'
  sudo systemctl enable docker || printf '    systemctl enable skipped\n'
  sudo systemctl start docker || printf '    systemctl start skipped\n'
fi

if docker info > /dev/null 2>&1; then
  DOCKER=(docker)
elif sudo docker info > /dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  printf 'Docker is unavailable. Install it, then rerun this script.\n' >&2
  exit 2
fi

printf '==> Building %s\n' "$IMAGE"
"${DOCKER[@]}" build -t "$IMAGE" .

SUDO=()
if command -v sudo > /dev/null 2>&1; then
  SUDO=(sudo)
fi

as_root() {
  if [[ ${#SUDO[@]} -eq 0 ]]; then
    "$@"
  else
    "${SUDO[@]}" "$@"
  fi
}

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  printf '==> Created config.json from config.example.json; calibrate it before moving hardware\n'
fi

mkdir -p data 2> /dev/null || as_root mkdir -p data
if [[ ! -f data/board_state.json ]]; then
  if cp examples/board_state.standard.json data/board_state.json 2> /dev/null; then
    printf '==> Seeded data/board_state.json\n'
  else
    as_root cp examples/board_state.standard.json data/board_state.json
    printf '==> Seeded data/board_state.json as root\n'
  fi
fi

DATA_OWNER="$(stat -c '%u:%g' data 2> /dev/null || echo unknown)"
if [[ $DATA_OWNER != "${APP_UID}:${APP_GID}" ]]; then
  as_root chown -R "${APP_UID}:${APP_GID}" data
  printf '==> data/ handed to %s:%s so the container can write it\n' "$APP_UID" "$APP_GID"
fi

MDNS_PID=""
publish_mdns() {
  local short="${MDNS_NAME%.local}"
  if [[ "$(hostname -s 2> /dev/null || true)" == "$short" ]]; then
    printf '==> Hostname is already %s, so avahi answers %s\n' "$short" "$MDNS_NAME"
    return
  fi
  if ! command -v avahi-publish > /dev/null 2>&1; then
    printf '==> %s will not resolve. Install avahi (sudo apt-get install -y avahi-daemon avahi-utils)\n' "$MDNS_NAME"
    printf '    or rename the host with: sudo hostnamectl set-hostname %s\n' "$short"
    return
  fi
  local address
  address="$(hostname -I 2> /dev/null | awk '{print $1}')"
  if [[ -z $address ]]; then
    printf '==> Could not determine a LAN address, skipping the %s alias\n' "$MDNS_NAME"
    return
  fi
  avahi-publish -a -R "$MDNS_NAME" "$address" > /dev/null 2>&1 &
  MDNS_PID=$!
  printf '==> Advertising %s as %s over mDNS (pid %s)\n' "$MDNS_NAME" "$address" "$MDNS_PID"
}

cleanup() {
  if [[ -n $MDNS_PID ]]; then
    kill "$MDNS_PID" 2> /dev/null || true
  fi
  "${DOCKER[@]}" rm -f "$CONTAINER" > /dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

publish_mdns

"${DOCKER[@]}" rm -f "$CONTAINER" > /dev/null 2>&1 || true

BIND_INTERFACE="${CHESS_GANTRY_BIND_INTERFACE:-eth0}"

address_of_interface() {
  local interface="$1"
  command -v ip > /dev/null 2>&1 || return 1
  ip -4 -o addr show dev "$interface" 2> /dev/null \
    | awk '{print $4}' | cut -d/ -f1 | head -n 1
}

LAN_IP="$(address_of_interface "$BIND_INTERFACE" || true)"
if [[ -n $LAN_IP ]]; then
  BIND_ADDRESS="$LAN_IP"
  printf '==> Binding to %s (%s)\n' "$BIND_INTERFACE" "$BIND_ADDRESS"
else
  BIND_ADDRESS=0.0.0.0
  LAN_IP="$(hostname -I 2> /dev/null | awk '{print $1}')"
  LAN_IP="${LAN_IP:-127.0.0.1}"
  printf '==> %s has no IPv4 address; binding every interface instead\n' "$BIND_INTERFACE"
  printf '    set CHESS_GANTRY_BIND_INTERFACE to the right one, for example wlan0\n'
  if command -v ip > /dev/null 2>&1; then
    printf '    interfaces with addresses: %s\n' \
      "$(ip -4 -o addr show scope global 2> /dev/null | awk '{printf "%s=%s ", $2, $4}')"
  fi
fi

port_in_use() {
  local port="$1"
  command -v ss > /dev/null 2>&1 || return 1
  ss -lntH 2> /dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$"
}

open_firewall_port() {
  local port="$1"
  if command -v firewall-cmd > /dev/null 2>&1 && as_root firewall-cmd --state > /dev/null 2>&1; then
    if as_root firewall-cmd --add-port="${port}/tcp" > /dev/null 2>&1; then
      printf '    firewalld: opened %s/tcp until the next reboot\n' "$port"
    else
      printf '    firewalld: could not open %s/tcp\n' "$port"
    fi
  elif command -v ufw > /dev/null 2>&1 && as_root ufw status 2> /dev/null | grep -q 'Status: active'; then
    if as_root ufw allow "${port}/tcp" > /dev/null 2>&1; then
      printf '    ufw: allowed %s/tcp\n' "$port"
    else
      printf '    ufw: could not allow %s/tcp\n' "$port"
    fi
  fi
}

printf '==> Opening the LAN path on %s\n' "$LAN_IP"
if port_in_use "$HTTP_PORT"; then
  printf '    port %s is already in use on this host, skipping it\n' "$HTTP_PORT"
  printf '    find the holder with: sudo ss -lntp | grep :%s\n' "$HTTP_PORT"
  HTTP_PORT="$ALT_PORT"
fi
open_firewall_port "$HTTP_PORT"
if [[ $ALT_PORT != "$HTTP_PORT" ]]; then
  if port_in_use "$ALT_PORT"; then
    printf '    port %s is already in use on this host, skipping it\n' "$ALT_PORT"
    ALT_PORT="$HTTP_PORT"
  else
    open_firewall_port "$ALT_PORT"
  fi
fi

RUN_ARGS=(
  --name "$CONTAINER"
  --publish "${BIND_ADDRESS}:${HTTP_PORT}:${APP_PORT}"
  --volume "$ROOT/config.json:/app/config.json:ro"
  --volume "$ROOT/data:/app/data"
  --env "CHESS_GANTRY_PUBLIC_HOST=$MDNS_NAME"
  --env "CHESS_GANTRY_WEB_HOST=0.0.0.0"
  --env "CHESS_GANTRY_WEB_PORT=$APP_PORT"
  --env "CHESS_GANTRY_DISTROLESS=1"
)

if [[ -n $CLERK_PUBLISHABLE_KEY ]]; then
  RUN_ARGS+=(--env "CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY")
  printf '==> Clerk authentication enabled\n'
else
  printf '==> WARNING: Clerk is unset; anyone who can reach the dashboard can control the gantry\n'
fi

if [[ -n $CAMERA_SOURCE && -n $VIDEO_DEVICE ]]; then
  printf 'Set only CHESS_GANTRY_CAMERA_SOURCE or CHESS_GANTRY_VIDEO_DEVICE, not both.\n' >&2
  exit 2
fi

if [[ -n $VIDEO_DEVICE ]]; then
  if [[ ! -c $VIDEO_DEVICE ]]; then
    printf 'Camera device %s does not exist or is not a character device.\n' "$VIDEO_DEVICE" >&2
    exit 2
  fi
  RUN_ARGS+=(--device "$VIDEO_DEVICE:/dev/video0" --env "CHESS_GANTRY_CAMERA_SOURCE=/dev/video0" --env "CHESS_GANTRY_CAMERA_ENABLED=1")
  if video_gid="$(stat -c '%g' "$VIDEO_DEVICE" 2> /dev/null)"; then
    RUN_ARGS+=(--group-add "$video_gid")
  fi
  printf '==> Camera device %s attached as /dev/video0\n' "$VIDEO_DEVICE"
elif [[ -n $CAMERA_SOURCE ]]; then
  RUN_ARGS+=(--env "CHESS_GANTRY_CAMERA_SOURCE=$CAMERA_SOURCE" --env "CHESS_GANTRY_CAMERA_ENABLED=1")
  printf '==> Network or configured camera source attached\n'
else
  CAMERA_SOURCE="auto:http://192.168.100.88:8080"
  RUN_ARGS+=(--env "CHESS_GANTRY_CAMERA_SOURCE=$CAMERA_SOURCE" --env "CHESS_GANTRY_CAMERA_ENABLED=1")
  printf '==> Using default phone camera %s\n' "$CAMERA_SOURCE"
fi

if [[ -n ${OPENAI_API_KEY:-} ]]; then
  RUN_ARGS+=(--env "OPENAI_API_KEY=$OPENAI_API_KEY")
  printf '==> OpenAI Sol API key attached\n'
else
  printf '==> WARNING: OPENAI_API_KEY is unset; camera preview works but Sol recognition cannot start\n'
fi

if [[ -n ${ANTHROPIC_API_KEY:-} ]]; then
  RUN_ARGS+=(--env "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")
  printf '==> Anthropic Claude API key attached\n'
else
  printf '==> ANTHROPIC_API_KEY is unset; Claude vs ChatGPT arena is disabled\n'
fi

if [[ -n ${LICHESS_TOKEN:-} ]]; then
  RUN_ARGS+=(--env "LICHESS_TOKEN=$LICHESS_TOKEN")
  printf '==> Lichess Board API write token attached\n'
else
  printf '==> LICHESS_TOKEN is unset; Lichess game modes are disabled\n'
fi

if [[ $ALT_PORT != "$HTTP_PORT" ]]; then
  RUN_ARGS+=(--publish "${BIND_ADDRESS}:${ALT_PORT}:${APP_PORT}")
fi

if [[ -e $SERIAL_DEVICE ]]; then
  RUN_ARGS+=(--device "$SERIAL_DEVICE" --env "CHESS_GANTRY_SERIAL_PORT=$SERIAL_DEVICE")
  if serial_gid="$(stat -c '%g' "$SERIAL_DEVICE" 2> /dev/null)"; then
    RUN_ARGS+=(--group-add "$serial_gid")
  fi
  printf '==> Serial device %s attached\n' "$SERIAL_DEVICE"
else
  RUN_ARGS+=(--env "CHESS_GANTRY_DEMO=1")
  printf '==> %s is absent, starting in demo mode with a simulated controller\n' "$SERIAL_DEVICE"
fi

url_for() {
  local host="$1" port="$2"
  if [[ $port == 80 ]]; then
    printf 'http://%s' "$host"
  else
    printf 'http://%s:%s' "$host" "$port"
  fi
}

printf '==> Dashboard on this LAN: %s\n' "$(url_for "$LAN_IP" "$HTTP_PORT")"
if [[ $ALT_PORT != "$HTTP_PORT" ]]; then
  printf '==>                   also: %s\n' "$(url_for "$LAN_IP" "$ALT_PORT")"
fi
printf '==>              by name: %s\n' "$(url_for "$MDNS_NAME" "$HTTP_PORT")"
printf '==> Open to every host that can route here. Configure Clerk for authenticated access.\n'
printf '==> Control-C stops the server.\n'

"${DOCKER[@]}" run --rm "${RUN_ARGS[@]}" "$IMAGE"
