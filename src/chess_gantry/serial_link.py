from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import threading
from time import monotonic, sleep
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from .config import SerialSettings
from .errors import SerialProtocolError


_PRINTER_TOKENS = (
    "creality",
    "ender",
    "ch340",
    "ch341",
    "wch",
    "usb serial",
    "usb-serial",
    "usbserial",
    "usbmodem",
    "1a86",
    "10c4",
    "ftdi",
    "cp210",
    "arduino",
)


@dataclass(frozen=True)
class PortInfo:
    device: str
    description: str
    hwid: str
    likely_printer: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "description": self.description,
            "hwid": self.hwid,
            "likely_printer": self.likely_printer,
        }


@dataclass(frozen=True)
class ConnectionInfo:
    port: str
    baudrate: int
    firmware: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "baudrate": self.baudrate,
            "firmware": self.firmware,
        }


@dataclass(frozen=True)
class CommandResult:
    command: str
    responses: Tuple[str, ...]


def _looks_like_printer(device: str, description: str, hwid: str) -> bool:
    text = f"{device} {description} {hwid}".lower()
    if any(token in text for token in _PRINTER_TOKENS):
        return True
    return (
        device.startswith("/dev/ttyUSB")
        or device.startswith("/dev/ttyACM")
        or device.startswith("/dev/serial/by-id/")
        or device.startswith("/dev/cu.usb")
        or bool(re.fullmatch(r"COM\d+", device, re.IGNORECASE))
    )


def discover_serial_ports(
    port_provider: Optional[Callable[[], Iterable[Any]]] = None,
) -> Tuple[PortInfo, ...]:
    if port_provider is None:
        try:
            from serial.tools import list_ports
        except ImportError as exc:
            raise SerialProtocolError("pyserial is not installed") from exc
        port_provider = list_ports.comports

    found: List[PortInfo] = []
    for port in port_provider():
        device = str(getattr(port, "device", "") or "").strip()
        if not device:
            continue
        description = str(getattr(port, "description", "") or "USB serial device")
        hwid = str(getattr(port, "hwid", "") or "")
        combined = f"{device} {description} {hwid}".lower()
        if "bluetooth" in combined:
            continue
        found.append(
            PortInfo(
                device=device,
                description=description,
                hwid=hwid,
                likely_printer=_looks_like_printer(device, description, hwid),
            )
        )

    found.sort(key=lambda item: (not item.likely_printer, item.device.lower()))
    return tuple(found)


def list_serial_ports() -> Tuple[Tuple[str, str], ...]:
    return tuple((item.device, item.description) for item in discover_serial_ports())


def parse_endstop_states(lines: Sequence[str]) -> dict[str, bool]:
    states: dict[str, bool] = {}
    for line in lines:
        match = re.fullmatch(
            r"\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(open|triggered)\s*",
            line,
            flags=re.IGNORECASE,
        )
        if match:
            states[match.group(1).lower()] = match.group(2).lower() == "triggered"
    return states


def endstop_transition_lines(
    previous: dict[str, bool], current: dict[str, bool]
) -> Tuple[str, ...]:
    lines = []
    for name in sorted(current):
        triggered = current[name]
        if name not in previous:
            state = "TRIGGERED" if triggered else "OPEN"
            lines.append(f"INITIAL {name} {state}")
        elif previous[name] != triggered:
            event = "HIT" if triggered else "RELEASED"
            lines.append(f"{event} {name}")
    return tuple(lines)


class MarlinSerial:
    def __init__(
        self,
        settings: SerialSettings,
        serial_factory: Optional[Callable[..., Any]] = None,
        port_provider: Optional[Callable[[], Iterable[Any]]] = None,
    ) -> None:
        self.settings = settings
        self._serial_factory = serial_factory
        self._port_provider = port_provider
        self._serial: Optional[Any] = None
        self._connection_info: Optional[ConnectionInfo] = None
        self._lifecycle_lock = threading.RLock()
        self._command_lock = threading.RLock()

    @property
    def connected(self) -> bool:
        serial_object = self._serial
        if serial_object is None:
            return False
        is_open = getattr(serial_object, "is_open", True)
        return bool(is_open)

    @property
    def connection_info(self) -> Optional[ConnectionInfo]:
        return self._connection_info

    @property
    def active_port(self) -> Optional[str]:
        return None if self._connection_info is None else self._connection_info.port

    @property
    def active_baudrate(self) -> Optional[int]:
        return None if self._connection_info is None else self._connection_info.baudrate

    @property
    def firmware_identity(self) -> Optional[str]:
        return None if self._connection_info is None else self._connection_info.firmware

    def _factory(self) -> Callable[..., Any]:
        if self._serial_factory is not None:
            return self._serial_factory
        try:
            import serial
        except ImportError as exc:
            raise SerialProtocolError(
                "pyserial is not installed; run 'uv sync'"
            ) from exc
        return serial.Serial

    def _candidate_ports(self) -> Tuple[str, ...]:
        if not self.settings.auto_detect:
            return (self.settings.port,)
        ports = discover_serial_ports(self._port_provider)
        likely = tuple(item.device for item in ports if item.likely_printer)
        return likely or tuple(item.device for item in ports)

    def _open_connection(
        self, factory: Callable[..., Any], device: str, baudrate: int
    ) -> Any:
        options = {
            "baudrate": baudrate,
            "timeout": self.settings.read_timeout_s,
            "write_timeout": self.settings.write_timeout_s,
            "rtscts": False,
            "dsrdtr": False,
        }
        if self._serial_factory is not None:
            return factory(port=device, **options)
        serial_object = factory(port=None, **options)
        try:
            serial_object.dtr = False
            serial_object.rts = False
            serial_object.port = device
            serial_object.open()
            return serial_object
        except Exception:
            self._close_object(serial_object)
            raise

    def _wait_for_device(self, device: str, timeout_s: float) -> Optional[str]:
        if self._serial_factory is not None:
            return device
        deadline = monotonic() + timeout_s
        original = Path(device)
        while monotonic() < deadline:
            if original.exists():
                return str(original)
            ports = discover_serial_ports(self._port_provider)
            likely = [item.device for item in ports if item.likely_printer]
            if len(likely) == 1:
                return likely[0]
            sleep(0.2)
        return None

    @staticmethod
    def _close_object(serial_object: Any) -> None:
        try:
            close = getattr(serial_object, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    @staticmethod
    def _identity_from(lines: Sequence[str]) -> Optional[str]:
        for line in lines:
            upper = line.upper()
            if "FIRMWARE_NAME" in upper or "MARLIN" in upper:
                return line
        return None

    @staticmethod
    def _clean_command(command: str) -> str:
        stripped = command.split(";", 1)[0].strip()
        if "\n" in stripped or "\r" in stripped:
            raise SerialProtocolError("a serial command cannot contain a newline")
        return stripped

    @classmethod
    def _send_on_connection(
        cls,
        serial_object: Any,
        command: str,
        *,
        timeout_s: float,
    ) -> CommandResult:
        stripped = cls._clean_command(command)
        if not stripped:
            return CommandResult(command="", responses=())
        try:
            payload = (stripped + "\n").encode("ascii")
        except UnicodeEncodeError as exc:
            raise SerialProtocolError(f"G-code must be ASCII: {stripped!r}") from exc

        try:
            serial_object.write(payload)
            flush = getattr(serial_object, "flush", None)
            if callable(flush):
                flush()
        except Exception as exc:
            raise SerialProtocolError(
                f"failed to write command {stripped!r}: {exc}"
            ) from exc

        deadline = monotonic() + timeout_s
        responses: List[str] = []
        while monotonic() < deadline:
            try:
                raw = serial_object.readline()
            except Exception as exc:
                raise SerialProtocolError(
                    f"failed while reading response to {stripped!r}: {exc}"
                ) from exc
            if not raw:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw).strip()
            if not line:
                continue
            responses.append(line)
            lowered = line.lower()
            if lowered == "ok" or lowered.startswith("ok "):
                return CommandResult(command=stripped, responses=tuple(responses))
            if (
                lowered.startswith("error:")
                or lowered.startswith("error ")
                or lowered.startswith("!!")
                or "unknown command" in lowered
                or "printer halted" in lowered
                or "kill() called" in lowered
            ):
                raise SerialProtocolError(
                    f"Marlin rejected {stripped!r}: {' | '.join(responses[-8:])}"
                )
            if lowered.startswith("resend:") or lowered.startswith("rs "):
                raise SerialProtocolError(
                    "Marlin requested a numbered-line resend, which this USB transport does not use: "
                    + line
                )

        detail = " | ".join(responses[-8:]) if responses else "no response"
        raise SerialProtocolError(
            f"timeout waiting for Marlin acknowledgement of {stripped!r}: {detail}"
        )

    def connect(self) -> None:
        with self._lifecycle_lock:
            if self.connected:
                return

            candidate_ports = self._candidate_ports()
            if not candidate_ports:
                raise SerialProtocolError(
                    "no USB serial port was found. On Fedora, check for /dev/ttyUSB* or "
                    "/dev/ttyACM*, use a USB data cable, and close other printer software"
                )

            factory = self._factory()
            errors: List[str] = []
            for device in candidate_ports:
                for baudrate in self.settings.candidate_baudrates:
                    serial_object = None
                    try:
                        active_device = self._wait_for_device(
                            device, self.settings.startup_wait_s + 3.0
                        )
                        if active_device is None:
                            raise SerialProtocolError(
                                f"{device} disappeared and did not re-enumerate; check USB cable and controller power"
                            )
                        serial_object = self._open_connection(
                            factory, active_device, baudrate
                        )
                        if self.settings.startup_wait_s:
                            sleep(self.settings.startup_wait_s)
                        reset = getattr(serial_object, "reset_input_buffer", None)
                        if callable(reset):
                            reset()

                        firmware = None
                        if self.settings.verify_marlin:
                            try:
                                result = self._send_on_connection(
                                    serial_object,
                                    "M115",
                                    timeout_s=self.settings.handshake_timeout_s,
                                )
                            except SerialProtocolError:
                                if callable(reset):
                                    reset()
                                sleep(0.1)
                                result = self._send_on_connection(
                                    serial_object,
                                    "M115",
                                    timeout_s=self.settings.handshake_timeout_s,
                                )
                            firmware = self._identity_from(result.responses)
                            if firmware is None:
                                raise SerialProtocolError(
                                    "M115 was acknowledged but did not identify Marlin"
                                )

                        self._serial = serial_object
                        self._connection_info = ConnectionInfo(
                            port=active_device,
                            baudrate=baudrate,
                            firmware=firmware,
                        )
                        return
                    except Exception as exc:
                        errors.append(f"{device} at {baudrate}: {exc}")
                        if serial_object is not None:
                            self._close_object(serial_object)

            self._serial = None
            self._connection_info = None
            detail = "; ".join(errors[-6:])
            hint = (
                " Check USB power/cable if the device vanishes, close Cura/Pronterface and other "
                "dashboard instances, verify dialout access, and confirm Marlin baud/firmware."
            )
            raise SerialProtocolError(
                "serial ports were found, but none completed the Marlin handshake. "
                f"Details: {detail}.{hint}"
            )

    def close(self) -> None:
        with self._lifecycle_lock:
            serial_object = self._serial
            self._serial = None
            self._connection_info = None
            if serial_object is not None:
                self._close_object(serial_object)

    def __enter__(self) -> "MarlinSerial":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _require_connected(self) -> Any:
        if not self.connected or self._serial is None:
            raise SerialProtocolError("serial link is not connected")
        return self._serial

    def send_command(
        self,
        command: str,
        timeout_s: Optional[float] = None,
    ) -> CommandResult:
        with self._command_lock:
            serial_object = self._require_connected()
            return self._send_on_connection(
                serial_object,
                command,
                timeout_s=(
                    timeout_s
                    if timeout_s is not None
                    else self.settings.command_timeout_s
                ),
            )

    def send_program(self, commands: Iterable[str]) -> Tuple[CommandResult, ...]:
        with self._command_lock:
            serial_object = self._require_connected()
            results = []
            for command in commands:
                result = self._send_on_connection(
                    serial_object,
                    command,
                    timeout_s=self.settings.command_timeout_s,
                )
                if result.command:
                    results.append(result)
            return tuple(results)

    def best_effort(self, commands: Sequence[str]) -> None:
        with self._command_lock:
            if not self.connected or self._serial is None:
                return
            for command in commands:
                try:
                    self._send_on_connection(
                        self._serial,
                        command,
                        timeout_s=min(5.0, self.settings.command_timeout_s),
                    )
                except Exception:
                    return

    def emergency_stop(self, command: str = "M112") -> None:
        serial_object = self._require_connected()
        try:
            payload = (self._clean_command(command) + "\n").encode("ascii")
            serial_object.write(payload)
            flush = getattr(serial_object, "flush", None)
            if callable(flush):
                flush()
        except Exception as exc:
            raise SerialProtocolError(f"failed to send emergency stop: {exc}") from exc


class DemoMarlinSerial:
    POSITION_RE = re.compile(
        r"\bX(-?\d+(?:\.\d+)?)\s+Y(-?\d+(?:\.\d+)?)"
        r"(?:\s+Z(-?\d+(?:\.\d+)?))?(?:\s+E(-?\d+(?:\.\d+)?))?"
    )

    def __init__(self, settings: SerialSettings) -> None:
        self.settings = settings
        self._connected = False
        self._x = 0.0
        self._y = 0.0
        self._z = 0.0
        self.commands: List[str] = []

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def active_port(self) -> Optional[str]:
        return "DEMO" if self._connected else None

    @property
    def active_baudrate(self) -> Optional[int]:
        return self.settings.baudrate if self._connected else None

    @property
    def firmware_identity(self) -> Optional[str]:
        return "FIRMWARE_NAME:Marlin DEMO" if self._connected else None

    @property
    def connection_info(self) -> Optional[ConnectionInfo]:
        if not self._connected:
            return None
        return ConnectionInfo("DEMO", self.settings.baudrate, self.firmware_identity)

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def __enter__(self) -> "DemoMarlinSerial":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def send_command(
        self, command: str, timeout_s: Optional[float] = None
    ) -> CommandResult:
        if not self._connected:
            raise SerialProtocolError("serial link is not connected")
        stripped = MarlinSerial._clean_command(command)
        if not stripped:
            return CommandResult("", ())
        self.commands.append(stripped)
        upper = stripped.upper()
        if upper.startswith("G28"):
            self._x = 0.0
            self._y = 350.0
            self._z = 350.0
        elif upper.startswith(("G0 ", "G1 ", "G92 ")):
            match = self.POSITION_RE.search(upper)
            if match:
                self._x = float(match.group(1))
                self._y = float(match.group(2))
                if match.group(3) is not None:
                    self._z = float(match.group(3))
        if upper == "M115":
            responses = ("FIRMWARE_NAME:Marlin DEMO", "ok")
        elif upper == "M119":
            responses = (
                "Reporting endstop status",
                "x_min: TRIGGERED",
                "y_max: TRIGGERED",
                "z_max: TRIGGERED",
                "ok",
            )
        elif upper == "M114":
            responses = (
                f"X:{self._x:.3f} Y:{self._y:.3f} Z:{self._z:.3f} E:0.000",
                "ok",
            )
        else:
            responses = ("ok",)
        return CommandResult(stripped, responses)

    def send_program(self, commands: Iterable[str]) -> Tuple[CommandResult, ...]:
        return tuple(
            result
            for command in commands
            if (result := self.send_command(command)).command
        )

    def best_effort(self, commands: Sequence[str]) -> None:
        for command in commands:
            try:
                self.send_command(command)
            except Exception:
                return

    def emergency_stop(self, command: str = "M112") -> None:
        if self._connected:
            self.commands.append(MarlinSerial._clean_command(command))
        self._connected = False
