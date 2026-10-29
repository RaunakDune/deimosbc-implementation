"""Volunteer movement and GPS noise.

Walkers move along *open* streets only (they physically cannot cross a flooded
street). On reaching an intersection they may notice an adjacent blocked street
and report a hazard a short distance into it.

GPS noise uses the same knobs as Proteus' evaluation (jitter, offset,
duplication, deletion) so the two systems can be stress-tested under
identical data-quality regimes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, List, Optional, Set, Tuple

from ..geo import XY, bearing, dist
from .roadnet import RoadNetwork


@dataclass
class GpsNoise:
    jitter_m: float = 4.0          # per-fix Gaussian error (sigma)
    offset_m: float = 2.0          # constant per-device bias magnitude
    duplication: float = 0.02      # probability a fix repeats the previous one
    deletion: float = 0.03         # probability a fix is dropped

    def device_bias(self, rng: random.Random) -> XY:
        a = rng.uniform(0, 2 * math.pi)
        return (self.offset_m * math.cos(a), self.offset_m * math.sin(a))


FAULTY_SENSOR = GpsNoise(jitter_m=18.0, offset_m=35.0, duplication=0.2, deletion=0.15)


class Walker:
    def __init__(self, road: RoadNetwork, start: int, rng: random.Random, speed: float, component: Set[int]):
        self.road = road
        self.node = start
        self.pos: XY = road.nodes[start]
        self.rng = rng
        self.speed = speed
        self.component = component
        self.path: List[int] = []
        self.goal: Optional[XY] = None
        self.visited_edges: Set[int] = set()
        self.reported: Set[int] = set()
        self._seg_progress = 0.0

    @property
    def idle(self) -> bool:
        return not self.path

    def _plan(self, target: int) -> bool:
        """Route to ``target``; if we are mid-street, finish the street first."""
        if self.path and self._seg_progress > 0:
            via = self.path[0]
            p = self.road.shortest_path(via, target)
            if p is None:
                return False
            self.path = p
        else:
            p = self.road.shortest_path(self.node, target)
            if p is None:
                return False
            self.path = p[1:]
            self._seg_progress = 0.0
        return True

    def go_to(self, xy: XY) -> bool:
        target = self.road.nearest_node(xy, self.component)
        if not self._plan(target):
            return False
        self.goal = xy
        return True

    def explore(self, rect: Tuple[float, float, float, float], margin: float = 30.0) -> bool:
        """Pick a destination inside ``rect``, preferring unvisited streets."""
        x0, y0, x1, y1 = rect
        inside = [n for n in self.component
                  if x0 - margin <= self.road.nodes[n][0] <= x1 + margin and y0 - margin <= self.road.nodes[n][1] <= y1 + margin]
        if not inside:
            return self.go_to(((x0 + x1) / 2, (y0 + y1) / 2))

        def novelty(n: int) -> int:
            return sum(1 for _, ei in self.road.open_neighbours(n) if ei not in self.visited_edges)

        inside.sort()
        weights = [1 + 4 * novelty(n) for n in inside]
        target = self.rng.choices(inside, weights=weights, k=1)[0]
        if target == self.node and not self.path:
            return False
        if not self._plan(target):
            return False
        self.goal = self.road.nodes[target]
        return True

    def step(self, dt: float, on_node: Optional[Callable[[int], None]] = None) -> XY:
        budget = self.speed * dt
        while budget > 0 and self.path:
            nxt = self.path[0]
            a, b = self.road.nodes[self.node], self.road.nodes[nxt]
            seg = dist(a, b)
            remaining = seg - self._seg_progress
            if budget >= remaining:
                budget -= remaining
                for v, ei in self.road.adj[self.node]:
                    if v == nxt:
                        self.visited_edges.add(ei)
                self.node = nxt
                self.path.pop(0)
                self._seg_progress = 0.0
                self.pos = b
                if on_node:
                    on_node(nxt)
            else:
                self._seg_progress += budget
                t = self._seg_progress / seg if seg else 1.0
                self.pos = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                budget = 0
        return self.pos

    def hazards_seen(self, node: int, prob: float) -> List[Tuple[XY, float]]:
        """Blocked streets visible from an intersection: (location, heading)."""
        out = []
        for v, ei in self.road.blocked_incident(node):
            if ei in self.reported or self.rng.random() >= prob:
                continue
            self.reported.add(ei)
            a, b = self.road.nodes[node], self.road.nodes[v]
            f = self.rng.uniform(0.3, 0.45)
            out.append(((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])), bearing(a, b)))
        return out


def fabricate_along_blocked(road: RoadNetwork, rng: random.Random, near: XY, radius: float, t0: int,
                            spacing: float = 10.0, speed: float = 1.4) -> List[Tuple[XY, int]]:
    """A malicious trace claiming that a blocked street is passable."""
    cands = sorted(ei for ei in road.blocked if dist(road.midpoint(ei), near) <= radius)
    if not cands:
        return []
    ei = rng.choice(cands)
    a, b = road.edge_xy(ei)
    if rng.random() < 0.5:
        a, b = b, a
    n = max(3, int(dist(a, b) // spacing) + 1)
    out = []
    for i in range(n):
        t = i / (n - 1)
        x = a[0] + t * (b[0] - a[0]) + rng.gauss(0, 2.0)
        y = a[1] + t * (b[1] - a[1]) + rng.gauss(0, 2.0)
        out.append(((x, y), t0 + int(round(i * spacing / speed))))
    return out
