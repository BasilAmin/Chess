<p align="center">
  <img src="./FullLogoWhite.webp" alt="Patch" width="220">
</p>

<h1 align="center">Chess Gantry</h1>

<p align="center">
  <strong>A physical chessboard that moves the pieces.</strong><br>
  Collision-aware planning, Marlin motion control, board sensing, Lichess integration, and a Clerk-authenticated dashboard.
</p>

<p align="center">
  <strong>Patch</strong><br>
  Basil Amin · Ben Hewston · Kelvin Gao · Odin Glynn
</p>

This README is the complete operator runbook. Run commands from the repository
root unless a section says otherwise. No Redis, cloud database, desktop
environment, or separate Lichess service is required.

> [!WARNING]
> This machine moves real hardware and energizes an electromagnet. Keep an
> independent physical power cutoff within reach. Never run a physical command
> until the gantry is calibrated, all three endstops work, the requested path is
> clear, and the magnet driver has flyback protection. Software checks do not
> replace safe mechanical and electrical design.

## Tomorrow: No Hardware

This is the safe starting sequence when no controller, motors, endstops, magnet,
MCP23017, or reed switches are connected.

### 1. Install The Development Tools

Requirements:

- Python 3.9 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Node.js and npm
- Git

Install dependencies:

```bash
uv sync
npm ci
```

### 2. Run The Complete Hardware-Free Check

```bash
./scripts/demo_check.sh
```

This compiles Python, runs all automated tests, runs formatting and policy
checks, plans `e2e4`, simulates the magnet, and streams a simulated board sweep.
It uses isolated files under `data/demo/` and does not open a serial or I2C
device. Success ends with:

```text
Demo readiness checks passed. No physical serial port was opened.
```

Run the checks separately when diagnosing a failure:

```bash
./scripts/check.sh
npm run check
```

### 3. Start The Simulated Dashboard

The dashboard uses Clerk authentication, including in demo mode. Export a Clerk
development publishable key, then launch it:

```bash
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./scripts/live_demo.sh
```

Open <http://127.0.0.1:8000> and sign in. `live_demo.sh` reruns the complete
hardware-free check before starting a simulated Marlin controller. Stop it with
Control-C.

To skip the readiness check on later launches:

```bash
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
uv run chess-gantry \
  --config config.demo.json \
  --state data/demo/board_state.json \
  --journal data/demo/pending_move.json \
  --audit data/demo/audit.jsonl \
  web --host 127.0.0.1 --demo
```

Use a `pk_test_` Clerk instance for plain HTTP. A `pk_live_` session cookie
normally requires HTTPS. Anyone permitted to sign in to the configured Clerk
instance can use the controls, so restrict sign-ups in Clerk before physical
operation.

### 4. Exercise The Synthetic 8 x 8 Sensor

Open **Synthetic 8 x 8 sensor lab** in the dashboard. The matrix format is:

```text
0 = empty square
1 = occupied square
```

Try the built-in datasets in this order:

1. White pawn `e2e4`
2. Reset to the standard position
3. White pawn `d2d4`
4. Reset to the standard position
5. White knight `g1f3`
6. Reset to the standard position
7. White knight `b1c3`

You can also click squares or paste one of these exact 8 x 8 integer matrices:

```text
examples/sensor_matrix_standard.json
examples/sensor_matrix_e2_lifted.json
examples/sensor_matrix_e2e4.json
```

The server samples its in-memory matrix at 300 Hz and accepts a state after
three stable samples. The browser polls status at about 10 Hz. Lifting a piece
produces a waiting state; placing it on a uniquely matching legal destination
commits the move and updates the piece map, side to move, FEN, and event history.

The engine starts from the standard chess position and uses legal move history
to preserve piece identity. An occupancy matrix alone cannot identify arbitrary
piece types. Ambiguous positions are not committed. Promotion ambiguity in this
synthetic detector defaults to a queen and is recorded as an event.

### 5. Run Individual Simulations

Plan `e2e4` without opening serial or changing board state:

```bash
uv run chess-gantry \
  --config config.demo.json \
  --state data/demo/board_state.json \
  --journal data/demo/pending_move.json \
  --audit data/demo/audit.jsonl \
  plan examples/move_e2_e4.json --summary-json
```

Simulate the MCP23017 reed switch changing between open and closed:

```bash
uv run chess-gantry reed-test --demo --samples 20 --interval 0.1
```

Simulate representative physical programs:

```bash
uv run chess-gantry --config config.demo.json motor-test \
  --distance-mm 20 --feed-mm-min 600 --confirm-motion --demo

uv run chess-gantry --config config.demo.json magnet-test \
  --duration-s 1 --confirm-motion --demo

uv run chess-gantry --config config.demo.json circle-demo \
  --diameter-mm 200 --feed-mm-min 1800 --confirm-motion --demo

uv run chess-gantry --config config.demo.json perimeter-demo \
  --width-mm 250 --height-mm 250 --feed-mm-min 1800 \
  --confirm-motion --demo

uv run chess-gantry --config config.demo.json square-center-demo \
  --feed-mm-min 1800 --dwell-ms 150 --confirm-motion --demo

uv run chess-gantry --config config.demo.json board-sweep \
  --feed-mm-min 1800 --magnet-on --confirm-motion --demo

uv run chess-gantry --config config.demo.json workspace-test \
  --feed-mm-min 1200 --confirm-motion --demo
```

`--demo` is the important no-hardware switch. The confirmation flags make the
simulator exercise the same streaming path used for real commands; they do not
connect to hardware when `--demo` is present.

## Raspberry Pi Deployment

The target is a Raspberry Pi running a 64-bit Linux OS. The current deployment
path is `run.sh`; the old Docker Compose scripts are not part of the operating
workflow.

### First Installation

```bash
sudo apt update
sudo apt install -y git curl
git clone --recurse-submodules https://github.com/odinglyn0/Chess.git
cd Chess
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./scripts/install_pi.sh
```

The installer installs Docker and `i2c-tools`, enables I2C when possible, adds
the current user to the `docker` and `dialout` groups, seeds local configuration
and state, and builds the image. If it enables I2C, reboot and rerun it:

```bash
sudo reboot
```

After logging back in:

```bash
cd ~/Chess
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./scripts/install_pi.sh
```

### Start The Pi Dashboard

```bash
cd ~/Chess
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./run.sh
```

`run.sh` does all of the following:

- builds `chess:latest` on the current host;
- creates `config.json` and local state when missing;
- mounts `config.json` read-only and `data/` read-write;
- passes `/dev/ttyUSB0` and `/dev/i2c-1` through when present;
- grants the container the serial and I2C device groups;
- starts simulated Marlin automatically when the serial device is absent;
- publishes the dashboard on port 80 and, when available, port 8000;
- advertises `chess.local` through mDNS when Avahi is available;
- attaches `LICHESS_TOKEN` only when it is exported;
- runs in the foreground and removes the container when Control-C is pressed.

Open the URL printed by the script, normally one of:

```text
http://chess.local
http://RASPBERRY_PI_IP
http://RASPBERRY_PI_IP:8000
```

The production image uses a Fedora 42 build stage to assemble a root filesystem
and a final `FROM scratch` runtime. The default image has Python, the app, curl,
CA certificates, and runtime libraries, but no shell, package manager, Node.js,
compiler, or Git client. It runs as UID/GID 65532.

### Useful `run.sh` Overrides

```bash
CHESS_GANTRY_SERIAL_PORT=/dev/ttyACM0 ./run.sh
CHESS_GANTRY_I2C_DEVICE=/dev/i2c-0 ./run.sh
CHESS_GANTRY_HTTP_PORT=8080 ./run.sh
CHESS_GANTRY_BIND_INTERFACE=wlan0 ./run.sh
CHESS_GANTRY_MDNS_NAME=relay-chess.local ./run.sh
CHESS_GANTRY_IMAGE=chess:test ./run.sh
```

If no serial controller is connected, `run.sh` explicitly prints that it is
starting in demo mode. If no I2C device exists, the dashboard still starts and
the physical reed panel reports an I2C error; the synthetic sensor lab remains
usable.

### Update The Pi

Stop the foreground server with Control-C, then update and restart:

```bash
cd ~/Chess
git pull --ff-only
git submodule update --init --recursive
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./run.sh
```

`run.sh` rebuilds the image. It preserves `config.json` and `data/`.

### Docker Diagnostics

Run these in a second terminal while `run.sh` is active:

```bash
docker ps --filter name=chess-gantry
docker logs -f chess-gantry
docker inspect --format '{{.State.Health.Status}}' chess-gantry
```

The image health check requests the dashboard every 30 seconds after a 20-second
startup grace period. There is no shell in the default runtime, so use logs,
inspection, or a rebuilt image instead of `docker exec ... bash`.

Manual build only:

```bash
docker build \
  --tag chess:latest \
  --build-arg FEDORA_VERSION=42 \
  --build-arg INCLUDE_GPIO=1 \
  --build-arg INCLUDE_DEV_TOOLS=0 \
  .
```

Set `INCLUDE_GPIO=0` for a non-Pi dashboard-only image. Set
`INCLUDE_DEV_TOOLS=1` only for development; it adds shell and Node tooling, so
the resulting image is no longer the minimal shell-free runtime.

## Machine Configuration

### Mechanical Geometry

| Property                   | Value          |
| -------------------------- | -------------- |
| Inner gantry width         | 330 mm         |
| Outer paired gantry height | 300 mm         |
| Board                      | 8 x 8          |
| Square-center spacing      | 40 mm          |
| Nearest-home square        | h1             |
| Nearest-home center        | `X2 Y298 Z320` |
| Homed host reference       | `X2 Y298 Z328` |

The four measured raw Marlin corner centers are:

```text
h1: X2   Y298 Z320
a1: X2   Y298 Z40
h8: X282 Y18  Z320
a8: X282 Y18  Z40
```

Seven 40 mm intervals span 280 mm between the eight centers.

### Motor And Magnet Mapping

```text
Physical X driver -> logical X -> x_min
Physical Y driver -> logical Y -> y_max
Physical E driver -> logical Z -> z_max
Physical Z driver -> unused
Electromagnet     -> Marlin fan P0
```

The physical E connector is intentionally controlled as logical Marlin Z. Host
commands must use `X`, `Y`, and `Z`, never extrusion `E`. Magnet commands are:

```gcode
M106 P0 S255
M107 P0
```

### Homing Reference

Configured homing runs:

```gcode
G28 X Y Z
M400
G92 X2 Y298 Z328
M400
```

The installed firmware may retain nominal 350 mm axis limits. The host remap and
workspace bounds restrict generated movement to the measured 330 x 300 mm
machine. `config.json` is the physical configuration; `config.demo.json` is for
simulation.

> [!IMPORTANT]
> `safety.home_before_execute` is currently `false`. A normal `execute`, `run
--confirm-motion`, or raw CLI follower does not automatically home. Home the
> machine explicitly after every boot, controller reset, or emergency stop.

> [!IMPORTANT]
> The example configuration currently has `safety.calibrated: true` because it
> contains measured project geometry. That value is not proof that a newly
> assembled or repaired machine is safe. Re-measure the real mechanism and
> compare every value in `config.json` before allowing motion.

### Capture Limitation

Physical capture slots are disabled:

```json
"capture": {
  "enabled": false,
  "slots": []
}
```

The measured board leaves no verified safe capture storage area inside the
current travel. Non-capturing moves work. Physical captures stop before motion
until external storage coordinates are measured and configured. Promotion also
requires verified physical piece replacement.

## Physical Commissioning

Do this in order when the hardware is available. Do not skip directly to a
piece move.

### 1. Inspect Before Power

Verify all of the following physically:

- X and Y paired gantries are square and move freely by hand when unpowered.
- The physical E motor cable is the inner axis and firmware exposes it as Z.
- Every axis moves toward its own endstop when homing.
- Endstops cannot be hit by the wrong mechanical surface.
- The electromagnet has a suitable driver, flyback protection, and independent cutoff.
- No motor current passes through the Raspberry Pi.
- USB, controller, Pi, I2C expander, and sensor grounds are correct.
- The complete requested path is clear of people, pieces, cables, and tools.

### 2. Confirm Devices

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2> /dev/null
ls -l /dev/i2c-* 2> /dev/null
```

List and rank serial ports from the application:

```bash
uv run chess-gantry --config config.json ports
```

If the controller is not `/dev/ttyUSB0`, update `serial.port` in `config.json`
or export `CHESS_GANTRY_SERIAL_PORT` for `run.sh`.

### 3. Diagnose Without Motion

```bash
uv run chess-gantry --config config.json diagnose
```

This connects, verifies Marlin with `M115`, and reads `M119` and `M114`. It does
not move motors.

Watch endstop transitions while pressing and releasing each switch by hand:

```bash
uv run chess-gantry --config config.json endstop-watch
```

Validate the installed firmware identity and endstops:

```bash
uv run python scripts/check_firmware.py --config config.json
```

The checker expects Marlin to identify as `Relay Chess Gantry` and verifies the
configured endstop states without moving.

### 4. Home With An Empty Workspace

Keep a hand on the independent power cutoff:

```bash
uv run chess-gantry --config config.json home-gantry \
  --confirm-motion --confirm-clear-path
```

The expected host reference after homing is `X2 Y298 Z328`. A homing record is
written to `data/gantry_home.json` by default.

Guarded firmware verification including homing:

```bash
uv run python scripts/check_firmware.py \
  --config config.json --home --confirm-clear-path
```

### 5. Test Small Motion With Magnet Off

First print the program without opening serial:

```bash
uv run chess-gantry --config config.json motor-test \
  --distance-mm 5 --feed-mm-min 300
```

After inspecting it, run the same bounded test physically:

```bash
uv run chess-gantry --config config.json motor-test \
  --distance-mm 5 --feed-mm-min 300 --confirm-motion
```

### 6. Test The Magnet Separately

Print the one-second program:

```bash
uv run chess-gantry --config config.json magnet-test --duration-s 1
```

After verifying the driver and flyback protection, energize it physically:

```bash
uv run chess-gantry --config config.json magnet-test \
  --duration-s 1 --confirm-motion
```

The duration is limited to five seconds by the CLI.

### 7. Traverse The Workspace With No Pieces

```bash
uv run chess-gantry --config config.json workspace-test \
  --feed-mm-min 1200 \
  --confirm-motion --confirm-empty-workspace --confirm-at-switches
```

Test the measured 64 square centers with the magnet off:

```bash
uv run chess-gantry --config config.json square-center-demo \
  --feed-mm-min 1800 --dwell-ms 150 \
  --confirm-motion --confirm-clear-workspace
```

Test the full configured perimeter with the magnet off:

```bash
uv run chess-gantry --config config.json perimeter-demo \
  --width-mm 330 --height-mm 300 --feed-mm-min 1800 \
  --confirm-motion --confirm-clear-workspace
```

Run a 250 x 250 mm perimeter instead:

```bash
uv run chess-gantry --config config.json perimeter-demo \
  --width-mm 250 --height-mm 250 --feed-mm-min 1800 \
  --confirm-motion --confirm-clear-workspace
```

### 8. Run Optional Magnet Motion Demos

Only after separate motion and magnet tests pass:

```bash
uv run chess-gantry --config config.json piece-demo \
  --distance-mm 20 --feed-mm-min 1200 \
  --confirm-motion --confirm-at-switches --confirm-piece --confirm-magnet

uv run chess-gantry --config config.json circle-demo \
  --diameter-mm 200 --feed-mm-min 1800 \
  --confirm-motion --confirm-clear-workspace --confirm-magnet

uv run chess-gantry --config config.json square-center-demo \
  --feed-mm-min 1800 --dwell-ms 150 --magnet-on \
  --confirm-motion --confirm-clear-workspace --confirm-magnet
```

For a board sweep, remove every piece and obstruction and place the gantry at
the configured origin first:

```bash
uv run chess-gantry --config config.json board-sweep \
  --feed-mm-min 1800 --magnet-on \
  --confirm-motion --confirm-empty-board --confirm-origin --confirm-magnet
```

### Emergency Stop

Use the physical cutoff first when immediate electrical isolation is required.
The software emergency command is:

```bash
uv run chess-gantry --config config.json stop
```

This sends Marlin `M112`. Reset or power-cycle Marlin afterward, rerun non-motion
diagnostics, and home again before any further movement.

## Plan And Execute Chess Moves

### State Files

Default local files:

```text
data/board_state.json   Last committed board state
data/pending_move.json  Interrupted-move transaction journal
data/audit.jsonl        Append-only operation history
```

Inspect current state:

```bash
uv run chess-gantry --config config.json show-state
```

Use isolated state by putting all global options before the subcommand:

```bash
uv run chess-gantry \
  --config config.json \
  --state data/games/example/board_state.json \
  --journal data/games/example/pending_move.json \
  --audit data/games/example/audit.jsonl \
  show-state
```

### Plan One Move

Planning does not open serial or mutate persistent state:

```bash
uv run chess-gantry --config config.json \
  plan examples/move_e2_e4.json --summary-json
```

Write the generated G-code to a file:

```bash
uv run chess-gantry --config config.json \
  plan examples/move_e2_e4.json \
  --summary-json --output data/e2e4.gcode
```

Useful examples:

```text
examples/board_state.standard.json
examples/board_state.capture_demo.json
examples/board_state.en_passant_demo.json
examples/move_e2_e4.json
examples/move_nested.json
examples/move_capture_demo.json
examples/move_en_passant.json
```

Convert a legal UCI move into gantry move JSON:

```bash
uv run chess-gantry --config config.json uci-to-json e2e4
```

Use `--help` after a subcommand to see its exact options:

```bash
uv run chess-gantry uci-to-json --help
```

### Execute One Move

Before every execution, verify the physical board exactly matches
`data/board_state.json`, clear any pending journal, home explicitly, and inspect
the plan. Then execute:

```bash
uv run chess-gantry --config config.json home-gantry \
  --confirm-motion --confirm-clear-path

uv run chess-gantry --config config.json \
  plan examples/move_e2_e4.json --summary-json

uv run chess-gantry --config config.json \
  execute examples/move_e2_e4.json --confirm-motion
```

State commits only after every Marlin command acknowledges successfully. A
failure leaves `data/pending_move.json` and blocks later planning or execution
until it is reconciled.

### Reset Tracked State

Physically arrange every piece in the standard starting position first, stop all
followers and movement, then run:

```bash
uv run chess-gantry --config config.json \
  reset-state --confirm-standard-position
```

Never use reset merely to bypass a pending-move warning.

### Recover A Pending Move

Inspect the journal without changing it:

```bash
uv run chess-gantry --config config.json reconcile
```

If the displayed physical move completed exactly as proposed:

```bash
uv run chess-gantry --config config.json reconcile \
  --mark-applied --confirm-physical-state
```

If no part of the move completed and the physical board still exactly matches
the committed `board_state.json`:

```bash
uv run chess-gantry --config config.json reconcile \
  --discard --confirm-physical-state
```

If the physical result is uncertain, stop and manually restore a known board
position. Do not guess between `--mark-applied` and `--discard`.

## Dashboard Operation

### Local Hardware Dashboard

```bash
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
uv run chess-gantry --config config.json web --host 127.0.0.1
```

Open <http://127.0.0.1:8000>. Add `--no-browser` for headless startup.

### Trusted LAN Dashboard

```bash
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./scripts/run_network_ui.sh
```

Open `http://HOST_IP:8000` from another device on the same trusted network and
sign in through Clerk. This is plain HTTP; do not expose it directly to the
public internet.

The dashboard provides live Marlin position, guarded jogging, homing and demos,
move planning/execution, state and recovery, Lichess operation, the synthetic
sensor matrix, MCP23017 state, task logs, cancellation, and emergency stop.

The browser currently treats pressing a physical-operation button as the
operation confirmation; it does not present separate confirmation checkboxes.
Perform the physical preflight checks in this README before pressing Run.

### Keyboard Jogging

After connecting and homing, enable arrow-key motion in the page:

```text
Arrow Left / Right -> inner gantry width
Arrow Up / Down    -> paired outer gantry height
Escape             -> disarm keyboard movement
```

Select a 0.5, 1, 5, or 10 mm step and a bounded feed rate. Events are ignored
while a form field has focus, while a key is held, while the controller is
disconnected or unhomed, or while another task owns serial. One jog is limited
to one logical axis, 20 mm, and the configured workspace.

The live position panel polls `M114` about every 750 ms when connected and idle.
It displays raw outer X, outer Y, and inner Z coordinates.

## Raw G-code Debug Console

The Django console shares one persistent Marlin connection among browser
clients. Install its optional dependency:

```bash
uv sync --extra debug-console
```

Simulated, local only:

```bash
uv run chess-gantry --config config.demo.json debug-console --demo
```

Physical, local only:

```bash
uv run chess-gantry --config config.json debug-console
```

Physical, reachable on a trusted LAN:

```bash
export CHESS_GANTRY_DEBUG_TOKEN="use-a-long-random-value"
uv run chess-gantry --config config.json debug-console \
  --host 0.0.0.0 --allow-network --no-browser
```

Open <http://127.0.0.1:8300> locally. A generated token is printed when
`CHESS_GANTRY_DEBUG_TOKEN` and `--token` are both absent.

Only one process may own the serial port. Stop the normal dashboard before
starting the physical debug console. Raw G-code bypasses workspace, board-state,
and magnet safeguards. The raw path rejects the configured emergency command;
use the dedicated stop control. Every command, response, and client is appended
to the audit log.

## MCP23017 Reed Switch

The first physical board sensor is a normally open reed switch on MCP23017 GPB0.

### Wiring

```text
Pi pin 3 / GPIO 2 (SDA) -> MCP23017 SDA
Pi pin 5 / GPIO 3 (SCL) -> MCP23017 SCL
Pi pin 1           (3V3) -> MCP23017 VDD and RESET
Pi pin 6           (GND) -> MCP23017 VSS and A0/A1/A2
MCP23017 GPB0            -> reed switch -> GND
```

Grounding A0, A1, and A2 selects address `0x20`. Software configures GPB0 as an
input with the MCP23017 pull-up enabled:

```text
No magnet:      OPEN, raw HIGH
Magnet present: CLOSED, raw LOW
```

### Enable And Test I2C

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

After reboot:

```bash
sudo apt install -y i2c-tools
i2cdetect -y 1
```

The scan should show `20`. Run a finite ten-second test:

```bash
uv run chess-gantry reed-test \
  --bus 1 --address 0x20 --samples 100 --interval 0.1
```

Watch until Control-C instead:

```bash
uv run chess-gantry reed-test --bus 1 --address 0x20
```

Use `--active-high` only for deliberately inverted electrical behavior. The
dashboard polls the same input and shows OPEN/CLOSED, HIGH/LOW, errors, and the
transition count. If it fails, check `/dev/i2c-1`, address straps, RESET, 3.3 V,
shared ground, and the result of `i2cdetect -y 1`.

## Lichess

The application talks directly to `https://lichess.org` with `berserk` and uses
`python-chess` to translate legal moves. Watching a public game does not require
a token. A Board API token is required to submit sensor-inferred moves.

### Replay A Public Game Without Hardware

```bash
uv run chess-gantry \
  --config config.demo.json \
  --state data/lichess/GAME_ID/board_state.json \
  lichess-pgn GAME_ID --output-dir data/lichess/GAME_ID/replay
```

### Follow A Public Game Without Hardware

The helper defaults to game `6RkOwfp1`; pass another game ID as the second
argument:

```bash
./scripts/lichess_game.sh check GAME_ID
./scripts/lichess_game.sh dry-run GAME_ID
```

`check` runs software readiness and replays the currently recorded game. A
`dry-run` follows new moves continuously, writes move JSON and G-code under
`data/lichess/GAME_ID/`, reconnects after stream drops, and opens no serial
device. Stop it with Control-C.

Inspect isolated state:

```bash
./scripts/lichess_game.sh status GAME_ID
./scripts/lichess_game.sh reconcile GAME_ID
```

### Follow A Game Physically

Start with a newly reset standard physical board. The helper homes first and
then follows new moves:

```bash
./scripts/lichess_game.sh reset GAME_ID
./scripts/lichess_game.sh play GAME_ID
```

Successfully executed event IDs prevent replay after restart. The physical
follower stops safely on captures because capture storage is disabled. Promotion
requires physical replacement. Test castling and other multi-piece movement in
simulation before presentation use.

For lowest-latency presentation, use **Live Lichess TV game** in the dashboard:

1. Create a fresh public standard game containing zero moves.
2. Put the physical board in the standard starting position.
3. Start the dashboard and enter the game ID.
4. Perform the physical preflight checks in this README.
5. Start immediate live play before White's first move.

The browser live workflow rejects a game that already contains moves, creates
fresh isolated state under `data/web-live/`, homes once, and executes newly
streamed plies through one serial connection.

### Submit Synthetic Sensor Moves To Lichess

Create a Lichess Board API token intended for the playing account, then start
the app with it kept on the server:

```bash
export LICHESS_TOKEN="lip_your_board_api_token"
export CLERK_PUBLISHABLE_KEY="pk_test_your_key"
./run.sh
```

In **Synthetic 8 x 8 sensor lab**, enter the active game ID and enable **Write
inferred moves**. A uniquely inferred move is submitted with the Board API
`make_move` endpoint. The dashboard never receives the token. The lab does not
create games automatically.

## Firmware

The project firmware targets a Creality 4.2.2 board with STM32F103RET6 using the
Marlin PlatformIO environment `STM32F103RE_creality`.

Committed artifact:

```text
firmware/relay-chess-v422-stm32f103ret6.bin
firmware/relay-chess-v422-stm32f103ret6.bin.sha256
```

Verify the committed binary:

```bash
(cd firmware && sha256sum -c relay-chess-v422-stm32f103ret6.bin.sha256)
```

Build it again after installing PlatformIO:

```bash
git submodule update --init --recursive
./scripts/build_firmware.sh
```

The script builds in `chicken/`, copies the newest binary to `firmware/`, and
regenerates its checksum. The repository does not currently provide an
automated flashing command. Use the controller manufacturer's supported Marlin
firmware procedure and verify the exact board MCU before flashing.

Installed firmware expectations:

```text
MACHINE_TYPE: Relay Chess Gantry
EXTRUDER_COUNT: 0
EMERGENCY_PARSER: enabled
QUICK_HOME: enabled
Heaters, bed, hotend, extrusion behavior, and BLTouch: disabled
```

Validate installed firmware without motion:

```bash
uv run python scripts/check_firmware.py --config config.json
```

## Complete Command Reference

Show the current authoritative CLI help:

```bash
uv run chess-gantry --help
uv run chess-gantry COMMAND --help
```

| Command              | Purpose                                                        |
| -------------------- | -------------------------------------------------------------- |
| `plan`               | Validate a move and print or write G-code without hardware     |
| `validate`           | Validate move, state, and path without G-code output           |
| `execute`            | Send one move and commit state after acknowledgements          |
| `run`                | Generate G-code; add `--confirm-motion` for physical streaming |
| `init-state`         | Install a validated initial board-state JSON                   |
| `show-state`         | Print the tracked board state                                  |
| `reset-state`        | Reset tracked state to standard position                       |
| `uci-to-json`        | Convert legal UCI notation to gantry move JSON                 |
| `lichess-pgn`        | Fetch and dry-run currently recorded Lichess moves             |
| `lichess-follow`     | Follow newly streamed Lichess moves                            |
| `ports`              | List ranked pyserial ports                                     |
| `diagnose`           | Read Marlin identity, endstops, and position without motion    |
| `endstop-watch`      | Print endstop hit/release transitions                          |
| `reed-test`          | Read MCP23017 GPB0 or simulate transitions                     |
| `reference-gantry`   | Assign origin while all three endstops are held                |
| `home-gantry`        | Guarded `G28 X Y Z`, verification, and homing record           |
| `home`               | Run configured homing commands                                 |
| `web`                | Start the Clerk-authenticated operator dashboard               |
| `debug-console`      | Start the token-authenticated raw G-code console               |
| `motor-test`         | Test bounded motion on each logical axis                       |
| `piece-demo`         | Pick up one piece, move, release, and return                   |
| `magnet-test`        | Pulse the electromagnet for at most five seconds               |
| `circle-demo`        | Home, energize magnet, and trace a circle                      |
| `perimeter-demo`     | Home and trace a rectangular perimeter                         |
| `square-center-demo` | Home and visit all 64 measured centers                         |
| `board-sweep`        | Traverse every board square in a serpentine path               |
| `workspace-test`     | Traverse a bounded workspace grid with magnet off              |
| `stop`               | Send the configured Marlin emergency stop immediately          |
| `reconcile`          | Resolve an interrupted move after physical inspection          |

## Troubleshooting

### A Command Reports A Pending Move

```bash
uv run chess-gantry --config config.json reconcile
```

Do not delete the journal manually. Follow the recovery section after checking
the physical board.

### The Dashboard Will Not Start

Verify the Clerk key is exported:

```bash
test -n "$CLERK_PUBLISHABLE_KEY" && printf 'Clerk key is set\n'
```

Use a Clerk development key over plain HTTP. Check whether the port is occupied:

```bash
sudo ss -lntp | grep ':8000\|:80'
```

### `chess.local` Does Not Resolve

Use the printed numeric Pi IP, or install mDNS tools:

```bash
sudo apt install -y avahi-daemon avahi-utils
```

You can permanently name the host instead:

```bash
sudo hostnamectl set-hostname chess
```

### Serial Is Missing Or Denied

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2> /dev/null
groups
uv run chess-gantry --config config.json ports
```

Log out and back in after being added to `dialout`. Only one dashboard, follower,
debug console, or CLI process may own the serial port at a time.

### The Container Starts In Demo Mode Unexpectedly

`run.sh` defaults to `/dev/ttyUSB0`. Set the actual node:

```bash
CHESS_GANTRY_SERIAL_PORT=/dev/ttyACM0 ./run.sh
```

### Reed Input Fails

```bash
ls -l /dev/i2c-1
i2cdetect -y 1
```

Confirm that the scan shows `20`, RESET is held high, A0/A1/A2 are grounded,
and all devices share ground.

### Motion Or Position Looks Wrong

Stop immediately. Do not compensate by increasing workspace limits. Verify
motor mapping, endstop direction, `M119`, installed firmware identity, measured
home position, and `config.json`. Rehome only after the path to every switch is
clear.

### Capture Or Promotion Stops A Game

This is expected. Capture storage is disabled until safe off-board coordinates
are calibrated. Promotion needs physical piece replacement. Restore a known
physical and tracked state before continuing.

## Development

Install and run all checks:

```bash
uv sync
npm ci
./scripts/check.sh
npm run check
```

Apply repository formatting:

```bash
npm run format
```

The suite covers geometry, path planning, persistence, serial acknowledgements,
firmware configuration, container deployment, Clerk authentication, dashboard
task ownership, keyboard jogging, board sensing, reed switching, and Lichess
state handling.

---

<p align="center">
  <strong>Built at Patch for a chessboard that refuses to sit still.</strong>
</p>
