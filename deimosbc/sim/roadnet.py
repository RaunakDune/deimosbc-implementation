"""Ground-truth road networks and disaster damage.

Two sources:

* :meth:`RoadNetwork.synthetic_grid` - an irregular street grid (default
  scenario; reproducible from a seed).
* :meth:`RoadNetwork.from_edge_csv` - an edge list in the ProteusData format
  (``x0,y0,x1,y1`` as lon/lat), e.g. ``houston_downtown_map_gen.csv`` or
  ``university_map_gen.csv`` from https://github.com/UH-DAIS/ProteusData,
  so DEIMOSBC and Proteus can be evaluated on the same ground truth.

Damage model (Sec. II, Category 4/5 hurricane on the Gulf Coast): circular
flood zones block every street whose midpoint is inside them; scattered
debris blocks an additional random fraction of streets.
"""

from __future__ import annotations

import csv
import heapq
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from ..geo import XY, LocalFrame, dist

Edge = Tuple[int, int]


@dataclass
class FloodZone:
    x: float
    y: float
    radius: float


@dataclass
class RoadNetwork:
    nodes: List[XY]
    edges: List[Edge]
    blocked: Set[int] = field(default_factory=set)
    floods: List[FloodZone] = field(default_factory=list)

    def __post_init__(self):
        self.adj: Dict[int, List[Tuple[int, int]]] = {i: [] for i in range(len(self.nodes))}
        for ei, (u, v) in enumerate(self.edges):
            self.adj[u].append((v, ei))
            self.adj[v].append((u, ei))

    # ---------------------------------------------------------- constructors
    @classmethod
    def synthetic_grid(
        cls,
        width: float,
        height: float,
        block: float = 100.0,
        margin: float = 40.0,
        jitter: float = 6.0,
        drop_frac: float = 0.06,
        rng: Optional[random.Random] = None,
    ) -> "RoadNetwork":
        rng = rng or random.Random(0)
        nx = int((width - 2 * margin) // block) + 1
        ny = int((height - 2 * margin) // block) + 1
        idx: Dict[Tuple[int, int], int] = {}
        nodes: List[XY] = []
        for j in range(ny):
            for i in range(nx):
                idx[(i, j)] = len(nodes)
                nodes.append((margin + i * block + rng.uniform(-jitter, jitter), margin + j * block + rng.uniform(-jitter, jitter)))
        edges: List[Edge] = []
        for j in range(ny):
            for i in range(nx):
                for di, dj in ((1, 0), (0, 1)):
                    if (i + di, j + dj) in idx and rng.random() >= drop_frac:
                        edges.append((idx[(i, j)], idx[(i + di, j + dj)]))
        return cls(nodes, edges)

    @classmethod
    def from_edge_csv(cls, path: str, margin: float = 40.0, snap_m: float = 1.0) -> Tuple["RoadNetwork", float, float, float, float]:
        """Load a lon/lat edge list. Returns (network, origin_lat, origin_lon, width_m, height_m).

        Endpoints closer than ``snap_m`` are merged into one intersection. Use a
        larger value (10-20 m) for fragmentary edge lists such as map outputs.
        """
        raw: List[Tuple[float, float, float, float]] = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                raw.append((float(row["x0"]), float(row["y0"]), float(row["x1"]), float(row["y1"])))
        if not raw:
            raise ValueError(f"no edges in {path}")
        lons = [r[0] for r in raw] + [r[2] for r in raw]
        lats = [r[1] for r in raw] + [r[3] for r in raw]
        f0 = LocalFrame(min(lats), min(lons))
        lat0, lon0 = f0.to_latlon(-margin, -margin)
        frame = LocalFrame(lat0, lon0)
        grid: Dict[Tuple[int, int], List[int]] = {}
        nodes: List[XY] = []
        cell = max(snap_m, 1e-6)

        def node(lon: float, lat: float) -> int:
            x, y = frame.to_xy(lat, lon)
            gx, gy = math.floor(x / cell), math.floor(y / cell)
            best, best_d = None, snap_m
            for ix in (gx - 1, gx, gx + 1):
                for iy in (gy - 1, gy, gy + 1):
                    for i in grid.get((ix, iy), ()):
                        d = dist(nodes[i], (x, y))
                        if d <= best_d:
                            best, best_d = i, d
            if best is not None:
                return best
            nodes.append((x, y))
            grid.setdefault((gx, gy), []).append(len(nodes) - 1)
            return len(nodes) - 1

        edges: Set[Edge] = set()
        for x0, y0, x1, y1 in raw:
            u, v = node(x0, y0), node(x1, y1)
            if u != v:
                edges.add((min(u, v), max(u, v)))
        w = max(p[0] for p in nodes) + margin
        h = max(p[1] for p in nodes) + margin
        return cls(nodes, sorted(edges)), lat0, lon0, w, h

    # --------------------------------------------------------------- damage
    def apply_hurricane(self, rng: random.Random, width: float, height: float, n_floods: int = 2,
                        flood_radius: Tuple[float, float] = (180.0, 320.0), debris_frac: float = 0.04) -> None:
        self.floods = [
            FloodZone(rng.uniform(0.2, 0.8) * width, rng.uniform(0.2, 0.8) * height, rng.uniform(*flood_radius))
            for _ in range(n_floods)
        ]
        for ei, (u, v) in enumerate(self.edges):
            m = self.midpoint(ei)
            if any(dist(m, (z.x, z.y)) <= z.radius for z in self.floods) or rng.random() < debris_frac:
                self.blocked.add(ei)

    # -------------------------------------------------------------- queries
    def midpoint(self, ei: int) -> XY:
        u, v = self.edges[ei]
        a, b = self.nodes[u], self.nodes[v]
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    def edge_xy(self, ei: int) -> Tuple[XY, XY]:
        u, v = self.edges[ei]
        return self.nodes[u], self.nodes[v]

    def is_open(self, ei: int) -> bool:
        return ei not in self.blocked

    def open_neighbours(self, u: int) -> List[Tuple[int, int]]:
        return [(v, ei) for v, ei in self.adj[u] if ei not in self.blocked]

    def blocked_incident(self, u: int) -> List[Tuple[int, int]]:
        return [(v, ei) for v, ei in self.adj[u] if ei in self.blocked]

    def open_components(self) -> List[Set[int]]:
        seen: Set[int] = set()
        comps = []
        for s in range(len(self.nodes)):
            if s in seen or not self.open_neighbours(s):
                continue
            comp, stack = set(), [s]
            while stack:
                u = stack.pop()
                if u in comp:
                    continue
                comp.add(u)
                stack.extend(v for v, _ in self.open_neighbours(u) if v not in comp)
            seen |= comp
            comps.append(comp)
        comps.sort(key=len, reverse=True)
        return comps

    def nearest_node(self, p: XY, among: Optional[Set[int]] = None) -> int:
        cand = among if among is not None else range(len(self.nodes))
        return min(cand, key=lambda i: (dist(self.nodes[i], p), i))

    def shortest_path(self, s: int, t: int) -> Optional[List[int]]:
        """Dijkstra over open streets."""
        if s == t:
            return [s]
        pq = [(0.0, s)]
        prev: Dict[int, int] = {}
        best = {s: 0.0}
        while pq:
            d, u = heapq.heappop(pq)
            if u == t:
                path = [t]
                while path[-1] != s:
                    path.append(prev[path[-1]])
                return path[::-1]
            if d > best.get(u, math.inf):
                continue
            for v, _ in self.open_neighbours(u):
                nd = d + dist(self.nodes[u], self.nodes[v])
                if nd < best.get(v, math.inf):
                    best[v], prev[v] = nd, u
                    heapq.heappush(pq, (nd, v))
        return None

    def length(self, open_only: Optional[bool] = None) -> float:
        tot = 0.0
        for ei in range(len(self.edges)):
            if open_only is None or (open_only == self.is_open(ei)):
                a, b = self.edge_xy(ei)
                tot += dist(a, b)
        return tot
