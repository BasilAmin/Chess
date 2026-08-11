#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
APP_UID_DEFAULT=65532
APP_GID_DEFAULT=65532
problems=0

note() { printf '%s\n' "$*"; }
warn() {
  printf 'WARN  %s\n' "$*" >&2
  problems=$((problems + 1))
}

if [[ ! -f $ENV_FILE ]]; then
  cp .env.example "$ENV_FILE"
  note "Created .env from .env.example."
fi

set_env() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    local tmp
    tmp="$(mktemp)"
    sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" > "$tmp"
    mv "$tmp" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  note "  ${key}=${value}"
}

read_env() {
  local key="$1" fallback="$2" line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 || true)"
  line="${line#*=}"
  printf '%s' "${line:-$fallback}"
}

note "Host group IDs for the passed-through devices:"
for entry in "DIALOUT_GID dialout 20" "GPIO_GID gpio 993" "SPI_GID spi 995"; do
  set -- $entry
  key="$1"
  group="$2"
  fallback="$3"
  if gid="$(getent group "$group" | cut -d: -f3)" && [[ -n $gid ]]; then
    set_env "$key" "$gid"
  else
    set_env "$key" "$fallback"
    warn "no '${group}' group on this host; kept the ${fallback} placeholder"
  fi
done

note ""
note "Device nodes:"
marlin="$(read_env MARLIN_PORT /dev/ttyUSB0)"
for device in \
  "$marlin" \
  "$(read_env GPIO_MEM_DEVICE /dev/gpiomem)" \
  "$(read_env GPIO_CHIP_DEVICE /dev/gpiochip0)" \
  "$(read_env SPI_DEVICE_0 /dev/spidev0.0)" \
  "$(read_env SPI_DEVICE_1 /dev/spidev0.1)"; do
  if [[ -e $device ]]; then
    note "  present  $device  ($(stat -c '%U:%G %a' "$device"))"
  else
    warn "missing $device; docker compose up will fail until it exists or the entry is removed from .env and docker-compose.yml"
  fi
done

if [[ ! -e /dev/spidev0.0 ]]; then
  note "  enable SPI with 'dtparam=spi=on' in /boot/firmware/config.txt, then reboot"
fi

note ""
note "Application files:"
if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  note "  created config.json from config.example.json (calibrate it before moving hardware)"
else
  note "  present  config.json"
fi

app_uid="$(read_env APP_UID "$APP_UID_DEFAULT")"
app_gid="$(read_env APP_GID "$APP_GID_DEFAULT")"
mkdir -p data
owner="$(stat -c '%u:%g' data)"
if [[ $owner != "${app_uid}:${app_gid}" ]]; then
  if chown -R "${app_uid}:${app_gid}" data 2> /dev/null; then
    note "  data/ now owned by ${app_uid}:${app_gid}"
  else
    warn "data/ is owned by ${owner}; the container runs as ${app_uid}:${app_gid} and needs write access"
    note "  run: sudo chown -R ${app_uid}:${app_gid} data"
  fi
else
  note "  data/ owned by ${app_uid}:${app_gid}"
fi

note ""
arch="$(uname -m)"
if [[ $arch != aarch64 && $arch != arm64 && $arch != x86_64 ]]; then
  warn "kernel architecture is ${arch}; this image is built for arm64 and needs a 64-bit Raspberry Pi OS"
else
  note "Architecture ${arch} is supported."
fi

if [[ $problems -gt 0 ]]; then
  note ""
  note "${problems} item(s) need attention before 'docker compose up -d gantry'."
  exit 1
fi

note "Host is ready. Next: docker compose build && docker compose up -d gantry"
