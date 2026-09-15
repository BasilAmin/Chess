from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Tuple

EXCLUDED_PARTS = {
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
}


def iter_python_files(paths: Iterable[str]) -> Iterator[Path]:
    seen = set()
    for raw in paths:
        base = Path(raw)
        if base.is_dir():
            candidates: Iterable[Path] = base.rglob("*.py")
        elif base.suffix == ".py":
            candidates = [base]
        else:
            candidates = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            if any(part in EXCLUDED_PARTS for part in candidate.parts):
                continue
            seen.add(resolved)
            yield candidate


def _is_shebang_row(source: str, row: int) -> bool:
    if row != 1:
        return False
    first_line = source.splitlines()[0] if source else ""
    return first_line.startswith("#!")


def comment_rows(source: str) -> Dict[int, int]:
    rows: Dict[int, int] = {}
    reader = io.StringIO(source).readline
    for token in tokenize.generate_tokens(reader):
        if token.type == tokenize.COMMENT:
            row, col = token.start
            if row not in rows or col < rows[row]:
                rows[row] = col
    return rows


def collect_comments(source: str) -> Dict[int, int]:
    try:
        rows = comment_rows(source)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {}
    return {row: col for row, col in rows.items() if not _is_shebang_row(source, row)}


def _docstring_holders(tree: ast.AST) -> Iterator[Tuple[ast.AST, ast.Expr]]:
    holders: List[ast.AST] = [tree]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            holders.append(node)
    for holder in holders:
        body = getattr(holder, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            yield holder, first


def docstring_spans(source: str) -> List[Tuple[int, int, bool, int]]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: List[Tuple[int, int, bool, int]] = []
    for holder, expr in _docstring_holders(tree):
        sole = len(getattr(holder, "body")) == 1 and not isinstance(holder, ast.Module)
        end = expr.end_lineno if expr.end_lineno is not None else expr.lineno
        spans.append((expr.lineno, end, sole, expr.col_offset))
    return spans


def collect_docstrings(source: str) -> List[int]:
    return sorted(start for start, _end, _sole, _indent in docstring_spans(source))


def _newline_style(lines: List[str]) -> str:
    for line in lines:
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith("\n"):
            return "\n"
        if line.endswith("\r"):
            return "\r"
    return "\n"


def strip_docstrings(source: str) -> str:
    spans = docstring_spans(source)
    if not spans:
        return source
    lines = source.splitlines(keepends=True)
    newline = _newline_style(lines)
    remove: set = set()
    replace: Dict[int, str] = {}
    for start, end, sole, indent in spans:
        for row in range(start, end + 1):
            remove.add(row)
        if sole:
            replace[start] = " " * indent + "pass"
    output: List[str] = []
    for index, line in enumerate(lines, start=1):
        if index in replace:
            output.append(replace[index] + newline)
        elif index in remove:
            continue
        else:
            output.append(line)
    return "".join(output)


def strip_comments(source: str) -> str:
    rows = collect_comments(source)
    if not rows:
        return source
    lines = source.splitlines(keepends=True)
    output: List[str] = []
    for index, line in enumerate(lines, start=1):
        if index not in rows:
            output.append(line)
            continue
        newline = ""
        body = line
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                newline = ending
                body = line[: -len(ending)]
                break
        code = body[: rows[index]].rstrip()
        if code == "":
            continue
        output.append(code + newline)
    return "".join(output)


def strip_all(source: str) -> str:
    return strip_comments(strip_docstrings(source))


def _parse_args(argv: List[str]) -> Tuple[bool, bool, List[str]]:
    write = "--write" in argv
    check = "--check" in argv
    targets = [arg for arg in argv if not arg.startswith("-")]
    if not targets:
        targets = ["."]
    if not write and not check:
        write = True
    return write, check, targets


def main(argv: List[str]) -> int:
    write, check, targets = _parse_args(argv)
    offenders: List[Tuple[Path, List[int], List[int]]] = []
    changed: List[Path] = []
    skipped: List[Path] = []
    for file in iter_python_files(targets):
        try:
            source = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        comments = sorted(collect_comments(source))
        docstrings = collect_docstrings(source)
        if check and (comments or docstrings):
            offenders.append((file, comments, docstrings))
        if write:
            updated = strip_all(source)
            if updated == source:
                continue
            try:
                ast.parse(updated)
            except SyntaxError:
                skipped.append(file)
                continue
            file.write_text(updated, encoding="utf-8")
            changed.append(file)
    for file in changed:
        print(f"cleaned: {file.as_posix()}")
    for file in skipped:
        print(f"skipped (would not parse after cleaning): {file.as_posix()}")
    if check and offenders:
        print(
            "Python comments and docstrings are not allowed (no-stubs steering rule):"
        )
        for file, comments, docstrings in offenders:
            if comments:
                listed = ", ".join(str(line) for line in comments)
                print(f"  {file.as_posix()}: comment line(s) {listed}")
            if docstrings:
                listed = ", ".join(str(line) for line in docstrings)
                print(f"  {file.as_posix()}: docstring line(s) {listed}")
        return 1
    if skipped:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
