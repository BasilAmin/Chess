from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from chess_gantry.config import AppConfig
from chess_gantry.errors import ConfigurationError, ValidationError
from chess_gantry.game_replay import GameReplay, load_replay_game
from chess_gantry.lichess_mirror import MirrorTerminal


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
        }
        for name, (plies, result) in expected.items():
            with self.subTest(name=name):
                first = load_replay_game(SAMPLES / name)
                second = load_replay_game(SAMPLES / name)
                self.assertEqual(len(first.moves), plies)
                self.assertEqual(first.result, result)
                self.assertEqual(first.replay_id, second.replay_id)
                self.assertRegex(first.replay_id, r"^rp[0-9a-f]{24}$")

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

    def test_reset_refuses_to_delete_pending_recovery_evidence(self) -> None:
        replay = self.replay("capture-checkmate.pgn")
        replay.run(max_plies=1)
        replay.mirror.journal_path.write_text('{"pending": true}\n')
        with self.assertRaisesRegex(ConfigurationError, "reconcile"):
            replay.reset()

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

    def test_negative_delay_and_nonpositive_limit_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "delay"):
            self.replay("capture-checkmate.pgn", move_delay_s=-1)
        with self.assertRaisesRegex(ValidationError, "max plies"):
            self.replay("capture-checkmate.pgn").run(max_plies=0)
