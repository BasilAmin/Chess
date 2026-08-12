from __future__ import annotations

import unittest

from chess_gantry.config import BoardGeometry, PlannerSettings, Workspace
from chess_gantry.errors import PlanningError
from chess_gantry.kinematics import grid_to_machine
from chess_gantry.models import GridPosition, MachinePoint
from chess_gantry.path_planning import (
    astar_path,
    safest_path_to_any_goal,
    segment_is_clear,
)


class KinematicsTests(unittest.TestCase):
    def test_flip_and_swap(self) -> None:
        geometry = BoardGeometry(
            width=8,
            height=8,
            square_size_mm=20.0,
            origin_x_mm=10.0,
            origin_y_mm=10.0,
            flip_x=True,
            flip_y=False,
            swap_xy=True,
        )
        self.assertEqual(
            grid_to_machine(GridPosition(1, 2), geometry), MachinePoint(50.0, 130.0)
        )


class AStarTests(unittest.TestCase):
    def settings(self, keepout: float = 15.0) -> PlannerSettings:
        return PlannerSettings(
            kind="astar",
            grid_step_mm=10.0,
            obstacle_keepout_mm=keepout,
            allow_diagonal=True,
            simplify_path=True,
            max_expanded_nodes=10000,
        )

    def test_routes_around_obstacle(self) -> None:
        workspace = Workspace(0.0, 100.0, 0.0, 100.0)
        obstacle = MachinePoint(50.0, 50.0)
        path = astar_path(
            MachinePoint(10.0, 50.0),
            MachinePoint(90.0, 50.0),
            [obstacle],
            workspace,
            self.settings(),
        )
        self.assertEqual(path[0], MachinePoint(10.0, 50.0))
        self.assertEqual(path[-1], MachinePoint(90.0, 50.0))
        self.assertGreater(len(path), 2)
        for start, end in zip(path, path[1:]):
            self.assertTrue(segment_is_clear(start, end, [obstacle], 15.0))

    def test_reports_no_path_when_keepout_blocks_workspace(self) -> None:
        workspace = Workspace(0.0, 100.0, 0.0, 20.0)
        with self.assertRaisesRegex(PlanningError, "no collision-free path"):
            astar_path(
                MachinePoint(0.0, 10.0),
                MachinePoint(100.0, 10.0),
                [MachinePoint(50.0, 10.0)],
                workspace,
                self.settings(keepout=30.0),
            )

    def test_capture_route_prefers_clearer_chute_over_nearer_chute(self) -> None:
        workspace = Workspace(0.0, 100.0, 0.0, 100.0)
        start = MachinePoint(90.0, 20.0)
        lower = MachinePoint(0.0, 0.0)
        upper = MachinePoint(0.0, 100.0)
        obstacles = (
            MachinePoint(40.0, 15.0),
            MachinePoint(20.0, 25.0),
        )
        path = safest_path_to_any_goal(
            start,
            (lower, upper),
            obstacles,
            workspace,
            self.settings(keepout=15.0),
        )
        self.assertEqual(path[-1], upper)
        for segment_start, segment_end in zip(path, path[1:]):
            self.assertTrue(
                segment_is_clear(segment_start, segment_end, obstacles, 15.0)
            )

    def test_capture_route_fails_closed_when_all_chutes_are_blocked(self) -> None:
        workspace = Workspace(0.0, 100.0, 0.0, 20.0)
        with self.assertRaisesRegex(PlanningError, "magnetically clear"):
            safest_path_to_any_goal(
                MachinePoint(100.0, 10.0),
                (MachinePoint(0.0, 0.0), MachinePoint(0.0, 20.0)),
                (MachinePoint(50.0, 10.0),),
                workspace,
                self.settings(keepout=30.0),
            )


if __name__ == "__main__":
    unittest.main()
