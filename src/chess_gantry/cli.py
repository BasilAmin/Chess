from __future__ import annotations

from argparse import ArgumentParser, Namespace
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
import json
import os
import sys
import asyncio
from time import sleep

from .config import AppConfig
from .errors import GantryError, PendingTransactionError, ValidationError
from .environment import load_local_environment
from .models import BoardState, MoveDelta
from .persistence import read_json
from .serial_link import (
    DemoMarlinSerial,
    MarlinSerial,
    discover_serial_ports,
    endstop_transition_lines,
    parse_endstop_states,
)
from .service import GantryService
from .debug_console.runtime import TOKEN_ENVIRONMENT_KEY
from .lichess_pgn import fetch_pgn, pgn_moves
from .lichess_follow import follow_game
from .uci_adapter import uci_to_move


def _parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="chess-gantry", description="Plan and stream chess-gantry G-code to Marlin"
    )
    parser.add_argument(
        "--config", default="config.json", help="gantry configuration JSON"
    )
    parser.add_argument(
        "--state", default="data/board_state.json", help="persistent board-state JSON"
    )
    parser.add_argument(
        "--journal",
        default="data/pending_move.json",
        help="pending transaction journal",
    )
    parser.add_argument(
        "--audit", default="data/audit.jsonl", help="append-only audit log"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser(
        "plan", help="validate a move and print/write G-code without moving hardware"
    )
    plan.add_argument("move_json", help="move-delta JSON file")
    plan.add_argument("--output", "-o", help="write G-code to this file")
    plan.add_argument(
        "--summary-json",
        action="store_true",
        help="print plan summary as JSON before G-code",
    )

    validate = commands.add_parser(
        "validate",
        help="validate move, board state, and path without outputting G-code",
    )
    validate.add_argument("move_json", help="move-delta JSON file")

    execute = commands.add_parser(
        "execute", help="send a move over serial and commit board state on success"
    )
    execute.add_argument("move_json", help="move-delta JSON file")
    execute.add_argument(
        "--confirm-motion",
        action="store_true",
        help="required acknowledgement that the physical workspace is clear",
    )

    run = commands.add_parser(
        "run",
        help="read move JSON, generate G-code, and stream it to Marlin (dry-run without --confirm-motion)",
    )
    run.add_argument(
        "move_json", help="move-delta JSON file (e.g. examples/move_e2_e4.json)"
    )
    run.add_argument(
        "--confirm-motion",
        action="store_true",
        help="stream G-code over serial to the gantry controller and commit board state on success",
    )
    run.add_argument("--output", "-o", help="write G-code to this file")
    run.add_argument(
        "--summary-json", action="store_true", help="print plan summary as JSON"
    )

    init_state = commands.add_parser(
        "init-state", help="validate and install an initial board-state JSON"
    )
    init_state.add_argument("source_json", help="source board-state JSON")
    init_state.add_argument(
        "--overwrite", action="store_true", help="replace an existing state file"
    )

    commands.add_parser("show-state", help="print the current board state")
    reset_state = commands.add_parser(
        "reset-state",
        help="reset tracked state to the standard starting position and clear any journal",
    )
    reset_state.add_argument(
        "--confirm-standard-position",
        action="store_true",
        help="required confirmation that all physical pieces are in their standard starting squares",
    )
    uci = commands.add_parser(
        "uci-to-json",
        help="convert a legal UCI move such as e2e4 to gantry move-delta JSON",
    )
    uci.add_argument("uci_move", help="four-character UCI move, e.g. e2e4")
    uci.add_argument("--event-id", help="optional unique game/ply identifier")
    uci.add_argument(
        "--en-passant",
        action="store_true",
        help="treat a diagonal pawn move to an empty square as en passant",
    )
    uci.add_argument(
        "--output", "-o", help="write move JSON to this file instead of stdout"
    )
    pgn = commands.add_parser(
        "lichess-pgn",
        help="fetch a Lichess game and dry-run all recorded moves to JSON and G-code",
    )
    pgn.add_argument("game_id", help="Lichess game id")
    pgn.add_argument(
        "--output-dir",
        default="data/lichess",
        help="directory for generated event JSON and G-code",
    )
    follow = commands.add_parser(
        "lichess-follow",
        help="stream a Lichess game in real time and create JSON/G-code for new moves",
    )
    follow.add_argument("game_id", help="Lichess game id")
    follow.add_argument(
        "--output-dir",
        default="data/lichess",
        help="directory for generated move JSON and G-code",
    )
    follow.add_argument("--session", help="persistent follow-session JSON path")
    follow.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds to wait before reconnecting a dropped stream",
    )
    follow.add_argument(
        "--once",
        action="store_true",
        help="process the current game state once, then exit",
    )
    follow.add_argument(
        "--reset-session",
        action="store_true",
        help="forget emitted move history and recreate files",
    )
    follow.add_argument(
        "--execute",
        action="store_true",
        help="stream new moves to Marlin and commit board state",
    )
    follow.add_argument(
        "--execute-existing",
        action="store_true",
        help="with --execute, also stream already dry-run recorded moves",
    )
    follow.add_argument(
        "--confirm-motion", action="store_true", help="required with --execute"
    )
    follow.add_argument(
        "--obstacle-keepout-mm",
        type=float,
        help="dry-run-only A* clearance override; does not modify config.json",
    )
    mirror = commands.add_parser(
        "lichess-mirror",
        help="mirror a fresh Lichess game in a terminal with no vision",
    )
    mirror.add_argument("game_id", help="fresh standard Lichess game ID")
    mirror.add_argument(
        "--execute",
        action="store_true",
        help="home once and physically execute each new Lichess move",
    )
    mirror.add_argument("--demo", action="store_true", help="use simulated Marlin")
    mirror.add_argument("--once", action="store_true", help="sync once and exit")
    mirror.add_argument(
        "--configured-speed",
        action="store_true",
        help="use config motion feeds instead of fast 12000/3000 mm/min profile",
    )
    mirror.add_argument(
        "--confirm-motion",
        action="store_true",
        help="required for physical execution",
    )
    mirror.add_argument(
        "--confirm-standard-position",
        action="store_true",
        help="required confirmation that the physical board is at the standard position",
    )
    mirror.add_argument(
        "--confirm-clear-path",
        action="store_true",
        help="required confirmation that homing and board paths are clear",
    )
    mirror.add_argument(
        "--confirm-high-speed",
        action="store_true",
        help="required for the fast 12000/3000 mm/min profile",
    )
    mirror.add_argument("--unicode", action="store_true", help="render Unicode pieces")
    mirror.add_argument(
        "--no-screen",
        action="store_true",
        help="append terminal updates instead of clearing the screen",
    )
    mirror.add_argument(
        "--stream-mode",
        choices=("auto", "public", "board"),
        default="public",
        help="public mirrors any public game; board requires a participant token",
    )
    commands.add_parser("ports", help="list ranked serial ports visible to pyserial")

    diagnose = commands.add_parser(
        "diagnose",
        help="connect, verify Marlin with M115, and read endstops/position without moving",
    )
    diagnose.add_argument(
        "--port", help="override serial port; omit to use config/auto-detection"
    )
    diagnose.add_argument("--baudrate", type=int, help="try only this baud rate")

    endstop_watch = commands.add_parser(
        "endstop-watch",
        help="poll M119 and print each endstop hit or release transition",
    )
    endstop_watch.add_argument(
        "--interval",
        type=float,
        default=0.2,
        help="seconds between M119 polls (default: 0.2)",
    )
    endstop_watch.add_argument(
        "--samples",
        type=int,
        default=0,
        help="stop after this many samples; zero watches until Ctrl+C",
    )
    endstop_watch.add_argument(
        "--port", help="override serial port; omit to use config/auto-detection"
    )
    endstop_watch.add_argument("--baudrate", type=int, help="try only this baud rate")
    endstop_watch.add_argument(
        "--demo", action="store_true", help="use simulated open endstops"
    )

    vision_test = commands.add_parser(
        "vision-test",
        help="capture one or more frames and transcribe the board with OpenAI Sol",
    )
    vision_test.add_argument(
        "--source",
        default="snapshot:http://192.168.100.88:8080/shot.jpg",
        help="image path, camera index/device, stream URL, or snapshot:URL",
    )
    vision_test.add_argument(
        "--frames",
        type=int,
        default=1,
        help="frames to transcribe; zero watches until Ctrl+C (default: 1)",
    )
    vision_test.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="seconds between Sol requests (default: 3)",
    )
    vision_test.add_argument(
        "--preview-output",
        help="write the latest camera frame to this JPEG path",
    )
    vision_test.add_argument(
        "--orientation",
        choices=("white_bottom", "black_bottom"),
        default="white_bottom",
    )

    reference_gantry = commands.add_parser(
        "reference-gantry",
        help="require X/Y/Z endstops triggered, then assign the mirrored X/Y/Z origin",
    )
    reference_gantry.add_argument(
        "--confirm-at-switches",
        action="store_true",
        help="required confirmation that all three carriages are manually held at their endstops",
    )

    home_gantry = commands.add_parser(
        "home-gantry",
        help="run Marlin G28 X Y Z, verify the result, and save a homing record",
    )
    home_gantry.add_argument(
        "--record",
        default="data/gantry_home.json",
        help="JSON homing record path (default: data/gantry_home.json)",
    )
    home_gantry.add_argument(
        "--confirm-motion", action="store_true", help="required before homing movement"
    )
    home_gantry.add_argument(
        "--confirm-clear-path",
        action="store_true",
        help="required confirmation that all three paths to the switches are clear",
    )

    web = commands.add_parser("web", help="launch the browser controller")
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default: local only)",
    )
    web.add_argument(
        "--web-port", type=int, default=8000, help="browser port (default: 8000)"
    )
    web.add_argument(
        "--no-browser", action="store_true", help="do not open a browser automatically"
    )
    web.add_argument(
        "--demo", action="store_true", help="run with a simulated Marlin controller"
    )
    web.add_argument(
        "--allow-network",
        action="store_true",
        help="allow an unauthenticated non-loopback bind; every reachable user can control the gantry",
    )
    web.add_argument(
        "--require-clerk",
        action="store_true",
        help="require Clerk sign-in using $CLERK_PUBLISHABLE_KEY",
    )

    console = commands.add_parser(
        "debug-console",
        help="serve the Django raw G-code debug console for this host's serial link",
    )
    console.add_argument(
        "--host", default="127.0.0.1", help="bind address (default: local only)"
    )
    console.add_argument(
        "--console-port", type=int, default=8300, help="console port (default: 8300)"
    )
    console.add_argument(
        "--token",
        help=f"shared access token; defaults to ${TOKEN_ENVIRONMENT_KEY} or a generated one",
    )
    console.add_argument(
        "--command-timeout",
        type=float,
        help="default acknowledgement timeout in seconds for a raw command",
    )
    console.add_argument(
        "--no-browser", action="store_true", help="do not open a browser automatically"
    )
    console.add_argument(
        "--demo", action="store_true", help="run with a simulated Marlin controller"
    )
    console.add_argument(
        "--allow-network",
        action="store_true",
        help="allow a non-loopback bind so other machines can send raw G-code",
    )

    home = commands.add_parser(
        "home",
        help="run configured homing for outer X/Y and inner Z gantry axes",
    )
    home.add_argument(
        "--confirm-motion", action="store_true", help="required before physical motion"
    )

    motor_test = commands.add_parser(
        "motor-test",
        help="print outer X/Y plus inner Z sample G-code; add --confirm-motion to send it to Marlin",
    )
    motor_test.add_argument(
        "--confirm-motion",
        action="store_true",
        help="home and send the displayed test program to physical hardware",
    )
    motor_test.add_argument(
        "--distance-mm",
        type=float,
        default=20.0,
        help="distance to move each axis before returning (default: 20)",
    )
    motor_test.add_argument(
        "--feed-mm-min",
        type=float,
        default=600.0,
        help="test movement feed rate in mm/min (default: 600)",
    )
    motor_test.add_argument(
        "--magnet-on",
        action="store_true",
        help="pick up at the origin, hold during the outbound move, and release before returning",
    )
    motor_test.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required for physical motor tests with the electromagnet energized",
    )
    motor_test.add_argument(
        "--presentation-loops",
        type=int,
        default=0,
        help="repeat the four-leg path while continuously refreshing full magnet power",
    )
    motor_test.add_argument(
        "--demo",
        action="store_true",
        help="simulate Marlin instead of opening the serial port",
    )

    piece_demo = commands.add_parser(
        "piece-demo",
        help="reference all three endstops, pick up one piece, move it, release it, and return",
    )
    piece_demo.add_argument(
        "--distance-mm",
        type=float,
        default=20.0,
        help="inner and outer movement distance (default: 20)",
    )
    piece_demo.add_argument(
        "--feed-mm-min",
        type=float,
        default=1200.0,
        help="movement feed rate (default: 1200)",
    )
    piece_demo.add_argument(
        "--output", "-o", help="write generated piece-demo G-code to this file"
    )
    piece_demo.add_argument(
        "--confirm-motion", action="store_true", help="stream the test to Marlin"
    )
    piece_demo.add_argument(
        "--confirm-at-switches",
        action="store_true",
        help="required confirmation that X/Y/Z endstops are simultaneously held",
    )
    piece_demo.add_argument(
        "--confirm-piece",
        action="store_true",
        help="required confirmation that one piece is under the magnet and the path is clear",
    )
    piece_demo.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required confirmation that the magnet driver is safe to energize",
    )
    piece_demo.add_argument(
        "--demo", action="store_true", help="simulate Marlin without opening serial"
    )

    magnet_test = commands.add_parser(
        "magnet-test",
        help="pulse the configured electromagnet output; dry-run unless motion is confirmed",
    )
    magnet_test.add_argument(
        "--duration-s",
        type=float,
        default=1.0,
        help="energized duration in seconds, maximum 5 (default: 1)",
    )
    magnet_test.add_argument(
        "--confirm-motion",
        action="store_true",
        help="required before energizing the physical electromagnet",
    )
    magnet_test.add_argument(
        "--demo",
        action="store_true",
        help="simulate Marlin instead of opening the serial port",
    )

    circle_demo = commands.add_parser(
        "circle-demo",
        help="home, energize the magnet, and trace a circular gantry path",
    )
    circle_demo.add_argument(
        "--diameter-mm",
        type=float,
        default=200.0,
        help="circle diameter in millimetres (default: 200)",
    )
    circle_demo.add_argument(
        "--feed-mm-min",
        type=float,
        default=1800.0,
        help="circle feed rate in mm/min (default: 1800)",
    )
    circle_demo.add_argument(
        "--segments",
        type=int,
        default=72,
        help="linear segments used for the circle (default: 72)",
    )
    circle_demo.add_argument(
        "--output", "-o", help="write generated circle G-code to this file"
    )
    circle_demo.add_argument(
        "--confirm-motion", action="store_true", help="stream the test to Marlin"
    )
    circle_demo.add_argument(
        "--confirm-clear-workspace",
        action="store_true",
        help="required confirmation that the full 200 mm circle path is clear",
    )
    circle_demo.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required confirmation that the magnet driver is safe to energize",
    )
    circle_demo.add_argument(
        "--demo", action="store_true", help="simulate Marlin without opening serial"
    )

    perimeter_demo = commands.add_parser(
        "perimeter-demo",
        help="home and trace the configured rectangular workspace perimeter",
    )
    perimeter_demo.add_argument(
        "--width-mm",
        type=float,
        default=330.0,
        help="inner-gantry width from the home corner (default: 330)",
    )
    perimeter_demo.add_argument(
        "--height-mm",
        type=float,
        default=300.0,
        help="outer-gantry height from the home corner (default: 300)",
    )
    perimeter_demo.add_argument(
        "--feed-mm-min",
        type=float,
        default=1800.0,
        help="perimeter feed rate in mm/min (default: 1800)",
    )
    perimeter_demo.add_argument(
        "--magnet-on",
        action="store_true",
        help="energize the magnet while tracing the four perimeter edges",
    )
    perimeter_demo.add_argument(
        "--output", "-o", help="write generated perimeter G-code to this file"
    )
    perimeter_demo.add_argument(
        "--confirm-motion", action="store_true", help="stream the test to Marlin"
    )
    perimeter_demo.add_argument(
        "--confirm-clear-workspace",
        action="store_true",
        help="required confirmation that the complete rectangular path is clear",
    )
    perimeter_demo.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required for a physical perimeter with the magnet energized",
    )
    perimeter_demo.add_argument(
        "--demo", action="store_true", help="simulate Marlin without opening serial"
    )

    square_center_demo = commands.add_parser(
        "square-center-demo",
        help="home and visit all 64 measured chess-square centers",
    )
    square_center_demo.add_argument(
        "--feed-mm-min",
        type=float,
        default=1800.0,
        help="movement feed (default: 1800)",
    )
    square_center_demo.add_argument(
        "--dwell-ms", type=int, default=150, help="pause at each center (default: 150)"
    )
    square_center_demo.add_argument(
        "--magnet-on",
        action="store_true",
        help="keep the magnet on through all 64 centers",
    )
    square_center_demo.add_argument(
        "--output", "-o", help="write generated square-center G-code"
    )
    square_center_demo.add_argument(
        "--confirm-motion", action="store_true", help="stream the test to Marlin"
    )
    square_center_demo.add_argument(
        "--confirm-clear-workspace",
        action="store_true",
        help="required confirmation that the board path is clear",
    )
    square_center_demo.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required for physical continuous-magnet traversal",
    )
    square_center_demo.add_argument(
        "--demo", action="store_true", help="simulate Marlin without serial"
    )

    board_sweep = commands.add_parser(
        "board-sweep",
        help="visit every board square in a serpentine path; dry-run by default",
    )
    board_sweep.add_argument(
        "--feed-mm-min",
        type=float,
        default=1800.0,
        help="sweep feed rate in mm/min (default: 1800)",
    )
    board_sweep.add_argument(
        "--magnet-on",
        action="store_true",
        help="pulse the configured electromagnet at every square",
    )
    board_sweep.add_argument(
        "--output", "-o", help="write the generated sweep G-code to this file"
    )
    board_sweep.add_argument(
        "--confirm-motion",
        action="store_true",
        help="send the sweep to Marlin instead of printing a dry run",
    )
    board_sweep.add_argument(
        "--confirm-empty-board",
        action="store_true",
        help="required for physical execution after removing all pieces and obstructions",
    )
    board_sweep.add_argument(
        "--confirm-origin",
        action="store_true",
        help="required confirmation that the gantry is positioned at the configured coordinate origin",
    )
    board_sweep.add_argument(
        "--confirm-magnet",
        action="store_true",
        help="required for a physical sweep with the electromagnet energized",
    )
    board_sweep.add_argument(
        "--demo",
        action="store_true",
        help="simulate acknowledged Marlin streaming without opening a serial port",
    )

    workspace_test = commands.add_parser(
        "workspace-test",
        help="traverse a serpentine grid across the configured workspace with the magnet off",
    )
    workspace_test.add_argument(
        "--feed-mm-min",
        type=float,
        default=1200.0,
        help="movement feed (default: 1200)",
    )
    workspace_test.add_argument(
        "--margin-mm", type=float, default=20.0, help="edge margin (default: 20)"
    )
    workspace_test.add_argument(
        "--columns", type=int, default=8, help="grid columns (default: 8)"
    )
    workspace_test.add_argument(
        "--rows", type=int, default=8, help="grid rows (default: 8)"
    )
    workspace_test.add_argument(
        "--dwell-ms", type=int, default=100, help="pause at each point (default: 100)"
    )
    workspace_test.add_argument(
        "--output", "-o", help="write generated workspace G-code to this file"
    )
    workspace_test.add_argument(
        "--confirm-motion", action="store_true", help="stream the test to Marlin"
    )
    workspace_test.add_argument(
        "--confirm-empty-workspace",
        action="store_true",
        help="required confirmation that the complete configured workspace is clear",
    )
    workspace_test.add_argument(
        "--confirm-at-switches",
        action="store_true",
        help="required confirmation that X/Y/Z endstops are simultaneously held",
    )
    workspace_test.add_argument(
        "--demo", action="store_true", help="simulate Marlin without opening serial"
    )

    commands.add_parser(
        "stop", help="send the configured Marlin emergency-stop command immediately"
    )

    reconcile = commands.add_parser(
        "reconcile",
        help="resolve a pending move after physically checking where the pieces ended up",
    )
    action = reconcile.add_mutually_exclusive_group()
    action.add_argument(
        "--mark-applied",
        action="store_true",
        help="commit the journal's expected next state",
    )
    action.add_argument(
        "--discard",
        action="store_true",
        help="keep current state and remove the journal",
    )
    reconcile.add_argument(
        "--confirm-physical-state",
        action="store_true",
        help="required for --mark-applied or --discard",
    )
    return parser


def _load_move(path: Path, config: AppConfig) -> MoveDelta:
    raw = read_json(path)
    return MoveDelta.from_mapping(raw, config.board.width, config.board.height)


def _service(args: Namespace, config: AppConfig) -> GantryService:
    return GantryService(config, args.state, args.journal, args.audit)


def _print_json(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


_COMMANDS = frozenset(
    {
        "plan",
        "validate",
        "execute",
        "run",
        "init-state",
        "show-state",
        "reset-state",
        "uci-to-json",
        "lichess-pgn",
        "lichess-follow",
        "lichess-mirror",
        "ports",
        "diagnose",
        "endstop-watch",
        "vision-test",
        "reference-gantry",
        "home-gantry",
        "web",
        "home",
        "motor-test",
        "piece-demo",
        "magnet-test",
        "circle-demo",
        "perimeter-demo",
        "square-center-demo",
        "board-sweep",
        "workspace-test",
        "stop",
        "reconcile",
    }
)


_OPTION_VALUE_FLAGS = frozenset(
    {
        "--config",
        "--state",
        "--journal",
        "--audit",
        "--port",
        "--output",
        "-o",
        "--baudrate",
    }
)


def _normalize_argv(argv: Optional[Sequence[str]]) -> Optional[Sequence[str]]:
    if argv is None:
        return None
    normalized = list(argv)
    if any(token in _COMMANDS for token in normalized):
        return normalized
    skip_next = False
    for index, token in enumerate(normalized):
        if skip_next:
            skip_next = False
            continue
        if token in _OPTION_VALUE_FLAGS:
            skip_next = True
            continue
        if token.endswith(".json") and not token.startswith("-"):
            return [*normalized[:index], "run", *normalized[index:]]
    return normalized


def run(argv: Optional[Sequence[str]] = None) -> int:
    load_local_environment()
    parser = _parser()
    args = parser.parse_args(_normalize_argv(argv))

    try:
        if args.command == "ports":
            ports = discover_serial_ports()
            if not ports:
                print("No serial ports found.")
            for item in ports:
                marker = "likely-printer" if item.likely_printer else "serial"
                print(f"{item.device}\t{marker}\t{item.description}\t{item.hwid}")
            return 0

        config = AppConfig.load(args.config)

        if args.command == "diagnose":
            settings = config.serial
            if args.port:
                settings = replace(settings, port=args.port)
            if args.baudrate is not None:
                if args.baudrate <= 0:
                    parser.error("--baudrate must be positive")
                settings = replace(
                    settings,
                    baudrate=args.baudrate,
                    fallback_baudrates=(),
                )
            with MarlinSerial(settings) as link:
                info = link.connection_info
                assert info is not None
                _print_json({"connection": info.as_dict()})
                for command in ("M119", "M114"):
                    result = link.send_command(command, timeout_s=10.0)
                    print(f"\n> {command}")
                    for line in result.responses:
                        print(line)
            return 0

        if args.command == "endstop-watch":
            if args.interval <= 0:
                parser.error("--interval must be positive")
            if args.samples < 0:
                parser.error("--samples cannot be negative")
            settings = config.serial
            if args.port:
                settings = replace(settings, port=args.port)
            if args.baudrate is not None:
                if args.baudrate <= 0:
                    parser.error("--baudrate must be positive")
                settings = replace(
                    settings,
                    baudrate=args.baudrate,
                    fallback_baudrates=(),
                )
            link_type = DemoMarlinSerial if args.demo else MarlinSerial
            previous: dict[str, bool] = {}
            sample = 0
            with link_type(settings) as link:
                print(
                    f"Watching endstops on {link.active_port} at "
                    f"{link.active_baudrate} baud; press Ctrl+C to stop.",
                    flush=True,
                )
                while args.samples == 0 or sample < args.samples:
                    result = link.send_command("M119", timeout_s=10.0)
                    current = parse_endstop_states(result.responses)
                    if not current:
                        raise ValidationError(
                            "M119 did not return any parseable endstop states"
                        )
                    for line in endstop_transition_lines(previous, current):
                        print(line, flush=True)
                    previous = current
                    sample += 1
                    if args.samples == 0 or sample < args.samples:
                        sleep(args.interval)
            return 0

        if args.command == "vision-test":
            from .vision import SolTranscriber, encode_jpeg, open_frame_source

            if args.frames < 0:
                parser.error("--frames cannot be negative")
            if args.interval <= 0:
                parser.error("--interval must be positive")
            transcriber = SolTranscriber()
            reader = open_frame_source(args.source)
            frame_index = 0
            try:
                while args.frames == 0 or frame_index < args.frames:
                    frame = reader.read()
                    jpeg = encode_jpeg(frame)
                    result = transcriber.transcribe(jpeg)
                    status = result.model_dump()
                    if args.preview_output:
                        target = Path(args.preview_output)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(jpeg)
                    frame_index += 1
                    _print_json(status)
                    if args.frames == 0 or frame_index < args.frames:
                        sleep(args.interval)
            finally:
                reader.close()
            return 0

        if args.command == "reference-gantry":
            if not args.confirm_at_switches:
                parser.error("reference-gantry requires --confirm-at-switches")
            service = _service(args, config)
            program = service.reference_gantry()
            print("All X/Y/Z endstops are triggered; assigned X=max, Y=0, E=0.")
            print("\n".join(program))
            return 0

        if args.command == "home-gantry":
            if not args.confirm_motion:
                parser.error("home-gantry requires --confirm-motion")
            if not args.confirm_clear_path:
                parser.error("home-gantry requires --confirm-clear-path")
            service = _service(args, config)
            record = service.home_gantry(Path(args.record))
            _print_json(record)
            print(f"Saved gantry homing record to {args.record}")
            return 0

        if args.command == "web":
            from .web_app import run_web_server

            run_web_server(
                config=config,
                state_path=args.state,
                journal_path=args.journal,
                audit_path=args.audit,
                host=args.host,
                port=args.web_port,
                open_browser=not args.no_browser,
                demo=args.demo,
                allow_network=args.allow_network,
                require_clerk=args.require_clerk,
            )
            return 0

        if args.command == "debug-console":
            from .debug_console.server import run_debug_console

            run_debug_console(
                config=config,
                state_path=args.state,
                journal_path=args.journal,
                audit_path=args.audit,
                host=args.host,
                port=args.console_port,
                token=args.token,
                open_browser=not args.no_browser,
                demo=args.demo,
                allow_network=args.allow_network,
                default_timeout_s=args.command_timeout,
            )
            return 0

        if args.command == "lichess-follow" and args.obstacle_keepout_mm is not None:
            if args.execute:
                parser.error(
                    "--obstacle-keepout-mm is dry-run-only and cannot be combined with --execute"
                )
            if args.obstacle_keepout_mm <= 0:
                parser.error("--obstacle-keepout-mm must be positive")
            config = replace(
                config,
                planner=replace(
                    config.planner, obstacle_keepout_mm=args.obstacle_keepout_mm
                ),
            )
            print(
                f"Dry-run planner override: obstacle_keepout_mm={args.obstacle_keepout_mm:g}; "
                "config.json was not changed."
            )

        service = _service(args, config)

        if args.command == "init-state":
            source = BoardState.from_mapping(
                read_json(Path(args.source_json)),
                config.board.width,
                config.board.height,
            )
            service.store.initialize(source, overwrite=args.overwrite)
            print(
                f"Installed board state at {service.store.path} (revision {source.revision})."
            )
            return 0

        if args.command == "show-state":
            _print_json(service.store.load().to_dict())
            return 0

        if args.command == "reset-state":
            if not args.confirm_standard_position:
                parser.error(
                    "reset-state requires --confirm-standard-position after arranging the physical board"
                )
            state = service.reset_state()
            print(
                "Board state reset to the standard starting position at "
                f"revision {state.revision}; pending journal cleared."
            )
            return 0

        if args.command == "uci-to-json":
            move = uci_to_move(
                args.uci_move,
                service.store.load(),
                event_id=args.event_id,
                en_passant=args.en_passant,
                width=config.board.width,
                height=config.board.height,
            )
            payload = move.to_dict()
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="ascii",
                )
                print(f"Wrote move JSON to {output}.")
            else:
                _print_json(payload)
            return 0

        if args.command == "lichess-pgn":
            if service.journal.exists():
                raise PendingTransactionError(
                    f"pending transaction exists at {service.journal.path}; reconcile it first"
                )
            output_dir = Path(args.output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            pgn = fetch_pgn(args.game_id)
            state = service.store.load()
            count = 0
            for move in pgn_moves(args.game_id, pgn, state):
                plan = service.plan(move, state)
                state = plan.next_state
                (output_dir / f"{move.event_id}.json").write_text(
                    json.dumps(move.to_dict(), indent=2, sort_keys=True) + "\n",
                    encoding="ascii",
                )
                (output_dir / f"{move.event_id}.gcode").write_text(
                    plan.program.text(), encoding="ascii"
                )
                count += 1
            print(f"Dry-run planned {count} Lichess move(s) in {output_dir}.")
            return 0

        if args.command == "lichess-follow":
            if args.execute and not args.confirm_motion:
                parser.error("lichess-follow --execute requires --confirm-motion")
            if args.execute_existing and not args.execute:
                parser.error("lichess-follow --execute-existing requires --execute")
            output_dir = Path(args.output_dir)
            session_path = (
                Path(args.session)
                if args.session
                else output_dir / f"{args.game_id}.session.json"
            )
            follow_game(
                service,
                args.game_id,
                output_dir,
                session_path,
                interval_s=args.interval,
                execute=args.execute,
                execute_existing=args.execute_existing,
                reset_session=args.reset_session,
                once=args.once,
            )
            return 0

        if args.command == "lichess-mirror":
            from .lichess_mirror import LichessMirror, MirrorTerminal

            if args.execute and not args.demo:
                if not args.confirm_motion:
                    parser.error("physical lichess-mirror requires --confirm-motion")
                if not args.confirm_standard_position:
                    parser.error(
                        "physical lichess-mirror requires --confirm-standard-position"
                    )
                if not args.confirm_clear_path:
                    parser.error(
                        "physical lichess-mirror requires --confirm-clear-path"
                    )
            if (
                not args.configured_speed
                and args.execute
                and not args.confirm_high_speed
            ):
                parser.error("fast lichess-mirror requires --confirm-high-speed")
            mirror_session = LichessMirror(
                game_id=args.game_id,
                config=config,
                root=Path.cwd(),
                execute=args.execute,
                demo=args.demo,
                fast=not args.configured_speed,
                stream_mode=args.stream_mode,
                terminal=MirrorTerminal(
                    screen=not args.no_screen, unicode=args.unicode
                ),
            )
            mirror_session.run(once=args.once)
            return 0

        if args.command == "reconcile":
            if not service.journal.exists():
                raise PendingTransactionError("there is no pending transaction")
            if not args.mark_applied and not args.discard:
                _print_json(dict(service.journal.load()))
                return 0
            if not args.confirm_physical_state:
                parser.error(
                    "reconcile changes require --confirm-physical-state after inspecting the board"
                )
            if args.mark_applied:
                state = service.reconcile_mark_applied()
                print(f"Journal applied; board state is now revision {state.revision}.")
            else:
                service.reconcile_discard()
                print("Journal discarded; stored board state was not changed.")
            return 0

        if args.command == "home":
            if not args.confirm_motion:
                parser.error("home requires --confirm-motion")
            service.home()
            print("Homing commands completed.")
            return 0

        if args.command == "motor-test":
            program = service.motor_test_program(
                args.distance_mm,
                args.feed_mm_min,
                args.magnet_on,
                args.presentation_loops,
            )
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
                print("; outer X and Y are coupled; inner Z moves independently")
                print("\n".join(program))
                return 0
            if args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if args.magnet_on and not args.confirm_magnet:
                    parser.error(
                        "physical motor-test --magnet-on requires --confirm-magnet"
                    )
                program = service.motor_test(
                    args.distance_mm,
                    args.feed_mm_min,
                    args.magnet_on,
                    args.presentation_loops,
                )
            print("; outer X/Y and inner Z motor test sent successfully")
            print("\n".join(program))
            return 0

        if args.command == "piece-demo":
            program = service.piece_demo_program(args.distance_mm, args.feed_mm_min)
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_at_switches:
                    parser.error("physical piece-demo requires --confirm-at-switches")
                if not args.confirm_piece:
                    parser.error("physical piece-demo requires --confirm-piece")
                if not args.confirm_magnet:
                    parser.error("physical piece-demo requires --confirm-magnet")
                program = service.piece_demo(args.distance_mm, args.feed_mm_min)
                print("; physical piece demo completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote piece-demo G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "magnet-test":
            program = service.magnet_test_program(args.duration_s)
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
                print("\n".join(program))
                return 0
            if args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                program = service.magnet_test(args.duration_s)
            print("; configured electromagnet test completed successfully")
            print("\n".join(program))
            return 0

        if args.command == "circle-demo":
            program = service.circle_demo_program(
                args.diameter_mm, args.feed_mm_min, args.segments
            )
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_clear_workspace:
                    parser.error(
                        "physical circle-demo requires --confirm-clear-workspace"
                    )
                if not args.confirm_magnet:
                    parser.error("physical circle-demo requires --confirm-magnet")
                program = service.circle_demo(
                    args.diameter_mm, args.feed_mm_min, args.segments
                )
                print("; physical circle demo completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote circle-demo G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "perimeter-demo":
            program = service.perimeter_demo_program(
                args.width_mm, args.height_mm, args.feed_mm_min, args.magnet_on
            )
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_clear_workspace:
                    parser.error(
                        "physical perimeter-demo requires --confirm-clear-workspace"
                    )
                if args.magnet_on and not args.confirm_magnet:
                    parser.error(
                        "physical perimeter-demo --magnet-on requires --confirm-magnet"
                    )
                program = service.perimeter_demo(
                    args.width_mm, args.height_mm, args.feed_mm_min, args.magnet_on
                )
                print("; physical perimeter demo completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote perimeter-demo G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "square-center-demo":
            program = service.square_center_demo_program(
                args.feed_mm_min, args.dwell_ms, args.magnet_on
            )
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_clear_workspace:
                    parser.error(
                        "physical square-center-demo requires --confirm-clear-workspace"
                    )
                if args.magnet_on and not args.confirm_magnet:
                    parser.error(
                        "physical square-center-demo --magnet-on requires --confirm-magnet"
                    )
                program = service.square_center_demo(
                    args.feed_mm_min, args.dwell_ms, args.magnet_on
                )
                print("; physical square-center traversal completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote square-center G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "board-sweep":
            program = service.board_sweep_program(args.feed_mm_min, args.magnet_on)
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(config.safety.preflight_commands)
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_empty_board:
                    parser.error("physical board-sweep requires --confirm-empty-board")
                if not args.confirm_origin:
                    parser.error("physical board-sweep requires --confirm-origin")
                if args.magnet_on and not args.confirm_magnet:
                    parser.error(
                        "physical board-sweep --magnet-on requires --confirm-magnet"
                    )
                program = service.board_sweep(args.feed_mm_min, args.magnet_on)
                print("; physical board sweep completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote board sweep G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "workspace-test":
            program = service.workspace_test_program(
                args.feed_mm_min,
                args.margin_mm,
                args.columns,
                args.rows,
                args.dwell_ms,
            )
            if not args.confirm_motion:
                print("; DRY RUN ONLY: no serial port was opened")
            elif args.demo:
                link = DemoMarlinSerial(config.serial)
                with link:
                    link.send_program(program)
                print("; DEMO ONLY: no serial port was opened")
            else:
                if not args.confirm_empty_workspace:
                    parser.error(
                        "physical workspace-test requires --confirm-empty-workspace"
                    )
                if not args.confirm_at_switches:
                    parser.error(
                        "physical workspace-test requires --confirm-at-switches"
                    )
                program = service.workspace_test(
                    args.feed_mm_min,
                    args.margin_mm,
                    args.columns,
                    args.rows,
                    args.dwell_ms,
                )
                print("; physical workspace test completed successfully")
            text = "\n".join(program) + "\n"
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="ascii")
                print(f"; wrote workspace test G-code to {output}")
            else:
                print(text, end="")
            return 0

        if args.command == "stop":
            service.emergency_stop()
            print(
                "Emergency-stop command sent. Marlin normally requires a reset before further operation."
            )
            return 0

        move = _load_move(Path(args.move_json), config)

        if args.command in {"plan", "validate"}:
            if service.journal.exists():
                raise PendingTransactionError(
                    f"a pending transaction exists at {service.journal.path}; state may not match the physical board"
                )
            with service.store.locked():
                plan = service.plan(move, service.store.load())
            if args.command == "validate":
                _print_json(plan.summary())
                return 0
            if args.summary_json:
                _print_json(plan.summary())
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(plan.program.text(), encoding="ascii")
                print(f"Wrote {len(plan.program.commands)} commands to {output}.")
            else:
                print(plan.program.text(), end="")
            return 0

        if args.command == "execute":
            if not args.confirm_motion:
                parser.error("execute requires --confirm-motion")
            plan = service.execute(move)
            _print_json(plan.summary())
            return 0

        if args.command == "run":
            if service.journal.exists():
                raise PendingTransactionError(
                    f"a pending transaction exists at {service.journal.path}; state may not match the physical board"
                )
            if not args.confirm_motion:
                with service.store.locked():
                    plan = service.plan(move, service.store.load())
                if args.summary_json:
                    _print_json(plan.summary())
                    return 0
                if args.output:
                    output = Path(args.output)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(plan.program.text(), encoding="ascii")
                    print(
                        f"Wrote {len(plan.program.commands)} commands to {output}.",
                        file=sys.stderr,
                    )
                else:
                    print(plan.program.text(), end="")
                return 0
            plan = service.execute(move)
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(plan.program.text(), encoding="ascii")
                print(
                    f"Wrote {len(plan.program.commands)} commands to {output}.",
                    file=sys.stderr,
                )
            if args.summary_json:
                _print_json(plan.summary())
            else:
                print(
                    f"Move completed: {plan.move.piece_id} "
                    f"({plan.move.previous.x},{plan.move.previous.y}) -> "
                    f"({plan.move.new.x},{plan.move.new.y}); "
                    f"board revision {plan.next_state.revision}.",
                    file=sys.stderr,
                )
            return 0

        parser.error(f"unsupported command {args.command}")
        return 2
    except GantryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def main() -> None:
    raise SystemExit(run())
