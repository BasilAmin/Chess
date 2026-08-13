from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Optional
import json
import re
import time

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_mirror import LichessMirror, MirrorCursor, MirrorTerminal
from .serial_link import DemoMarlinSerial, MarlinSerial
from .persistence import atomic_write_json, read_json


FRESH_CONFIRMATION = "REPLAY BOARD AND CHUTES READY"
RESUME_CONFIRMATION = "REPLAY BOARD MATCHES SAVED STATE"
RECOVERY_CONFIRMATION = "REPLAY PHYSICAL STATE VERIFIED"
_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}


@dataclass(frozen=True)
class ReplayGame:
    replay_id: str
    title: str
    moves: tuple[str, ...]
    result: str


def load_replay_game(path: Path) -> ReplayGame:
    import chess.pgn

    if not path.is_file():
        raise ValidationError(f"replay PGN does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    terminal = re.search(r"(1-0|0-1|1/2-1/2|\*)\s*$", text)
    if terminal is None:
        raise ValidationError(
            "replay PGN must end with one result token and no trailing content"
        )
    stream = StringIO(text)
    game = chess.pgn.read_game(stream)
    if game is None:
        raise ValidationError(f"replay PGN is empty or unreadable: {path}")
    if game.errors:
        raise ValidationError(f"replay PGN contains parser errors: {game.errors[0]}")
    if chess.pgn.read_game(stream) is not None:
        raise ValidationError("replay accepts exactly one PGN game per file")
    if (
        game.headers.get("Variant", "Standard") != "Standard"
        or game.headers.get("FEN")
        or game.headers.get("SetUp", "0") != "0"
    ):
        raise ConfigurationError(
            "physical replay supports only standard chess from the initial position"
        )
    board = game.board()
    moves = []
    for move in game.mainline_moves():
        if move not in board.legal_moves:
            raise ValidationError(f"replay PGN contains illegal move {move.uci()}")
        moves.append(move.uci())
        board.push(move)
    if not moves:
        raise ValidationError("replay PGN must contain at least one move")
    title = game.headers.get("Event", "Offline replay").strip() or "Offline replay"
    result = game.headers.get("Result", "*")
    if result not in _RESULTS:
        raise ValidationError(f"replay PGN has invalid Result header {result!r}")
    if result != terminal.group(1):
        raise ValidationError(
            "replay PGN Result header does not match the movetext result"
        )
    canonical = " ".join(moves).encode("ascii")
    replay_id = "rp" + sha256(canonical).hexdigest()[:24]
    return ReplayGame(replay_id, title, tuple(moves), result)


class GameReplay:
    def __init__(
        self,
        *,
        source: Path,
        config: AppConfig,
        root: Path,
        execute: bool,
        demo: bool,
        fast: bool,
        move_delay_s: float = 0.0,
        terminal: Optional[MirrorTerminal] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if move_delay_s < 0:
            raise ValidationError("replay move delay must be zero or greater")
        self.game = load_replay_game(source)
        self.source = source
        self.execute = execute
        self.demo = demo
        self.move_delay_s = move_delay_s
        session_kind = "demo" if demo else "physical" if execute else "simulation"
        directory = root / "data" / "chess-replay" / self.game.replay_id / session_kind
        self.metadata_path = directory / "replay.json"
        empty_pgn = '[Event "Replay initialization"]\n[Result "*"]\n\n*\n'
        self.mirror = LichessMirror(
            game_id=self.game.replay_id,
            config=config,
            root=root,
            execute=execute,
            demo=demo,
            fast=fast,
            stream_mode="public",
            terminal=terminal or MirrorTerminal(title="Chess Gantry Game Replay"),
            client=object(),
            pgn_fetcher=lambda *args, **kwargs: empty_pgn,
            sleep=sleep,
            directory=directory,
        )
        self.sleep = sleep

    @property
    def directory(self) -> Path:
        return self.mirror.directory

    def reset(self) -> None:
        if self.mirror.journal_path.exists():
            raise ConfigurationError(
                "cannot reset a replay with a pending transaction; reconcile it first"
            )
        for path in (
            self.mirror.cursor_path,
            self.mirror.state_path,
            self.mirror.journal_path,
            self.mirror.audit_path,
            self.metadata_path,
        ):
            path.unlink(missing_ok=True)

    def has_saved_position(self) -> bool:
        if not self.mirror.cursor_path.exists():
            return False
        cursor = MirrorCursor.load(self.mirror.cursor_path, self.game.replay_id)
        return bool(cursor.moves or cursor.pending_uci)

    def _motion_fingerprint(self) -> str:
        config = self.mirror.config
        value = {
            "board": asdict(config.board),
            "workspace": asdict(config.workspace),
            "motion": asdict(config.motion),
            "magnet": asdict(config.magnet),
            "planner": asdict(config.planner),
            "capture": asdict(config.capture),
            "home_commands": list(config.safety.home_commands),
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return sha256(canonical).hexdigest()

    def validate_session(
        self, *, create: bool = False, allow_motion_mismatch: bool = False
    ) -> None:
        expected = {
            "schema_version": 1,
            "replay_id": self.game.replay_id,
            "moves": list(self.game.moves),
            "motion_config_sha256": self._motion_fingerprint(),
        }
        if self.metadata_path.exists():
            actual = read_json(self.metadata_path)
            identity_matches = (
                actual.get("schema_version") == expected["schema_version"]
                and actual.get("replay_id") == expected["replay_id"]
                and actual.get("moves") == expected["moves"]
            )
            motion_matches = (
                actual.get("motion_config_sha256") == expected["motion_config_sha256"]
            )
            if not identity_matches or (
                not motion_matches and not allow_motion_mismatch
            ):
                raise ConfigurationError(
                    "replay session does not match this move list or motion configuration; "
                    "restore the previous configuration or reset from the standard position"
                )
            return
        if self.mirror.cursor_path.exists() or self.mirror.state_path.exists():
            raise ConfigurationError(
                "replay state exists without its motion-configuration metadata"
            )
        if create:
            atomic_write_json(self.metadata_path, expected)
        else:
            raise ConfigurationError("replay session has not been started")

    def run(self, *, max_plies: Optional[int] = None) -> MirrorCursor:
        if max_plies is not None and max_plies <= 0:
            raise ValidationError("replay max plies must be greater than zero")
        self.validate_session(create=True)
        cursor = self.mirror._initialize()
        if (
            len(cursor.moves) > len(self.game.moves)
            or self.game.moves[: len(cursor.moves)] != cursor.moves
        ):
            raise ConfigurationError(
                "replay cursor diverges from the selected PGN; reset the replay session"
            )
        board = self.mirror._board(cursor)
        self.mirror.terminal.render(
            board,
            game_id=self.game.replay_id,
            state="replay-ready",
            cursor=cursor,
            message=f"{self.game.title} | {self.source}",
        )
        remaining = self.game.moves[len(cursor.moves) :]
        if max_plies is not None:
            remaining = remaining[:max_plies]
        if not remaining:
            if cursor.result != self.game.result:
                cursor = replace(cursor, result=self.game.result)
                cursor.save(self.mirror.cursor_path)
            return cursor
        if self.execute:
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            self.mirror.link = link_type(self.mirror.config.serial)
            self.mirror.link.connect()
            self.mirror.service.home_with_link(self.mirror.link)
        try:
            for index, uci in enumerate(remaining):
                cursor = self.mirror._execute_ply(cursor, uci)
                board = self.mirror._board(cursor)
                completed = len(cursor.moves) == len(self.game.moves)
                if completed:
                    cursor = replace(cursor, result=self.game.result)
                    cursor.save(self.mirror.cursor_path)
                self.mirror.terminal.render(
                    board,
                    game_id=self.game.replay_id,
                    state="completed" if completed else "replaying",
                    cursor=cursor,
                    message=f"{self.game.title} | move {len(cursor.moves)}/{len(self.game.moves)}",
                )
                if index + 1 < len(remaining) and self.move_delay_s:
                    self.sleep(self.move_delay_s)
            return cursor
        finally:
            if self.mirror.link is not None:
                self.mirror.link.best_effort(
                    (*self.mirror.config.magnet.off_commands, "M211 S1")
                )
                self.mirror.link.close()
                self.mirror.link = None
