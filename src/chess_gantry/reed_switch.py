from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Optional

from .errors import ConfigurationError, GantryError


IODIRB = 0x01
IODIRA = 0x00
GPPUA = 0x0C
GPPUB = 0x0D
GPIOA = 0x12
GPIOB = 0x13
GPB0_MASK = 0x01


@dataclass(frozen=True)
class ReedState:
    closed: bool
    raw_high: bool
    bus: int
    address: int
    pin: str = "GPB0"

    def as_dict(self) -> dict[str, Any]:
        return {
            "closed": self.closed,
            "state": "closed" if self.closed else "open",
            "raw_high": self.raw_high,
            "bus": self.bus,
            "address": f"0x{self.address:02X}",
            "pin": self.pin,
        }


class MCP23017ReedSwitch:
    def __init__(
        self,
        *,
        bus_number: int = 1,
        address: int = 0x20,
        active_low: bool = True,
        bus_factory: Optional[Callable[[int], Any]] = None,
    ) -> None:
        if not 0 <= bus_number <= 255:
            raise ConfigurationError("I2C bus number must be between 0 and 255")
        if not 0x03 <= address <= 0x77:
            raise ConfigurationError("MCP23017 address must be between 0x03 and 0x77")
        self.bus_number = bus_number
        self.address = address
        self.active_low = active_low
        self._bus_factory = bus_factory or self._default_bus_factory
        self._lock = threading.RLock()
        self._configured = False

    @staticmethod
    def _default_bus_factory(bus_number: int) -> Any:
        try:
            from smbus2 import SMBus
        except ImportError as exc:
            raise ConfigurationError(
                "smbus2 is not installed; install Pi GPIO dependencies or use the Docker image"
            ) from exc
        try:
            return SMBus(bus_number)
        except OSError as exc:
            raise GantryError(
                f"cannot open /dev/i2c-{bus_number}: {exc}; enable I2C and pass the device into the container"
            ) from exc

    def _configure(self, bus: Any) -> None:
        direction = bus.read_byte_data(self.address, IODIRB)
        bus.write_byte_data(self.address, IODIRB, direction | GPB0_MASK)
        pullups = bus.read_byte_data(self.address, GPPUB)
        bus.write_byte_data(self.address, GPPUB, pullups | GPB0_MASK)
        self._configured = True

    def read(self) -> ReedState:
        with self._lock:
            try:
                with self._bus_factory(self.bus_number) as bus:
                    if not self._configured:
                        self._configure(bus)
                    raw_high = bool(bus.read_byte_data(self.address, GPIOB) & GPB0_MASK)
            except GantryError:
                raise
            except OSError as exc:
                self._configured = False
                raise GantryError(
                    f"MCP23017 at 0x{self.address:02X} did not respond on I2C bus {self.bus_number}: {exc}"
                ) from exc
        closed = not raw_high if self.active_low else raw_high
        return ReedState(
            closed=closed,
            raw_high=raw_high,
            bus=self.bus_number,
            address=self.address,
        )


class SimulatedReedSwitch:
    def __init__(self, *, bus_number: int = 1, address: int = 0x20) -> None:
        self.bus_number = bus_number
        self.address = address
        self._closed = False

    def read(self) -> ReedState:
        state = ReedState(
            closed=self._closed,
            raw_high=not self._closed,
            bus=self.bus_number,
            address=self.address,
        )
        self._closed = not self._closed
        return state


def reed_transition(previous: Optional[ReedState], current: ReedState) -> str:
    if previous is None:
        return f"INITIAL {current.pin} {current.as_dict()['state'].upper()}"
    if previous.closed == current.closed:
        return ""
    return f"{'CLOSED' if current.closed else 'OPENED'} {current.pin}"


class MCP23017BankDiagnostic:
    def __init__(
        self,
        *,
        bus_number: int = 1,
        addresses: tuple[int, ...] = tuple(range(0x20, 0x28)),
        active_low: bool = True,
        configure: bool = False,
        bus_factory: Optional[Callable[[int], Any]] = None,
    ) -> None:
        if not 0 <= bus_number <= 255:
            raise ConfigurationError("I2C bus number must be between 0 and 255")
        if not addresses or any(not 0x03 <= value <= 0x77 for value in addresses):
            raise ConfigurationError("diagnostic addresses must be valid I2C addresses")
        self.bus_number = bus_number
        self.addresses = addresses
        self.active_low = active_low
        self.configure = configure
        self._bus_factory = bus_factory or MCP23017ReedSwitch._default_bus_factory
        self._configured: set[int] = set()

    def read(self) -> dict[str, Any]:
        responding: dict[str, Any] = {}
        failures: dict[str, str] = {}
        try:
            with self._bus_factory(self.bus_number) as bus:
                for address in self.addresses:
                    label = f"0x{address:02X}"
                    try:
                        if self.configure and address not in self._configured:
                            bus.write_byte_data(address, IODIRA, 0xFF)
                            bus.write_byte_data(address, IODIRB, 0xFF)
                            bus.write_byte_data(address, GPPUA, 0xFF)
                            bus.write_byte_data(address, GPPUB, 0xFF)
                            self._configured.add(address)
                        gpio_a = bus.read_byte_data(address, GPIOA)
                        gpio_b = bus.read_byte_data(address, GPIOB)
                        pins = {}
                        for port, raw in (("A", gpio_a), ("B", gpio_b)):
                            for pin in range(8):
                                raw_high = bool(raw & (1 << pin))
                                closed = not raw_high if self.active_low else raw_high
                                pins[f"GP{port}{pin}"] = {
                                    "closed": closed,
                                    "raw_high": raw_high,
                                }
                        responding[label] = {
                            "configured": self.configure,
                            "gpio_a": f"0x{gpio_a:02X}",
                            "gpio_b": f"0x{gpio_b:02X}",
                            "closed_pins": [
                                pin for pin, value in pins.items() if value["closed"]
                            ],
                            "pins": pins,
                        }
                    except OSError as exc:
                        self._configured.discard(address)
                        failures[label] = str(exc)
        except GantryError:
            raise
        return {
            "bus": self.bus_number,
            "responding": responding,
            "failures": failures,
        }


def bank_transitions(
    previous: Optional[dict[str, Any]], current: dict[str, Any]
) -> list[str]:
    messages = []
    previous_devices = previous["responding"] if previous else {}
    for address, device in current["responding"].items():
        if address not in previous_devices:
            messages.append(f"FOUND {address}")
        old_pins = previous_devices.get(address, {}).get("pins", {})
        for pin, value in device["pins"].items():
            prior = old_pins.get(pin)
            if prior is None:
                continue
            if prior["closed"] != value["closed"]:
                state = "CLOSED" if value["closed"] else "OPENED"
                messages.append(f"{address} {pin} {state}")
    for address in previous_devices:
        if address not in current["responding"]:
            messages.append(f"LOST {address}")
    return messages
