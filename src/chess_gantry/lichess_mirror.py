from __future__ import annotations

from dataclasses import dataclass, replace
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TextIO
import json
import os
import sys
import time

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_pgn import chess_move_deltas, fetch_pgn, lichess_client
from .models import BoardState
from .kinematics import grid_to_machine
from .persistence import atomic_write_json, read_json, utc_now
from .serial_link import DemoMarlinSerial, MarlinSerial
from .service import GantryService


FAST_TRAVEL_MM_MIN = 12000.0
FAST_DRAG_MM_MIN = 3000.0


def _game_snapshot(pgn: str) -> tuple[tuple[str, ...], str, str]:
    import chess.pgn

    game = chess.pgn.read_game(StringIO(pgn))
    if game is None:
        raise ValidationError("Lichess returned unreadable PGN")
    variant = game.headers.get("Variant", "Standard")
    if variant != "Standard" or game.headers.get("FEN"):
        raise ConfigurationError("physical mirror supports only standard initial chess")
    moves = tuple(move.uci() for move in game.mainline_moves())
    result = game.headers.get("Result", "*")
    status = "finished" if result != "*" else "started"
    return moves, status, result


@dataclass(frozen=True)
class MirrorCursor:
    game_id: str
    moves: tuple[str, ...]
    fen: str
    physical_revision: int
    pending_uci: Optional[str] = None
    pending_total: int = 0
    pending_completed: int = 0
    result: str = "*"

    @classmethod
    def create(cls, game_id: str) -> "MirrorCursor":
        import chess

        return cls(game_id, (), chess.Board().fen(), 0)

    @classmethod
    def load(cls, path: Path, game_id: str) -> "MirrorCursor":
        raw = read_json(path)
        if raw.get("schema_version") != 1 or raw.get("game_id") != game_id:
            raise ConfigurationError(
                f"mirror cursor {path} is incompatible with game {game_id}"
            )
        moves = raw.get("moves")
        if not isinstance(moves, list) or not all(
            isinstance(value, str) for value in moves
        ):
            raise ValidationError("mirror cursor moves must be strings")
        return cls(
            game_id=game_id,
            moves=tuple(moves),
            fen=str(raw.get("fen", "")),
            physical_revision=int(raw.get("physical_revision", -1)),
            pending_uci=raw.get("pending_uci"),
            pending_total=int(raw.get("pending_total", 0)),
            pending_completed=int(raw.get("pending_completed", 0)),
            result=str(raw.get("result", "*")),
        )

    def save(self, path: Path) -> None:
        atomic_write_json(
            path,
            {
                "schema_version": 1,
                "game_id": self.game_id,
                "moves": list(self.moves),
                "fen": self.fen,
                "physical_revision": self.physical_revision,
                "pending_uci": self.pending_uci,
                "pending_total": self.pending_total,
                "pending_completed": self.pending_completed,
                "result": self.result,
                "updated_at": utc_now(),
            },
        )


class MirrorTerminal:
    def __init__(
        self,
        output: TextIO = sys.stdout,
        *,
        screen: bool = True,
        unicode: bool = False,
        title: str = "Chess Gantry Lichess Mirror",
    ) -> None:
        self.output = output
        self.screen = screen and bool(getattr(output, "isatty", lambda: False)())
        self.unicode = unicode
        self.title = title

    def render(
        self,
        board: Any,
        *,
        game_id: str,
        state: str,
        cursor: MirrorCursor,
        message: str = "",
    ) -> None:
        if self.screen:
            self.output.write("\x1b[2J\x1b[H")
        board_text = board.unicode(borders=True) if self.unicode else str(board)
        self.output.write(
            f"{self.title}\n"
            f"Game: {game_id}  State: {state}  Ply: {len(cursor.moves)}  "
            f"Revision: {cursor.physical_revision}\n"
            f"Turn: {'White' if board.turn else 'Black'}  Result: {cursor.result}\n\n"
            f"{board_text}\n"
        )
        if cursor.moves:
            self.output.write(f"\nLast move: {cursor.moves[-1]}\n")
        if cursor.pending_uci:
            self.output.write(
                f"Pending physical ply: {cursor.pending_uci} "
                f"({cursor.pending_completed}/{cursor.pending_total})\n"
            )
        if message:
            self.output.write(f"\n{message}\n")
        self.output.flush()


def mirror_config(config: AppConfig, *, fast: bool) -> AppConfig:
    motion = replace(config.motion, park_after_move=False)
    if fast:
        motion = replace(
            motion,
            travel_feed_mm_min=FAST_TRAVEL_MM_MIN,
            drag_feed_mm_min=FAST_DRAG_MM_MIN,
        )
    return replace(config, motion=motion)


class LichessMirror:
    def __init__(
        self,
        *,
        game_id: str,
        config: AppConfig,
        root: Path,
        execute: bool,
        demo: bool,
        fast: bool,
        stream_mode: str = "public",
        terminal: Optional[MirrorTerminal] = None,
        client: Any = None,
        pgn_fetcher: Callable[..., str] = fetch_pgn,
        sleep: Callable[[float], None] = time.sleep,
        directory: Optional[Path] = None,
        allow_initial_history: bool = False,
    ) -> None:
        if directory is None:
            if not game_id.isalnum() or not 8 <= len(game_id) <= 12:
                raise ValidationError("Lichess game ID must be 8-12 letters or digits")
        elif not game_id.isalnum() or not 8 <= len(game_id) <= 64:
            raise ValidationError("session ID must be 8-64 letters or digits")
        if stream_mode not in {"auto", "public", "board"}:
            raise ValidationError("stream mode must be auto, public, or board")
        if not config.capture.buffer_points:
            raise ConfigurationError(
                "Lichess mirror requires at least one castling buffer point"
            )
        self.game_id = game_id
        self.config = mirror_config(config, fast=fast)
        self.root = root
        self.execute = execute
        self.demo = demo
        self.fast = fast
        self.stream_mode = stream_mode
        self.terminal = terminal or MirrorTerminal()
        self.token = os.environ.get("LICHESS_TOKEN", "").strip() or None
        self.client = client or lichess_client(self.token)
        self.pgn_fetcher = pgn_fetcher
        self.sleep = sleep
        self.allow_initial_history = allow_initial_history
        session_kind = "demo" if demo else "physical" if execute else "simulation"
        self.directory = directory or (
            root / "data" / "lichess-mirror" / game_id / session_kind
        )
        self.cursor_path = self.directory / "cursor.json"
        self.state_path = self.directory / "board_state.json"
        self.journal_path = self.directory / "pending_move.json"
        self.audit_path = self.directory / "audit.jsonl"
        self.service = GantryService(
            self.config, self.state_path, self.journal_path, self.audit_path
        )
        self.link: Any = None

    def _initialize(self) -> MirrorCursor:
        import chess

        self.directory.mkdir(parents=True, exist_ok=True)
        if self.journal_path.exists():
            raise ConfigurationError(
                f"pending physical transaction at {self.journal_path}; reconcile it first"
            )
        if self.cursor_path.exists():
            cursor = MirrorCursor.load(self.cursor_path, self.game_id)
            if not self.state_path.exists():
                raise ConfigurationError(
                    "mirror cursor exists but physical board state is missing"
                )
            state = self.service.store.load()
            allowed_revisions = {cursor.physical_revision}
            if cursor.pending_uci:
                allowed_revisions.add(cursor.physical_revision + 1)
            if state.revision not in allowed_revisions:
                raise ConfigurationError(
                    "physical board revision does not match mirror cursor; reconcile before resume"
                )
            board = chess.Board()
            for uci in cursor.moves:
                move = chess.Move.from_uci(uci)
                if move not in board.legal_moves:
                    raise ValidationError(
                        "mirror cursor contains an illegal move prefix"
                    )
                board.push(move)
            if board.fen() != cursor.fen:
                raise ValidationError("mirror cursor FEN does not match its UCI prefix")
            return cursor
        if self.state_path.exists():
            raise ConfigurationError(
                "physical mirror state exists without a cursor; move it aside or restore the matching cursor"
            )
        snapshot, _, result = _game_snapshot(
            self.pgn_fetcher(self.game_id, token=self.token, client=self.client)
        )
        if snapshot and not self.allow_initial_history:
            raise ConfigurationError(
                "physical mirror must start before the first Lichess move; create a fresh game"
            )
        self.service.store.initialize(BoardState.standard(), overwrite=True)
        cursor = replace(MirrorCursor.create(self.game_id), result=result)
        cursor.save(self.cursor_path)
        return cursor

    def _board(self, cursor: MirrorCursor) -> Any:
        import chess

        board = chess.Board()
        for uci in cursor.moves:
            board.push_uci(uci)
        return board

    def _verify_prefix(self, cursor: MirrorCursor, remote: tuple[str, ...]) -> None:
        if (
            len(remote) < len(cursor.moves)
            or remote[: len(cursor.moves)] != cursor.moves
        ):
            raise ConfigurationError(
                "Lichess move history diverged from the committed mirror cursor"
            )

    def _execute_remote_prefix(
        self, cursor: MirrorCursor, remote: tuple[str, ...]
    ) -> MirrorCursor:
        self._verify_prefix(cursor, remote)
        while len(cursor.moves) < len(remote):
            cursor = self._execute_ply(cursor, remote[len(cursor.moves)])
        return cursor

    def _recover_reconciled_submove(self, cursor: MirrorCursor) -> MirrorCursor:
        if not cursor.pending_uci:
            return cursor
        revision = self.service.store.load().revision
        if revision == cursor.physical_revision:
            return cursor
        if revision == cursor.physical_revision + 1:
            cursor = replace(
                cursor,
                physical_revision=revision,
                pending_completed=cursor.pending_completed + 1,
            )
            cursor.save(self.cursor_path)
            return cursor
        raise ConfigurationError(
            "physical state advanced unexpectedly during mirror recovery"
        )

    def _state_before_pending_ply(self, cursor: MirrorCursor) -> BoardState:
        import chess

        board = chess.Board()
        state = BoardState.standard()
        for ply, uci in enumerate(cursor.moves, 1):
            move = board.parse_uci(uci)
            deltas = chess_move_deltas(
                board,
                move,
                state,
                f"rebuild.{ply}",
                allow_promotion_replacement=True,
            )
            for delta in deltas:
                captured = state.validate_move(delta)
                capture_slot = None
                if captured is not None:
                    used = set(state.used_capture_slots())
                    capture_slot = next(
                        value for value in range(64) if value not in used
                    )
                state = state.applied(delta, capture_slot)
            board.push(move)
        return state

    def _execute_ply(self, cursor: MirrorCursor, uci: str) -> MirrorCursor:
        import chess

        board = self._board(cursor)
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValidationError(f"Lichess move {uci} is illegal in mirror state")
        if board.is_castling(move):
            return self._execute_buffered_castling(cursor, board, move)
        if board.is_capture(move) and not self.config.capture.enabled:
            raise ConfigurationError(
                f"Lichess move {uci} is a capture, but physical capture storage is disabled"
            )
        delta_state = (
            self._state_before_pending_ply(cursor)
            if cursor.pending_uci
            else self.service.store.load()
        )
        deltas = chess_move_deltas(
            board,
            move,
            delta_state,
            f"{self.game_id}.{len(cursor.moves) + 1}",
            allow_promotion_replacement=True,
        )
        if board.is_capture(move) and self.config.capture.mode == "eject":
            return self._execute_ejection_capture(cursor, board, move, deltas[0])
        if cursor.pending_uci and cursor.pending_uci != uci:
            raise ConfigurationError("mirror cursor has another pending physical ply")
        if not cursor.pending_uci:
            cursor = replace(
                cursor,
                pending_uci=uci,
                pending_total=len(deltas),
                pending_completed=0,
            )
            cursor.save(self.cursor_path)
        cursor = self._recover_reconciled_submove(cursor)
        for index in range(cursor.pending_completed, len(deltas)):
            delta = deltas[index]
            if self.execute:
                assert self.link is not None
                self.service.execute_with_link(delta, self.link)
            else:
                state = self.service.store.load()
                plan = self.service.plan(delta, state)
                self.service.store.save(plan.next_state)
            cursor = replace(
                cursor,
                physical_revision=self.service.store.load().revision,
                pending_completed=index + 1,
            )
            cursor.save(self.cursor_path)
        board.push(move)
        cursor = replace(
            cursor,
            moves=cursor.moves + (uci,),
            fen=board.fen(),
            pending_uci=None,
            pending_total=0,
            pending_completed=0,
        )
        cursor.save(self.cursor_path)
        return cursor

    def _execute_ejection_capture(
        self,
        cursor: MirrorCursor,
        board: Any,
        move: Any,
        delta: Any,
    ) -> MirrorCursor:
        if cursor.pending_uci and cursor.pending_uci != move.uci():
            raise ConfigurationError("mirror cursor has another pending physical ply")
        if not cursor.pending_uci:
            cursor = replace(
                cursor,
                pending_uci=move.uci(),
                pending_total=2,
                pending_completed=0,
            )
            cursor.save(self.cursor_path)
        cursor = self._recover_reconciled_submove(cursor)
        stages = (
            lambda: self.service.plan_capture_ejection(delta),
            lambda: self.service.plan(replace(delta, capture=None)),
        )
        for index in range(cursor.pending_completed, 2):
            self._apply_plan(stages[index]())
            cursor = replace(
                cursor,
                physical_revision=self.service.store.load().revision,
                pending_completed=index + 1,
            )
            cursor.save(self.cursor_path)
        board.push(move)
        cursor = replace(
            cursor,
            moves=cursor.moves + (move.uci(),),
            fen=board.fen(),
            pending_uci=None,
            pending_total=0,
            pending_completed=0,
        )
        cursor.save(self.cursor_path)
        return cursor

    def _apply_plan(self, plan: Any) -> None:
        if self.execute:
            assert self.link is not None
            self.service.execute_plan_with_link(plan, self.link)
        else:
            self.service.store.save(plan.next_state)

    def _execute_buffered_castling(
        self, cursor: MirrorCursor, board: Any, move: Any
    ) -> MirrorCursor:
        deltas = chess_move_deltas(
            board,
            move,
            (
                self._state_before_pending_ply(cursor)
                if cursor.pending_uci
                else self.service.store.load()
            ),
            f"{self.game_id}.{len(cursor.moves) + 1}",
        )
        if len(deltas) != 2 or not self.config.capture.buffer_points:
            raise ConfigurationError(
                "castling requires king/rook deltas and a buffer point"
            )
        king_delta, rook_delta = deltas
        rook_start = grid_to_machine(rook_delta.previous, self.config.board)
        buffer_point = min(
            self.config.capture.buffer_points,
            key=lambda point: (
                (point.x - rook_start.x) ** 2 + (point.y - rook_start.y) ** 2
            ),
        )
        if cursor.pending_uci and cursor.pending_uci != move.uci():
            raise ConfigurationError("mirror cursor has another pending physical ply")
        if not cursor.pending_uci:
            cursor = replace(
                cursor,
                pending_uci=move.uci(),
                pending_total=3,
                pending_completed=0,
            )
            cursor.save(self.cursor_path)
        cursor = self._recover_reconciled_submove(cursor)
        stages = (
            lambda: self.service.plan_buffer_out(
                replace(rook_delta, event_id=f"{rook_delta.event_id}.buffer-out"),
                buffer_point,
            ),
            lambda: self.service.plan(king_delta),
            lambda: self.service.plan_buffer_in(
                replace(rook_delta, event_id=f"{rook_delta.event_id}.buffer-in"),
                rook_delta.new,
            ),
        )
        for index in range(cursor.pending_completed, 3):
            plan = stages[index]()
            self._apply_plan(plan)
            cursor = replace(
                cursor,
                physical_revision=self.service.store.load().revision,
                pending_completed=index + 1,
            )
            cursor.save(self.cursor_path)
        board.push(move)
        cursor = replace(
            cursor,
            moves=cursor.moves + (move.uci(),),
            fen=board.fen(),
            pending_uci=None,
            pending_total=0,
            pending_completed=0,
        )
        cursor.save(self.cursor_path)
        return cursor

    def _stream_events(self) -> Iterable[Any]:
        if self.stream_mode == "board" or (
            self.stream_mode == "auto" and self.token is not None
        ):
            yield from self.client.board.stream_game_state(self.game_id)
            return
        yield from self.client.games.stream_game_moves(self.game_id)

    def _moves_from_event(self, event: Any) -> tuple[tuple[str, ...], str, str]:
        if isinstance(event, dict):
            state = event.get("state", {}) if event.get("type") == "gameFull" else event
            moves = state.get("moves")
            if isinstance(moves, str):
                status = str(state.get("status", "started"))
                winner = state.get("winner")
                result = (
                    "1-0"
                    if winner == "white"
                    else (
                        "0-1"
                        if winner == "black"
                        else "1/2-1/2" if status in {"draw", "stalemate"} else "*"
                    )
                )
                return (
                    tuple(moves.split()) if moves.strip() else (),
                    status,
                    result,
                )
        pgn = self.pgn_fetcher(self.game_id, token=self.token, client=self.client)
        return _game_snapshot(pgn)

    def run(self, *, once: bool = False) -> MirrorCursor:
        cursor = self._initialize()
        board = self._board(cursor)
        self.terminal.render(
            board,
            game_id=self.game_id,
            state="connecting",
            cursor=cursor,
            message=(
                f"Profile: {'FAST 12000/3000' if self.fast else 'configured'}  "
                f"Mode: {'demo physical path' if self.demo else 'physical' if self.execute else 'simulation'}"
            ),
        )
        if self.execute:
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            self.link = link_type(self.config.serial)
            self.link.connect()
            self.service.home_with_link(self.link)
        try:
            if self.stream_mode == "public" or (
                self.stream_mode == "auto" and not self.token
            ):
                return self._run_public_polling(cursor, board, once=once)
            reconnect_delay = 1.0
            while True:
                try:
                    snapshot, status, result = _game_snapshot(
                        self.pgn_fetcher(
                            self.game_id, token=self.token, client=self.client
                        )
                    )
                    self._verify_prefix(cursor, snapshot)
                    if len(snapshot) > len(cursor.moves):
                        cursor = self._execute_remote_prefix(cursor, snapshot)
                        board = self._board(cursor)
                    cursor = replace(cursor, result=result)
                    cursor.save(self.cursor_path)
                    self.terminal.render(
                        board,
                        game_id=self.game_id,
                        state=status,
                        cursor=cursor,
                    )
                    if status == "finished" or once:
                        return cursor
                    for event in self._stream_events():
                        remote, stream_status, stream_result = self._moves_from_event(
                            event
                        )
                        self._verify_prefix(cursor, remote)
                        if len(remote) > len(cursor.moves):
                            cursor = self._execute_remote_prefix(cursor, remote)
                            board = self._board(cursor)
                        if stream_result != cursor.result:
                            cursor = replace(cursor, result=stream_result)
                            cursor.save(self.cursor_path)
                        self.terminal.render(
                            board,
                            game_id=self.game_id,
                            state=stream_status,
                            cursor=cursor,
                        )
                        if stream_status in {
                            "mate",
                            "resign",
                            "stalemate",
                            "draw",
                            "timeout",
                            "outoftime",
                            "aborted",
                        }:
                            return cursor
                    self.sleep(reconnect_delay)
                    reconnect_delay = min(30.0, reconnect_delay * 2.0)
                except KeyboardInterrupt:
                    raise
                except ConfigurationError:
                    raise
                except Exception as exc:
                    self.terminal.render(
                        board,
                        game_id=self.game_id,
                        state="reconnecting",
                        cursor=cursor,
                        message=f"Stream error: {exc}; retrying in {reconnect_delay:g}s",
                    )
                    self.sleep(reconnect_delay)
                    reconnect_delay = min(30.0, reconnect_delay * 2.0)
        finally:
            if self.link is not None:
                self.link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                self.link.close()
                self.link = None

    def _run_public_polling(
        self, cursor: MirrorCursor, board: Any, *, once: bool
    ) -> MirrorCursor:
        retry_delay = 2.0
        while True:
            try:
                pgn = self.pgn_fetcher(self.game_id, token=None, client=self.client)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self.terminal.render(
                    board,
                    game_id=self.game_id,
                    state="reconnecting",
                    cursor=cursor,
                    message=f"Public PGN error: {exc}; retrying in {retry_delay:g}s",
                )
                self.sleep(retry_delay)
                retry_delay = min(60.0, retry_delay * 2.0)
                continue
            snapshot, status, result = _game_snapshot(pgn)
            cursor = self._execute_remote_prefix(cursor, snapshot)
            board = self._board(cursor)
            cursor = replace(cursor, result=result)
            cursor.save(self.cursor_path)
            self.terminal.render(
                board,
                game_id=self.game_id,
                state=status,
                cursor=cursor,
            )
            if status == "finished" or once:
                return cursor
            retry_delay = 2.0
            self.sleep(2.0)
