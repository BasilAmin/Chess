from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
import os
import threading
from typing import Any, Optional

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_pgn import chess_move_deltas, fetch_pgn, lichess_client
from .models import BoardState
from .openai_opponent import SolChessOpponent
from .persistence import atomic_write_json
from .serial_link import DemoMarlinSerial, MarlinSerial
from .service import GantryService


def pgn_uci_moves(pgn: str) -> tuple[str, ...]:
    import chess.pgn

    game = chess.pgn.read_game(StringIO(pgn))
    if game is None:
        raise ValidationError("Lichess returned unreadable PGN")
    return tuple(move.uci() for move in game.mainline_moves())


class GameCoordinator:
    def __init__(
        self,
        root: Path,
        config: AppConfig,
        vision: Any,
        *,
        demo: bool,
        client_factory: Any = lichess_client,
        pgn_fetcher: Any = fetch_pgn,
        token_provider: Optional[Any] = None,
        opponent: Optional[Any] = None,
    ) -> None:
        self.root = root
        self.config = config
        self.vision = vision
        self.demo = demo
        self._client_factory = client_factory
        self._pgn_fetcher = pgn_fetcher
        self._token_provider = token_provider or (
            lambda: os.environ.get("LICHESS_TOKEN", "").strip() or None
        )
        self._opponent = opponent
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._link: Any = None
        self._service: Optional[GantryService] = None
        self._client: Any = None
        self._board: Any = None
        self._mode = "idle"
        self._local_color: Optional[str] = None
        self._game_id: Optional[str] = None
        self._state = "idle"
        self._error: Optional[str] = None
        self._pending_echo: Optional[str] = None
        self._confirmed_ply = 0
        self._executed = 0
        self._logs = ""
        self._serial_settings = config.serial
        self._opponent_style = "balanced"
        self._history: list[dict[str, Any]] = []
        self._last_ai: Optional[dict[str, str]] = None

    def _append(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._logs = (self._logs + f"[{stamp}] {text}\n")[-100_000:]

    def _set_error(self, exc: Exception) -> None:
        self._error = str(exc)
        self._state = "failed"
        self._append(f"FAILED: {exc}")

    def running(self) -> bool:
        with self._lock:
            return self._state in {
                "starting",
                "homing",
                "waiting_camera",
                "waiting_lichess",
                "executing",
                "thinking",
            }

    def status(self) -> dict[str, Any]:
        import chess

        with self._lock:
            rows = None
            if self._board is not None:
                rows = [
                    "".join(
                        (
                            self._board.piece_at(
                                chess.square(file_index, rank)
                            ).symbol()
                            if self._board.piece_at(chess.square(file_index, rank))
                            else "."
                        )
                        for file_index in range(8)
                    )
                    for rank in range(7, -1, -1)
                ]
            return {
                "status": {
                    "state": self._state,
                    "mode": self._mode,
                    "game_id": self._game_id,
                    "local_color": self._local_color,
                    "fen": self._board.fen() if self._board is not None else None,
                    "turn": (
                        "white"
                        if self._board is not None and self._board.turn
                        else "black"
                    ),
                    "confirmed_ply": self._confirmed_ply,
                    "executed_count": self._executed,
                    "pending_local_echo": self._pending_echo,
                    "error": self._error,
                    "history": list(self._history),
                    "last_ai": dict(self._last_ai) if self._last_ai else None,
                    "opponent_style": self._opponent_style,
                    "rows": rows,
                    "legal_move_count": (
                        self._board.legal_moves.count()
                        if self._board is not None
                        else 0
                    ),
                    "in_check": (
                        self._board.is_check() if self._board is not None else False
                    ),
                    "game_over": (
                        self._board.is_game_over() if self._board is not None else False
                    ),
                    "result": (
                        self._board.result() if self._board is not None else None
                    ),
                },
                "logs": self._logs,
            }

    def _new_service(self, game_id: str) -> GantryService:
        path = self.root / "data" / "games" / game_id
        path.mkdir(parents=True, exist_ok=True)
        state = path / "board_state.json"
        service = GantryService(
            self.config,
            state,
            path / "pending_move.json",
            path / "audit.jsonl",
        )
        if service.journal.exists():
            raise ConfigurationError(
                f"pending game transaction at {service.journal.path}; reconcile it first"
            )
        atomic_write_json(state, BoardState.standard().to_dict())
        return service

    def start(
        self,
        *,
        mode: str,
        game_id: Optional[str],
        local_color: Optional[str],
        confirm_motion: bool,
        serial_port: Optional[str] = None,
        serial_baudrate: Optional[int] = None,
        opponent_style: str = "balanced",
    ) -> dict[str, Any]:
        import chess

        if mode not in {"local", "lichess", "mirror", "openai"}:
            raise ValidationError("game mode must be local, lichess, mirror, or openai")
        if mode in {"lichess", "mirror"} and (not game_id or not game_id.isalnum()):
            raise ValidationError("Lichess and mirror modes require a game ID")
        if mode in {"lichess", "mirror"} and not self._token_provider():
            raise ConfigurationError("LICHESS_TOKEN is required for Lichess game modes")
        if mode in {"lichess", "openai"} and local_color not in {"white", "black"}:
            raise ValidationError(f"{mode} mode requires local_color white or black")
        if opponent_style not in {"balanced", "aggressive", "positional", "creative"}:
            raise ValidationError("OpenAI style is invalid")
        if mode != "mirror":
            camera = self.vision.status()
            if (
                camera.get("status") != "complete"
                or camera.get("stable_observations", 0) < 2
                or not camera.get("matches_standard_position")
                or not camera.get("local", {}).get("profiles_ready")
            ):
                raise ConfigurationError(
                    "camera game requires ArUco/board calibration, all six sampled colors, and three stable local observations of the standard starting position"
                )
        if (
            not self.demo
            and mode in {"lichess", "mirror", "openai"}
            and not confirm_motion
        ):
            raise ValidationError(
                "remote physical execution requires motion confirmation"
            )
        with self._lock:
            if self.running():
                raise ConfigurationError("a game is already running")
            identifier = game_id or f"local-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            global_pending = self.root / "data" / "pending_move.json"
            if global_pending.exists():
                raise ConfigurationError(
                    f"pending physical transaction at {global_pending}; reconcile it before starting a game"
                )
            self._service = self._new_service(identifier)
            self._mode = mode
            self._game_id = game_id
            self._local_color = local_color
            self._board = chess.Board()
            self._state = "starting"
            self._error = None
            self._pending_echo = None
            self._confirmed_ply = 0
            self._executed = 0
            self._logs = ""
            self._history = []
            self._last_ai = None
            self._opponent_style = opponent_style
            self._serial_settings = self.config.serial
            if serial_port:
                self._serial_settings = replace(self._serial_settings, port=serial_port)
            if serial_baudrate is not None:
                if serial_baudrate <= 0:
                    raise ValidationError("serial baudrate must be positive")
                self._serial_settings = replace(
                    self._serial_settings,
                    baudrate=serial_baudrate,
                    fallback_baudrates=(),
                )
            self._stop.clear()
            self.vision.start_game()
            self.vision.set_move_handler(
                self.on_camera_move if mode != "mirror" else None
            )
            self._client = (
                self._client_factory(self._token_provider())
                if mode in {"lichess", "mirror"}
                else None
            )
            if mode == "openai" and self._opponent is None:
                self._opponent = SolChessOpponent()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self.status()

    def _run(self) -> None:
        try:
            if self._mode in {"lichess", "mirror", "openai"}:
                link_type = DemoMarlinSerial if self.demo else MarlinSerial
                self._link = link_type(self._serial_settings)
                with self._link:
                    self._state = "homing"
                    self._append("Homing gantry on the game serial connection.")
                    assert self._service is not None
                    self._service.home_with_link(self._link)
                    if self._mode == "openai":
                        self._state = "waiting_camera"
                        if self._local_color == "black":
                            self._play_openai_move()
                        while not self._stop.wait(0.5):
                            pass
                    else:
                        self._state = "waiting_lichess"
                        self._follow_lichess()
            else:
                self._state = "waiting_camera"
                while not self._stop.wait(0.5):
                    pass
        except Exception as exc:
            with self._lock:
                self._set_error(exc)
        finally:
            self._link = None
            self.vision.pause_inference(False)
            if self._state != "failed":
                self._state = "stopped"

    def on_camera_move(self, uci: str) -> None:
        import chess

        with self._lock:
            if self._mode not in {"local", "lichess", "openai"} or self._board is None:
                return
            move = chess.Move.from_uci(uci)
            if move not in self._board.legal_moves:
                raise ValidationError(f"camera move {uci} is illegal")
            moving_color = "white" if self._board.turn else "black"
            if (
                self._mode in {"lichess", "openai"}
                and moving_color != self._local_color
            ):
                raise ValidationError("camera detected a move for the remote side")
            assert self._service is not None
            event = f"{self._game_id or 'local'}.{self._board.ply() + 1}.camera"
            deltas = chess_move_deltas(
                self._board, move, self._service.store.load(), event
            )
            self._service.commit_observed_moves(deltas)
            san = self._board.san(move)
            if self._mode == "lichess":
                assert self._client is not None and self._game_id is not None
                self._client.board.make_move(self._game_id, uci)
                self._pending_echo = uci
                self._append(f"Submitted camera move {uci} to Lichess.")
            else:
                self._append(f"Registered local camera move {uci}.")
            self._board.push(move)
            self._history.append(
                {"ply": self._board.ply(), "uci": uci, "san": san, "actor": "human"}
            )
            self._confirmed_ply += 1
            self._state = (
                "waiting_lichess" if self._mode == "lichess" else "waiting_camera"
            )
            if self._mode == "openai":
                self._play_openai_move()

    def _play_openai_move(self) -> None:
        assert self._board is not None and self._opponent is not None
        self._state = "thinking"
        self._append("OpenAI Sol is choosing from the server legal-move allowlist.")
        choice = self._opponent.choose_move(self._board, style=self._opponent_style)
        move = self._board.parse_uci(choice.uci)
        san = self._board.san(move)
        self._last_ai = {
            "uci": choice.uci,
            "san": san,
            "rationale": choice.rationale,
            "plan": choice.plan,
        }
        self._execute_remote(choice.uci, actor="openai", san=san)

    def _follow_lichess(self) -> None:
        assert self._client is not None and self._game_id is not None
        first = True
        for event in self._client.board.stream_game_state(self._game_id):
            if self._stop.is_set():
                return
            event_type = event.get("type")
            state = event.get("state", {}) if event_type == "gameFull" else event
            moves_text = str(state.get("moves", "")).strip()
            moves = tuple(moves_text.split()) if moves_text else ()
            if first and moves:
                raise ConfigurationError(
                    "start a Sol game before the first Lichess move"
                )
            first = False
            while self._confirmed_ply < len(moves):
                uci = moves[self._confirmed_ply]
                self._execute_remote(uci)
            if self._pending_echo is not None and self._pending_echo in moves:
                self._append(
                    f"Confirmed local move echo {self._pending_echo}; no gantry movement."
                )
                self._pending_echo = None

    def _execute_remote(
        self, uci: str, *, actor: str = "remote", san: Optional[str] = None
    ) -> None:
        import chess

        assert self._board is not None and self._service is not None
        move = chess.Move.from_uci(uci)
        if move not in self._board.legal_moves:
            raise ValidationError(f"remote move {uci} is illegal")
        if self._board.is_capture(move) and not self.config.capture.enabled:
            raise ConfigurationError(
                "remote capture blocked: capture storage is not calibrated"
            )
        event = f"{self._game_id or self._mode}.{self._board.ply() + 1}.{actor}"
        notation = san or self._board.san(move)
        deltas = chess_move_deltas(self._board, move, self._service.store.load(), event)
        self._state = "executing"
        self.vision.pause_inference(True)
        for delta in deltas:
            if self.demo:
                state = self._service.store.load()
                plan = self._service.plan(delta, state)
                self._service.store.save(plan.next_state)
            else:
                assert self._link is not None
                self._service.execute_with_link(delta, self._link)
        self._board.push(move)
        self.vision.apply_expected_move(uci)
        self.vision.pause_inference(False)
        self._confirmed_ply += 1
        self._executed += 1
        self._history.append(
            {
                "ply": self._board.ply(),
                "uci": uci,
                "san": notation,
                "actor": actor,
            }
        )
        self._state = "waiting_camera" if self._mode == "openai" else "waiting_lichess"
        self._append(f"Physically executed {actor} move {uci} ({notation}).")

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.running():
                raise ConfigurationError("there is no running game")
            self._stop.set()
            self.vision.set_move_handler(None)
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=6)
        return self.status()
