from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import signal
import subprocess
import threading
from typing import Any, Callable, Mapping, Optional, Sequence

from .controller import GantryController
from .errors import ConfigurationError, ValidationError


@dataclass(frozen=True)
class Confirmation:
    key: str
    label: str


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    title: str
    description: str
    category: str
    command: tuple[str, ...]
    serial: bool = False
    physical: bool = False
    long_running: bool = False
    confirmations: tuple[Confirmation, ...] = ()
    development_only: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "id": self.operation_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "serial": self.serial,
            "physical": self.physical,
            "long_running": self.long_running,
            "confirmations": [
                {"key": item.key, "label": item.label} for item in self.confirmations
            ],
            "development_only": self.development_only,
        }


def _base_command(
    config_path: Path, state_path: Path, journal_path: Path, audit_path: Path
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "chess-gantry",
        "--config",
        str(config_path),
        "--state",
        str(state_path),
        "--journal",
        str(journal_path),
        "--audit",
        str(audit_path),
    )


def operation_catalog(
    root: Path,
    config_path: Path,
    state_path: Path,
    journal_path: Path,
    audit_path: Path,
) -> tuple[OperationSpec, ...]:
    base = _base_command(config_path, state_path, journal_path, audit_path)
    demo_base = _base_command(
        root / "config.demo.json",
        root / "data" / "dashboard-demo-state.json",
        root / "data" / "dashboard-demo-pending.json",
        root / "data" / "dashboard-demo-audit.jsonl",
    )
    clear_path = Confirmation(
        "clear_path", "All motor paths are clear and the emergency cutoff is ready."
    )
    clear_workspace = Confirmation(
        "clear_workspace",
        "The complete workspace is clear and the emergency cutoff is ready.",
    )
    magnet_safe = Confirmation(
        "magnet_safe",
        "The magnet driver, power supply, and flyback protection are safe.",
    )
    at_switches = Confirmation(
        "at_switches", "X, Y, and Z are physically held at their end switches."
    )
    return (
        OperationSpec(
            "unit-tests",
            "Run all tests",
            "Compile Python and run the complete automated test suite.",
            "Checks",
            ("./scripts/check.sh",),
            development_only=True,
        ),
        OperationSpec(
            "quality-checks",
            "Formatting and policy",
            "Run Black, Prettier, and repository policy checks.",
            "Checks",
            ("npm", "run", "check"),
            development_only=True,
        ),
        OperationSpec(
            "demo-readiness",
            "Complete demo readiness",
            "Run tests, quality checks, planning, magnet simulation, and sweep simulation.",
            "Checks",
            ("./scripts/demo_check.sh",),
            development_only=True,
        ),
        OperationSpec(
            "simulate-circle",
            "Simulate 200 mm circle",
            "Home simulated Marlin and trace the magnet-on circle without hardware.",
            "Simulation",
            demo_base
            + (
                "circle-demo",
                "--diameter-mm",
                "200",
                "--feed-mm-min",
                "1800",
                "--segments",
                "72",
                "--confirm-motion",
                "--demo",
            ),
        ),
        OperationSpec(
            "simulate-perimeter",
            "Simulate full perimeter",
            "Trace the home-origin 330 x 300 mm machine perimeter without hardware.",
            "Simulation",
            demo_base
            + (
                "perimeter-demo",
                "--width-mm",
                "330",
                "--height-mm",
                "300",
                "--feed-mm-min",
                "1800",
                "--confirm-motion",
                "--demo",
            ),
        ),
        OperationSpec(
            "simulate-square-centers",
            "Simulate all 64 square centers",
            "Home and visit every measured 40 mm square center without hardware.",
            "Simulation",
            demo_base
            + (
                "square-center-demo",
                "--feed-mm-min",
                "1800",
                "--dwell-ms",
                "150",
                "--confirm-motion",
                "--demo",
            ),
        ),
        OperationSpec(
            "firmware-check",
            "Check installed firmware",
            "Verify Relay Chess firmware identity and read all endstops without movement.",
            "Hardware checks",
            ("uv", "run", "python", "scripts/check_firmware.py"),
            serial=True,
        ),
        OperationSpec(
            "endstop-sample",
            "Sample endstops",
            "Read x_min, y_max, and z_max twenty times without movement.",
            "Hardware checks",
            base + ("endstop-watch", "--samples", "20", "--interval", "0.1"),
            serial=True,
        ),
        OperationSpec(
            "home-gantry",
            "Home the gantry",
            "Run firmware G28 X Y Z and save the homing report.",
            "Physical hardware",
            base
            + (
                "home-gantry",
                "--record",
                str(root / "data" / "gantry_home.json"),
                "--confirm-motion",
                "--confirm-clear-path",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_path,),
        ),
        OperationSpec(
            "short-movement",
            "Short movement test",
            "Move each gantry direction 5 mm at 300 mm/min and return.",
            "Physical hardware",
            base
            + (
                "motor-test",
                "--distance-mm",
                "5",
                "--feed-mm-min",
                "300",
                "--confirm-motion",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_path,),
        ),
        OperationSpec(
            "magnet-pulse",
            "One-second magnet pulse",
            "Drive fan P0 at full power for one second, then switch it off.",
            "Physical hardware",
            base + ("magnet-test", "--duration-s", "1", "--confirm-motion"),
            serial=True,
            physical=True,
            confirmations=(magnet_safe,),
        ),
        OperationSpec(
            "circle-demo",
            "Run 200 mm magnet circle",
            "Home, hold the magnet on, trace the 200 mm circle, then return home.",
            "Physical hardware",
            base
            + (
                "circle-demo",
                "--diameter-mm",
                "200",
                "--feed-mm-min",
                "1800",
                "--segments",
                "72",
                "--confirm-motion",
                "--confirm-clear-workspace",
                "--confirm-magnet",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_workspace, magnet_safe),
        ),
        OperationSpec(
            "perimeter-demo",
            "Run full perimeter",
            "Home and trace the complete 330 x 300 mm machine perimeter, magnet off.",
            "Physical hardware",
            base
            + (
                "perimeter-demo",
                "--width-mm",
                "330",
                "--height-mm",
                "300",
                "--feed-mm-min",
                "1800",
                "--confirm-motion",
                "--confirm-clear-workspace",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_workspace,),
        ),
        OperationSpec(
            "workspace-grid",
            "Run full workspace grid",
            "Visit an 8 x 8 grid over the configured workspace with the magnet off.",
            "Physical hardware",
            base
            + (
                "workspace-test",
                "--feed-mm-min",
                "1200",
                "--margin-mm",
                "20",
                "--columns",
                "8",
                "--rows",
                "8",
                "--dwell-ms",
                "100",
                "--confirm-motion",
                "--confirm-empty-workspace",
                "--confirm-at-switches",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_workspace, at_switches),
        ),
        OperationSpec(
            "square-centers",
            "Visit all 64 square centers",
            "Home and snake through every measured 40 mm center with the magnet off.",
            "Physical hardware",
            base
            + (
                "square-center-demo",
                "--feed-mm-min",
                "1800",
                "--dwell-ms",
                "150",
                "--confirm-motion",
                "--confirm-clear-workspace",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_workspace,),
        ),
        OperationSpec(
            "square-centers-magnet",
            "Visit 64 centers with magnet on",
            "Keep fan P0 energized continuously while visiting every measured center.",
            "Physical hardware",
            base
            + (
                "square-center-demo",
                "--feed-mm-min",
                "1800",
                "--dwell-ms",
                "150",
                "--magnet-on",
                "--confirm-motion",
                "--confirm-clear-workspace",
                "--confirm-magnet",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_workspace, magnet_safe),
        ),
        OperationSpec(
            "piece-demo",
            "Pick up and move one piece",
            "Use all endstops, pick up one piece, move 20 mm, release, and return.",
            "Physical hardware",
            base
            + (
                "piece-demo",
                "--distance-mm",
                "20",
                "--feed-mm-min",
                "1200",
                "--confirm-motion",
                "--confirm-at-switches",
                "--confirm-piece",
                "--confirm-magnet",
            ),
            serial=True,
            physical=True,
            confirmations=(clear_path, at_switches, magnet_safe),
        ),
        OperationSpec(
            "reset-state",
            "Reset board JSON",
            "Reset tracked pieces to the standard position and clear the pending journal.",
            "State and games",
            base + ("reset-state", "--confirm-standard-position"),
            confirmations=(
                Confirmation(
                    "standard_position",
                    "Every physical chess piece is in its standard starting square.",
                ),
            ),
        ),
        OperationSpec(
            "show-state",
            "Show board JSON",
            "Print the current tracked piece positions and processed events.",
            "State and games",
            base + ("show-state",),
        ),
        OperationSpec(
            "pending-status",
            "Inspect pending move",
            "Print the recovery journal without changing state.",
            "State and games",
            base + ("reconcile",),
        ),
        OperationSpec(
            "reconcile-applied",
            "Mark pending move applied",
            "Commit the journal's expected state after physically verifying the move completed.",
            "State and games",
            base
            + (
                "reconcile",
                "--mark-applied",
                "--confirm-physical-state",
            ),
            confirmations=(
                Confirmation(
                    "physical_move_applied",
                    "The physical move completed exactly as recorded in the pending journal.",
                ),
            ),
        ),
        OperationSpec(
            "reconcile-discard",
            "Discard pending move",
            "Keep current JSON state after verifying the physical board still matches it.",
            "State and games",
            base + ("reconcile", "--discard", "--confirm-physical-state"),
            confirmations=(
                Confirmation(
                    "physical_move_not_applied",
                    "The physical board still matches board_state.json; the pending move did not complete.",
                ),
            ),
        ),
        OperationSpec(
            "lichess-check",
            "Check Lichess game 6RkOwfp1",
            "Run readiness checks and plan the current public game without movement.",
            "State and games",
            demo_base
            + (
                "lichess-pgn",
                "6RkOwfp1",
                "--output-dir",
                str(root / "data" / "lichess" / "6RkOwfp1-check"),
            ),
        ),
        OperationSpec(
            "lichess-dry-run",
            "Follow Lichess without hardware",
            "Continuously poll game 6RkOwfp1 and generate plans. Stop from the dashboard.",
            "State and games",
            demo_base
            + (
                "lichess-follow",
                "6RkOwfp1",
                "--output-dir",
                str(root / "data" / "lichess" / "6RkOwfp1-dry"),
                "--session",
                str(root / "data" / "lichess" / "6RkOwfp1-dry.session.json"),
                "--reset-session",
            ),
            long_running=True,
        ),
        OperationSpec(
            "lichess-play",
            "Play Lichess physically",
            "Home, then continuously mirror game 6RkOwfp1 on the physical board.",
            "State and games",
            base
            + (
                "lichess-follow",
                "6RkOwfp1",
                "--output-dir",
                str(root / "data" / "lichess" / "6RkOwfp1-physical"),
                "--session",
                str(root / "data" / "lichess" / "6RkOwfp1-physical.session.json"),
                "--reset-session",
                "--execute",
                "--confirm-motion",
            ),
            serial=True,
            physical=True,
            long_running=True,
            confirmations=(
                clear_workspace,
                magnet_safe,
                Confirmation(
                    "standard_position",
                    "The physical and JSON boards match at the standard starting position.",
                ),
            ),
        ),
    )


class OperationManager:
    def __init__(
        self,
        root: Path,
        controller: GantryController,
        operations: Sequence[OperationSpec],
        *,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        allow_physical: bool = True,
        allow_development: bool = True,
    ) -> None:
        self.root = root
        self.controller = controller
        self._operations = {item.operation_id: item for item in operations}
        self._process_factory = process_factory
        self.allow_physical = allow_physical
        self.allow_development = allow_development
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._run: Optional[dict[str, Any]] = None
        self._logs = ""
        self._cancel_requested = False

    def catalog(self) -> list[dict[str, Any]]:
        catalog = []
        for item in self._operations.values():
            value = item.public()
            value["enabled"] = (self.allow_physical or not item.physical) and (
                self.allow_development or not item.development_only
            )
            catalog.append(value)
        return catalog

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run": None if self._run is None else dict(self._run),
                "logs": self._logs,
            }

    def running(self) -> bool:
        with self._lock:
            return self._run is not None and self._run["state"] in {
                "starting",
                "running",
                "stopping",
            }

    def _append(self, text: str) -> None:
        with self._lock:
            self._logs = (self._logs + text)[-100_000:]

    def start(
        self, operation_id: str, confirmations: Optional[Mapping[str, Any]] = None
    ) -> dict[str, Any]:
        with self._lock:
            if self._run is not None and self._run["state"] in {
                "starting",
                "running",
                "stopping",
            }:
                raise ConfigurationError("another dashboard task is already running")
            try:
                spec = self._operations[operation_id]
            except KeyError as exc:
                raise ValidationError(
                    f"unknown dashboard task: {operation_id}"
                ) from exc
            supplied = confirmations or {}
            missing = [
                item.label
                for item in spec.confirmations
                if supplied.get(item.key) is not True
            ]
            if missing:
                raise ValidationError(
                    "required confirmations are missing: " + "; ".join(missing)
                )
            if spec.physical and not self.allow_physical:
                raise ConfigurationError(
                    "physical dashboard tasks are disabled while web mode uses --demo"
                )
            if spec.development_only and not self.allow_development:
                raise ConfigurationError(
                    "development dashboard tasks are unavailable in the distroless runtime"
                )
            if self.controller.connected:
                self.controller.disconnect()
            now = datetime.now(timezone.utc).isoformat()
            self._logs = f"$ {' '.join(spec.command)}\n"
            self._cancel_requested = False
            self._run = {
                "operation_id": spec.operation_id,
                "title": spec.title,
                "state": "starting",
                "physical": spec.physical,
                "long_running": spec.long_running,
                "started_at": now,
                "ended_at": None,
                "returncode": None,
            }
            self._thread = threading.Thread(
                target=self._execute, args=(spec,), daemon=True
            )
            self._thread.start()
            return self.status()

    def _execute(self, spec: OperationSpec) -> None:
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = self._process_factory(
                spec.command,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,
            )
            with self._lock:
                self._process = process
                if self._run is not None and self._run["state"] == "starting":
                    self._run["state"] = "running"
                cancel_requested = self._cancel_requested
            if cancel_requested:
                os.killpg(process.pid, signal.SIGTERM)
            if process.stdout is not None:
                for line in process.stdout:
                    self._append(line)
            returncode = process.wait()
            with self._lock:
                if self._run is not None and self._cancel_requested:
                    self._run["state"] = "cancelled"
                elif self._run is not None and self._run["state"] in {
                    "starting",
                    "running",
                }:
                    self._run["state"] = "completed" if returncode == 0 else "failed"
                if self._run is not None:
                    self._run["returncode"] = returncode
                    self._run["ended_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as exc:
            self._append(f"Dashboard task failed to start: {exc}\n")
            with self._lock:
                if self._run is not None and self._run["state"] not in {
                    "stopping",
                    "cancelled",
                }:
                    self._run["state"] = "failed"
                    self._run["returncode"] = -1
                    self._run["ended_at"] = datetime.now(timezone.utc).isoformat()
        finally:
            with self._lock:
                process = self._process
                self._process = None
            if process is not None and process.stdout is not None:
                process.stdout.close()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._run is None or self._run["state"] not in {"starting", "running"}:
                raise ConfigurationError("there is no running dashboard task")
            self._run["state"] = "stopping"
            self._cancel_requested = True
            physical = bool(self._run["physical"])
            process = self._process
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=3)
        if physical:
            self._append(
                "Sending M112 emergency stop after cancelling physical task.\n"
            )
            result = subprocess.run(
                ("uv", "run", "chess-gantry", "--config", "config.json", "stop"),
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                check=False,
            )
            self._append(result.stdout)
        with self._lock:
            if self._run is not None:
                self._run["state"] = "cancelled"
                self._run["ended_at"] = datetime.now(timezone.utc).isoformat()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        return self.status()
