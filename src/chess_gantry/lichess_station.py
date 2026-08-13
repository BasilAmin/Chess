from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Callable, Optional
import time

from .config import AppConfig
from .errors import ConfigurationError, ValidationError
from .lichess_mirror import LichessMirror, MirrorCursor, MirrorTerminal, _game_snapshot
from .lichess_open import OpenChallenge, create_open_challenge
from .lichess_pgn import fetch_pgn, lichess_client
from .persistence import atomic_write_json, read_json
from .serial_link import DemoMarlinSerial, MarlinSerial


STATION_CONFIRMATION = "STATION BOARD AND CHUTES READY"
STATION_RECOVERY_CONFIRMATION = "STATION PHYSICAL STATE VERIFIED"
POLL_INTERVAL_S = 2.0
MAX_RETRY_DELAY_S = 60.0


@dataclass(frozen=True)
class StationChallenge:
    game_id: str
    white_url: str
    black_url: str
    challenge_url: str


class LichessStation:
    def __init__(
        self,
        root: Path,
        config: AppConfig,
        *,
        demo: bool,
        challenge_factory: Callable[..., OpenChallenge] = create_open_challenge,
        client: Any = None,
        pgn_fetcher: Callable[..., str] = fetch_pgn,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.config = config
        self.demo = demo
        self.challenge_factory = challenge_factory
        self.client = client or lichess_client()
        self.pgn_fetcher = pgn_fetcher
        self.sleep = sleep
        self._lock = RLock()
        self._generation = 0
        self._state = "idle"
        self._error: Optional[str] = None
        self._result: Optional[str] = None
        self._challenge: Optional[StationChallenge] = None
        self._mirror: Optional[LichessMirror] = None
        self._started_at: Optional[float] = None
        self._history: list[str] = []
        self._network_error: Optional[str] = None
        self._retry_delay_s = POLL_INTERVAL_S

    def active(self) -> bool:
        with self._lock:
            return self._state not in {"idle", "finished", "stopped"}

    def reserves_hardware(self) -> bool:
        return self.active()

    def create(self, *, base_url: str, confirmation: str) -> dict[str, Any]:
        del base_url
        if confirmation != STATION_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_CONFIRMATION}")
        with self._lock:
            if self.active():
                raise ConfigurationError("a Lichess station game is already active")
        challenge = self.challenge_factory(name="Chess Gantry Station")
        station_challenge = StationChallenge(
            challenge.id if hasattr(challenge, "id") else challenge.challenge_id,
            challenge.white_url,
            challenge.black_url,
            challenge.challenge_url,
        )
        directory = self.root / "data" / "station-games" / station_challenge.game_id
        mirror = LichessMirror(
            game_id=station_challenge.game_id,
            config=self.config,
            root=self.root,
            execute=True,
            demo=self.demo,
            fast=True,
            stream_mode="public",
            terminal=MirrorTerminal(output=StringIO(), screen=False),
            client=self.client,
            pgn_fetcher=self.pgn_fetcher,
            sleep=self.sleep,
            directory=directory,
            allow_initial_history=True,
        )
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._state = "waiting_players"
            self._error = None
            self._result = None
            self._challenge = station_challenge
            self._mirror = mirror
            self._started_at = time.time()
            self._history = []
            self._network_error = None
            self._retry_delay_s = POLL_INTERVAL_S
            atomic_write_json(
                directory / "station.json",
                {
                    "schema_version": 1,
                    "game_id": station_challenge.game_id,
                    "white_url": station_challenge.white_url,
                    "black_url": station_challenge.black_url,
                    "challenge_url": station_challenge.challenge_url,
                    "created_at": self._started_at,
                    "state": self._state,
                },
            )
        Thread(
            target=self._run_game,
            args=(generation, mirror, station_challenge),
            daemon=True,
        ).start()
        return self.admin_status()

    def _current(self, generation: int, mirror: LichessMirror) -> bool:
        return generation == self._generation and mirror is self._mirror

    def _run_game(
        self,
        generation: int,
        mirror: LichessMirror,
        challenge: StationChallenge,
    ) -> None:
        retry_delay = POLL_INTERVAL_S
        while True:
            with self._lock:
                if not self._current(generation, mirror) or self._state == "stopped":
                    return
            try:
                pgn = self.pgn_fetcher(
                    challenge.game_id, token=None, client=self.client
                )
                snapshot, status, result = _game_snapshot(pgn)
                with self._lock:
                    self._network_error = None
                    self._retry_delay_s = POLL_INTERVAL_S
                break
            except Exception as exc:
                with self._lock:
                    self._network_error = str(exc)
                    self._retry_delay_s = retry_delay
                self.sleep(retry_delay)
                retry_delay = min(MAX_RETRY_DELAY_S, retry_delay * 2.0)
        with self._lock:
            if not self._current(generation, mirror):
                return
            self._state = "playing"
        try:
            cursor = mirror._initialize()
            link_type = DemoMarlinSerial if self.demo else MarlinSerial
            mirror.link = link_type(mirror.config.serial)
            mirror.link.connect()
            mirror.service.home_with_link(mirror.link)
            while True:
                with self._lock:
                    if (
                        not self._current(generation, mirror)
                        or self._state == "stopped"
                    ):
                        return
                try:
                    snapshot, status, result = _game_snapshot(
                        self.pgn_fetcher(
                            challenge.game_id, token=None, client=self.client
                        )
                    )
                except Exception as exc:
                    with self._lock:
                        self._network_error = str(exc)
                        self._retry_delay_s = retry_delay
                    self.sleep(retry_delay)
                    retry_delay = min(MAX_RETRY_DELAY_S, retry_delay * 2.0)
                    continue
                retry_delay = POLL_INTERVAL_S
                with self._lock:
                    self._network_error = None
                    self._retry_delay_s = POLL_INTERVAL_S
                cursor = mirror._execute_remote_prefix(cursor, snapshot)
                with self._lock:
                    self._history = list(cursor.moves)
                    self._result = result
                if status == "finished":
                    break
                self.sleep(POLL_INTERVAL_S)
            with self._lock:
                if not self._current(generation, mirror):
                    return
                self._history = list(cursor.moves)
                self._result = cursor.result
                self._state = "finished"
        except Exception as exc:
            with self._lock:
                if self._current(generation, mirror):
                    self._error = str(exc)
                    self._state = "failed"
        finally:
            if mirror.link is not None:
                mirror.link.best_effort((*self.config.magnet.off_commands, "M211 S1"))
                mirror.link.close()
                mirror.link = None

    def _cursor(self) -> Optional[MirrorCursor]:
        mirror = self._mirror
        if mirror is None or not mirror.cursor_path.exists():
            return None
        return MirrorCursor.load(mirror.cursor_path, mirror.game_id)

    def admin_status(self, *, base_url: Optional[str] = None) -> dict[str, Any]:
        del base_url
        with self._lock:
            cursor = self._cursor()
            if cursor is not None:
                self._history = list(cursor.moves)
                if cursor.result != "*":
                    self._result = cursor.result
            challenge = self._challenge
            return {
                "game_id": challenge.game_id if challenge else None,
                "state": self._state,
                "joined": {
                    "white": self._state not in {"idle", "waiting_players"},
                    "black": self._state not in {"idle", "waiting_players"},
                },
                "left": {"white": False, "black": False},
                "join_urls": (
                    {
                        "white": challenge.white_url,
                        "black": challenge.black_url,
                    }
                    if challenge
                    else {}
                ),
                "challenge_url": challenge.challenge_url if challenge else None,
                "history": list(self._history),
                "result": self._result,
                "error": self._error,
                "network_error": self._network_error,
                "retry_delay_s": self._retry_delay_s,
                "started_at": self._started_at,
                "clock": None,
                "expires_at": None,
                "pending_uci": cursor.pending_uci if cursor else None,
            }

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._state == "idle":
                raise ConfigurationError("there is no station game")
            self._generation += 1
            mirror = self._mirror
            self._state = "stopped"
        if mirror is not None and mirror.link is not None:
            try:
                mirror.link.emergency_stop(self.config.safety.emergency_stop_command)
            finally:
                mirror.link.close()
                mirror.link = None
        return self.admin_status()

    def reconcile(self, *, applied: bool, confirmation: str) -> dict[str, Any]:
        if confirmation != STATION_RECOVERY_CONFIRMATION:
            raise ValidationError(f"type exactly: {STATION_RECOVERY_CONFIRMATION}")
        mirror = self._mirror
        if self._state != "failed" or mirror is None:
            raise ConfigurationError("station has no failed move to reconcile")
        if applied:
            mirror.service.reconcile_mark_applied()
        else:
            mirror.service.reconcile_discard()
        with self._lock:
            self._state = "stopped"
            self._error = None
        return self.admin_status()
