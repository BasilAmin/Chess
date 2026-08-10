from __future__ import annotations

import unittest

from chess_gantry.reed_switch import (
    GPIOA,
    GPIOB,
    GPPUA,
    GPPUB,
    IODIRA,
    IODIRB,
    MCP23017BankDiagnostic,
    MCP23017ReedSwitch,
    ReedState,
    bank_transitions,
    reed_transition,
)


class FakeBus:
    def __init__(self, registers):
        self.registers = registers
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read_byte_data(self, address, register):
        return self.registers.get(register, 0)

    def write_byte_data(self, address, register, value):
        self.writes.append((address, register, value))
        self.registers[register] = value


class ReedSwitchTests(unittest.TestCase):
    def test_configures_gpb0_input_pullup_and_reads_open(self) -> None:
        registers = {IODIRB: 0xA0, GPPUB: 0x40, GPIOB: 0x01}
        bus = FakeBus(registers)
        reader = MCP23017ReedSwitch(bus_factory=lambda number: bus)
        state = reader.read()
        self.assertFalse(state.closed)
        self.assertTrue(state.raw_high)
        self.assertIn((0x20, IODIRB, 0xA1), bus.writes)
        self.assertIn((0x20, GPPUB, 0x41), bus.writes)

    def test_active_low_reads_grounded_switch_as_closed(self) -> None:
        registers = {IODIRB: 0xFF, GPPUB: 0xFF, GPIOB: 0x00}
        reader = MCP23017ReedSwitch(bus_factory=lambda number: FakeBus(registers))
        state = reader.read()
        self.assertTrue(state.closed)
        self.assertEqual(state.as_dict()["state"], "closed")
        self.assertEqual(state.as_dict()["address"], "0x20")

    def test_preserves_other_port_b_configuration_bits(self) -> None:
        registers = {IODIRB: 0x54, GPPUB: 0xA8, GPIOB: 0x01}
        bus = FakeBus(registers)
        MCP23017ReedSwitch(bus_factory=lambda number: bus).read()
        self.assertEqual(registers[IODIRB], 0x55)
        self.assertEqual(registers[GPPUB], 0xA9)

    def test_transition_messages(self) -> None:
        opened = ReedState(False, True, 1, 0x20)
        closed = ReedState(True, False, 1, 0x20)
        self.assertEqual(reed_transition(None, opened), "INITIAL GPB0 OPEN")
        self.assertEqual(reed_transition(opened, closed), "CLOSED GPB0")
        self.assertEqual(reed_transition(closed, opened), "OPENED GPB0")
        self.assertEqual(reed_transition(opened, opened), "")

    def test_bank_diagnostic_configures_and_reads_all_sixteen_inputs(self) -> None:
        registers = {GPIOA: 0xFE, GPIOB: 0x7F}
        bus = FakeBus(registers)
        result = MCP23017BankDiagnostic(
            addresses=(0x20,), configure=True, bus_factory=lambda number: bus
        ).read()
        device = result["responding"]["0x20"]
        self.assertEqual(device["closed_pins"], ["GPA0", "GPB7"])
        self.assertIn((0x20, IODIRA, 0xFF), bus.writes)
        self.assertIn((0x20, IODIRB, 0xFF), bus.writes)
        self.assertIn((0x20, GPPUA, 0xFF), bus.writes)
        self.assertIn((0x20, GPPUB, 0xFF), bus.writes)

    def test_bank_discovery_does_not_write_unknown_i2c_devices(self) -> None:
        bus = FakeBus({GPIOA: 0xFF, GPIOB: 0xFF})
        result = MCP23017BankDiagnostic(
            addresses=(0x20, 0x21), bus_factory=lambda number: bus
        ).read()
        self.assertEqual(bus.writes, [])
        self.assertFalse(result["responding"]["0x20"]["configured"])

    def test_bank_transition_reports_pin_and_device_changes(self) -> None:
        previous = {
            "responding": {
                "0x20": {"pins": {"GPA0": {"closed": False}}},
                "0x21": {"pins": {}},
            }
        }
        current = {
            "responding": {
                "0x20": {"pins": {"GPA0": {"closed": True}}},
                "0x22": {"pins": {}},
            }
        }
        self.assertEqual(
            bank_transitions(previous, current),
            ["0x20 GPA0 CLOSED", "FOUND 0x22", "LOST 0x21"],
        )


if __name__ == "__main__":
    unittest.main()
