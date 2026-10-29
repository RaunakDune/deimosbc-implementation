"""Data-quality rules enforced by the map contract.

Sec. IV-C, *Upload Sensory Data*: "The miners validate the quality of the
uploaded data. The qualified data is accepted and recorded ... If the sensory
data is unqualified, the worker loses their deposit." and *Smart Contract
Creation*: the processor defines "rules for workers to ensure ... the quality
of the uploaded sensory data".

Each rule returns an error code string or None. Rules operate on already
projected coordinates (metres in the local frame).
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from ..geo import XY, dist


def check_trace_quality(
    params,
    points: Sequence[XY],
    times: Sequence[int],
    task_rect: Optional[Tuple[float, float, float, float]],
) -> Optional[str]:
    n = len(points)
    if n < params.min_trace_points:
        return "too_few_points"
    if n > params.max_trace_points:
        return "too_many_points"
    w, h = params.area_width_m, params.area_height_m
    if any(not (0.0 <= x <= w and 0.0 <= y <= h) for x, y in points):
        return "outside_area"
    for i in range(n - 1):
        d = dist(points[i], points[i + 1])
        dt = times[i + 1] - times[i]
        if d < params.min_point_spacing_m:
            return "points_too_close"       # duplicated / stuck GPS fixes
        if d > params.max_point_gap_m:
            return "gap_too_large"          # teleport or missing data
        if dt <= 0 or d / dt > params.max_speed_mps:
            return "implausible_speed"
    if task_rect is not None:
        x0, y0, x1, y1 = task_rect
        tol = params.task_overlap_tolerance_m
        if not any(x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol for x, y in points):
            return "outside_task"
    return None


def check_hazards(params, hazards: Sequence[XY]) -> Optional[str]:
    w, h = params.area_width_m, params.area_height_m
    if len(hazards) > params.max_trace_points:
        return "too_many_hazards"
    if any(not (0.0 <= x <= w and 0.0 <= y <= h) for x, y in hazards):
        return "hazard_outside_area"
    return None


def task_rect_for_cell(params, cell: Tuple[int, int]) -> Tuple[float, float, float, float]:
    s = params.task_cell_size_m
    return (cell[0] * s, cell[1] * s, (cell[0] + 1) * s, (cell[1] + 1) * s)


def cell_of_xy(params, xy: XY) -> Tuple[int, int]:
    s = params.task_cell_size_m
    cx = min(max(int(xy[0] // s), 0), params.n_cells_x - 1)
    cy = min(max(int(xy[1] // s), 0), params.n_cells_y - 1)
    return (cx, cy)


__all__: List[str] = ["check_trace_quality", "check_hazards", "task_rect_for_cell", "cell_of_xy"]
