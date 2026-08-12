from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
import json

from .errors import ValidationError
from .persistence import atomic_write_json, read_json


TYPE_TO_COLOR = {
    "pawn": "green",
    "bishop": "blue",
    "rook": "brown",
    "knight": "pink",
    "king": "yellow",
    "queen": "orange",
}
COLOR_TO_TYPE = {value: key for key, value in TYPE_TO_COLOR.items()}
TYPE_TO_SYMBOL = {
    "pawn": "P",
    "bishop": "B",
    "rook": "R",
    "knight": "N",
    "king": "K",
    "queen": "Q",
}
DEFAULT_PROFILES = {
    "green": {
        "hue": 60,
        "hue_tolerance": 18,
        "sat_min": 70,
        "val_min": 45,
        "val_max": 255,
    },
    "blue": {
        "hue": 110,
        "hue_tolerance": 18,
        "sat_min": 70,
        "val_min": 45,
        "val_max": 255,
    },
    "brown": {
        "hue": 10,
        "hue_tolerance": 9,
        "sat_min": 45,
        "val_min": 20,
        "val_max": 145,
    },
    "pink": {
        "hue": 165,
        "hue_tolerance": 18,
        "sat_min": 60,
        "val_min": 70,
        "val_max": 255,
    },
    "yellow": {
        "hue": 30,
        "hue_tolerance": 8,
        "sat_min": 70,
        "val_min": 80,
        "val_max": 255,
    },
    "orange": {
        "hue": 12,
        "hue_tolerance": 7,
        "sat_min": 85,
        "val_min": 165,
        "val_max": 255,
    },
}


@dataclass(frozen=True)
class LocalObservation:
    types: Mapping[int, str]
    confidence: float
    unresolved: tuple[str, ...]
    square_scores: Mapping[str, Mapping[str, float]]

    @property
    def complete(self) -> bool:
        return not self.unresolved


class ColorProfiles:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.values = json.loads(json.dumps(DEFAULT_PROFILES))
        self.sampled: set[str] = set()
        if path is not None and path.exists():
            raw = read_json(path)
            for color, value in raw.get("profiles", {}).items():
                if color in self.values:
                    self.values[color] = dict(value)
            self.sampled = set(raw.get("sampled", ())) & set(self.values)

    def sample(self, frame: Any, color: str, x: float, y: float) -> dict[str, Any]:
        cv2, numpy = _modules()
        if color not in COLOR_TO_TYPE:
            raise ValidationError(f"unsupported piece color: {color}")
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValidationError("color sample coordinate must be normalized 0..1")
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        height, width = hsv.shape[:2]
        cx, cy = int(x * width), int(y * height)
        radius = max(5, min(width, height) // 80)
        region = hsv[
            max(0, cy - radius) : min(height, cy + radius + 1),
            max(0, cx - radius) : min(width, cx + radius + 1),
        ]
        pixels = region.reshape(-1, 3)
        pixels = pixels[(pixels[:, 1] >= 40) & (pixels[:, 2] >= 25)]
        if len(pixels) < 20:
            raise ValidationError(
                "color sample does not contain enough saturated pixels"
            )
        hue = int(numpy.median(pixels[:, 0]))
        saturation = int(numpy.percentile(pixels[:, 1], 20))
        value_low = int(numpy.percentile(pixels[:, 2], 10))
        value_high = int(numpy.percentile(pixels[:, 2], 95))
        profile = {
            "hue": hue,
            "hue_tolerance": 12,
            "sat_min": max(35, saturation - 25),
            "val_min": max(15, value_low - 25),
            "val_max": min(255, value_high + 35),
        }
        if color == "brown":
            profile["val_max"] = min(155, profile["val_max"])
        elif color == "orange":
            profile["val_min"] = max(145, profile["val_min"])
        self.values[color] = profile
        self.sampled.add(color)
        self.save()
        return {"color": color, "type": COLOR_TO_TYPE[color], **profile}

    def save(self) -> None:
        if self.path is not None:
            atomic_write_json(
                self.path,
                {
                    "schema_version": 1,
                    "profiles": self.values,
                    "sampled": sorted(self.sampled),
                },
            )

    def status(self) -> dict[str, Any]:
        return {
            color: {
                "type": COLOR_TO_TYPE[color],
                "sampled": color in self.sampled,
                **value,
            }
            for color, value in self.values.items()
        }

    @property
    def ready(self) -> bool:
        return self.sampled == set(COLOR_TO_TYPE)


def _modules() -> tuple[Any, Any]:
    import cv2
    import numpy

    return cv2, numpy


def _hue_mask(hsv: Any, profile: Mapping[str, int]) -> Any:
    cv2, numpy = _modules()
    hue = int(profile["hue"])
    tolerance = int(profile["hue_tolerance"])
    lower, upper = hue - tolerance, hue + tolerance
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    eligible = (
        (saturation >= int(profile["sat_min"]))
        & (value >= int(profile["val_min"]))
        & (value <= int(profile["val_max"]))
    )
    channel = hsv[:, :, 0]
    if lower < 0:
        hue_match = (channel >= lower + 180) | (channel <= upper)
    elif upper >= 180:
        hue_match = (channel >= lower) | (channel <= upper - 180)
    else:
        hue_match = (channel >= lower) & (channel <= upper)
    return (hue_match & eligible).astype(numpy.uint8) * 255


def detect_colored_board(
    frame: Any,
    profiles: ColorProfiles,
    *,
    orientation: str,
    minimum_fraction: float = 0.012,
    ambiguity_ratio: float = 1.35,
) -> LocalObservation:
    import chess

    cv2, numpy = _modules()
    if frame is None or frame.shape[0] < 256 or frame.shape[1] < 256:
        raise ValidationError("local detector requires a calibrated board image")
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    masks = {
        color: cv2.morphologyEx(
            _hue_mask(hsv, profile),
            cv2.MORPH_OPEN,
            numpy.ones((5, 5), dtype=numpy.uint8),
        )
        for color, profile in profiles.values.items()
    }
    height, width = hsv.shape[:2]
    observed = {}
    unresolved = []
    square_scores = {}
    accepted_confidence = []
    for image_row in range(8):
        for image_column in range(8):
            x0, x1 = round(image_column * width / 8), round(
                (image_column + 1) * width / 8
            )
            y0, y1 = round(image_row * height / 8), round((image_row + 1) * height / 8)
            margin_x, margin_y = max(2, (x1 - x0) // 10), max(2, (y1 - y0) // 10)
            area = max(1, (x1 - x0 - 2 * margin_x) * (y1 - y0 - 2 * margin_y))
            scores = {
                color: float(
                    cv2.countNonZero(
                        mask[
                            y0 + margin_y : y1 - margin_y, x0 + margin_x : x1 - margin_x
                        ]
                    )
                )
                / area
                for color, mask in masks.items()
            }
            square = _image_square(image_row, image_column, orientation)
            square_name = chess.square_name(square)
            square_scores[square_name] = {
                key: round(value, 4) for key, value in scores.items()
            }
            ranked = sorted(scores.items(), key=lambda value: value[1], reverse=True)
            best_color, best = ranked[0]
            second = ranked[1][1]
            if best < minimum_fraction:
                continue
            if second > 0 and best / second < ambiguity_ratio:
                unresolved.append(square_name)
                continue
            piece_type = COLOR_TO_TYPE[best_color]
            observed[square] = piece_type
            accepted_confidence.append(
                min(1.0, best / max(minimum_fraction * 4, 0.001))
            )
    confidence = (
        sum(accepted_confidence) / len(accepted_confidence)
        if accepted_confidence
        else 0.0
    )
    return LocalObservation(
        types=observed,
        confidence=round(confidence, 4),
        unresolved=tuple(sorted(unresolved)),
        square_scores=square_scores,
    )


def annotate_local_board(
    frame: Any, observation: LocalObservation, *, orientation: str
) -> Any:
    import chess

    cv2, _ = _modules()
    output = frame.copy()
    height, width = output.shape[:2]
    palette = {
        "green": (70, 210, 90),
        "blue": (225, 125, 40),
        "brown": (40, 80, 130),
        "pink": (190, 100, 245),
        "yellow": (40, 225, 240),
        "orange": (40, 145, 245),
    }
    for index in range(9):
        x = round(index * width / 8)
        y = round(index * height / 8)
        cv2.line(output, (x, 0), (x, height), (30, 220, 220), 2)
        cv2.line(output, (0, y), (width, y), (30, 220, 220), 2)
    unresolved = set(observation.unresolved)
    for square_name in unresolved:
        square = chess.parse_square(square_name)
        file_index, rank_index = chess.square_file(square), chess.square_rank(square)
        if orientation == "white_bottom":
            row, column = 7 - rank_index, file_index
        else:
            row, column = rank_index, 7 - file_index
        x0, y0 = round(column * width / 8), round(row * height / 8)
        x1, y1 = round((column + 1) * width / 8), round((row + 1) * height / 8)
        cv2.rectangle(output, (x0 + 3, y0 + 3), (x1 - 3, y1 - 3), (30, 30, 240), 5)
    for square, piece_type in observation.types.items():
        file_index, rank_index = chess.square_file(square), chess.square_rank(square)
        if orientation == "white_bottom":
            row, column = 7 - rank_index, file_index
        else:
            row, column = rank_index, 7 - file_index
        center = (
            round((column + 0.5) * width / 8),
            round((row + 0.5) * height / 8),
        )
        color_name = TYPE_TO_COLOR[piece_type]
        color = palette[color_name]
        radius = max(13, min(width, height) // 45)
        cv2.circle(output, center, radius, color, -1)
        cv2.circle(output, center, radius, (250, 250, 250), 2)
        label = TYPE_TO_SYMBOL[piece_type]
        cv2.putText(
            output,
            label,
            (center[0] - radius // 2, center[1] + radius // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.5, radius / 25),
            (10, 10, 10),
            2,
            cv2.LINE_AA,
        )
    cv2.putText(
        output,
        f"LOCAL {observation.confidence * 100:.0f}%  pieces={len(observation.types)}  unresolved={len(observation.unresolved)}",
        (14, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        (255, 255, 255),
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        output,
        f"LOCAL {observation.confidence * 100:.0f}%  pieces={len(observation.types)}  unresolved={len(observation.unresolved)}",
        (14, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        (0, 0, 0),
        1,
        cv2.LINE_AA,
    )
    return output


def _image_square(row: int, column: int, orientation: str) -> int:
    import chess

    if orientation == "white_bottom":
        return chess.square(column, 7 - row)
    if orientation == "black_bottom":
        return chess.square(7 - column, row)
    raise ValidationError("camera orientation must be white_bottom or black_bottom")


def board_piece_types(board: Any) -> dict[int, str]:
    import chess

    return {
        square: chess.piece_name(piece.piece_type)
        for square, piece in board.piece_map().items()
    }


def infer_type_move(board: Any, observed: Mapping[int, str]) -> Optional[Any]:
    current = board_piece_types(board)
    if dict(observed) == current:
        return None
    matches = []
    for move in board.legal_moves:
        candidate = board.copy(stack=False)
        candidate.push(move)
        if board_piece_types(candidate) == dict(observed):
            matches.append(move)
    if len(matches) != 1:
        raise ValidationError(
            "local colors do not match exactly one legal move"
            if not matches
            else "local colors match multiple legal moves"
        )
    return matches[0]


def detect_aruco_references(frame: Any) -> list[dict[str, float]]:
    cv2, numpy = _modules()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    detector = cv2.aruco.ArucoDetector(dictionary, cv2.aruco.DetectorParameters())
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None:
        raise ValidationError("no ArUco board references detected")
    detected = {}
    board_corner_indices = {0: 2, 1: 3, 2: 0, 3: 1}
    for marker_corners, marker_id in zip(corners, ids.flatten().tolist()):
        if marker_id in {0, 1, 2, 3}:
            point = marker_corners.reshape(4, 2)[board_corner_indices[int(marker_id)]]
            detected[int(marker_id)] = {
                "x": float(point[0] / frame.shape[1]),
                "y": float(point[1] / frame.shape[0]),
            }
    if set(detected) != {0, 1, 2, 3}:
        missing = sorted({0, 1, 2, 3} - set(detected))
        raise ValidationError(f"missing ArUco board references: {missing}")
    ordered = [detected[index] for index in (0, 1, 2, 3)]
    points = numpy.float32([(value["x"], value["y"]) for value in ordered])
    if not cv2.isContourConvex((points * 1000).astype(numpy.int32)):
        raise ValidationError("ArUco board references are not a convex quadrilateral")
    return ordered


def generate_reference_markers(output_dir: Path, *, pixels: int = 400) -> list[Path]:
    cv2, numpy = _modules()
    if pixels < 100:
        raise ValidationError("reference markers must be at least 100 pixels")
    output_dir.mkdir(parents=True, exist_ok=True)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    paths = []
    for marker_id, name in (
        (0, "top-left"),
        (1, "top-right"),
        (2, "bottom-right"),
        (3, "bottom-left"),
    ):
        marker_size = round(pixels * 0.68)
        marker = cv2.aruco.generateImageMarker(dictionary, marker_id, marker_size)
        canvas = numpy.full((pixels, pixels), 255, dtype=numpy.uint8)
        offset = (pixels - marker_size) // 2
        canvas[offset : offset + marker_size, offset : offset + marker_size] = marker
        cv2.putText(
            canvas,
            f"{marker_id} {name}",
            (12, pixels - 14),
            cv2.FONT_HERSHEY_SIMPLEX,
            max(0.45, pixels / 900),
            0,
            max(1, pixels // 250),
            cv2.LINE_AA,
        )
        path = output_dir / f"board-{marker_id}-{name}.png"
        if not cv2.imwrite(str(path), canvas):
            raise ValidationError(f"could not write {path}")
        paths.append(path)
    return paths
