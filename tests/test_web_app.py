from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import re
import subprocess
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

from chess_gantry.clerk_auth import ClerkSettings, render_dashboard
from chess_gantry.config import AppConfig
from chess_gantry.controller import GantryController
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.models import BoardState
from chess_gantry.persistence import atomic_write_json
from chess_gantry.service import GantryService
from chess_gantry.lichess_open import OpenChallenge
from chess_gantry.lichess_station import LichessStation, STATION_CONFIRMATION
from tests.test_lichess_mirror import FakeClient
from chess_gantry.web_app import (
    HTML,
    GantryHTTPServer,
    RequestHandler,
    web_clerk_settings,
    web_bind_error,
    station_public_url,
)


ROOT = Path(__file__).resolve().parents[1]
CLERK_ENVIRONMENT = {"CLERK_PUBLISHABLE_KEY": "pk_test_Y2xlcmsuZXhhbXBsZS5jb20k"}


class FakeCamera:
    def __init__(self):
        self.source = "http://192.168.100.88:8080/video"
        self.enabled = False
        self.rotation = 0
        self.calibrated = False

    def configure(self, *, source, enabled, orientation=None, rotation=None):
        self.source = source
        self.enabled = enabled
        if rotation is not None:
            self.rotation = rotation
        return self.status()

    def status(self):
        return {
            "enabled": self.enabled,
            "running": self.enabled,
            "source": self.source,
            "orientation": "white_bottom",
            "rotation": self.rotation,
            "model": "gpt-5.6-sol",
            "error": None,
            "capture_error": None,
            "inference_error": None,
            "resolved_source": self.source,
            "status": "complete" if self.enabled else "idle",
            "confidence": None,
            "problems": [],
            "rows": (
                [
                    "rnbqkbnr",
                    "pppppppp",
                    "........",
                    "........",
                    "........",
                    "........",
                    "PPPPPPPP",
                    "RNBQKBNR",
                ]
                if self.enabled
                else None
            ),
            "pieces": [],
            "stable_observations": 2 if self.enabled else 0,
            "last_move": None,
            "fen": None,
            "turn": None,
            "frames": 1 if self.enabled else 0,
            "requests": 1 if self.enabled else 0,
            "paused": False,
            "last_frame_at": None,
            "frame_age_s": None,
            "stale": not self.enabled,
            "consecutive_errors": 0,
            "calibrated": self.calibrated,
            "calibration": None,
            "detection_mode": "calibrate_colors",
            "local": {
                "confidence": 0.0,
                "stable_frames": 0,
                "unresolved": [],
                "error": None,
                "last_at": None,
                "last_move": None,
                "frames": 0,
                "last_ms": None,
                "hz": 0.0,
                "profiles_ready": False,
                "profiles": {
                    color: {"type": piece_type, "sampled": False}
                    for color, piece_type in {
                        "green": "pawn",
                        "blue": "bishop",
                        "brown": "rook",
                        "pink": "knight",
                        "yellow": "king",
                        "orange": "queen",
                    }.items()
                },
                "detected": {},
            },
        }

    def calibrate(self, corners):
        self.calibrated = True
        return self.status()

    def calibrate_aruco(self):
        self.calibrated = True
        return self.status()

    def sample_piece_color(self, color, x, y):
        return {"color": color, "type": "pawn", "hue": 60}

    def clear_calibration(self):
        self.calibrated = False
        return self.status()

    def preview_jpeg(self):
        return b"\xff\xd8fake\xff\xd9"

    def raw_preview_jpeg(self):
        return b"\xff\xd8raw\xff\xd9"

    def probe(self, source):
        self.source = source
        return {
            "ok": True,
            "source": source,
            "resolved_source": "snapshot:http://phone/shot.jpg",
            "width": 1920,
            "height": 1080,
            "latency_ms": 42.0,
        }


class FakeGame:
    def __init__(self):
        self.started = None

    def running(self):
        return self.started is not None

    def start(self, **kwargs):
        self.started = kwargs
        return self.status()

    def stop(self):
        self.started = None
        return self.status()

    def status(self):
        return {
            "status": {
                "state": "waiting_camera" if self.started else "idle",
                "mode": self.started["mode"] if self.started else "idle",
            },
            "logs": "",
        }


class FakeOAuth:
    def __init__(self):
        self.connected = False
        self.started = []

    def token(self, optional=True):
        return "token" if self.connected else None

    def status(self):
        return {
            "connected": self.connected,
            "username": "test-user" if self.connected else None,
            "scopes": ["board:play"] if self.connected else [],
            "expires_at": None,
        }

    def begin(self, redirect_uri):
        self.started.append(redirect_uri)
        return "https://lichess.org/oauth?state=test"

    def complete(self, *, code, state):
        if code != "valid" or state != "test":
            raise ValidationError("invalid oauth callback")
        self.connected = True
        return self.status()

    def validate(self):
        if not self.connected:
            raise ValidationError("not connected")
        return self.status()

    def validate_game(self, game_id):
        if not self.connected:
            raise ValidationError("not connected")
        return {
            "ready": True,
            "game_id": game_id,
            "local_color": "white",
            "opponent": "Opponent",
            "white": "test-user",
            "black": "Opponent",
            "moves": 0,
            "result": "*",
        }

    def disconnect(self):
        self.connected = False


class FakeArena:
    def __init__(self):
        self.started = None
        self.manual = None

    def running(self):
        return self.started is not None

    def start(self, **kwargs):
        self.started = kwargs
        return self.status()

    def stop(self):
        self.started = None
        return self.status()

    def confirm_manual_action(self):
        self.manual = None
        return self.status()

    def status(self):
        return {
            "state": "running" if self.started else "idle",
            "error": None,
            "white": "chatgpt",
            "black": "claude",
            "style": "balanced",
            "delay_s": 1,
            "max_plies": 200,
            "fen": None,
            "rows": None,
            "turn": "white",
            "history": [],
            "ply": 0,
            "legal_move_count": 0,
            "in_check": False,
            "game_over": False,
            "result": None,
            "physical": bool(self.started and self.started.get("physical")),
            "manual_action": self.manual,
        }


class FakeCommissioning:
    def __init__(self):
        self.commissioned = True

    def status(self):
        return {
            "commissioned": self.commissioned,
            "reason": None if self.commissioned else "not_attested",
        }

    def attest(self, confirmation, git_commit="unknown"):
        self.commissioned = True
        return self.status()

    def clear(self):
        self.commissioned = False
        return self.status()


class FakeOpponent:
    def choose_move(self, board, *, style="balanced"):
        from chess_gantry.openai_opponent import OpponentMove

        return OpponentMove(
            uci="e2e4",
            rationale=f"A {style} move.",
            plan="Develop quickly.",
        )


class StubVerifier:
    def verify(self, token):
        if token != "valid":
            raise ValidationError("rejected")
        return {"sub": "user"}


class WebSecurityModeTests(unittest.TestCase):
    def test_address_in_use_error_is_actionable(self):
        error = web_bind_error("127.0.0.1", 8000, OSError(98, "in use"))
        self.assertIn("already in use", str(error))
        self.assertIn("--web-port 8001", str(error))

    def test_local_mode_requires_no_clerk(self):
        self.assertIsNone(
            web_clerk_settings(
                "127.0.0.1", allow_network=False, require_clerk=False, clerk=None
            )
        )

    def test_network_mode_requires_opt_in(self):
        with self.assertRaisesRegex(ValidationError, "--allow-network"):
            web_clerk_settings(
                "0.0.0.0", allow_network=False, require_clerk=False, clerk=None
            )

    def test_explicit_clerk_requires_key(self):
        with self.assertRaises(ConfigurationError):
            web_clerk_settings(
                "127.0.0.1", allow_network=False, require_clerk=True, clerk=None
            )

    def test_service_store_can_initialize_missing_state(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.loads((ROOT / "config.demo.json").read_text())
            config = AppConfig.from_mapping(raw)
            service = GantryService(
                config,
                root / "missing.json",
                root / "pending.json",
                root / "audit.jsonl",
            )
            if not service.store.path.exists():
                service.store.initialize(BoardState.standard(), overwrite=True)
            self.assertEqual(service.store.load().revision, 0)

    def test_station_public_url_prefers_explicit_origin(self):
        with mock.patch.dict(
            "os.environ", {"CHESS_GANTRY_PUBLIC_URL": "https://station.example"}
        ):
            self.assertEqual(
                station_public_url("0.0.0.0", 8000), "https://station.example"
            )


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        root = Path(self.temporary.name)
        raw = json.loads((ROOT / "config.demo.json").read_text())
        self.config = AppConfig.from_mapping(raw)
        state = root / "state.json"
        atomic_write_json(state, BoardState.standard().to_dict())
        service = GantryService(
            self.config, state, root / "pending.json", root / "audit.jsonl"
        )
        self.controller = GantryController(self.config, service, demo=True)
        RequestHandler.controller = self.controller
        self.server = GantryHTTPServer(("127.0.0.1", 0), RequestHandler)
        self.server.camera = FakeCamera()
        self.server.game = FakeGame()
        self.server.ai_arena = FakeArena()
        self.server.commissioning = FakeCommissioning()
        challenge = OpenChallenge(
            "game1234",
            "https://lichess.org/game1234?color=white",
            "https://lichess.org/game1234?color=black",
            "https://lichess.org/game1234",
        )
        self.server.station_game = LichessStation(
            root,
            self.config,
            demo=True,
            challenge_factory=lambda **kwargs: challenge,
            client=FakeClient([]),
            pgn_fetcher=lambda *args, **kwargs: '[Event "Station"]\n[Result "*"]\n\n*\n',
            sleep=lambda value: time.sleep(0.01),
        )
        self.server.openai_opponent = FakeOpponent()
        self.server.lichess_oauth = FakeOAuth()
        self.server.clerk_verifier = None
        self.server.dashboard_html = HTML
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.server.station_public_url = self.base

    def tearDown(self):
        if self.server.station_game.active():
            self.server.station_game.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.controller.disconnect()
        self.temporary.cleanup()

    def request(self, path, payload=None, headers=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            content = response.read()
            return response.status, json.loads(content) if content else None

    def test_dashboard_contains_full_game_camera_and_recovery_controls(self):
        with urllib.request.urlopen(self.base + "/") as response:
            html = response.read().decode()
        self.assertIn("Start full game", html)
        self.assertIn("192.168.100.88:8080", html)
        self.assertIn("OpenAI Sol", html)
        self.assertIn("Rotate phone image", html)
        self.assertIn("Pending transaction recovery", html)
        self.assertIn("Calibrate 4 corners", html)
        self.assertIn("Connect Lichess", html)
        self.assertIn("Scan ports", html)
        self.assertIn("Claude vs ChatGPT", html)
        self.assertIn("Mark commissioned", html)
        self.assertIn("Start Claude vs ChatGPT", html)
        self.assertIn("ArUco + color caps", html)
        self.assertIn("Green · pawns", html)
        self.assertIn("Blue · bishops", html)
        self.assertIn("Brown · rooks", html)
        self.assertIn("Pink · knights", html)
        self.assertIn("Yellow · kings", html)
        self.assertIn("Orange · queens", html)
        self.assertIn('id="flipBoard"', html)
        self.assertIn('id="moveHistory"', html)
        self.assertNotIn('id="phoneStream"', html)
        self.assertNotIn('id="cameraCanvas"', html)
        self.assertIn("auto:http://192.168.100.88:8080", html)
        self.assertIn('id="readyCamera"', html)
        self.assertIn("@media(max-width:680px)", html)
        self.assertNotIn("MCP23017", html)
        self.assertIn("ArUco + color caps", html)
        self.assertIn("Generate 4 references", html)
        self.assertIn("Auto-calibrate ArUco", html)

    def test_station_page_generates_fixed_color_lichess_qr_codes(self):
        with urllib.request.urlopen(self.base + "/station") as response:
            station_html = response.read().decode()
        self.assertIn("Token-free Lichess game", station_html)
        self.assertNotIn("gradient", station_html)
        self.assertNotIn("border-radius:18px", station_html)
        _, created = self.request(
            "/api/station/create",
            {"confirmation": STATION_CONFIRMATION, "base_url": self.base},
        )
        urls = created["result"]["join_urls"]
        self.assertEqual(urls["white"], "https://lichess.org/game1234?color=white")
        self.assertEqual(urls["black"], "https://lichess.org/game1234?color=black")
        with urllib.request.urlopen(
            self.base + "/api/station/qr?seat=white"
        ) as response:
            self.assertEqual(response.headers.get_content_type(), "image/svg+xml")
            self.assertIn(b"<svg", response.read())
        self.server.station_game.stop()
        _, next_game = self.request(
            "/api/station/create",
            {"confirmation": STATION_CONFIRMATION, "base_url": "ignored"},
        )
        self.assertEqual(next_game["result"]["game_id"], "game1234")

    def test_station_reservation_blocks_other_gantry_modes(self):
        self.request(
            "/api/station/create",
            {"confirmation": STATION_CONFIRMATION},
        )
        for path, payload in (
            ("/api/controller/connect", {}),
            ("/api/setup/home", {"confirmation": "SETUP AREA CLEAR"}),
            ("/api/game/start", {"mode": "local", "confirm_motion": True}),
            (
                "/api/arena/start",
                {
                    "white": "chatgpt",
                    "black": "claude",
                    "physical": True,
                    "confirm_motion": True,
                },
            ),
        ):
            request = urllib.request.Request(
                self.base + path,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with (
                self.subTest(path=path),
                self.assertRaises(urllib.error.HTTPError) as raised,
            ):
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 409)

    def test_every_javascript_dom_reference_exists_in_dashboard(self):
        ids = set(re.findall(r'id="([A-Za-z][A-Za-z0-9_-]*)"', HTML))
        references = set(re.findall(r"\$\('([A-Za-z][A-Za-z0-9_-]*)'\)", HTML))
        self.assertEqual(references - ids, set())

    def test_embedded_dashboard_javascript_is_syntactically_valid(self):
        scripts = re.findall(r"<script>(.*?)</script>", HTML, flags=re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2)
        for script in scripts:
            result = subprocess.run(
                ["node", "--check", "-"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_dashboard_wires_all_critical_action_routes(self):
        for route in (
            "/api/camera/configure",
            "/api/camera/probe",
            "/api/camera/calibrate",
            "/api/camera/calibrate-aruco",
            "/api/camera/color/sample",
            "/api/camera/markers/generate",
            "/api/camera/calibration/clear",
            "/api/controller/connect",
            "/api/controller/disconnect",
            "/api/controller/home",
            "/api/controller/stop",
            "/api/game/start",
            "/api/game/stop",
            "/api/analysis/openai",
            "/api/arena/start",
            "/api/arena/stop",
            "/api/arena/confirm-manual",
            "/api/commissioning/attest",
            "/api/commissioning/clear",
            "/api/reconcile/apply",
            "/api/reconcile/discard",
            "/api/lichess/oauth/start",
            "/api/lichess/disconnect",
            "/api/lichess/game/validate",
            "/api/setup/diagnostics",
            "/api/setup/endstops",
            "/api/setup/home",
            "/api/setup/movement",
            "/api/setup/magnet",
            "/api/setup/centers",
            "/api/setup/combined",
        ):
            self.assertIn(route, HTML)

    def test_dashboard_contains_all_setup_steps_and_confirmation(self):
        for text in (
            "1. Diagnostics",
            "2. Endstops",
            "3. Home",
            "4. Move 5 mm",
            "5. Pulse magnet",
            "6. Visit centers",
            "Run complete setup",
            "SETUP AREA CLEAR",
        ):
            self.assertIn(text, HTML)

    def test_setup_motion_endpoints_require_exact_confirmation(self):
        for route in ("home", "movement", "magnet", "centers", "combined"):
            request = urllib.request.Request(
                self.base + f"/api/setup/{route}",
                data=json.dumps({"confirmation": "wrong"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 409)

    def test_setup_diagnostics_and_endstops_update_controller_status(self):
        self.request("/api/controller/connect", {})
        _, diagnostics = self.request("/api/setup/diagnostics", {})
        self.assertTrue(diagnostics["result"]["setup"]["diagnostics"])
        _, endstops = self.request("/api/setup/endstops", {})
        self.assertTrue(endstops["result"]["setup"]["endstops"])

    def test_complete_setup_endpoint_runs_every_demo_step(self):
        self.request("/api/controller/connect", {})
        _, data = self.request(
            "/api/setup/combined", {"confirmation": "SETUP AREA CLEAR"}
        )
        for step in (
            "diagnostics",
            "endstops",
            "homed",
            "movement",
            "magnet",
            "centers",
        ):
            self.assertTrue(data["result"]["setup"][step])

    def test_dashboard_includes_all_four_game_modes_and_prerequisite_gating(self):
        for value in ("local", "openai", "lichess", "mirror"):
            self.assertIn(f'value="{value}"', HTML)
        self.assertIn("modeReady", HTML)
        self.assertIn("stable_observations", HTML)
        self.assertIn("cap.lichess", HTML)
        self.assertIn("Boolean(p)", HTML)
        self.assertIn("mode==='openai'", HTML)
        self.assertIn('id="opponentStyle"', HTML)

    def test_dashboard_displays_grid_and_machine_coordinates(self):
        self.assertIn("piece.grid.x", HTML)
        self.assertIn("piece.grid.y", HTML)
        self.assertIn("piece.machine_mm.x", HTML)
        self.assertIn("piece.machine_mm.y", HTML)

    def test_dashboard_exposes_snapshot_freshness_and_reconnect_errors(self):
        self.assertIn("last_frame_at", HTML)
        self.assertIn("camera.stale", HTML)
        self.assertIn("capture_error", HTML)
        self.assertIn("inference_error", HTML)
        self.assertIn("consecutive_errors", self.server.camera.status())
        self.assertIn("data.controller.last_error", HTML)

    def test_full_status_includes_camera_game_board_and_pending(self):
        _, data = self.request("/api/full-status")
        self.assertEqual(data["camera"]["model"], "gpt-5.6-sol")
        self.assertEqual(data["game"]["status"]["state"], "idle")
        self.assertEqual(data["board"]["revision"], 0)
        self.assertIsNone(data["pending"])
        self.assertIn("openai", data["capabilities"])
        self.assertIn("anthropic", data["capabilities"])
        self.assertIn("lichess", data["capabilities"])
        self.assertIn("arena", data)
        self.assertTrue(data["commissioning"]["commissioned"])
        self.assertIn("ports", data)
        self.assertTrue(all(value["likely_printer"] for value in data["ports"]))
        self.assertFalse(data["lichess"]["connected"])

    def test_camera_configuration_and_preview(self):
        self.request(
            "/api/camera/configure",
            {
                "source": "snapshot:http://phone/shot.jpg",
                "orientation": "white_bottom",
                "rotation": 90,
                "enabled": True,
            },
        )
        _, data = self.request("/api/camera/status")
        self.assertTrue(data["camera"]["running"])
        self.assertEqual(data["camera"]["rotation"], 90)
        with urllib.request.urlopen(self.base + "/api/camera/frame") as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertTrue(response.read().startswith(b"\xff\xd8"))

    def test_camera_probe_and_raw_preview_do_not_require_sol(self):
        _, data = self.request(
            "/api/camera/probe", {"source": "auto:http://phone:8080"}
        )
        self.assertEqual(data["result"]["width"], 1920)
        self.assertEqual(data["result"]["height"], 1080)
        with urllib.request.urlopen(self.base + "/api/camera/raw-frame") as response:
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")
            self.assertEqual(response.read(), b"\xff\xd8raw\xff\xd9")

    def test_browser_frame_upload_reaches_camera_manager(self):
        received = []

        def ingest(payload):
            received.append(payload)
            return self.server.camera.status()

        self.server.camera.ingest_browser_jpeg = ingest
        request = urllib.request.Request(
            self.base + "/api/camera/browser-frame",
            data=b"jpeg-payload",
            headers={"Content-Type": "image/jpeg"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(received, [b"jpeg-payload"])

    def test_camera_calibration_and_clear_endpoints(self):
        corners = [
            {"x": 0.1, "y": 0.1},
            {"x": 0.9, "y": 0.1},
            {"x": 0.9, "y": 0.9},
            {"x": 0.1, "y": 0.9},
        ]
        _, data = self.request("/api/camera/calibrate", {"corners": corners})
        self.assertTrue(data["result"]["calibrated"])
        _, data = self.request("/api/camera/calibration/clear", {})
        self.assertFalse(data["result"]["calibrated"])

    def test_aruco_and_color_sampling_endpoints(self):
        _, data = self.request("/api/camera/calibrate-aruco", {})
        self.assertTrue(data["result"]["calibrated"])
        _, data = self.request(
            "/api/camera/color/sample",
            {"color": "green", "x": 0.5, "y": 0.5},
        )
        self.assertEqual(data["result"]["color"], "green")

    def test_marker_generation_returns_downloadable_pngs(self):
        _, data = self.request("/api/camera/markers/generate", {})
        self.assertEqual(len(data["result"]["files"]), 4)
        first = data["result"]["files"][0]
        self.assertTrue(first["name"].endswith(".png"))
        with urllib.request.urlopen(self.base + first["url"]) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertTrue(response.read().startswith(b"\x89PNG"))

    def test_game_start_disconnects_manual_controller_and_routes_settings(self):
        self.server.lichess_oauth.connected = True
        self.request("/api/controller/connect", {})
        self.request(
            "/api/game/start",
            {
                "mode": "lichess",
                "game_id": "game1234",
                "local_color": "white",
                "confirm_motion": True,
                "serial_port": "DEMO",
                "serial_baudrate": "250000",
            },
        )
        self.assertFalse(self.controller.connected)
        self.assertEqual(self.server.game.started["mode"], "lichess")
        self.assertEqual(self.server.game.started["serial_port"], "DEMO")
        self.assertEqual(self.server.game.started["serial_baudrate"], 250000)

    def test_openai_game_mode_routes_style_without_lichess(self):
        self.request(
            "/api/game/start",
            {
                "mode": "openai",
                "local_color": "white",
                "confirm_motion": True,
                "opponent_style": "creative",
            },
        )
        self.assertEqual(self.server.game.started["mode"], "openai")
        self.assertEqual(self.server.game.started["opponent_style"], "creative")

    def test_commissioning_attestation_and_clear_routes(self):
        self.server.commissioning.commissioned = False
        _, data = self.request(
            "/api/commissioning/attest",
            {"confirmation": "I CONFIRM PHYSICAL SETUP IS SAFE"},
        )
        self.assertTrue(data["result"]["commissioned"])
        _, data = self.request("/api/commissioning/clear", {})
        self.assertFalse(data["result"]["commissioned"])

    def test_physical_arena_requires_commissioning(self):
        self.server.commissioning.commissioned = False
        request = urllib.request.Request(
            self.base + "/api/arena/start",
            data=json.dumps(
                {
                    "white": "chatgpt",
                    "black": "claude",
                    "physical": True,
                    "confirm_motion": True,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 409)

    def test_arena_start_stop_and_manual_confirmation_routes(self):
        _, data = self.request(
            "/api/arena/start",
            {
                "white": "chatgpt",
                "black": "claude",
                "style": "balanced",
                "delay_s": 1,
                "max_plies": 20,
                "physical": True,
                "confirm_motion": True,
            },
        )
        self.assertEqual(data["result"]["state"], "running")
        self.assertIsNone(self.server.ai_arena.started["max_plies"])
        self.server.ai_arena.manual = {"uci": "e4d5"}
        _, data = self.request(
            "/api/arena/confirm-manual",
            {"confirmation": "AI MOVE COMPLETED"},
        )
        self.assertIsNone(data["result"]["manual_action"])
        _, data = self.request("/api/arena/stop", {})
        self.assertEqual(data["result"]["state"], "idle")

    def test_lichess_game_validation_route_returns_player_side(self):
        self.server.lichess_oauth.connected = True
        _, data = self.request("/api/lichess/game/validate", {"game_id": "game1234"})
        self.assertTrue(data["result"]["ready"])
        self.assertEqual(data["result"]["local_color"], "white")

    def test_openai_analysis_endpoint_returns_structured_legal_move(self):
        original = self.server.game.status
        import chess

        self.server.game.status = lambda: {
            "status": {"fen": chess.Board().fen()},
            "logs": "",
        }
        try:
            _, data = self.request("/api/analysis/openai", {"style": "aggressive"})
            self.assertEqual(data["result"]["uci"], "e2e4")
            self.assertIn("aggressive", data["result"]["rationale"])
        finally:
            self.server.game.status = original

    def test_controller_connect_accepts_selected_port_and_baud(self):
        _, data = self.request(
            "/api/controller/connect", {"port": "DEMO", "baudrate": "250000"}
        )
        self.assertTrue(data["result"]["connected"])
        self.assertEqual(data["result"]["baudrate"], 250000)

    def test_lichess_oauth_start_returns_official_authorization_url(self):
        _, data = self.request("/api/lichess/oauth/start", {})
        self.assertTrue(data["result"]["url"].startswith("https://lichess.org/oauth"))
        self.assertEqual(
            self.server.lichess_oauth.started,
            [f"http://127.0.0.1:{self.server.server_port}/auth/lichess/callback"],
        )

    def test_lichess_oauth_callback_connects_and_redirects(self):
        request = urllib.request.Request(
            self.base + "/auth/lichess/callback?code=valid&state=test"
        )
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(request) as response:
            self.assertEqual(response.geturl(), self.base + "/")
        self.assertTrue(self.server.lichess_oauth.connected)

    def test_lichess_disconnect_forgets_session_token(self):
        self.server.lichess_oauth.connected = True
        _, data = self.request("/api/lichess/disconnect", {})
        self.assertFalse(data["result"]["connected"])

    def test_cross_site_post_is_rejected_without_clerk(self):
        request = urllib.request.Request(
            self.base + "/api/controller/connect",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 401)

    def test_recovery_endpoint_requires_exact_confirmation(self):
        request = urllib.request.Request(
            self.base + "/api/reconcile/discard",
            data=json.dumps({"confirmation": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 409)

    def test_unknown_get_and_post_routes_return_404(self):
        for method in (None, b"{}"):
            request = urllib.request.Request(
                self.base + "/api/does-not-exist",
                data=method,
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request)
            self.assertEqual(raised.exception.code, 404)

    def test_camera_frame_before_capture_is_controlled_conflict(self):
        self.server.camera.preview_jpeg = lambda: (_ for _ in ()).throw(
            ValidationError("no camera frame")
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base + "/api/camera/frame")
        self.assertEqual(raised.exception.code, 409)

    def test_clerk_mode_guards_api(self):
        self.server.clerk_verifier = StubVerifier()
        settings = ClerkSettings.require_from_environment(CLERK_ENVIRONMENT)
        self.server.dashboard_html = render_dashboard(HTML, settings)
        request = urllib.request.Request(self.base + "/api/full-status")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(raised.exception.code, 401)
        request.add_header("Cookie", "__session=valid")
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
