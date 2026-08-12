from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from math import ceil, hypot
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import PlannerSettings, Workspace
from .errors import PlanningError
from .models import MachinePoint

_EPSILON = 1e-9


def _distance(a: MachinePoint, b: MachinePoint) -> float:
    return hypot(a.x - b.x, a.y - b.y)


def _point_segment_distance(
    point: MachinePoint, start: MachinePoint, end: MachinePoint
) -> float:
    dx = end.x - start.x
    dy = end.y - start.y
    length_sq = dx * dx + dy * dy
    if length_sq <= _EPSILON:
        return _distance(point, start)
    t = ((point.x - start.x) * dx + (point.y - start.y) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    closest = MachinePoint(start.x + t * dx, start.y + t * dy)
    return _distance(point, closest)


def segment_is_clear(
    start: MachinePoint,
    end: MachinePoint,
    obstacles: Sequence[MachinePoint],
    keepout_mm: float,
) -> bool:
    if keepout_mm <= 0:
        return True
    return all(
        _point_segment_distance(obstacle, start, end) + _EPSILON >= keepout_mm
        for obstacle in obstacles
    )


def _assert_in_workspace(point: MachinePoint, workspace: Workspace, name: str) -> None:
    if not workspace.contains(point):
        raise PlanningError(
            f"{name} ({point.x:.3f}, {point.y:.3f}) is outside the configured workspace"
        )


def direct_path(
    start: MachinePoint,
    goal: MachinePoint,
    obstacles: Sequence[MachinePoint],
    workspace: Workspace,
    settings: PlannerSettings,
) -> Tuple[MachinePoint, ...]:
    del obstacles, settings
    _assert_in_workspace(start, workspace, "path start")
    _assert_in_workspace(goal, workspace, "path goal")
    return (start, goal)


def _axis_values(
    minimum: float, maximum: float, step: float, extras: Sequence[float]
) -> List[float]:
    count = int(ceil((maximum - minimum) / step))
    values = [min(maximum, minimum + index * step) for index in range(count + 1)]
    values.extend(
        value for value in extras if minimum - _EPSILON <= value <= maximum + _EPSILON
    )
    values.extend((minimum, maximum))
    unique: Dict[float, float] = {}
    for value in values:
        key = round(value, 9)
        unique[key] = min(max(value, minimum), maximum)
    return [unique[key] for key in sorted(unique)]


def _simplify(
    path: Sequence[MachinePoint],
    obstacles: Sequence[MachinePoint],
    keepout_mm: float,
) -> Tuple[MachinePoint, ...]:
    if len(path) <= 2:
        return tuple(path)
    simplified = [path[0]]
    current = 0
    while current < len(path) - 1:
        candidate = len(path) - 1
        while candidate > current + 1:
            if segment_is_clear(path[current], path[candidate], obstacles, keepout_mm):
                break
            candidate -= 1
        simplified.append(path[candidate])
        current = candidate
    return tuple(simplified)


def astar_path(
    start: MachinePoint,
    goal: MachinePoint,
    obstacles: Sequence[MachinePoint],
    workspace: Workspace,
    settings: PlannerSettings,
    proximity_weight: float = 0.0,
) -> Tuple[MachinePoint, ...]:
    _assert_in_workspace(start, workspace, "path start")
    _assert_in_workspace(goal, workspace, "path goal")
    if start == goal:
        raise PlanningError("path start and goal are identical")

    filtered = tuple(
        obstacle
        for obstacle in obstacles
        if _distance(obstacle, start) > _EPSILON
        and _distance(obstacle, goal) > _EPSILON
    )

    xs = _axis_values(
        workspace.min_x_mm, workspace.max_x_mm, settings.grid_step_mm, (start.x, goal.x)
    )
    ys = _axis_values(
        workspace.min_y_mm, workspace.max_y_mm, settings.grid_step_mm, (start.y, goal.y)
    )
    node_count = len(xs) * len(ys)
    if node_count > settings.max_expanded_nodes * 4:
        raise PlanningError(
            f"planner grid has {node_count} nodes; increase grid_step_mm or max_expanded_nodes"
        )

    x_lookup = {round(value, 9): index for index, value in enumerate(xs)}
    y_lookup = {round(value, 9): index for index, value in enumerate(ys)}
    start_node = (x_lookup[round(start.x, 9)], y_lookup[round(start.y, 9)])
    goal_node = (x_lookup[round(goal.x, 9)], y_lookup[round(goal.y, 9)])

    def point(node: Tuple[int, int]) -> MachinePoint:
        return MachinePoint(xs[node[0]], ys[node[1]])

    free_cache: Dict[Tuple[int, int], bool] = {}

    def is_free(node: Tuple[int, int]) -> bool:
        if node in {start_node, goal_node}:
            return True
        if node not in free_cache:
            candidate = point(node)
            free_cache[node] = all(
                _distance(candidate, obstacle) + _EPSILON
                >= settings.obstacle_keepout_mm
                for obstacle in filtered
            )
        return free_cache[node]

    cardinal = ((1, 0), (-1, 0), (0, 1), (0, -1))
    diagonal = ((1, 1), (1, -1), (-1, 1), (-1, -1)) if settings.allow_diagonal else ()

    def neighbours(node: Tuple[int, int]) -> Iterable[Tuple[Tuple[int, int], float]]:
        ix, iy = node
        current = point(node)
        for dx, dy in cardinal + diagonal:
            nxt = (ix + dx, iy + dy)
            if not (0 <= nxt[0] < len(xs) and 0 <= nxt[1] < len(ys)):
                continue
            if not is_free(nxt):
                continue
            if dx and dy:
                if not is_free((ix + dx, iy)) or not is_free((ix, iy + dy)):
                    continue
            target = point(nxt)
            if not segment_is_clear(
                current, target, filtered, settings.obstacle_keepout_mm
            ):
                continue
            step_distance = _distance(current, target)
            clearance = min(
                (
                    _point_segment_distance(obstacle, current, target)
                    for obstacle in filtered
                ),
                default=float("inf"),
            )
            proximity_cost = 0.0
            if proximity_weight > 0 and clearance != float("inf"):
                proximity_cost = (
                    proximity_weight
                    * step_distance
                    / max(clearance, settings.obstacle_keepout_mm)
                )
            yield nxt, step_distance + proximity_cost

    frontier: List[Tuple[float, int, Tuple[int, int]]] = []
    sequence = 0
    heappush(frontier, (_distance(start, goal), sequence, start_node))
    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
    cost: Dict[Tuple[int, int], float] = {start_node: 0.0}
    closed: Set[Tuple[int, int]] = set()

    while frontier:
        _, _, current = heappop(frontier)
        if current in closed:
            continue
        closed.add(current)
        if len(closed) > settings.max_expanded_nodes:
            raise PlanningError(
                f"path search exceeded {settings.max_expanded_nodes} expanded nodes; "
                "increase grid_step_mm or max_expanded_nodes"
            )
        if current == goal_node:
            break
        for nxt, step_cost in neighbours(current):
            new_cost = cost[current] + step_cost
            if new_cost + _EPSILON < cost.get(nxt, float("inf")):
                cost[nxt] = new_cost
                came_from[nxt] = current
                sequence += 1
                priority = new_cost + _distance(point(nxt), goal)
                heappush(frontier, (priority, sequence, nxt))
    else:
        raise PlanningError(
            "no collision-free path was found. Check board state, workspace margin, "
            "grid_step_mm, and obstacle_keepout_mm"
        )

    nodes = [goal_node]
    while nodes[-1] != start_node:
        parent = came_from.get(nodes[-1])
        if parent is None:
            raise PlanningError("planner failed to reconstruct a complete path")
        nodes.append(parent)
    nodes.reverse()
    path = tuple(point(node) for node in nodes)
    if settings.simplify_path:
        path = _simplify(path, filtered, settings.obstacle_keepout_mm)
    return path


def plan_path(
    start: MachinePoint,
    goal: MachinePoint,
    obstacles: Sequence[MachinePoint],
    workspace: Workspace,
    settings: PlannerSettings,
) -> Tuple[MachinePoint, ...]:
    if settings.kind == "direct":
        return direct_path(start, goal, obstacles, workspace, settings)
    if settings.kind == "astar":
        return astar_path(start, goal, obstacles, workspace, settings)
    raise PlanningError(f"unsupported planner kind {settings.kind!r}")


def safest_path_to_any_goal(
    start: MachinePoint,
    goals: Sequence[MachinePoint],
    obstacles: Sequence[MachinePoint],
    workspace: Workspace,
    settings: PlannerSettings,
) -> Tuple[MachinePoint, ...]:
    if not goals:
        raise PlanningError("capture ejection requires at least one goal")
    candidates = []
    errors = []
    for index, goal in enumerate(goals):
        try:
            path = astar_path(
                start,
                goal,
                obstacles,
                workspace,
                settings,
                proximity_weight=settings.obstacle_keepout_mm,
            )
        except PlanningError as exc:
            errors.append(f"chute {index}: {exc}")
            continue
        length = sum(_distance(a, b) for a, b in zip(path, path[1:]))
        minimum_clearance = min(
            (
                _point_segment_distance(obstacle, a, b)
                for a, b in zip(path, path[1:])
                for obstacle in obstacles
            ),
            default=float("inf"),
        )
        candidates.append((-minimum_clearance, length, index, path))
    if not candidates:
        detail = " | ".join(errors)
        raise PlanningError(
            "no magnetically clear capture-ejection route was found"
            + (f": {detail}" if detail else "")
        )
    return min(candidates)[3]
