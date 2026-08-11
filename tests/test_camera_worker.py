from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

import numpy

from chess_gantry.errors import ValidationError
from chess_gantry.vision import (
    BoardTranscription,
    SolVisionManager,
    _AutoSource,
    _MjpegSource,
    probe_camera_source,
)


def result():
    return BoardTranscription.model_validate(
        {
            "status": "complete",
            "confidence": {
                "board_detection": "high",
                "grid_mapping": "high",
                "piece_recognition": "high",
            },
            "rows": {
                "row_1": "rnbqkbnr",
                "row_2": "pppppppp",
                "row_3": "........",
                "row_4": "........",
                "row_5": "........",
                "row_6": "........",
                "row_7": "PPPPPPPP",
                "row_8": "RNBQKBNR",
            },
            "problems": [],
        }
    )


class FrameSource:
    def __init__(self, shape=(480, 640, 3), fail=False):
        self.shape = shape
        self.fail = fail
        self.reads = 0
        self.closed = False

    def read(self):
        if self.fail:
            raise ValidationError("source failed")
        self.reads += 1
        return numpy.full(self.shape, self.reads % 255, dtype=numpy.uint8)

    def close(self):
        self.closed = True


class BlockingTranscriber:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def transcribe(self, jpeg):
        self.calls += 1
        self.started.set()
        self.release.wait(3)
        return result()


class CameraWorkerTests(unittest.TestCase):
    def test_mjpeg_reader_extracts_consecutive_jpeg_frames(self):
        import cv2

        frames = []
        for value in (40, 180):
            ok, encoded = cv2.imencode(
                ".jpg", numpy.full((20, 30, 3), value, dtype=numpy.uint8)
            )
            self.assertTrue(ok)
            frames.append(encoded.tobytes())
        payload = (
            b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n"
            + frames[0]
            + b"\r\n--boundary\r\nContent-Type: image/jpeg\r\n\r\n"
            + frames[1]
            + b"\r\n"
        )

        class Headers:
            def get(self, name, default=None):
                return "multipart/x-mixed-replace; boundary=boundary"

        class Response:
            headers = Headers()

            def __init__(self):
                self.offset = 0

            def read(self, count):
                chunk = payload[self.offset : self.offset + 900]
                self.offset += len(chunk)
                return chunk

            def read1(self, count):
                return self.read(count)

            def close(self):
                return None

        source = _MjpegSource(
            "http://phone/video", opener=lambda req, timeout: Response()
        )
        try:
            first = source.read()
            second = source.read()
            self.assertEqual(first.shape, (20, 30, 3))
            self.assertEqual(second.shape, (20, 30, 3))
            self.assertLess(float(first.mean()), float(second.mean()))
        finally:
            source.close()

    def test_probe_returns_dimensions_latency_and_jpeg_without_inference(self):
        source = FrameSource((720, 1280, 3))
        value = probe_camera_source("fake", lambda name: source)
        self.assertEqual((value["width"], value["height"]), (1280, 720))
        self.assertTrue(value["jpeg"].startswith(b"\xff\xd8"))
        self.assertGreaterEqual(value["latency_ms"], 0)
        self.assertTrue(source.closed)

    def test_browser_probe_uses_one_snapshot_health_check(self):
        opened = []
        source = FrameSource((1080, 1920, 3))

        def factory(name):
            opened.append(name)
            return source

        value = probe_camera_source("browser:http://phone:8080", factory)
        self.assertEqual(opened, ["snapshot:http://phone:8080/shot.jpg"])
        self.assertEqual(value["source"], "browser:http://phone:8080")
        self.assertEqual(
            value["resolved_source"], "snapshot:http://phone:8080/shot.jpg"
        )

    def test_capture_continues_while_sol_inference_is_blocked(self):
        source = FrameSource()
        transcriber = BlockingTranscriber()
        manager = SolVisionManager(
            source="fake",
            interval_s=1,
            transcriber=transcriber,
            source_factory=lambda name: source,
        )
        try:
            manager.configure(source="fake", enabled=True)
            self.assertTrue(transcriber.started.wait(2))
            before = manager.status()["frames"]
            time.sleep(0.8)
            after = manager.status()["frames"]
            self.assertGreater(after, before)
            self.assertTrue(manager.preview_jpeg().startswith(b"\xff\xd8"))
        finally:
            transcriber.release.set()
            manager.close()

    def test_capture_failure_marks_frame_stale_and_preserves_last_preview(self):
        source = FrameSource()
        manager = SolVisionManager(
            source="fake",
            transcriber=BlockingTranscriber(),
            source_factory=lambda name: source,
        )
        manager._transcriber.release.set()
        try:
            manager.configure(source="fake", enabled=True)
            deadline = time.monotonic() + 2
            while manager.status()["frames"] == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreater(manager.status()["frames"], 0)
            preview = manager.preview_jpeg()
            source.fail = True
            time.sleep(0.8)
            status = manager.status()
            self.assertFalse(status["running"])
            self.assertIsNotNone(status["capture_error"])
            self.assertEqual(manager.preview_jpeg(), preview)
        finally:
            manager.close()

    def test_calibrated_plan_view_is_square_1024(self):
        import cv2

        source = FrameSource((720, 1280, 3))
        transcriber = BlockingTranscriber()
        transcriber.release.set()
        manager = SolVisionManager(
            source="fake",
            transcriber=transcriber,
            source_factory=lambda name: source,
        )
        try:
            manager.calibrate(
                [
                    {"x": 0.1, "y": 0.1},
                    {"x": 0.9, "y": 0.1},
                    {"x": 0.9, "y": 0.9},
                    {"x": 0.1, "y": 0.9},
                ]
            )
            manager.configure(source="fake", enabled=True)
            deadline = time.monotonic() + 2
            while manager.status()["frames"] == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
            image = cv2.imdecode(
                numpy.frombuffer(manager.preview_jpeg(), dtype=numpy.uint8),
                cv2.IMREAD_COLOR,
            )
            self.assertEqual(image.shape[:2], (1024, 1024))
        finally:
            manager.close()

    def test_browser_jpeg_ingest_updates_raw_and_plan_frames(self):
        import cv2

        transcriber = BlockingTranscriber()
        transcriber.release.set()
        manager = SolVisionManager(
            source="browser:http://phone", transcriber=transcriber
        )
        try:
            manager.configure(source="browser:http://phone", enabled=True)
            ok, encoded = cv2.imencode(
                ".jpg", numpy.zeros((720, 1280, 3), dtype=numpy.uint8)
            )
            self.assertTrue(ok)
            status = manager.ingest_browser_jpeg(encoded.tobytes())
            self.assertEqual(status["frames"], 1)
            self.assertTrue(status["running"])
            self.assertFalse(status["stale"])
            self.assertTrue(manager.raw_preview_jpeg().startswith(b"\xff\xd8"))
            self.assertTrue(manager.preview_jpeg().startswith(b"\xff\xd8"))
        finally:
            manager.close()

    def test_browser_ingest_rejects_invalid_or_oversized_payload(self):
        manager = SolVisionManager(transcriber=BlockingTranscriber())
        try:
            with self.assertRaises(ValidationError):
                manager.ingest_browser_jpeg(b"not-jpeg")
            with self.assertRaises(ValidationError):
                manager.ingest_browser_jpeg(b"x" * 8_000_001)
        finally:
            manager.close()

    def test_auto_source_prefers_snapshot_and_falls_back_to_video(self):
        import chess_gantry.vision as vision

        original = vision.open_frame_source
        opened = []

        def fake_open(source):
            opened.append(source)
            if source.startswith("snapshot:"):
                return FrameSource(fail=True)
            return FrameSource()

        vision.open_frame_source = fake_open
        source = _AutoSource("http://phone:8080")
        try:
            frame = source.read()
            self.assertEqual(frame.shape, (480, 640, 3))
            self.assertEqual(
                opened,
                [
                    "snapshot:http://phone:8080/shot.jpg",
                    "http://phone:8080/video",
                ],
            )
            self.assertEqual(source.resolved_source, "http://phone:8080/video")
        finally:
            source.close()
            vision.open_frame_source = original

    def test_snapshot_source_retries_transient_failures(self):
        import chess_gantry.vision as vision

        original = vision.urlopen
        calls = []

        class Headers:
            def get_content_type(self):
                return "image/jpeg"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                import cv2

                ok, encoded = cv2.imencode(
                    ".jpg", numpy.zeros((20, 30, 3), dtype=numpy.uint8)
                )
                if not ok:
                    raise AssertionError("test JPEG encoding failed")
                return encoded.tobytes()

        def fake_urlopen(request, timeout):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise TimeoutError("temporary phone stall")
            return Response()

        vision.urlopen = fake_urlopen
        try:
            frame = vision._SnapshotSource("http://phone/shot.jpg").read()
            self.assertEqual(frame.shape, (20, 30, 3))
            self.assertEqual(len(calls), 3)
            self.assertTrue(all("_=" in value for value in calls))
        finally:
            vision.urlopen = original


if __name__ == "__main__":
    unittest.main()
