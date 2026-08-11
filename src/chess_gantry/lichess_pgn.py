from __future__ import annotations

from io import StringIO
from typing import Any, Iterator, Optional
import os

from .errors import ConfigurationError, ValidationError
from .models import BoardState, GridPosition, MoveDelta


def resolve_token(token: Optional[str] = None) -> Optional[str]:
    if token is not None:
        return token
    value = os.environ.get("LICHESS_TOKEN")
    return value.strip() if isinstance(value, str) and value.strip() else None


def lichess_client(token: Optional[str] = None) -> Any:
    try:
        import berserk
    except ImportError as exc:
        raise ConfigurationError(
            "berserk (the Lichess client) is not installed; run 'uv sync'"
        ) from exc
    resolved = resolve_token(token)
    session = berserk.TokenSession(resolved) if resolved else None
    return berserk.Client(session=session)


def fetch_pgn(game_id: str, *, token: Optional[str] = None, client: Any = None) -> str:
    active = client if client is not None else lichess_client(token)
    try:
        pgn = active.games.export(game_id, as_pgn=True)
    except Exception as exc:
        raise ConfigurationError(
            f"could not fetch Lichess game {game_id!r}: {exc}"
        ) from exc
    if not isinstance(pgn, str) or not pgn.strip():
        raise ValidationError(
            f"Lichess returned an empty or non-PGN response for game {game_id!r}"
        )
    return pgn


def chess_move_deltas(
    board: Any,
    move: Any,
    state: BoardState,
    event_id: str,
) -> tuple[MoveDelta, ...]:
    import chess

    if move not in board.legal_moves:
        raise ValidationError(f"move {move.uci()} is illegal in the current position")
    if move.promotion is not None:
        raise ValidationError("promotion requires verified physical piece replacement")
    rank = chess.square_rank(move.from_square)
    if board.is_castling(move):
        kingside = board.is_kingside_castling(move)
        king_source = GridPosition(chess.square_file(move.from_square), rank)
        king_destination = GridPosition(6 if kingside else 2, rank)
        rook_source = GridPosition(7 if kingside else 0, rank)
        rook_destination = GridPosition(5 if kingside else 3, rank)
        king = state.piece_at(king_source)
        rook = state.piece_at(rook_source)
        if king is None or rook is None:
            raise ValidationError("physical state is missing the castling king or rook")
        return (
            MoveDelta.from_mapping(
                {
                    "event_id": f"{event_id}.king",
                    "position": king.piece_id,
                    "px": king_source.x,
                    "py": king_source.y,
                    "nx": king_destination.x,
                    "ny": king_destination.y,
                }
            ),
            MoveDelta.from_mapping(
                {
                    "event_id": f"{event_id}.rook",
                    "position": rook.piece_id,
                    "px": rook_source.x,
                    "py": rook_source.y,
                    "nx": rook_destination.x,
                    "ny": rook_destination.y,
                }
            ),
        )
    source = GridPosition(chess.square_file(move.from_square), rank)
    destination = GridPosition(
        chess.square_file(move.to_square), chess.square_rank(move.to_square)
    )
    moving = state.piece_at(source)
    if moving is None:
        raise ValidationError(
            f"physical state has no piece at {chess.square_name(move.from_square)}"
        )
    payload: dict[str, Any] = {
        "event_id": event_id,
        "position": moving.piece_id,
        "px": source.x,
        "py": source.y,
        "nx": destination.x,
        "ny": destination.y,
    }
    if board.is_capture(move):
        capture_square = (
            chess.square(chess.square_file(move.to_square), rank)
            if board.is_en_passant(move)
            else move.to_square
        )
        capture_position = GridPosition(
            chess.square_file(capture_square), chess.square_rank(capture_square)
        )
        captured = state.piece_at(capture_position)
        if captured is None:
            raise ValidationError("physical state is missing the captured piece")
        payload["capture"] = {
            "id": captured.piece_id,
            "x": capture_position.x,
            "y": capture_position.y,
        }
    return (MoveDelta.from_mapping(payload),)


def pgn_moves(game_id: str, pgn_text: str, state: BoardState) -> Iterator[MoveDelta]:
    try:
        import chess
        import chess.pgn
    except ImportError as exc:
        raise ConfigurationError(
            "python-chess is not installed; run 'uv sync'"
        ) from exc
    game = chess.pgn.read_game(StringIO(pgn_text))
    if game is None:
        raise ValidationError("Lichess export did not contain a readable PGN game")
    board = game.board()
    simulated = state
    for ply, move in enumerate(game.mainline_moves(), start=1):
        if board.is_castling(move):
            rank = chess.square_rank(move.from_square)
            kingside = board.is_kingside_castling(move)
            king_source = GridPosition(chess.square_file(move.from_square), rank)
            king_destination = GridPosition(6 if kingside else 2, rank)
            rook_source = GridPosition(7 if kingside else 0, rank)
            rook_destination = GridPosition(5 if kingside else 3, rank)
            king = simulated.piece_at(king_source)
            rook = simulated.piece_at(rook_source)
            if king is None or rook is None:
                raise ValidationError(
                    f"local board is missing the king or rook for Lichess castling ply {ply}"
                )
            king_move = MoveDelta.from_mapping(
                {
                    "event_id": f"{game_id}.{ply}.king",
                    "position": king.piece_id,
                    "px": king_source.x,
                    "py": king_source.y,
                    "nx": king_destination.x,
                    "ny": king_destination.y,
                }
            )
            simulated = simulated.applied(king_move, None)
            yield king_move
            rook_move = MoveDelta.from_mapping(
                {
                    "event_id": f"{game_id}.{ply}.rook",
                    "position": rook.piece_id,
                    "px": rook_source.x,
                    "py": rook_source.y,
                    "nx": rook_destination.x,
                    "ny": rook_destination.y,
                }
            )
            simulated = simulated.applied(rook_move, None)
            yield rook_move
            board.push(move)
            continue
        if move.promotion is not None:
            raise ValidationError(
                f"Lichess ply {ply} is a promotion, which needs physical replacement"
            )
        source = GridPosition(
            chess.square_file(move.from_square), chess.square_rank(move.from_square)
        )
        destination = GridPosition(
            chess.square_file(move.to_square), chess.square_rank(move.to_square)
        )
        moving = simulated.piece_at(source)
        if moving is None:
            raise ValidationError(
                f"local board has no piece at source for Lichess ply {ply}"
            )
        payload = {
            "event_id": f"{game_id}.{ply}",
            "position": moving.piece_id,
            "px": source.x,
            "py": source.y,
            "nx": destination.x,
            "ny": destination.y,
        }
        if board.is_capture(move):
            capture_square = (
                chess.square(
                    chess.square_file(move.to_square),
                    chess.square_rank(move.from_square),
                )
                if board.is_en_passant(move)
                else move.to_square
            )
            capture_position = GridPosition(
                chess.square_file(capture_square), chess.square_rank(capture_square)
            )
            captured = simulated.piece_at(capture_position)
            if captured is None:
                raise ValidationError(
                    f"local board has no capture target for Lichess ply {ply}"
                )
            payload["capture"] = {
                "id": captured.piece_id,
                "x": capture_position.x,
                "y": capture_position.y,
            }
        result = MoveDelta.from_mapping(payload)
        capture_slot = None
        if result.capture is not None:
            used_slots = set(simulated.used_capture_slots())
            capture_slot = next(slot for slot in range(32) if slot not in used_slots)
        simulated = simulated.applied(result, capture_slot)
        board.push(move)
        yield result
