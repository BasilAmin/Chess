from __future__ import annotations

from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerConfigurationTests(unittest.TestCase):
    def test_dockerfile_uses_fedora_builder_and_scratch_distroless_runtime(
        self,
    ) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM fedora:${FEDORA_VERSION} AS builder", dockerfile)
        self.assertIn("FROM scratch AS runtime", dockerfile)
        self.assertIn("uv export --frozen --no-dev", dockerfile)
        self.assertIn("COPY --from=builder /rootfs/ /", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/bin/python3",', dockerfile)
        self.assertIn('test ! -e "${ROOTFS}/usr/bin/sh"', dockerfile)

    def test_run_script_passes_devices_mounts_data_and_clerk_key(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn('--volume "$ROOT/config.json:/app/config.json:ro"', script)
        self.assertIn('--volume "$ROOT/data:/app/data"', script)
        self.assertIn("CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY", script)
        self.assertNotIn("CLERK_SECRET_KEY", script)
        self.assertNotIn("sk_test_", script)
        self.assertIn('RUN_ARGS+=(--device "$SERIAL_DEVICE"', script)
        self.assertNotIn("I2C_DEVICE", script)

    def test_run_script_passes_lichess_token_only_when_set(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn("if [[ -n ${LICHESS_TOKEN:-} ]]", script)
        self.assertIn('RUN_ARGS+=(--env "LICHESS_TOKEN=$LICHESS_TOKEN")', script)

    def test_python_entrypoint_runs_clerk_gated_ui_without_shell(self) -> None:
        entrypoint = (ROOT / "docker" / "bin" / "chess-gantry-docker").read_text(
            encoding="utf-8"
        )
        self.assertIn('"--allow-network"', entrypoint)
        self.assertIn("CHESS_GANTRY_WEB_HOST", entrypoint)
        self.assertNotIn("--auth-token", entrypoint)
        self.assertIn("this image is distroless and ships no shell", entrypoint)

    def test_pi_scripts_exist_and_are_executable(self) -> None:
        for path in (
            ROOT / "scripts" / "install_pi.sh",
            ROOT / "scripts" / "mirror_lichess.sh",
            ROOT / "run.sh",
        ):
            self.assertTrue(path.exists())
            self.assertTrue(path.stat().st_mode & 0o111)

    def test_pi_installer_uses_the_current_run_script_deployment(self) -> None:
        script = (ROOT / "scripts" / "install_pi.sh").read_text(encoding="utf-8")
        self.assertIn('build -t "${CHESS_GANTRY_IMAGE:-chess:latest}"', script)
        self.assertIn("./run.sh", script)
        self.assertNotIn("docker-compose.pi.yml", script)
        self.assertIn("vision requires a 64-bit Raspberry Pi OS", script)

    def test_run_script_builds_and_runs_the_image(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn('"${DOCKER[@]}" build -t "$IMAGE" .', script)
        self.assertIn('"${DOCKER[@]}" run --rm', script)

    def test_config_serial_path_matches_container_device(self) -> None:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["serial"]["port"], "/dev/ttyUSB0")

    def test_run_script_has_no_reed_or_i2c_runtime(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertNotIn("CHESS_GANTRY_I2C", script)
        self.assertNotIn("MCP23017", script)

    def test_image_dependency_set_includes_sol_and_camera_runtime(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("smbus2", project)
        self.assertIn("opencv-python-headless", project)
        self.assertIn("numpy", project)
        self.assertIn("openai", project)
        self.assertIn("anthropic", project)
        self.assertIn("pydantic", project)

    def test_distroless_image_verifies_vision_runtime(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        verifier = (ROOT / "docker" / "bin" / "verify-runtime").read_text(
            encoding="utf-8"
        )
        self.assertIn("libstdc++ libgcc", dockerfile)
        self.assertIn("CHESS_GANTRY_DISTROLESS=1", dockerfile)
        self.assertIn('"cv2"', verifier)
        self.assertNotIn("ArucoDetector", verifier)
        self.assertIn("FFMPEG", verifier)
        self.assertIn('"openai"', verifier)
        self.assertIn('"anthropic"', verifier)
        self.assertIn('"chess_gantry.local_vision"', verifier)
        self.assertIn('"chess_gantry.lichess_mirror"', verifier)

    def test_run_script_supports_network_and_v4l2_cameras(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn("CHESS_GANTRY_CAMERA_SOURCE", script)
        self.assertIn("CHESS_GANTRY_VIDEO_DEVICE", script)
        self.assertIn('--device "$VIDEO_DEVICE:/dev/video0"', script)
        self.assertIn("CHESS_GANTRY_CAMERA_ENABLED=1", script)
        self.assertIn("auto:http://192.168.100.88:8080", script)
        self.assertIn("OPENAI_API_KEY", script)

    def test_dockerignore_excludes_large_local_directories(self) -> None:
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for value in (".git", ".venv", "node_modules", "data", "chicken/.pio"):
            self.assertIn(value, ignored)


if __name__ == "__main__":
    unittest.main()
