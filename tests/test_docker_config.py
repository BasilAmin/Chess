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

    def test_run_script_passes_devices_mounts_data_and_clerk_keys(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn('--volume "$ROOT/config.json:/app/config.json:ro"', script)
        self.assertIn('--volume "$ROOT/data:/app/data"', script)
        self.assertIn("CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY", script)
        self.assertIn('RUN_ARGS+=(--device "$SERIAL_DEVICE"', script)
        self.assertIn('RUN_ARGS+=(--device "$I2C_DEVICE")', script)

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
        for path in (ROOT / "scripts" / "install_pi.sh", ROOT / "run.sh"):
            self.assertTrue(path.exists())
            self.assertTrue(path.stat().st_mode & 0o111)

    def test_pi_installer_uses_the_current_run_script_deployment(self) -> None:
        script = (ROOT / "scripts" / "install_pi.sh").read_text(encoding="utf-8")
        self.assertIn('build -t "${CHESS_GANTRY_IMAGE:-chess:latest}"', script)
        self.assertIn("./run.sh", script)
        self.assertNotIn("docker-compose.pi.yml", script)

    def test_run_script_builds_and_runs_the_image(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn('"${DOCKER[@]}" build -t "$IMAGE" .', script)
        self.assertIn('"${DOCKER[@]}" run --rm', script)

    def test_config_serial_path_matches_container_device(self) -> None:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["serial"]["port"], "/dev/ttyUSB0")

    def test_run_script_passes_i2c_bus_for_mcp23017(self) -> None:
        script = (ROOT / "run.sh").read_text(encoding="utf-8")
        self.assertIn("CHESS_GANTRY_I2C_DEVICE", script)
        self.assertIn("/dev/i2c-1", script)
        self.assertIn("CHESS_GANTRY_I2C_BUS=1", script)
        self.assertIn("CHESS_GANTRY_MCP23017_ADDRESS=0x20", script)
        self.assertIn('RUN_ARGS+=(--device "$I2C_DEVICE")', script)

    def test_image_dependency_set_includes_smbus2(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("smbus2", project)

    def test_dockerignore_excludes_large_local_directories(self) -> None:
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for value in (".git", ".venv", "node_modules", "data", "chicken/.pio"):
            self.assertIn(value, ignored)


if __name__ == "__main__":
    unittest.main()
