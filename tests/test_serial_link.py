from __future__ import annotations

from dataclasses import dataclass
import unittest

from chess_gantry.config import SerialSettings
from chess_gantry.errors import SerialProtocolError
from chess_gantry.serial_link import (
    MarlinSerial,
    discover_serial_ports,
    endstop_transition_lines,
    parse_endstop_states,
)


class FakeSerial:
    def __init__(self, responses=None, **kwargs):
        self.responses = list(responses or [])
        self.writes = []
        self.closed = False
        self.is_open = True
        self.kwargs = kwargs
        self.dtr = True
        self.rts = True

    def write(self, payload):
        self.writes.append(payload)
        return len(payload)

    def flush(self):
        pass

    def readline(self):
        if self.responses:
            return self.responses.pop(0)
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True
        self.is_open = False

    def open(self):
        self.is_open = True


@dataclass
class FakePort:
    device: str
    description: str = ""
    hwid: str = ""


class SerialLinkTests(unittest.TestCase):
    def settings(
        self,
        command_timeout_s=0.03,
        *,
        port="loop",
        verify_marlin=False,
        handshake_timeout_s=0.01,
    ):
        return SerialSettings(
            port=port,
            baudrate=115200,
            fallback_baudrates=(115200, 250000),
            read_timeout_s=0.001,
            write_timeout_s=1.0,
            command_timeout_s=command_timeout_s,
            startup_wait_s=0.0,
            verify_marlin=verify_marlin,
            handshake_timeout_s=handshake_timeout_s,
        )

    def test_acknowledgement(self) -> None:
        fake = FakeSerial([b"echo:busy processing\n", b"ok\n"])
        link = MarlinSerial(self.settings(), serial_factory=lambda **kwargs: fake)
        with link:
            result = link.send_command("G90 ; comment")
        self.assertEqual(fake.writes, [b"G90\n"])
        self.assertEqual(result.responses[-1], "ok")
        self.assertTrue(fake.closed)

    def test_endstop_parser_and_transition_messages(self) -> None:
        initial = parse_endstop_states(
            ("Reporting endstop status", "x_min: open", "y_min: TRIGGERED", "ok")
        )
        self.assertEqual(initial, {"x_min": False, "y_min": True})
        self.assertEqual(
            endstop_transition_lines({}, initial),
            ("INITIAL x_min OPEN", "INITIAL y_min TRIGGERED"),
        )
        hit = {"x_min": True, "y_min": True}
        self.assertEqual(endstop_transition_lines(initial, hit), ("HIT x_min",))
        released = {"x_min": False, "y_min": True}
        self.assertEqual(endstop_transition_lines(hit, released), ("RELEASED x_min",))

    def test_invalid_utf8_startup_bytes_do_not_crash(self) -> None:
        fake = FakeSerial([b"\xff\xfe startup\n", b"ok\n"])
        link = MarlinSerial(self.settings(), serial_factory=lambda **kwargs: fake)
        with link:
            result = link.send_command("M114")
        self.assertEqual(result.responses[-1], "ok")
        self.assertIn("\ufffd", result.responses[0])

    def test_unknown_command_is_error(self) -> None:
        fake = FakeSerial([b'echo:Unknown command: "M9999"\n', b"ok\n"])
        link = MarlinSerial(self.settings(), serial_factory=lambda **kwargs: fake)
        with link, self.assertRaisesRegex(SerialProtocolError, "rejected"):
            link.send_command("M9999")

    def test_resend_request_is_error(self) -> None:
        fake = FakeSerial([b"Resend: 4\n"])
        link = MarlinSerial(self.settings(), serial_factory=lambda **kwargs: fake)
        with link, self.assertRaisesRegex(SerialProtocolError, "numbered-line resend"):
            link.send_command("G90")

    def test_timeout(self) -> None:
        fake = FakeSerial([])
        link = MarlinSerial(
            self.settings(command_timeout_s=0.005), serial_factory=lambda **kwargs: fake
        )
        with link, self.assertRaisesRegex(SerialProtocolError, "timeout"):
            link.send_command("M400")

    def test_fedora_ports_are_discovered_and_ranked(self) -> None:
        ports = discover_serial_ports(
            lambda: [
                FakePort("/dev/ttyS0", "Built-in serial"),
                FakePort("/dev/ttyUSB0", "USB Serial", "USB VID:PID=1A86:7523"),
                FakePort("/dev/ttyACM0", "Creality Ender"),
                FakePort("/dev/rfcomm0", "Bluetooth serial"),
            ]
        )
        self.assertEqual(
            [item.device for item in ports[:2]], ["/dev/ttyACM0", "/dev/ttyUSB0"]
        )
        self.assertTrue(ports[0].likely_printer)
        self.assertNotIn("/dev/rfcomm0", [item.device for item in ports])

    def test_auto_candidates_ignore_builtin_ports_when_usb_exists(self) -> None:
        link = MarlinSerial(
            self.settings(port="auto"),
            serial_factory=lambda **kwargs: FakeSerial(**kwargs),
            port_provider=lambda: [
                FakePort("/dev/ttyS0", "Built-in serial"),
                FakePort("/dev/ttyUSB0", "CH340 USB serial"),
            ],
        )
        self.assertEqual(link._candidate_ports(), ("/dev/ttyUSB0",))

    def test_auto_connect_tries_fallback_baud_and_verifies_marlin(self) -> None:
        opened = []

        def factory(**kwargs):
            opened.append((kwargs["port"], kwargs["baudrate"]))
            if kwargs["baudrate"] == 115200:
                return FakeSerial([b"\xff noise\n"])
            return FakeSerial([b"FIRMWARE_NAME:Marlin 2.1\n", b"ok\n"])

        link = MarlinSerial(
            self.settings(port="auto", verify_marlin=True, handshake_timeout_s=0.002),
            serial_factory=factory,
            port_provider=lambda: [FakePort("/dev/ttyUSB0", "CH340 USB serial")],
        )
        with link:
            self.assertEqual(link.active_port, "/dev/ttyUSB0")
            self.assertEqual(link.active_baudrate, 250000)
            self.assertIn("Marlin", link.firmware_identity or "")
        self.assertEqual(opened, [("/dev/ttyUSB0", 115200), ("/dev/ttyUSB0", 250000)])


if __name__ == "__main__":
    unittest.main()
