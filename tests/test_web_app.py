from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import unittest
import urllib.error
import urllib.request

from chess_gantry.config import AppConfig
from chess_gantry.controller import GantryController
from chess_gantry.errors import ValidationError
from chess_gantry.models import BoardState
from chess_gantry.live_game import LiveGameManager
from chess_gantry.operations import OperationManager, OperationSpec
from chess_gantry.reed_switch import SimulatedReedSwitch
from chess_gantry.persistence import atomic_write_json
from chess_gantry.service import GantryService
from chess_gantry.web_app import HTML, GantryHTTPServer, RequestHandler
from chess_gantry.clerk_auth import ClerkSettings, render_dashboard
from chess_gantry.board_sensor import SyntheticBoardSensor

ROOT = Path(__file__).resolve().parents[1]
CLERK_ENVIRONMENT = {"CLERK_PUBLISHABLE_KEY": "pk_test_Y2xlcmsuZXhhbXBsZS5jb20k"}


class StubClerkVerifier:
    def __init__(self, accepted: str) -> None:
        self.accepted = accepted
        self.settings = ClerkSettings.require_from_environment(CLERK_ENVIRONMENT)

    def verify(self, session: str) -> dict:
        if session != self.accepted:
            raise ValidationError("the stub verifier rejected the session cookie")
        return {"sub": "user_stub"}


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        raw = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        raw["planner"]["kind"] = "direct"
        raw["capture"] = {"enabled": False, "slots": []}
        raw["safety"]["calibrated"] = True
        raw["safety"]["preflight_commands"] = []
        self.config = AppConfig.from_mapping(raw)
        state = BoardState.from_mapping(
            {
                "schema_version": 1,
                "revision": 0,
                "pieces": {"white_pawn_e": {"status": "board", "x": 4, "y": 1}},
                "processed_events": [],
            }
        )
        state_path = root / "state.json"
        atomic_write_json(state_path, state.to_dict())
        service = GantryService(
            self.config,
            state_path,
            root / "pending.json",
            root / "audit.jsonl",
        )
        self.controller = GantryController(self.config, service, demo=True)
        RequestHandler.controller = self.controller
        self.server = GantryHTTPServer(("127.0.0.1", 0), RequestHandler)
        self.server.operation_manager = OperationManager(
            root,
            self.controller,
            (
                OperationSpec(
                    "web-test",
                    "Web test",
                    "Print one line.",
                    "Checks",
                    ("/usr/bin/env", "true"),
                ),
            ),
        )
        self.server.live_game_manager = LiveGameManager(root, self.config, demo=True)
        self.server.reed_switch = SimulatedReedSwitch()
        self.server.board_sensor = SyntheticBoardSensor(sample_hz=300, stable_samples=2)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.controller.disconnect()
        self.server.board_sensor.close()
        self.temporary.cleanup()

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_plan_and_execute_api_share_integrated_workflow(self) -> None:
        move = {
            "event_id": "web-1",
            "position": "white_pawn_e",
            "px": 4,
            "py": 1,
            "nx": 4,
            "ny": 3,
        }
        _, plan = self.request("/api/plan", {"move": move})
        self.assertIn("G1 X122 Y178 Z200", plan["gcode"])
        _, board_before = self.request("/api/board")
        self.assertEqual(board_before["board_state"]["revision"], 0)

        _, connected = self.request("/api/connect", {})
        self.assertTrue(connected["status"]["connected"])
        self.request("/api/home", {"confirm_motion": True})
        _, executed = self.request(
            "/api/execute",
            {"move": move, "confirm_motion": True},
        )
        self.assertEqual(executed["summary"]["next_revision"], 1)
        _, board_after = self.request("/api/board")
        self.assertEqual(board_after["board_state"]["revision"], 1)
        self.assertEqual(board_after["board_state"]["pieces"]["white_pawn_e"]["y"], 3)

    def test_manual_api_homes_then_moves(self) -> None:
        self.request("/api/connect", {})
        _, home = self.request("/api/home", {"confirm_motion": True})
        self.assertTrue(home["status"]["homed"])
        _, moved = self.request(
            "/api/move",
            {"x_mm": 20, "y_mm": 30, "feed_mm_min": 600, "confirm_motion": True},
        )
        self.assertEqual(moved["status"]["position_mm"], {"x": 20.0, "y": 30.0})

    def test_live_position_and_keyboard_jog_api(self) -> None:
        self.request("/api/connect", {})
        self.request("/api/home", {"confirm_motion": True})
        _, position = self.request("/api/position", {})
        self.assertEqual(
            position["status"]["machine_position_mm"],
            {"x": 2.0, "y": 298.0, "z": 328.0},
        )
        _, jogged = self.request(
            "/api/jog",
            {
                "delta_x_mm": -5,
                "delta_y_mm": 0,
                "feed_mm_min": 600,
                "confirm_motion": True,
            },
        )
        self.assertEqual(jogged["status"]["position_mm"], {"x": 323.0, "y": 298.0})
        self.assertEqual(
            jogged["status"]["machine_position_mm"],
            {"x": 2.0, "y": 298.0, "z": 323.0},
        )

    def test_operations_catalog_and_task_api(self) -> None:
        _, catalog = self.request("/api/operations")
        self.assertEqual(catalog["operations"][0]["id"], "web-test")
        _, started = self.request(
            "/api/tasks/start",
            {"operation_id": "web-test", "confirmations": {}},
        )
        self.assertIn(started["run"]["state"], {"starting", "running", "completed"})
        for _ in range(100):
            _, status = self.request("/api/tasks/status")
            if status["run"]["state"] == "completed":
                break
            threading.Event().wait(0.01)
        self.assertEqual(status["run"]["state"], "completed")
        self.assertEqual(status["run"]["returncode"], 0)

    def test_dashboard_page_contains_operations_controls(self) -> None:
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn("Operations dashboard", html)
        self.assertIn('id="taskStop"', html)
        self.assertIn("/api/tasks/start", html)
        self.assertIn('id="machineZ"', html)
        self.assertIn("ArrowLeft", html)
        self.assertIn("/api/jog", html)
        self.assertIn('id="liveGameId"', html)
        self.assertIn("/api/live/start", html)
        self.assertIn("autoConnect", html)
        self.assertLess(
            html.index("async function autoConnect"), html.index("await autoConnect()")
        )
        self.assertIn('id="reedState"', html)
        self.assertIn("/api/reed", html)
        self.assertNotIn('id="keyboardArm"', html)
        self.assertNotIn('id="liveBoardReset"', html)
        self.assertNotIn('id="confirm"', html)
        self.assertIn('id="sensorBoard"', html)
        self.assertIn("/api/sensor/matrix", html)

    def test_reed_switch_api_reports_open_and_closed(self) -> None:
        _, first = self.request("/api/reed", {})
        _, second = self.request("/api/reed", {})
        self.assertEqual(first["reed"]["pin"], "GPB0")
        self.assertEqual(first["reed"]["state"], "open")
        self.assertEqual(second["reed"]["state"], "closed")
        self.assertEqual(second["reed"]["address"], "0x20")

    def test_reed_switch_api_reports_i2c_failure_without_crashing(self) -> None:
        class BrokenReader:
            def read(self):
                raise ValidationError("MCP23017 at 0x20 did not respond")

        self.server.reed_switch = BrokenReader()
        request = urllib.request.Request(
            self.base + "/api/reed",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 409)
        payload = json.loads(raised.exception.read())
        raised.exception.close()
        self.assertIn("did not respond", payload["error"])

    def test_synthetic_sensor_dataset_updates_piece_tracking(self) -> None:
        _, initial = self.request("/api/sensor/status")
        self.assertEqual(len(initial["sensor"]["matrix"]), 8)
        _, started = self.request("/api/sensor/dataset", {"dataset": "e2e4"})
        self.assertIn(started["sensor"]["state"], {"ready", "waiting", "move"})
        for _ in range(100):
            _, status = self.request("/api/sensor/status")
            if status["sensor"]["last_move"] == "e2e4":
                break
            threading.Event().wait(0.01)
        self.assertEqual(status["sensor"]["last_move"], "e2e4")
        self.assertTrue(
            any(piece["square"] == "e4" for piece in status["sensor"]["pieces"])
        )

    def test_synthetic_sensor_rejects_invalid_matrix(self) -> None:
        request = urllib.request.Request(
            self.base + "/api/sensor/matrix",
            data=json.dumps({"matrix": [[0]]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()

    def test_live_game_status_and_start_validation_api(self) -> None:
        _, status = self.request("/api/live/status")
        self.assertEqual(status["status"]["state"], "idle")
        request = urllib.request.Request(
            self.base + "/api/live/start",
            data=json.dumps(
                {"game_id": "game1234", "confirm_standard_position": False}
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 409)
        missing = json.loads(raised.exception.read())
        raised.exception.close()
        self.assertIn("standard", missing["error"])

    def _enable_clerk(self, token: str) -> None:
        verifier = StubClerkVerifier(token)
        self.server.clerk_verifier = verifier
        self.server.dashboard_html = render_dashboard(HTML, verifier.settings)

    def test_clerk_mode_serves_a_public_shell_and_guards_the_apis(self) -> None:
        self._enable_clerk("valid-clerk-session-token")
        with urllib.request.urlopen(self.base + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
        self.assertIn("clerkSignIn", html)
        self.assertIn("clerk.browser.js", html)
        self.assertIn("Operations dashboard", html)

        with self.assertRaises(urllib.error.HTTPError) as anonymous:
            urllib.request.urlopen(self.base + "/api/status", timeout=3)
        self.assertEqual(anonymous.exception.code, 401)
        self.assertIn(b"Clerk", anonymous.exception.read())
        anonymous.exception.close()

        rejected = urllib.request.Request(
            self.base + "/api/status", headers={"Cookie": "__session=wrong"}
        )
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            urllib.request.urlopen(rejected, timeout=3)
        self.assertEqual(invalid.exception.code, 401)
        invalid.exception.close()

        accepted = urllib.request.Request(
            self.base + "/api/status",
            headers={"Cookie": "__session=valid-clerk-session-token"},
        )
        with urllib.request.urlopen(accepted, timeout=3) as response:
            self.assertTrue(json.loads(response.read())["ok"])

    def test_a_bearer_header_is_no_longer_an_authentication_path(self) -> None:
        self._enable_clerk("valid-clerk-session-token")
        request = urllib.request.Request(
            self.base + "/api/status",
            headers={"Authorization": "Bearer valid-clerk-session-token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as bearer:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(bearer.exception.code, 401)
        bearer.exception.close()

    def test_the_session_cookie_guards_writes(self) -> None:
        self._enable_clerk("valid-clerk-session-token")
        anonymous_write = urllib.request.Request(
            self.base + "/api/disconnect",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(anonymous_write, timeout=3)
        self.assertEqual(blocked.exception.code, 401)
        blocked.exception.close()

        signed_write = urllib.request.Request(
            self.base + "/api/disconnect",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cookie": "__session=valid-clerk-session-token",
            },
        )
        with urllib.request.urlopen(signed_write, timeout=3) as response:
            self.assertTrue(json.loads(response.read())["ok"])

    def test_cross_site_writes_are_refused_even_with_the_cookie(self) -> None:
        self._enable_clerk("valid-clerk-session-token")
        forged = urllib.request.Request(
            self.base + "/api/home",
            data=b'{"confirm_motion": true}',
            headers={
                "Content-Type": "application/json",
                "Cookie": "__session=valid-clerk-session-token",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(forged, timeout=3)
        self.assertEqual(blocked.exception.code, 401)
        blocked.exception.close()

    def test_the_dashboard_shell_stays_public_so_sign_in_can_load(self) -> None:
        self._enable_clerk("valid-clerk-session-token")
        for path in ("/", "/?redirect=1"):
            with urllib.request.urlopen(self.base + path, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("clerkSignIn", response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
