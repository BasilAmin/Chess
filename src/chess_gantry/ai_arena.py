from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from .errors import ConfigurationError, ValidationError
from .lichess_pgn import chess_move_deltas
from .serial_link import DemoMarlinSerial, MarlinSerial
from .service import GantryService


class AIArena:
    def __init__(
        self,
        chatgpt: Any,
        claude: Any,
        *,
        config: Any = None,
        root: Optional[Path] = None,
        demo: bool = False,
    ) -> None:
        self.chatgpt = chatgpt
        self.claude = claude
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._board: Any = None
        self._state = "idle"
        self._error: Optional[str] = None
        self._history: list[dict[str, Any]] = []
        self._white = "chatgpt"
        self._black = "claude"
        self._style = "balanced"
        self._delay_s = 0.5
        self._max_plies = 200
        self._started_at: Optional[float] = None
        self.config = config
        self.root = root
        self.demo = demo
        self._physical = False
        self._link: Any = None
        self._service: Optional[GantryService] = None
        self._manual_action: Optional[dict[str, Any]] = None
        self._manual_event = threading.Event()
        self._serial_port: Optional[str] = None
        self._serial_baudrate: Optional[int] = None

    def running(self) -> bool:
        with self._lock:
            return self._state in {
                "starting",
                "homing",
                "thinking",
                "executing",
                "waiting_manual",
                "running",
            }

    def start(
        self,
        *,
        white: str = "chatgpt",
        black: str = "claude",
        style: str = "balanced",
        delay_s: float = 0.5,
        max_plies: int = 200,
        physical: bool = False,
        confirm_motion: bool = False,
        serial_port: Optional[str] = None,
        serial_baudrate: Optional[int] = None,
    ) -> dict[str, Any]:
        import chess

        if {white, black} != {"chatgpt", "claude"}:
            raise ValidationError(
                "AI arena requires one ChatGPT side and one Claude side"
            )
        if style not in {"balanced", "aggressive", "positional", "creative"}:
            raise ValidationError("AI arena style is invalid")
        if not 0 <= delay_s <= 30:
            raise ValidationError("AI arena delay must be between 0 and 30 seconds")
        if not 1 <= max_plies <= 500:
            raise ValidationError("AI arena ply limit must be between 1 and 500")
        if physical and (self.config is None or self.root is None):
            raise ConfigurationError("physical AI arena is not configured")
        if physical and not self.demo and not confirm_motion:
            raise ValidationError("physical AI arena requires motion confirmation")
        with self._lock:
            if self.running():
                raise ConfigurationError("AI arena is already running")
            self._board = chess.Board()
            self._state = "starting"
            self._error = None
            self._history = []
            self._white = white
            self._black = black
            self._style = style
            self._delay_s = delay_s
            self._max_plies = max_plies
            self._started_at = time.time()
            self._physical = physical
            self._manual_action = None
            self._manual_event.clear()
            self._serial_port = serial_port
            self._serial_baudrate = serial_baudrate
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self.status()

    def _run(self) -> None:
        try:
            if self._physical:
                self._open_physical()
            while not self._stop.is_set():
                with self._lock:
                    board = self._board
                    if (
                        board is None
                        or board.is_game_over()
                        or board.ply() >= self._max_plies
                    ):
                        self._state = "finished"
                        return
                    actor = self._white if board.turn else self._black
                    provider = self.chatgpt if actor == "chatgpt" else self.claude
                    self._state = "thinking"
                started = time.monotonic()
                choice = provider.choose_move(board.copy(stack=True), style=self._style)
                move = board.parse_uci(choice.uci)
                san = board.san(move)
                if self._physical:
                    if board.is_capture(move) or move.promotion is not None:
                        with self._lock:
                            self._manual_action = {
                                "actor": actor,
                                "uci": choice.uci,
                                "san": san,
                                "rationale": choice.rationale,
                                "plan": choice.plan,
                                "capture": board.is_capture(move),
                                "promotion": move.promotion is not None,
                                "instruction": (
                                    f"Execute {san} ({choice.uci}) on the physical board, including "
                                    "captured-piece removal or promotion replacement, then confirm."
                                ),
                            }
                            self._state = "waiting_manual"
                        while not self._stop.is_set() and not self._manual_event.wait(
                            0.2
                        ):
                            pass
                        if self._stop.is_set():
                            return
                        self._manual_event.clear()
                        with self._lock:
                            self._manual_action = None
                    else:
                        with self._lock:
                            self._state = "executing"
                        self._execute_physical(board, move, actor)
                board.push(move)
                with self._lock:
                    self._history.append(
                        {
                            "ply": board.ply(),
                            "actor": actor,
                            "uci": choice.uci,
                            "san": san,
                            "rationale": choice.rationale,
                            "plan": choice.plan,
                            "latency_s": round(time.monotonic() - started, 3),
                        }
                    )
                    self._state = "running"
                if self._stop.wait(self._delay_s):
                    return
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
                self._state = "failed"
        finally:
            if self._link is not None:
                try:
                    self._link.best_effort(self.config.magnet.off_commands)
                    self._link.close()
                finally:
                    self._link = None

    def _open_physical(self) -> None:
        from dataclasses import replace

        assert self.config is not None and self.root is not None
        path = self.root / "data" / "games" / "ai-arena"
        path.mkdir(parents=True, exist_ok=True)
        self._service = GantryService(
            self.config,
            path / "board_state.json",
            path / "pending_move.json",
            path / "audit.jsonl",
        )
        if self._service.journal.exists():
            raise ConfigurationError(
                f"pending AI arena transaction at {self._service.journal.path}; reconcile it first"
            )
        from .models import BoardState
        from .persistence import atomic_write_json

        atomic_write_json(self._service.store.path, BoardState.standard().to_dict())
        settings = self.config.serial
        if self._serial_port:
            settings = replace(settings, port=self._serial_port)
        if self._serial_baudrate:
            settings = replace(
                settings,
                baudrate=self._serial_baudrate,
                fallback_baudrates=(),
            )
        link_type = DemoMarlinSerial if self.demo else MarlinSerial
        self._link = link_type(settings)
        self._state = "homing"
        self._link.connect()
        self._service.home_with_link(self._link)

    def _execute_physical(self, board: Any, move: Any, actor: str) -> None:
        assert self._service is not None and self._link is not None
        event = f"ai-arena.{board.ply() + 1}.{actor}"
        deltas = chess_move_deltas(board, move, self._service.store.load(), event)
        for delta in deltas:
            if self.demo:
                state = self._service.store.load()
                plan = self._service.plan(delta, state)
                self._service.store.save(plan.next_state)
            else:
                self._service.execute_with_link(delta, self._link)

    def confirm_manual_action(self) -> dict[str, Any]:
        with self._lock:
            if self._state != "waiting_manual" or self._manual_action is None:
                raise ConfigurationError("AI arena is not waiting for a manual move")
            self._manual_event.set()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running():
                raise ConfigurationError("AI arena is not running")
            self._stop.set()
            self._manual_event.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=45)
        with self._lock:
            if self._state not in {"failed", "finished"}:
                self._state = "stopped"
        return self.status()

    def status(self) -> dict[str, Any]:
        import chess

        with self._lock:
            board = self._board
            rows = None
            if board is not None:
                rows = [
                    "".join(
                        (
                            board.piece_at(chess.square(file_index, rank)).symbol()
                            if board.piece_at(chess.square(file_index, rank))
                            else "."
                        )
                        for file_index in range(8)
                    )
                    for rank in range(7, -1, -1)
                ]
            return {
                "state": self._state,
                "error": self._error,
                "white": self._white,
                "black": self._black,
                "style": self._style,
                "delay_s": self._delay_s,
                "max_plies": self._max_plies,
                "fen": board.fen() if board else None,
                "rows": rows,
                "turn": "white" if board and board.turn else "black",
                "history": list(self._history),
                "ply": board.ply() if board else 0,
                "legal_move_count": board.legal_moves.count() if board else 0,
                "in_check": board.is_check() if board else False,
                "game_over": board.is_game_over() if board else False,
                "result": board.result() if board else None,
                "started_at": self._started_at,
                "physical": self._physical,
                "manual_action": (
                    dict(self._manual_action) if self._manual_action else None
                ),
            }
