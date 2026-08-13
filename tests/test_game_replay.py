from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import json
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.game_replay import GameReplay, load_replay_game
from chess_gantry.lichess_mirror import MirrorTerminal
from chess_gantry.models import BoardState
from scripts.endurance_replay import endurance_move


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "examples" / "replays"


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = AppConfig.from_mapping(
            json.loads((ROOT / "config.demo.json").read_text())
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def replay(self, sample: str, **kwargs) -> GameReplay:
        return GameReplay(
            source=SAMPLES / sample,
            config=self.config,
            root=self.root,
            execute=kwargs.pop("execute", False),
            demo=kwargs.pop("demo", False),
            fast=True,
            terminal=MirrorTerminal(
                StringIO(), screen=False, title="Chess Gantry Game Replay"
            ),
            sleep=lambda value: None,
            **kwargs,
        )

    def test_samples_are_legal_standard_games_with_stable_ids(self) -> None:
        expected = {
            "capture-checkmate.pgn": (7, "1-0"),
            "en-passant-castling.pgn": (11, "1-0"),
            "capture-promotion.pgn": (9, "1-0"),
            "opera-game.pgn": (33, "1-0"),
            "straight-pawns.pgn": (32, "1/2-1/2"),
            "clear-lanes-no-knights.pgn": (22, "1/2-1/2"),
        }
        for name, (plies, result) in expected.items():
            with self.subTest(name=name):
                first = load_replay_game(SAMPLES / name)
                second = load_replay_game(SAMPLES / name)
                self.assertEqual(len(first.moves), plies)
                self.assertEqual(first.result, result)
                self.assertEqual(first.replay_id, second.replay_id)
                self.assertRegex(first.replay_id, r"^rp[0-9a-f]{24}$")

    def test_straight_pawn_sample_has_no_knights_or_diagonal_moves(self) -> None:
        import chess

        game = load_replay_game(SAMPLES / "straight-pawns.pgn")
        board = chess.Board()
        for uci in game.moves:
            move = chess.Move.from_uci(uci)
            piece = board.piece_at(move.from_square)
            self.assertIsNotNone(piece)
            self.assertNotEqual(piece.piece_type, chess.KNIGHT)
            from_file = chess.square_file(move.from_square)
            to_file = chess.square_file(move.to_square)
            from_rank = chess.square_rank(move.from_square)
            to_rank = chess.square_rank(move.to_square)
            self.assertTrue(from_file == to_file or from_rank == to_rank)
            self.assertFalse(board.is_capture(move))
            board.push(move)

    def test_clear_lane_sample_has_no_knights_and_safe_diagonals(self) -> None:
        import chess

        from chess_gantry.kinematics import grid_to_machine
        from chess_gantry.models import GridPosition
        from chess_gantry.path_planning import segment_is_clear

        game = load_replay_game(SAMPLES / "clear-lanes-no-knights.pgn")
        board = chess.Board()
        diagonal_count = 0
        straight_count = 0
        for uci in game.moves:
            move = chess.Move.from_uci(uci)
            piece = board.piece_at(move.from_square)
            self.assertIsNotNone(piece)
            self.assertNotEqual(piece.piece_type, chess.KNIGHT)
            file_delta = abs(
                chess.square_file(move.to_square) - chess.square_file(move.from_square)
            )
            rank_delta = abs(
                chess.square_rank(move.to_square) - chess.square_rank(move.from_square)
            )
            if file_delta and rank_delta:
                self.assertEqual(file_delta, rank_delta)
                diagonal_count += 1
                start = grid_to_machine(
                    GridPosition(
                        chess.square_file(move.from_square),
                        chess.square_rank(move.from_square),
                    ),
                    self.config.board,
                )
                end = grid_to_machine(
                    GridPosition(
                        chess.square_file(move.to_square),
                        chess.square_rank(move.to_square),
                    ),
                    self.config.board,
                )
                obstacles = []
                for square in chess.SQUARES:
                    if square == move.from_square or board.piece_at(square) is None:
                        continue
                    obstacles.append(
                        grid_to_machine(
                            GridPosition(
                                chess.square_file(square), chess.square_rank(square)
                            ),
                            self.config.board,
                        )
                    )
                self.assertTrue(segment_is_clear(start, end, obstacles, 30.0))
            else:
                straight_count += 1
            board.push(move)
        self.assertGreater(diagonal_count, 0)
        self.assertGreater(straight_count, 0)

    def test_endurance_replay_has_20000_straight_noninterfering_transfers(self) -> None:
        state = BoardState.standard()
        for index in range(20000):
            move = endurance_move(index)
            self.assertEqual(move.previous.x, move.new.x)
            self.assertIsNone(state.validate_move(move))
            state = state.applied(move, None)

    def test_result_metadata_does_not_split_identical_physical_replay(self) -> None:
        first = self.root / "first.pgn"
        second = self.root / "second.pgn"
        first.write_text('[Event "One"]\n[Result "*"]\n\n1. e4 *\n')
        second.write_text('[Event "Two"]\n[Result "1-0"]\n\n1. e4 1-0\n')
        self.assertEqual(
            load_replay_game(first).replay_id,
            load_replay_game(second).replay_id,
        )

    def test_replay_resumes_exact_prefix_without_duplicate_moves(self) -> None:
        replay = self.replay("en-passant-castling.pgn")
        partial = replay.run(max_plies=5)
        self.assertEqual(len(partial.moves), 5)
        revision = partial.physical_revision
        resumed = self.replay("en-passant-castling.pgn").run()
        self.assertEqual(resumed.moves, replay.game.moves)
        self.assertGreater(resumed.physical_revision, revision)
        rerun = self.replay("en-passant-castling.pgn").run()
        self.assertEqual(rerun.physical_revision, resumed.physical_revision)

    def test_every_sample_streams_through_demo_marlin(self) -> None:
        for path in sorted(SAMPLES.glob("*.pgn")):
            with self.subTest(path=path.name):
                replay = self.replay(path.name, execute=True, demo=True)
                cursor = replay.run()
                self.assertEqual(cursor.moves, replay.game.moves)
                self.assertFalse(replay.mirror.journal_path.exists())
                self.assertIsNone(replay.mirror.link)

    def test_special_move_samples_reach_expected_physical_state(self) -> None:
        en_passant_replay = self.replay("en-passant-castling.pgn")
        en_passant = en_passant_replay.run()
        state = en_passant_replay.mirror.service.store.load()
        self.assertEqual(en_passant.moves[-1], "e1g1")
        self.assertEqual(state.pieces["white_king_e"].x, 6)
        self.assertEqual(state.pieces["white_rook_h"].x, 5)
        self.assertEqual(state.pieces["black_pawn_d"].status, "captured")
        promotion_replay = self.replay("capture-promotion.pgn")
        promotion = promotion_replay.run()
        promotion_state = promotion_replay.mirror.service.store.load()
        self.assertEqual(promotion.moves[-1], "b7a8q")
        self.assertEqual(promotion_state.pieces["white_pawn_a"].x, 0)
        self.assertEqual(promotion_state.pieces["white_pawn_a"].y, 7)
        self.assertEqual(promotion_state.pieces["black_rook_a"].status, "captured")

    def test_reset_restarts_completed_replay_from_standard_position(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        completed = replay.run()
        self.assertEqual(len(completed.moves), 7)
        replay.reset()
        restarted = replay.run(max_plies=1)
        self.assertEqual(restarted.moves, ("e2e4",))

    def test_completed_replay_returns_without_rehoming_or_motion(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        completed = replay.run()
        replay.execute = True
        replay.mirror.execute = True
        rerun = replay.run()
        self.assertEqual(rerun.physical_revision, completed.physical_revision)
        self.assertIsNone(replay.mirror.link)

    def test_completed_replay_repairs_result_after_interrupted_final_write(
        self,
    ) -> None:
        replay = self.replay("capture-checkmate.pgn")
        completed = replay.run()
        broken = replace(completed, result="*")
        broken.save(replay.mirror.cursor_path)
        repaired = replay.run()
        self.assertEqual(repaired.result, "1-0")
        self.assertEqual(repaired.physical_revision, completed.physical_revision)

    def test_reset_refuses_to_delete_pending_recovery_evidence(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        replay.run(max_plies=1)
        replay.mirror.journal_path.write_text('{"pending": true}\n')
        with self.assertRaisesRegex(ConfigurationError, "reconcile"):
            replay.reset()

    def test_resume_rejects_motion_configuration_change(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        replay.run(max_plies=1)
        changed = replace(
            self.config,
            board=replace(self.config.board, origin_x_mm=41.0),
        )
        resumed = GameReplay(
            source=SAMPLES / "capture-checkmate.pgn",
            config=changed,
            root=self.root,
            execute=False,
            demo=False,
            fast=True,
            terminal=MirrorTerminal(StringIO(), screen=False),
        )
        with self.assertRaisesRegex(ConfigurationError, "motion configuration"):
            resumed.run()
        resumed.validate_session(allow_motion_mismatch=True)

    def test_saved_position_detection_distinguishes_fresh_and_resume(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        self.assertFalse(replay.has_saved_position())
        replay.run(max_plies=1)
        self.assertTrue(replay.has_saved_position())

    def test_invalid_multi_game_and_nonstandard_pgn_are_rejected(self) -> None:
        multi = self.root / "multi.pgn"
        sample = (SAMPLES / "capture-checkmate.pgn").read_text()
        multi.write_text(sample + "\n" + sample)
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            load_replay_game(multi)
        custom = self.root / "custom.pgn"
        custom.write_text(
            '[Event "Custom"]\n[FEN "8/8/8/8/8/8/8/K6k w - - 0 1"]\n'
            '[SetUp "1"]\n[Result "1/2-1/2"]\n\n1. Ka2 1/2-1/2\n'
        )
        with self.assertRaisesRegex(ConfigurationError, "standard chess"):
            load_replay_game(custom)

    def test_invalid_result_mismatch_and_trailing_content_are_rejected(self) -> None:
        invalid = self.root / "invalid-result.pgn"
        invalid.write_text('[Event "Bad"]\n[Result "banana"]\n\n1. e4 banana\n')
        with self.assertRaisesRegex(ValidationError, "result token"):
            load_replay_game(invalid)
        mismatch = self.root / "mismatch.pgn"
        mismatch.write_text('[Event "Bad"]\n[Result "1-0"]\n\n1. e4 0-1\n')
        with self.assertRaisesRegex(ValidationError, "does not match"):
            load_replay_game(mismatch)
        trailing = self.root / "trailing.pgn"
        trailing.write_text('[Event "Bad"]\n[Result "*"]\n\n1. e4 * garbage\n')
        with self.assertRaisesRegex(ValidationError, "result token"):
            load_replay_game(trailing)
        setup = self.root / "setup.pgn"
        setup.write_text('[Event "Bad"]\n[SetUp "1"]\n[Result "*"]\n\n1. e4 *\n')
        with self.assertRaisesRegex(ConfigurationError, "standard chess"):
            load_replay_game(setup)

    def test_negative_delay_and_nonpositive_limit_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "delay"):
            self.replay("capture-checkmate.pgn", move_delay_s=-1)
        with self.assertRaisesRegex(ValidationError, "max plies"):
            self.replay("capture-checkmate.pgn").run(max_plies=0)
