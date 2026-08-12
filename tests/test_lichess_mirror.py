from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

import chess

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError
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

    def test_duplicate_stream_state_does_not_duplicate_execution(self):
        events = [
            {"type": "gameState", "moves": "e2e4", "status": "started"},
            {"type": "gameState", "moves": "e2e4", "status": "mate"},
        ]
        cursor = self.session(events).run()
        self.assertEqual(cursor.moves, ("e2e4",))
        self.assertEqual(cursor.physical_revision, 1)

    def test_stream_divergence_and_backlog_are_rejected(self):
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
        fresh = MirrorCursor.create("game1234")
        with self.assertRaisesRegex(ConfigurationError, "backlog"):
            session._verify_prefix(fresh, ("e2e4", "e7e5"))

    def test_capture_stops_before_motion_when_storage_is_disabled(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        for uci in ("e2e4", "d7d5"):
            cursor = session._execute_ply(cursor, uci)
        with self.assertRaisesRegex(ConfigurationError, "capture storage"):
            session._execute_ply(cursor, "e4d5")
        self.assertEqual(cursor.moves, ("e2e4", "d7d5"))

    def test_castling_advances_cursor_after_both_physical_transfers(self):
        session = self.session(execute=False)
        cursor = session._initialize()
        sequence = ("e2e4", "e7e5", "g1f3", "b8c6", "f1e2", "g8f6")
        for uci in sequence:
            cursor = session._execute_ply(cursor, uci)
        before_revision = cursor.physical_revision
        cursor = session._execute_ply(cursor, "e1g1")
        self.assertEqual(cursor.moves[-1], "e1g1")
        self.assertEqual(cursor.physical_revision, before_revision + 2)
        self.assertIsNone(cursor.pending_uci)
        state = session.service.store.load()
        self.assertEqual(state.pieces["white_king_e"].x, 6)
        self.assertEqual(state.pieces["white_rook_h"].x, 5)

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
