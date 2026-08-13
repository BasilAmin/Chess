from __future__ import annotations

from dataclasses import replace
from io import BytesIO, StringIO
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock, Thread
from typing import Any, Optional
import time
import uuid

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_mirror import LichessMirror, MirrorCursor, MirrorTerminal
from .persistence import atomic_write_json
from .serial_link import DemoMarlinSerial, MarlinSerial


STATION_CONFIRMATION = "r/shitter"


def qr_svg(value: str) -> bytes:
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(
        value,
        image_factory=qrcode.image.svg.SvgPathImage,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=3,
    )
    output = BytesIO()
    image.save(output)
    return output.getvalue()


class StationGame:
    def __init__(self, root: Path, config: AppConfig, *, demo: bool) -> None:
        self.root = root
        self.config = config
        self.demo = demo
        self._lock = RLock()
        self._motion_lock = RLock()
        self._mirror: Optional[LichessMirror] = None
        self._cursor: Optional[MirrorCursor] = None
        self._board: Any = None
        self._link: Any = None
        self._game_id: Optional[str] = None
        self._tokens: dict[str, str] = {}
        self._joined = {"white": False, "black": False}
        self._state = "idle"
        self._error: Optional[str] = None
        self._result: Optional[str] = None
        self._started_at: Optional[float] = None
        self._history: list[dict[str, Any]] = []
        self._base_url: Optional[str] = None

    def active(self) -> bool:
        with self._lock:
            return self._state not in {"idle", "finished", "stopped", "failed"}

    def create(self, *, base_url: str, confirmation: str) -> dict[str, Any]:
        if confirmation != STATION_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_CONFIRMATION}")
        if not base_url.startswith(("http://", "https://")):
            raise ValidationError("station base URL must be HTTP or HTTPS")
        with self._lock:
            if self.active():
                raise ConfigurationError("a station game is already active")
            game_id = "st" + uuid.uuid4().hex[:14]
            tokens = {"white": token_urlsafe(32), "black": token_urlsafe(32)}
            directory = self.root / "data" / "station-games" / game_id
            empty_pgn = '[Event "Station"]\n[Result "*"]\n\n*\n'
            mirror = LichessMirror(
                game_id=game_id,
                config=self.config,
                root=self.root,
                execute=True,
                demo=self.demo,
                fast=True,
                stream_mode="public",
                terminal=MirrorTerminal(StringIO(), screen=False),
                client=object(),
                pgn_fetcher=lambda *args, **kwargs: empty_pgn,
                directory=directory,
            )
            cursor = mirror._initialize()
            board = mirror._board(cursor)
            self._mirror = mirror
            self._cursor = cursor
            self._board = board
            self._game_id = game_id
            self._tokens = tokens
            self._joined = {"white": False, "black": False}
            self._state = "waiting_players"
            self._error = None
            self._result = None
            self._started_at = time.time()
            self._history = []
            self._base_url = base_url.rstrip("/")
            atomic_write_json(
                directory / "station.json",
                {
                    "schema_version": 1,
                    "game_id": game_id,
                    "created_at": self._started_at,
                    "token_sha256": {
                        color: sha256(token.encode()).hexdigest()
                        for color, token in tokens.items()
                    },
                },
            )
            return {
                **self.admin_status(base_url=self._base_url),
                "join_urls": {
                    color: f"{self._base_url}/station/play#{token}"
                    for color, token in tokens.items()
                },
            }

    def _seat(self, token: str) -> str:
        for color, expected in self._tokens.items():
            if token and token == expected:
                return color
        raise ValidationError("invalid station seat token")

    def join(self, token: str) -> dict[str, Any]:
        with self._lock:
            color = self._seat(token)
            if self._state in {"stopped", "failed"}:
                raise ConfigurationError("this station game is not available")
            self._joined[color] = True
            if all(self._joined.values()) and self._state == "waiting_players":
                self._state = "homing"
                Thread(target=self._start_motion, daemon=True).start()
            return self.player_status(token)

    def _start_motion(self) -> None:
        try:
            mirror = self._require_mirror()
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            link = link_type(mirror.config.serial)
            link.connect()
            mirror.service.home_with_link(link)
            with self._lock:
                if self._state != "homing":
                    link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                    link.close()
                    return
                self._link = link
                self._state = "playing"
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                self._state = "failed"
            self._close_link()

    def _require_mirror(self) -> LichessMirror:
        if self._mirror is None:
            raise ConfigurationError("create a station game first")
        return self._mirror

    def _rows(self) -> list[str]:
        import chess

        if self._board is None:
            return []
        return [
            "".join(
                self._board.piece_at(chess.square(file_index, rank)).symbol()
                if self._board.piece_at(chess.square(file_index, rank))
                else "."
                for file_index in range(8)
            )
            for rank in range(7, -1, -1)
        ]

    def _legal_moves(self, color: str) -> list[str]:
        if self._board is None or self._state != "playing":
            return []
        turn = "white" if self._board.turn else "black"
        if color != turn:
            return []
        return [move.uci() for move in self._board.legal_moves]

    def player_status(self, token: str) -> dict[str, Any]:
        with self._lock:
            color = self._seat(token)
            turn = (
                "white" if self._board is not None and self._board.turn else "black"
            )
            return {
                "game_id": self._game_id,
                "seat": color,
                "state": self._state,
                "joined": dict(self._joined),
                "turn": turn,
                "your_turn": self._state == "playing" and color == turn,
                "rows": self._rows(),
                "legal_moves": self._legal_moves(color),
                "history": list(self._history),
                "result": self._result,
                "error": self._error,
                "clock": None,
                "expires_at": None,
            }

    def admin_status(self, *, base_url: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            value = {
                "game_id": self._game_id,
                "state": self._state,
                "joined": dict(self._joined),
                "turn": (
                    "white" if self._board is not None and self._board.turn else "black"
                ),
                "rows": self._rows(),
                "history": list(self._history),
                "result": self._result,
                "error": self._error,
                "started_at": self._started_at,
                "clock": None,
                "expires_at": None,
            }
            resolved_base = base_url or self._base_url
            if resolved_base and self._tokens:
                value["join_urls"] = {
                    color: f"{resolved_base.rstrip('/')}/station/play#{token}"
                    for color, token in self._tokens.items()
                }
            return value

    def move(self, token: str, uci: str) -> dict[str, Any]:
        import chess

        color = self._seat(token)
        with self._motion_lock:
            with self._lock:
                if self._state != "playing":
                    raise ConfigurationError("station game is not ready for moves")
                assert self._board is not None
                turn = "white" if self._board.turn else "black"
                if color != turn:
                    raise ValidationError("it is not your turn")
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError as exc:
                    raise ValidationError("move must be legal UCI") from exc
                if move not in self._board.legal_moves:
                    raise ValidationError(f"illegal move {uci}")
                san = self._board.san(move)
                self._state = "executing"
            try:
                mirror = self._require_mirror()
                if self._link is None:
                    raise ConfigurationError("station Marlin connection is unavailable")
                mirror.link = self._link
                cursor = mirror._execute_ply(self._cursor, uci)
                board = mirror._board(cursor)
                with self._lock:
                    self._cursor = cursor
                    self._board = board
                    self._history.append(
                        {
                            "ply": len(cursor.moves),
                            "uci": uci,
                            "san": san,
                            "color": color,
                        }
                    )
                    outcome = board.outcome(claim_draw=True)
                    if outcome is not None:
                        self._result = outcome.result()
                        self._state = "finished"
                    else:
                        self._state = "playing"
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)
                    self._state = "failed"
                self._close_link()
                raise
            if self._state == "finished":
                self._close_link()
            return self.player_status(token)

    def resign(self, token: str) -> dict[str, Any]:
        with self._lock:
            color = self._seat(token)
            if self._state not in {"playing", "homing", "waiting_players"}:
                raise ConfigurationError("station game cannot be resigned")
            self._result = "0-1" if color == "white" else "1-0"
            self._state = "finished"
        self._close_link()
        return self.player_status(token)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "idle":
                raise ConfigurationError("there is no station game")
            self._state = "stopped"
        self._close_link()
        return self.admin_status()

    def _close_link(self) -> None:
        with self._lock:
            link = self._link
            self._link = None
        if link is not None:
            link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
            link.close()
