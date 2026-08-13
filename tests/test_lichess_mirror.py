from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import json
import unittest

import chess

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError
from chess_gantry.models import GridPosition
from chess_gantry.serial_link import DemoMarlinSerial
from chess_gantry.lichess_mirror import (
    FAST_DRAG_MM_MIN,
    FAST_TRAVEL_MM_MIN,
    LichessMirror,
    MirrorCursor,
    MirrorTerminal,
    mirror_config,
    _game_snapshot,
)


ROOT = Path(__file__).resolve().parents[1]


def pgn(moves=(), result="*"):
    board = chess.Board()
    sans = []
    for uci in moves:
        move = board.parse_uci(uci)
        sans.append(board.san(move))
        board.push(move)
    body = []
    for index, san in enumerate(sans):
        if index % 2 == 0:
            body.append(f"{index // 2 + 1}.")
        body.append(san)
    return (
        '[Event "Mirror Test"]\n[White "White"]\n[Black "Black"]\n'
        f'[Result "{result}"]\n\n'
        + " ".join(body)
        + (" " if body else "")
        + result
        + "\n"
    )


class FakeBoardClient:
    def __init__(self, events):
        self.events = events

    def stream_game_state(self, game_id):
        yield from self.events


class FakeGamesClient:
    def __init__(self, events):
        self.events = events

    def stream_game_moves(self, game_id):
        yield from self.events


class FakeClient:
    def __init__(self, events):
        self.board = FakeBoardClient(events)
        self.games = FakeGamesClient(events)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = AppConfig.from_mapping(
            json.loads((ROOT / "config.demo.json").read_text())
        )

    def tearDown(self):
        self.temporary.cleanup()

    def session(self, events=(), fetch_moves=(), **kwargs):
        return LichessMirror(
            game_id="game1234",
            config=self.config,
            root=self.root,
            execute=kwargs.pop("execute", True),
            demo=kwargs.pop("demo", True),
            fast=kwargs.pop("fast", True),
            stream_mode=kwargs.pop("stream_mode", "board"),
            terminal=kwargs.pop("terminal", MirrorTerminal(StringIO(), screen=False)),
            client=FakeClient(events),
            pgn_fetcher=lambda *args, **values: pgn(fetch_moves),
            sleep=lambda value: None,
            **kwargs,
        )

    def activate_demo_link(self, session):
        session.link = DemoMarlinSerial(session.config.serial)
        session.link.connect()
        session.service.home_with_link(session.link)
        self.addCleanup(session.link.close)

    def test_fast_profile_disables_parking_and_uses_commissioned_ceiling(self):
        value = mirror_config(self.config, fast=True)
        self.assertFalse(value.motion.park_after_move)
        self.assertEqual(value.motion.travel_feed_mm_min, FAST_TRAVEL_MM_MIN)
        self.assertEqual(value.motion.drag_feed_mm_min, FAST_DRAG_MM_MIN)
        configured = mirror_config(self.config, fast=False)
        self.assertFalse(configured.motion.park_after_move)
        self.assertEqual(
            configured.motion.travel_feed_mm_min,
            self.config.motion.travel_feed_mm_min,
        )

    def test_mirror_module_has_no_vision_or_model_dependency(self):
        source = (ROOT / "src" / "chess_gantry" / "lichess_mirror.py").read_text()
        self.assertNotIn("from .vision", source)
        self.assertNotIn("SolVisionManager", source)
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("anthropic", source.lower())

    def test_physical_demo_and_simulation_sessions_are_isolated(self):
        physical = self.session(execute=True, demo=False)
        demo = self.session(execute=True, demo=True)
        simulation = self.session(execute=False, demo=False)
        self.assertNotEqual(physical.directory, demo.directory)
        self.assertNotEqual(physical.directory, simulation.directory)
        self.assertTrue(str(physical.directory).endswith("physical"))
        self.assertTrue(str(demo.directory).endswith("demo"))
        self.assertTrue(str(simulation.directory).endswith("simulation"))

    def test_fresh_game_executes_one_stream_move_and_persists_cursor(self):
        events = [
            {"type": "gameFull", "state": {"moves": "", "status": "started"}},
            {"type": "gameState", "moves": "e2e4", "status": "mate"},
        ]
        session = self.session(events)
        cursor = session.run()
        self.assertEqual(cursor.moves, ("e2e4",))
        self.assertEqual(cursor.physical_revision, 1)
        self.assertIsNone(cursor.pending_uci)
        stored = MirrorCursor.load(session.cursor_path, "game1234")
        self.assertEqual(stored.moves, ("e2e4",))
        self.assertEqual(session.service.store.load().pieces["white_pawn_e"].y, 3)

    def test_existing_remote_history_is_rejected_before_motion(self):
        session = self.session(fetch_moves=("e2e4",))
        with self.assertRaisesRegex(ConfigurationError, "before the first"):
            session.run(once=True)
        self.assertFalse(session.state_path.exists())

    def test_nonstandard_variant_is_rejected(self):
        variant = '[Event "Variant"]\n[Variant "Chess960"]\n[Result "*"]\n\n*\n'
        with self.assertRaisesRegex(ConfigurationError, "standard"):
            _game_snapshot(variant)

    def test_orphan_physical_state_is_never_overwritten(self):
        session = self.session()
        session.directory.mkdir(parents=True, exist_ok=True)
        session.state_path.write_text('{"sentinel": true}\n')
        with self.assertRaisesRegex(ConfigurationError, "without a cursor"):
            session._initialize()
        self.assertIn("sentinel", session.state_path.read_text())

    def test_mirror_requires_configured_castling_buffer(self):
        config = replace(
            self.config,
            capture=replace(self.config.capture, buffer_points=()),
        )
        with self.assertRaisesRegex(ConfigurationError, "castling buffer"):
            LichessMirror(
                game_id="game1234",
                config=config,
                root=self.root,
                execute=False,
                demo=False,
                fast=False,
                client=FakeClient(()),
                pgn_fetcher=lambda *args, **kwargs: pgn(),
            )

    def test_duplicate_stream_state_does_not_duplicate_execution(self):
        events = [
            {"type": "gameState", "moves": "e2e4", "status": "started"},
            {"type": "gameState", "moves": "e2e4", "status": "mate"},
        ]
        cursor = self.session(events).run()
        self.assertEqual(cursor.moves, ("e2e4",))
        self.assertEqual(cursor.physical_revision, 1)

    def test_stream_divergence_is_rejected_but_backlog_is_allowed(self):
        session = self.session()
        cursor = MirrorCursor.create("game1234")
        cursor = cursor.__class__(
            game_id=cursor.game_id,
            moves=("e2e4",),
            fen=chess.Board().fen(),
            physical_revision=1,
        )
        with self.assertRaisesRegex(ConfigurationError, "diverged"):
            session._verify_prefix(cursor, ("d2d4",))
        session._verify_prefix(MirrorCursor.create("game1234"), ("e2e4", "e7e5"))

    def test_large_stream_update_executes_every_missing_ply_in_order(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        remote = (
            "e2e4",
            "e7e5",
            "g1f3",
            "b8c6",
            "f1e2",
            "g8f6",
            "e1g1",
            "f8e7",
            "d2d3",
            "e8g8",
            "b1c3",
            "d7d6",
        )
        completed = session._execute_remote_prefix(cursor, remote)
        self.assertEqual(completed.moves, remote)

    def test_public_polling_finishes_moves_after_five_ply_stall(self):
        moves = (
            "e2e4",
            "e7e5",
            "f1c4",
            "g8f6",
            "d2d3",
            "d7d5",
            "e4d5",
            "f6d5",
        )
        responses = iter(
            (
                pgn(),
                pgn(moves[:5]),
                TimeoutError("temporary export timeout"),
                pgn(moves[:5]),
                pgn(moves, result="0-1"),
            )
        )

        def fetcher(*args, **kwargs):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        session = LichessMirror(
            game_id="game1234",
            config=self.config,
            root=self.root,
            execute=False,
            demo=False,
            fast=True,
            stream_mode="public",
            terminal=MirrorTerminal(StringIO(), screen=False),
            client=FakeClient(()),
            pgn_fetcher=fetcher,
            sleep=lambda value: None,
        )
        cursor = session.run()
        self.assertEqual(cursor.moves, moves)
        self.assertEqual(cursor.result, "0-1")
        self.assertEqual(cursor.physical_revision, 10)

    def test_capture_ejects_piece_and_continues(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        for uci in ("e2e4", "d7d5", "e4d5"):
            cursor = session._execute_ply(cursor, uci)
        self.assertEqual(cursor.moves, ("e2e4", "d7d5", "e4d5"))
        state = session.service.store.load()
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")
        self.assertEqual(state.pieces["white_pawn_e"].board_position.x, 3)
        self.assertEqual(state.pieces["white_pawn_e"].board_position.y, 4)

    def test_capture_resumes_after_persisted_ejection_without_replaying(self):
        from chess_gantry.lichess_pgn import chess_move_deltas

        session = self.session(execute=False)
        cursor = session._initialize()
        for uci in ("e2e4", "d7d5"):
            cursor = session._execute_ply(cursor, uci)
        board = session._board(cursor)
        move = board.parse_uci("e4d5")
        (delta,) = chess_move_deltas(
            board,
            move,
            session.service.store.load(),
            "game1234.3",
            allow_promotion_replacement=True,
        )
        session._apply_plan(session.service.plan_capture_ejection(delta))
        cursor = replace(
            cursor,
            pending_uci="e4d5",
            pending_total=2,
            pending_completed=1,
            physical_revision=session.service.store.load().revision,
        )
        cursor.save(session.cursor_path)
        completed = session._execute_ply(session._initialize(), "e4d5")
        self.assertEqual(completed.moves[-1], "e4d5")
        state = session.service.store.load()
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")
        self.assertEqual(
            state.pieces["white_pawn_e"].board_position, GridPosition(3, 4)
        )

    def test_en_passant_ejects_pawn_from_actual_capture_square(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        sequence = ("e2e4", "a7a6", "e4e5", "d7d5", "e5d6")
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        state = session.service.store.load()
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")
        self.assertEqual(state.pieces["white_pawn_e"].board_position.x, 3)
        self.assertEqual(state.pieces["white_pawn_e"].board_position.y, 5)
        self.assertEqual(cursor.moves, sequence)

    def test_promotion_uses_pawn_proxy_and_continues_virtual_queen_state(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        sequence = (
            "a2a4",
            "h7h5",
            "a4a5",
            "h5h4",
            "a5a6",
            "h4h3",
            "a6b7",
            "h3g2",
            "b7a8q",
        )
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        board = session._board(cursor)
        self.assertEqual(board.piece_at(chess.A8).symbol(), "Q")
        state = session.service.store.load()
        self.assertEqual(state.pieces["white_pawn_a"].board_position.x, 0)
        self.assertEqual(state.pieces["white_pawn_a"].board_position.y, 7)

    def test_castling_advances_cursor_after_both_physical_transfers(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        sequence = ("e2e4", "e7e5", "g1f3", "b8c6", "f1e2", "g8f6")
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        before_revision = cursor.physical_revision
        cursor = session._execute_ply(cursor, "e1g1")
        self.assertEqual(cursor.moves[-1], "e1g1")
        self.assertEqual(cursor.physical_revision, before_revision + 3)
        self.assertIsNone(cursor.pending_uci)
        state = session.service.store.load()
        self.assertEqual(state.pieces["white_king_e"].x, 6)
        self.assertEqual(state.pieces["white_rook_h"].x, 5)

    def test_buffered_castling_resumes_after_first_stage_without_replaying(self):
        from chess_gantry.kinematics import grid_to_machine
        from chess_gantry.lichess_pgn import chess_move_deltas

        session = self.session(execute=False)
        cursor = session._initialize()
        sequence = ("e2e4", "e7e5", "g1f3", "b8c6", "f1e2", "g8f6")
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        board = session._board(cursor)
        castle = board.parse_uci("e1g1")
        _, rook_delta = chess_move_deltas(
            board,
            castle,
            session.service.store.load(),
            "game1234.7",
        )
        rook_start = grid_to_machine(rook_delta.previous, session.config.board)
        buffer = min(
            session.config.capture.buffer_points,
            key=lambda point: (point.x - rook_start.x) ** 2
            + (point.y - rook_start.y) ** 2,
        )
        plan = session.service.plan_buffer_out(rook_delta, buffer)
        session._apply_plan(plan)
        cursor = replace(
            cursor,
            pending_uci="e1g1",
            pending_total=3,
            pending_completed=1,
            physical_revision=session.service.store.load().revision,
        )
        cursor.save(session.cursor_path)
        resumed = session._initialize()
        completed = session._execute_ply(resumed, "e1g1")
        self.assertEqual(completed.moves[-1], "e1g1")
        self.assertIsNone(completed.pending_uci)
        self.assertEqual(completed.pending_completed, 0)
        state = session.service.store.load()
        self.assertEqual(
            state.pieces["white_king_e"].board_position, GridPosition(6, 0)
        )
        self.assertEqual(
            state.pieces["white_rook_h"].board_position, GridPosition(5, 0)
        )

    def test_complete_checkmate_game_with_capture_is_fully_mirrored(self):
        session = self.session(execute=True, demo=True)
        self.activate_demo_link(session)
        cursor = session._initialize()
        sequence = ("e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7")
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        board = session._board(cursor)
        self.assertTrue(board.is_checkmate())
        self.assertEqual(board.result(), "1-0")
        self.assertEqual(cursor.moves, sequence)
        state = session.service.store.load()
        self.assertEqual(state.pieces["black_pawn_f"].status, "captured")

    def test_special_moves_stream_actual_programs_through_demo_marlin(self):
        sequences = {
            "en-passant": ("e2e4", "a7a6", "e4e5", "d7d5", "e5d6"),
            "castling": (
                "e2e4",
                "e7e5",
                "g1f3",
                "b8c6",
                "f1e2",
                "g8f6",
                "e1g1",
            ),
            "promotion": (
                "a2a4",
                "h7h5",
                "a4a5",
                "h5h4",
                "a5a6",
                "h4h3",
                "a6b7",
                "h3g2",
                "b7a8q",
            ),
        }
        for name, sequence in sequences.items():
            with self.subTest(name=name), TemporaryDirectory() as directory:
                root = Path(directory)
                session = LichessMirror(
                    game_id="game1234",
                    config=self.config,
                    root=root,
                    execute=True,
                    demo=True,
                    fast=True,
                    stream_mode="board",
                    terminal=MirrorTerminal(StringIO(), screen=False),
                    client=FakeClient(()),
                    pgn_fetcher=lambda *args, **kwargs: pgn(),
                    sleep=lambda value: None,
                )
                session.link = DemoMarlinSerial(session.config.serial)
                session.link.connect()
                session.service.home_with_link(session.link)
                try:
                    cursor = session._initialize()
                    for uci in sequence:
                        cursor = session._execute_ply(cursor, uci)
                    self.assertEqual(cursor.moves, sequence)
                    self.assertGreater(len(session.link.commands), len(sequence))
                    self.assertIn("M106 P0 S100", session.link.commands)
                    self.assertIn("M107 P0", session.link.commands)
                finally:
                    session.link.close()

    def test_resume_uses_exact_cursor_without_replaying_move(self):
        first = self.session([{"type": "gameState", "moves": "e2e4", "status": "mate"}])
        cursor = first.run()
        second = LichessMirror(
            game_id="game1234",
            config=self.config,
            root=self.root,
            execute=True,
            demo=True,
            fast=True,
            stream_mode="board",
            terminal=MirrorTerminal(StringIO(), screen=False),
            client=FakeClient(
                [{"type": "gameState", "moves": "e2e4", "status": "mate"}]
            ),
            pgn_fetcher=lambda *args, **kwargs: pgn(("e2e4",)),
            sleep=lambda value: None,
        )
        resumed = second.run()
        self.assertEqual(resumed.moves, cursor.moves)
        self.assertEqual(resumed.physical_revision, 1)

    def test_reconciled_pending_move_completes_cursor_without_reexecution(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        board = chess.Board()
        move = board.parse_uci("e2e4")
        from chess_gantry.lichess_pgn import chess_move_deltas

        delta = chess_move_deltas(
            board, move, session.service.store.load(), "game1234.1"
        )[0]
        plan = session.service.plan(delta, session.service.store.load())
        session.service.store.save(plan.next_state)
        pending = MirrorCursor(
            game_id="game1234",
            moves=(),
            fen=chess.Board().fen(),
            physical_revision=0,
            pending_uci="e2e4",
            pending_total=1,
            pending_completed=0,
        )
        pending.save(session.cursor_path)
        resumed = session._initialize()
        completed = session._execute_ply(resumed, "e2e4")
        self.assertEqual(completed.moves, ("e2e4",))
        self.assertEqual(completed.physical_revision, 1)
        self.assertIsNone(completed.pending_uci)

    def test_terminal_renders_ascii_status_and_board(self):
        output = StringIO()
        terminal = MirrorTerminal(output, screen=False)
        cursor = MirrorCursor.create("game1234")
        terminal.render(
            chess.Board(),
            game_id="game1234",
            state="started",
            cursor=cursor,
            message="ready",
        )
        text = output.getvalue()
        self.assertIn("Chess Gantry Lichess Mirror", text)
        self.assertIn("Game: game1234", text)
        self.assertIn("r n b q k b n r", text)
        self.assertIn("ready", text)


if __name__ == "__main__":
    unittest.main()
