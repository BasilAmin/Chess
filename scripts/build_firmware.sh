#!/usr/bin/env bash
set -euo pipefail

cat >&2 << 'MESSAGE'
The customized Marlin source is not distributed in this repository.
Use the verified prebuilt image at:
  firmware/relay-chess-v422-stm32f103ret6.bin
Check it before flashing with:
  sha256sum --check firmware/relay-chess-v422-stm32f103ret6.bin.sha256
MESSAGE
exit 1
