from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Tuple

from strip_comments import collect_comments, collect_docstrings, iter_python_files

MARKERS = ["TO" + "DO", "FIX" + "ME", "XX" + "X", "HA" + "CK"]
MARKER_PATTERN = re.compile(r"\b(" + "|".join(MARKERS) + r")\b")
UNIMPLEMENTED_PATTERN = re.compile(r"raise\s+NotImplementedError")
SHELL_EXCLUDED_PARTS = {
    ".git",
    ".kilo",
    ".kiro",
    ".venv",
    "venv",
    "build",
    "dist",
    "data",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".husky",
}


def _iter_shell_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for candidate in root.rglob("*.sh"):
        if any(part in SHELL_EXCLUDED_PARTS for part in candidate.parts):
            continue
        files.append(candidate)
    return files


def check_python_comments(root: Path) -> List[str]:
    problems: List[str] = []
    for file in iter_python_files([str(root)]):
        source = file.read_text(encoding="utf-8")
        rows = collect_comments(source)
        if rows:
            listed = ", ".join(str(row) for row in sorted(rows))
            problems.append(f"{file.as_posix()}: Python comment on line(s) {listed}")
    return problems


def check_python_docstrings(root: Path) -> List[str]:
    problems: List[str] = []
    for file in iter_python_files([str(root)]):
        source = file.read_text(encoding="utf-8")
        rows = collect_docstrings(source)
        if rows:
            listed = ", ".join(str(row) for row in rows)
            problems.append(f"{file.as_posix()}: docstring on line(s) {listed}")
    return problems


def check_unimplemented(root: Path) -> List[str]:
    problems: List[str] = []
    for file in iter_python_files([str(root)]):
        source = file.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), start=1):
            if UNIMPLEMENTED_PATTERN.search(line):
                problems.append(
                    f"{file.as_posix()}: NotImplementedError on line {number}"
                )
    return problems


def check_stub_markers(root: Path) -> List[str]:
    problems: List[str] = []
    files = list(iter_python_files([str(root)])) + _iter_shell_files(root)
    self_path = Path(__file__).resolve()
    for file in files:
        if file.resolve() == self_path:
            continue
        try:
            source = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(source.splitlines(), start=1):
            match = MARKER_PATTERN.search(line)
            if match:
                problems.append(
                    f"{file.as_posix()}: stub marker '{match.group(1)}' on line {number}"
                )
    return problems


def run(root: Path) -> List[Tuple[str, List[str]]]:
    return [
        ("no Python comments (no-stubs)", check_python_comments(root)),
        ("no Python docstrings (no-stubs)", check_python_docstrings(root)),
        ("no stub markers (no-stubs)", check_stub_markers(root)),
        ("no unimplemented functions (no-stubs)", check_unimplemented(root)),
    ]


def main(argv: List[str]) -> int:
    root = Path(argv[0]) if argv else Path.cwd()
    results = run(root)
    failed = False
    for label, problems in results:
        if problems:
            failed = True
            print(f"FAIL {label}:")
            for problem in problems:
                print(f"  {problem}")
        else:
            print(f"PASS {label}")
    if failed:
        print(
            "\nSteering evaluation failed. See .kiro/steering for the governing rules."
        )
        return 1
    print("\nSteering evaluation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
