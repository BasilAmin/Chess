from __future__ import annotations

from pathlib import Path
from typing import Any
import argparse
import json
import time

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.models import BoardState, MoveDelta
from chess_gantry.persistence import atomic_write_json, read_json
from chess_gantry.serial_link import DemoMarlinSerial, MarlinSerial
from chess_gantry.service import GantryService


CONFIRMATION = "ENDURANCE BOARD AND PATH CLEAR"


def endurance_move(index: int) -> MoveDelta:
    setup = (
        ("white_pawn_a", 0, 1, 0, 2),
        ("black_pawn_a", 0, 6, 0, 5),
        ("white_rook_a", 0, 0, 0, 1),
        ("black_rook_a", 0, 7, 0, 6),
    )
    cycle = (
        ("white_rook_a", 0, 1, 0, 0),
        ("black_rook_a", 0, 6, 0, 7),
        ("white_rook_a", 0, 0, 0, 1),
        ("black_rook_a", 0, 7, 0, 6),
    )
    piece, px, py, nx, ny = setup[index] if index < 4 else cycle[(index - 4) % 4]
    return MoveDelta.from_mapping(
        {
            "position": piece,
            "px": px,
            "py": py,
            "nx": nx,
            "ny": ny,
            "event_id": f"endurance.{index + 1}",
        }
    )


def run(args: argparse.Namespace) -> None:
    if args.transfers < 4:
        raise ValidationError("endurance replay requires at least 4 transfers")
    if args.move_delay < 0:
        raise ValidationError("move delay cannot be negative")
    if args.execute and args.confirmation != CONFIRMATION:
        raise ValidationError(f"physical endurance replay requires: {CONFIRMATION}")
    config = AppConfig.from_mapping(json.loads(args.config.read_text()))
    mode = "demo" if args.demo else "physical" if args.execute else "simulation"
    directory = Path.cwd() / "data" / "endurance-replay" / mode
    state_path = directory / "board_state.json"
    journal_path = directory / "pending_move.json"
    audit_path = directory / "audit.jsonl"
    cursor_path = directory / "cursor.json"
    directory.mkdir(parents=True, exist_ok=True)
    if args.reset_session:
        if journal_path.exists():
            raise ConfigurationError("reconcile the pending endurance transfer first")
        for path in (state_path, audit_path, cursor_path):
            path.unlink(missing_ok=True)
    if not state_path.exists():
        atomic_write_json(state_path, BoardState.standard().to_dict())
    completed = 0
    if cursor_path.exists():
        cursor = read_json(cursor_path)
        if cursor.get("total") != args.transfers:
            raise ConfigurationError(
                "saved transfer total differs; reset from the standard position"
            )
        completed = int(cursor.get("completed", 0))
    service = GantryService(config, state_path, journal_path, audit_path)
    link: Any = None
    if args.execute or args.demo:
        link_type = DemoMarlinSerial if args.demo else MarlinSerial
        link = link_type(config.serial)
        link.connect()
        service.home_with_link(link)
    try:
        for index in range(completed, args.transfers):
            move = endurance_move(index)
            if link is None:
                service.store.save(service.plan(move).next_state)
            else:
                service.execute_with_link(move, link)
            atomic_write_json(
                cursor_path,
                {
                    "schema_version": 1,
                    "completed": index + 1,
                    "total": args.transfers,
                },
            )
            if (index + 1) % 100 == 0 or index + 1 == args.transfers:
                print(f"endurance transfer {index + 1}/{args.transfers}", flush=True)
            if args.move_delay and index + 1 < args.transfers:
                time.sleep(args.move_delay)
    finally:
        if link is not None:
            link.best_effort((*config.magnet.off_commands, "M211 S1"))
            link.close()


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--config", type=Path, default=Path("config.json"))
    value.add_argument("--transfers", type=int, default=20000)
    value.add_argument("--move-delay", type=float, default=0.5)
    value.add_argument("--reset-session", action="store_true")
    mode = value.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--demo", action="store_true")
    value.add_argument("--confirmation")
    return value


if __name__ == "__main__":
    run(parser().parse_args())
