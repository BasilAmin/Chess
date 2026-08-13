from __future__ import annotations

from dataclasses import asdict, replace
from io import BytesIO, StringIO
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from threading import RLock, Thread
from typing import Any, Optional
import hmac
import time
import uuid
import json

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_mirror import LichessMirror, MirrorCursor, MirrorTerminal
from .persistence import atomic_write_json, read_json
from .serial_link import DemoMarlinSerial, MarlinSerial


STATION_CONFIRMATION = "STATION BOARD AND CHUTES READY"
STATION_RECOVERY_CONFIRMATION = "STATION PHYSICAL STATE VERIFIED"
STATION_RESUME_CONFIRMATION = "STATION BOARD MATCHES SAVED STATE"


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
        self._token_hashes: dict[str, str] = {}
        self._joined = {"white": False, "black": False}
        self._left = {"white": False, "black": False}
        self._state = "idle"
        self._error: Optional[str] = None
        self._result: Optional[str] = None
        self._started_at: Optional[float] = None
        self._history: list[dict[str, Any]] = []
        self._base_url: Optional[str] = None
        self._metadata_path: Optional[Path] = None
        self._draw_offer: Optional[str] = None
        self._generation = 0
        self._restore_latest()

    def _new_mirror(self, game_id: str, directory: Path) -> LichessMirror:
        empty_pgn = '[Event "Station"]\n[Result "*"]\n\n*\n'
        return LichessMirror(
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

    def _motion_fingerprint(self) -> str:
        value = {
            "board": asdict(self.config.board),
            "workspace": asdict(self.config.workspace),
            "motion": asdict(self.config.motion),
            "magnet": asdict(self.config.magnet),
            "planner": asdict(self.config.planner),
            "capture": asdict(self.config.capture),
            "serial": asdict(self.config.serial),
            "safety": asdict(self.config.safety),
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return sha256(canonical).hexdigest()

    def _restore_latest(self) -> None:
        import chess

        metadata_files = sorted(
            self.root.glob("data/station-games/*/station.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for metadata_path in metadata_files:
            value = read_json(metadata_path)
            if value.get("state") in {"finished", "stopped"}:
                continue
            game_id = str(value.get("game_id", ""))
            if not game_id:
                continue
            if value.get("motion_config_sha256") != self._motion_fingerprint():
                self._state = "failed"
                self._error = (
                    "saved station game uses a different motion configuration; "
                    "restore that configuration or stop and reset the physical board"
                )
                self._game_id = game_id
                self._metadata_path = metadata_path
                return
            mirror = self._new_mirror(game_id, metadata_path.parent)
            cursor = MirrorCursor.load(mirror.cursor_path, game_id)
            board = mirror._board(cursor)
            outcome = board.outcome(claim_draw=False)
            history = []
            replay = chess.Board()
            for ply, uci in enumerate(cursor.moves, start=1):
                move = chess.Move.from_uci(uci)
                if move not in replay.legal_moves:
                    raise ConfigurationError(
                        f"station {game_id} cursor contains illegal move {uci}"
                    )
                history.append(
                    {
                        "ply": ply,
                        "uci": uci,
                        "san": replay.san(move),
                        "color": "white" if replay.turn else "black",
                    }
                )
                replay.push(move)
            self._mirror = mirror
            self._cursor = cursor
            self._board = board
            self._game_id = game_id
            self._token_hashes = {
                str(color): str(digest)
                for color, digest in value.get("token_sha256", {}).items()
            }
            self._joined = {
                color: bool(value.get("joined", {}).get(color))
                for color in ("white", "black")
            }
            self._left = {
                color: bool(value.get("left", {}).get(color))
                for color in ("white", "black")
            }
            self._state = (
                "failed"
                if mirror.journal_path.exists() or cursor.pending_uci
                else "resume_required"
            )
            self._error = (
                "interrupted physical move requires reconciliation"
                if self._state == "failed"
                else None
            )
            self._result = value.get("result")
            self._started_at = value.get("created_at")
            self._history = history
            self._base_url = value.get("base_url")
            self._metadata_path = metadata_path
            self._draw_offer = value.get("draw_offer")
            if outcome is not None and not cursor.pending_uci:
                self._result = outcome.result()
                self._state = "finished"
                self._save_metadata()
            return

    def active(self) -> bool:
        with self._lock:
            return self._state not in {"idle", "finished", "stopped"}

    def reserves_hardware(self) -> bool:
        return self.active()

    def create(self, *, base_url: str, confirmation: str) -> dict[str, Any]:
        if confirmation != STATION_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_CONFIRMATION}")
        if not base_url.startswith(("http://", "https://")):
            raise ValidationError("station base URL must be HTTP or HTTPS")
        with self._motion_lock:
            pending = tuple(self.root.glob("data/station-games/*/pending_move.json"))
            if pending:
                raise ConfigurationError(
                    f"pending station transaction at {pending[0]}; reconcile it before creating another game"
                )
            with self._lock:
                if self.active():
                    raise ConfigurationError("a station game is already active")
                game_id = "st" + uuid.uuid4().hex[:14]
                tokens = {"white": token_urlsafe(32), "black": token_urlsafe(32)}
                directory = self.root / "data" / "station-games" / game_id
                mirror = self._new_mirror(game_id, directory)
                cursor = mirror._initialize()
                board = mirror._board(cursor)
                self._mirror = mirror
                self._cursor = cursor
                self._board = board
                self._game_id = game_id
                self._tokens = tokens
                self._token_hashes = {
                    color: sha256(token.encode()).hexdigest()
                    for color, token in tokens.items()
                }
                self._joined = {"white": False, "black": False}
                self._left = {"white": False, "black": False}
                self._state = "waiting_players"
                self._error = None
                self._result = None
                self._started_at = time.time()
                self._history = []
                self._draw_offer = None
                self._base_url = base_url.rstrip("/")
                self._metadata_path = directory / "station.json"
                self._generation += 1
                self._save_metadata()
                return {
                    **self.admin_status(base_url=self._base_url),
                    "join_urls": {
                        color: f"{self._base_url}/station/play#{token}"
                        for color, token in tokens.items()
                    },
                }

    def _seat(self, token: str) -> str:
        digest = sha256(token.encode()).hexdigest() if token else ""
        for color, expected in self._token_hashes.items():
            if digest and hmac.compare_digest(digest, expected):
                return color
        raise ValidationError("invalid station seat token")

    def _save_metadata(self) -> None:
        if self._metadata_path is None:
            return
        atomic_write_json(
            self._metadata_path,
            {
                "schema_version": 1,
                "game_id": self._game_id,
                "created_at": self._started_at,
                "token_sha256": dict(self._token_hashes),
                "joined": dict(self._joined),
                "left": dict(self._left),
                "state": self._state,
                "result": self._result,
                "base_url": self._base_url,
                "draw_offer": self._draw_offer,
                "motion_config_sha256": self._motion_fingerprint(),
            },
        )

    def join(self, token: str) -> dict[str, Any]:
        with self._lock:
            color = self._seat(token)
            if self._state in {"stopped", "failed"}:
                raise ConfigurationError("this station game is not available")
            self._joined[color] = True
            self._left[color] = False
            self._save_metadata()
            if all(self._joined.values()) and self._state == "waiting_players":
                self._state = "homing"
                generation = self._generation
                mirror = self._require_mirror()
                Thread(
                    target=self._start_motion,
                    args=(generation, mirror),
                    daemon=True,
                ).start()
            return self.player_status(token)

    def _current_worker(self, generation: int, mirror: LichessMirror) -> bool:
        return self._generation == generation and self._mirror is mirror

    def _start_motion(self, generation: int, mirror: LichessMirror) -> None:
        try:
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            link = link_type(mirror.config.serial)
            link.connect()
            with self._lock:
                if (
                    not self._current_worker(generation, mirror)
                    or self._state != "homing"
                ):
                    link.close()
                    return
                self._link = link
            mirror.service.home_with_link(link)
            with self._lock:
                if (
                    not self._current_worker(generation, mirror)
                    or self._state != "homing"
                ):
                    link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                    link.close()
                    return
                self._state = "playing"
                self._save_metadata()
        except Exception as exc:
            with self._lock:
                if (
                    self._current_worker(generation, mirror)
                    and self._state != "stopped"
                ):
                    self._error = str(exc)
                    self._state = "failed"
                    self._save_metadata()
            if self._current_worker(generation, mirror):
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
                (
                    self._board.piece_at(chess.square(file_index, rank)).symbol()
                    if self._board.piece_at(chess.square(file_index, rank))
                    else "."
                )
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
            turn = "white" if self._board is not None and self._board.turn else "black"
            return {
                "game_id": self._game_id,
                "seat": color,
                "state": self._state,
                "joined": dict(self._joined),
                "left": dict(self._left),
                "turn": turn,
                "your_turn": self._state == "playing" and color == turn,
                "rows": self._rows(),
                "legal_moves": self._legal_moves(color),
                "history": list(self._history),
                "result": self._result,
                "error": self._error,
                "clock": None,
                "expires_at": None,
                "pending_uci": self._cursor.pending_uci if self._cursor else None,
                "draw_offer": self._draw_offer,
            }

    def admin_status(self, *, base_url: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            value = {
                "game_id": self._game_id,
                "state": self._state,
                "joined": dict(self._joined),
                "left": dict(self._left),
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
                "pending_uci": self._cursor.pending_uci if self._cursor else None,
                "draw_offer": self._draw_offer,
            }
            resolved_base = base_url or self._base_url
            if resolved_base and self._tokens:
                value["join_urls"] = {
                    color: f"{resolved_base.rstrip('/')}/station/play#{token}"
                    for color, token in self._tokens.items()
                }
            return value

    def move(
        self, token: str, uci: str, *, expected_ply: Optional[int] = None
    ) -> dict[str, Any]:
        import chess

        color = self._seat(token)
        with self._motion_lock:
            with self._lock:
                generation = self._generation
                mirror = self._require_mirror()
                if expected_ply is not None:
                    if expected_ply < 0:
                        raise ValidationError("expected ply cannot be negative")
                    if expected_ply < len(self._history):
                        existing = self._history[expected_ply]
                        if existing["uci"] == uci and existing["color"] == color:
                            return self.player_status(token)
                        raise ValidationError(
                            "move request conflicts with confirmed history"
                        )
                    if expected_ply != len(self._history):
                        raise ValidationError(
                            "move request is ahead of confirmed history"
                        )
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
                if self._link is None:
                    raise ConfigurationError("station Marlin connection is unavailable")
                mirror.link = self._link
                cursor = mirror._execute_ply(self._cursor, uci)
                board = mirror._board(cursor)
                with self._lock:
                    if not self._current_worker(generation, mirror):
                        raise ConfigurationError(
                            "station game changed while the move was executing"
                        )
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
                    self._draw_offer = None
                    outcome = board.outcome(claim_draw=False)
                    if self._state in {"stopped", "failed"}:
                        pass
                    elif outcome is not None:
                        self._result = outcome.result()
                        self._state = "finished"
                    else:
                        self._state = "playing"
                    self._save_metadata()
            except Exception as exc:
                with self._lock:
                    if self._mirror.cursor_path.exists():
                        self._cursor = MirrorCursor.load(
                            self._mirror.cursor_path, self._game_id
                        )
                    if self._state != "stopped":
                        self._error = str(exc)
                        self._state = "failed"
                        self._save_metadata()
                self._close_link()
                raise
            if self._state == "finished":
                self._close_link()
            return self.player_status(token)

    def resume(self, confirmation: str) -> dict[str, Any]:
        if confirmation != STATION_RESUME_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_RESUME_CONFIRMATION}")
        with self._lock:
            if self._state != "resume_required":
                raise ConfigurationError(
                    "station has no interrupted game ready to resume"
                )
            self._state = "homing"
            self._save_metadata()
            generation = self._generation
            mirror = self._require_mirror()
        Thread(
            target=self._start_motion,
            args=(generation, mirror),
            daemon=True,
        ).start()
        return self.admin_status()

    def resign(self, token: str) -> dict[str, Any]:
        with self._motion_lock:
            with self._lock:
                color = self._seat(token)
                if self._state not in {"playing", "homing", "waiting_players"}:
                    raise ConfigurationError("station game cannot be resigned")
                self._result = "0-1" if color == "white" else "1-0"
                self._state = "finished"
                self._save_metadata()
        self._close_link()
        return self.player_status(token)

    def leave(self, token: str) -> dict[str, Any]:
        with self._motion_lock:
            color = self._seat(token)
            with self._lock:
                if self._state in {"waiting_players", "homing", "playing"}:
                    self._result = "0-1" if color == "white" else "1-0"
                    self._state = "finished"
                self._left[color] = True
                self._save_metadata()
        self._close_link()
        return self.player_status(token)

    def offer_draw(self, token: str) -> dict[str, Any]:
        color = self._seat(token)
        with self._motion_lock:
            with self._lock:
                if self._state != "playing":
                    raise ConfigurationError(
                        "station game is not accepting draw offers"
                    )
                if self._draw_offer is not None and self._draw_offer != color:
                    self._result = "1/2-1/2"
                    self._state = "finished"
                    self._draw_offer = None
                else:
                    self._draw_offer = color
                self._save_metadata()
        if self._state == "finished":
            self._close_link()
        return self.player_status(token)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "idle":
                raise ConfigurationError("there is no station game")
            executing = self._state == "executing"
            self._state = "failed" if executing else "stopped"
            self._error = (
                "operator emergency stop during a move; verify physical state"
                if executing
                else self._error
            )
            self._generation += 1
            self._save_metadata()
            link = self._link
            self._link = None
        if link is not None:
            try:
                link.emergency_stop(self.config.safety.emergency_stop_command)
            finally:
                link.close()
        return self.admin_status()

    def reconcile(self, *, applied: bool, confirmation: str) -> dict[str, Any]:
        if confirmation != STATION_RECOVERY_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_RECOVERY_CONFIRMATION}")
        with self._motion_lock:
            with self._lock:
                if (
                    self._state != "failed"
                    or self._mirror is None
                    or self._cursor is None
                ):
                    raise ConfigurationError("station has no failed move to reconcile")
                pending_uci = self._cursor.pending_uci
                if not pending_uci:
                    raise ConfigurationError(
                        "station has no pending physical transaction"
                    )
            if self._mirror.journal_path.exists():
                if applied:
                    self._mirror.service.reconcile_mark_applied()
                else:
                    self._mirror.service.reconcile_discard()
            elif not applied:
                raise ConfigurationError(
                    "pending move state is already committed; choose Move completed"
                )
            cursor = self._mirror._recover_reconciled_submove(self._cursor)
            with self._lock:
                self._cursor = cursor
                self._error = None
                self._state = "homing"
                generation = self._generation
                mirror = self._mirror
                cursor = self._cursor
                self._save_metadata()
            Thread(
                target=self._resume_after_reconcile,
                args=(generation, mirror, cursor, pending_uci),
                daemon=True,
            ).start()
            return self.admin_status()

    def _resume_after_reconcile(
        self,
        generation: int,
        mirror: LichessMirror,
        base_cursor: MirrorCursor,
        pending_uci: str,
    ) -> None:
        import chess

        try:
            board_before = mirror._board(base_cursor)
            move = chess.Move.from_uci(pending_uci)
            if move not in board_before.legal_moves:
                raise ValidationError(
                    f"pending station move {pending_uci} is no longer legal"
                )
            san = board_before.san(move)
            color = "white" if board_before.turn else "black"
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            link = link_type(mirror.config.serial)
            link.connect()
            with self._lock:
                if (
                    not self._current_worker(generation, mirror)
                    or self._state != "homing"
                ):
                    link.close()
                    return
                self._link = link
            mirror.service.home_with_link(link)
            with self._lock:
                if (
                    not self._current_worker(generation, mirror)
                    or self._state != "homing"
                ):
                    link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                    link.close()
                    return
            mirror.link = link
            cursor = mirror._execute_ply(base_cursor, pending_uci)
            board = mirror._board(cursor)
            with self._lock:
                if not self._current_worker(generation, mirror):
                    link.close()
                    return
                self._cursor = cursor
                self._board = board
                if not self._history or self._history[-1]["ply"] != len(cursor.moves):
                    self._history.append(
                        {
                            "ply": len(cursor.moves),
                            "uci": pending_uci,
                            "san": san,
                            "color": color,
                        }
                    )
                outcome = board.outcome(claim_draw=False)
                if outcome is not None:
                    self._result = outcome.result()
                    self._state = "finished"
                else:
                    self._state = "playing"
                self._save_metadata()
            if self._state == "finished":
                self._close_link()
        except Exception as exc:
            with self._lock:
                if (
                    self._current_worker(generation, mirror)
                    and self._state != "stopped"
                ):
                    self._error = str(exc)
                    self._state = "failed"
                    self._save_metadata()
            if self._current_worker(generation, mirror):
                self._close_link()

    def _close_link(self) -> None:
        with self._lock:
            link = self._link
            self._link = None
        if link is not None:
            link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
            link.close()
