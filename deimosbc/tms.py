"""Trace Management System (Sec. IV-B.2), run on every Data Collector.

"This system facilitates the periodic collection of consecutive GPS points
through sensors present in each volunteer node and creation of trajectory
traces from such points. In our system, this collection of points creates a
trajectory based on the displacement of volunteer nodes rather than time, as
pioneered by Bhattacharjee et al. To smooth out abrupt direction changes on
part of the collectors, we use trajectory segmentation."

Pipeline::

    raw GPS fixes -> DisplacementSampler -> TrajectorySegmenter -> traces
                                         (split on sharp turns / gaps /
                                          max length, light smoothing)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .geo import XY, angle_diff, bearing, dist


@dataclass
class Fix:
    x: float
    y: float
    t: int
    sensors: Dict[str, float] = field(default_factory=dict)

    @property
    def xy(self) -> XY:
        return (self.x, self.y)


class DisplacementSampler:
    """Emit a fix only after the volunteer has moved ``threshold`` metres.

    Fixes implying a speed above ``max_speed`` relative to the last emitted
    sample are treated as GPS outliers and dropped (on-device cleaning, so
    honest volunteers are not penalised by the contract's speed rule for a
    single noisy fix).
    """

    def __init__(self, threshold: float, max_speed: Optional[float] = None):
        self.threshold = threshold
        self.max_speed = max_speed
        self.last: Optional[Fix] = None
        self.outliers = 0

    def feed(self, fix: Fix) -> Optional[Fix]:
        if self.last is None:
            self.last = fix
            return fix
        d = dist(self.last.xy, fix.xy)
        if d < self.threshold:
            return None
        if self.max_speed is not None and d > self.max_speed * max(fix.t - self.last.t, 0):
            self.outliers += 1
            return None
        self.last = fix
        return fix

    def reset(self) -> None:
        self.last = None


class TrajectorySegmenter:
    """Cut the sampled stream into traces.

    A trace ends when
      * the heading changes by more than ``angle_deg`` between consecutive
        segments (an abrupt direction change, e.g. a U-turn or GPS spike),
      * consecutive samples are more than ``gap_m`` apart (signal loss), or
      * it reaches ``max_points``.

    On a sharp-turn split the turning point is kept as the first point of the
    next trace so that the Data Summarization System can chain the pieces
    back together. Interior points are smoothed with a 3-point moving
    average, which suppresses GPS jitter without moving the endpoints.
    """

    def __init__(self, angle_deg: float, gap_m: float, min_points: int, max_points: int, smooth: bool = True):
        self.angle = angle_deg
        self.gap = gap_m
        self.min_points = min_points
        self.max_points = max_points
        self.smooth = smooth
        self.current: List[Fix] = []
        self.done: List[List[Fix]] = []

    def _close(self, carry_last: bool) -> None:
        cur = self.current
        if len(cur) >= self.min_points:
            self.done.append(self._smoothed(cur))
        self.current = [cur[-1]] if (carry_last and cur) else []

    def _smoothed(self, pts: List[Fix]) -> List[Fix]:
        if not self.smooth or len(pts) < 3:
            return list(pts)
        out = [pts[0]]
        for i in range(1, len(pts) - 1):
            a, b, c = pts[i - 1], pts[i], pts[i + 1]
            out.append(Fix((a.x + b.x + c.x) / 3, (a.y + b.y + c.y) / 3, b.t, b.sensors))
        out.append(pts[-1])
        return out

    def feed(self, fix: Fix) -> None:
        cur = self.current
        if cur and dist(cur[-1].xy, fix.xy) > self.gap:
            self._close(carry_last=False)
            cur = self.current
        if len(cur) >= 2:
            h1 = bearing(cur[-2].xy, cur[-1].xy)
            h2 = bearing(cur[-1].xy, fix.xy)
            if angle_diff(h1, h2) > self.angle:
                self._close(carry_last=True)
        self.current.append(fix)
        if len(self.current) >= self.max_points:
            self._close(carry_last=True)

    def flush(self) -> None:
        if self.current:
            self._close(carry_last=False)

    def pop_traces(self) -> List[List[Fix]]:
        out, self.done = self.done, []
        return out


class TraceManager:
    """Collector-side TMS: raw fixes in, finished traces out."""

    def __init__(self, params):
        # keep a safety margin under the contract's limit
        self.sampler = DisplacementSampler(params.sample_displacement_m, max_speed=0.8 * params.max_speed_mps)
        self.segmenter = TrajectorySegmenter(
            angle_deg=params.segmentation_angle_deg,
            gap_m=params.segmentation_gap_m,
            min_points=params.min_trace_points,
            max_points=params.max_trace_points,
        )

    def feed(self, fix: Fix) -> None:
        s = self.sampler.feed(fix)
        if s is not None:
            self.segmenter.feed(s)

    def flush(self) -> None:
        self.segmenter.flush()
        self.sampler.reset()

    def pop_traces(self) -> List[List[Fix]]:
        return self.segmenter.pop_traces()
