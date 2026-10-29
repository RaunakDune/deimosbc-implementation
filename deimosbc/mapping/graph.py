"""Map graph stored in contract state.

Sec. IV-A: "maps are represented as graphs with each vertex containing:
(i) which vertices it is connected with, (ii) real-world location, and
(iii) any additional sensor data."

Design notes
------------
* Vertices are *anchored*: a vertex keeps the position at which it was first
  observed. This makes insertions reversible, which the conflict rule needs
  ("both traces are discarded", Sec. IV-B.1) even after the earlier trace is
  already on chain. (The one residue: if a trace split an existing edge, the
  split vertex stays behind as a degree-2 vertex on the same geometry.)
* Every edge records the set of traces that support it. Removing a trace
  removes its support; edges with no support left disappear, and so do
  vertices that become isolated.
* Every edge is tagged with the *local map* it belongs to - the task cell
  containing its midpoint. The global map is the collation of all local maps
  (Sec. IV-B, "inferring the global pedestrian map by collating those local
  pedestrian maps").
* A uniform-grid spatial index keeps nearest-vertex / nearest-edge queries
  O(1) on average.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..encoding import hash_obj
from ..geo import XY, dist, point_segment_distance, project_on_segment

EdgeKey = Tuple[int, int]


def ekey(u: int, v: int) -> EdgeKey:
    return (u, v) if u < v else (v, u)


@dataclass
class Vertex:
    vid: int
    x: float
    y: float
    traces: Set[str] = field(default_factory=set)
    # (iii) additional sensor data, per contributing trace so it can be removed
    sensors: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def xy(self) -> XY:
        return (self.x, self.y)

    def sensor_summary(self) -> Dict[str, float]:
        acc: Dict[str, List[float]] = {}
        for readings in self.sensors.values():
            for k, v in readings.items():
                acc.setdefault(k, []).append(v)
        return {k: sum(v) / len(v) for k, v in sorted(acc.items())}


@dataclass
class Edge:
    u: int
    v: int
    cell: Tuple[int, int]
    support: Dict[str, str] = field(default_factory=dict)  # trace_id -> collector

    @property
    def key(self) -> EdgeKey:
        return ekey(self.u, self.v)

    @property
    def collectors(self) -> Set[str]:
        return set(self.support.values())


@dataclass
class Hazard:
    """A collector-reported obstruction (e.g. flood water, debris)."""

    x: float
    y: float
    trace_id: str
    collector: str
    kind: str = "blocked"
    heading: Optional[float] = None


class MapGraph:
    def __init__(self, index_cell: float, cell_of=None):
        self.index_cell = float(index_cell)
        self.cell_of = cell_of or (lambda xy: (0, 0))
        self.vertices: Dict[int, Vertex] = {}
        self.adj: Dict[int, Set[int]] = {}
        self.edges: Dict[EdgeKey, Edge] = {}
        self.hazards: Dict[str, List[Hazard]] = {}   # trace_id -> hazards
        self._vgrid: Dict[Tuple[int, int], Set[int]] = {}
        self._egrid: Dict[Tuple[int, int], Set[EdgeKey]] = {}
        self._next_vid = 1

    # ------------------------------------------------------------------ index
    def _g(self, p: XY) -> Tuple[int, int]:
        return (math.floor(p[0] / self.index_cell), math.floor(p[1] / self.index_cell))

    def _cells_around(self, p: XY, r: float) -> Iterable[Tuple[int, int]]:
        gx0, gy0 = self._g((p[0] - r, p[1] - r))
        gx1, gy1 = self._g((p[0] + r, p[1] + r))
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                yield (gx, gy)

    def _edge_cells(self, a: XY, b: XY) -> Iterable[Tuple[int, int]]:
        # conservative: every index cell overlapped by the edge's bounding box
        gx0, gy0 = self._g((min(a[0], b[0]), min(a[1], b[1])))
        gx1, gy1 = self._g((max(a[0], b[0]), max(a[1], b[1])))
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                yield (gx, gy)

    # --------------------------------------------------------------- queries
    def pos(self, vid: int) -> XY:
        return self.vertices[vid].xy

    def edge_points(self, k: EdgeKey) -> Tuple[XY, XY]:
        return self.pos(k[0]), self.pos(k[1])

    def edge_length(self, k: EdgeKey) -> float:
        a, b = self.edge_points(k)
        return dist(a, b)

    def nearest_vertex(self, p: XY, r: float) -> Optional[int]:
        best, best_d = None, r
        for c in self._cells_around(p, r):
            for vid in self._vgrid.get(c, ()):
                d = dist(p, self.vertices[vid].xy)
                if d <= best_d and (best is None or d < best_d or vid < best):
                    best, best_d = vid, d
        return best

    def edges_near(self, p: XY, r: float) -> List[Tuple[float, EdgeKey]]:
        seen: Set[EdgeKey] = set()
        out = []
        for c in self._cells_around(p, r):
            for k in self._egrid.get(c, ()):
                if k in seen:
                    continue
                seen.add(k)
                a, b = self.edge_points(k)
                d = point_segment_distance(p, a, b)
                if d <= r:
                    out.append((d, k))
        out.sort()
        return out

    def nearest_edge(self, p: XY, r: float) -> Optional[Tuple[EdgeKey, float, XY]]:
        near = self.edges_near(p, r)
        if not near:
            return None
        _, k = near[0]
        a, b = self.edge_points(k)
        t, q = project_on_segment(p, a, b)
        return k, t, q

    def hazards_near(self, p: XY, r: float) -> List[Hazard]:
        return [h for hs in self.hazards.values() for h in hs if dist(p, (h.x, h.y)) <= r]

    def total_length(self) -> float:
        return sum(self.edge_length(k) for k in self.edges)

    def shortest_hop_path(self, s: int, t: int, max_len: float, max_hops: int = 4) -> Optional[List[int]]:
        """Bounded Dijkstra: an existing path s->t no longer than ``max_len``."""
        import heapq

        if s == t:
            return [s]
        pq = [(0.0, 0, s, [s])]
        best: Dict[int, float] = {s: 0.0}
        while pq:
            d, hops, u, path = heapq.heappop(pq)
            if u == t:
                return path
            if hops >= max_hops:
                continue
            for w in sorted(self.adj.get(u, ())):
                nd = d + dist(self.pos(u), self.pos(w))
                if nd <= max_len and nd < best.get(w, math.inf):
                    best[w] = nd
                    heapq.heappush(pq, (nd, hops + 1, w, path + [w]))
        return None

    # -------------------------------------------------------------- mutation
    def add_vertex(self, p: XY) -> int:
        vid = self._next_vid
        self._next_vid += 1
        self.vertices[vid] = Vertex(vid, p[0], p[1])
        self.adj[vid] = set()
        self._vgrid.setdefault(self._g(p), set()).add(vid)
        return vid

    def _remove_vertex(self, vid: int) -> None:
        v = self.vertices.pop(vid)
        self.adj.pop(vid, None)
        cell = self._vgrid.get(self._g(v.xy))
        if cell is not None:
            cell.discard(vid)
            if not cell:
                del self._vgrid[self._g(v.xy)]

    def _index_edge(self, k: EdgeKey) -> None:
        a, b = self.edge_points(k)
        for c in self._edge_cells(a, b):
            self._egrid.setdefault(c, set()).add(k)

    def _unindex_edge(self, k: EdgeKey) -> None:
        a, b = self.edge_points(k)
        for c in self._edge_cells(a, b):
            s = self._egrid.get(c)
            if s is not None:
                s.discard(k)
                if not s:
                    del self._egrid[c]

    def add_edge_support(self, u: int, v: int, trace_id: str, collector: str) -> Tuple[bool, float]:
        """Add/extend edge u-v. Returns (is_new, length)."""
        k = ekey(u, v)
        length = self.edge_length(k) if k in self.edges else dist(self.pos(u), self.pos(v))
        if k in self.edges:
            self.edges[k].support[trace_id] = collector
            return False, length
        a, b = self.pos(u), self.pos(v)
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        self.edges[k] = Edge(k[0], k[1], self.cell_of(mid), {trace_id: collector})
        self.adj[u].add(v)
        self.adj[v].add(u)
        self._index_edge(k)
        return True, length

    def _remove_edge(self, k: EdgeKey) -> None:
        self._unindex_edge(k)
        del self.edges[k]
        self.adj[k[0]].discard(k[1])
        self.adj[k[1]].discard(k[0])

    def split_edge(self, k: EdgeKey, p: XY) -> int:
        """Insert a vertex at p on edge k; both halves inherit k's support."""
        e = self.edges[k]
        support = dict(e.support)
        self._remove_edge(k)
        w = self.add_vertex(p)
        for a in (k[0], k[1]):
            nk = ekey(a, w)
            pa, pw = self.pos(a), self.pos(w)
            mid = ((pa[0] + pw[0]) / 2, (pa[1] + pw[1]) / 2)
            self.edges[nk] = Edge(nk[0], nk[1], self.cell_of(mid), dict(support))
            self.adj[a].add(w)
            self.adj[w].add(a)
            self._index_edge(nk)
        # vertex inherits the traces that "passed through" it
        self.vertices[w].traces.update(support.keys())
        return w

    def add_hazard(self, h: Hazard) -> None:
        self.hazards.setdefault(h.trace_id, []).append(h)

    def remove_trace(self, trace_id: str) -> float:
        """Withdraw a trace's support. Returns map length removed."""
        removed = 0.0
        for k in sorted(k for k, e in self.edges.items() if trace_id in e.support):
            e = self.edges[k]
            del e.support[trace_id]
            if not e.support:
                removed += self.edge_length(k)
                self._remove_edge(k)
        for vid in sorted(self.vertices):
            v = self.vertices[vid]
            v.traces.discard(trace_id)
            v.sensors.pop(trace_id, None)
            if not self.adj.get(vid):
                self._remove_vertex(vid)
        self.hazards.pop(trace_id, None)
        return removed

    # ----------------------------------------------------------- local maps
    def local_map_ids(self) -> List[Tuple[int, int]]:
        return sorted({e.cell for e in self.edges.values()})

    def local_map(self, cell: Tuple[int, int]) -> List[EdgeKey]:
        return sorted(k for k, e in self.edges.items() if e.cell == cell)

    # ---------------------------------------------------------- serialisation
    def snapshot(self, frame=None, min_support: int = 1) -> Dict:
        """Canonical, self-describing global map (what requesters receive)."""
        edges = sorted(k for k, e in self.edges.items() if len(e.collectors) >= min_support)
        edge_set = set(edges)
        used = sorted({v for k in edges for v in k})
        verts = []
        for vid in used:
            v = self.vertices[vid]
            item = {
                "id": vid,
                "x": round(v.x, 2),
                "y": round(v.y, 2),
                "neighbors": sorted(w for w in self.adj[vid] if ekey(vid, w) in edge_set),
                "sensors": {k: round(val, 4) for k, val in v.sensor_summary().items()},
            }
            if frame is not None:
                lat, lon = frame.to_latlon(v.x, v.y)
                item["lat"], item["lon"] = round(lat, 7), round(lon, 7)
            verts.append(item)
        return {
            "vertices": verts,
            "edges": [
                {"u": k[0], "v": k[1], "local_map": list(self.edges[k].cell), "support": len(self.edges[k].collectors)}
                for k in edges
            ],
            "hazards": sorted(
                [{"x": round(h.x, 2), "y": round(h.y, 2), "kind": h.kind} for hs in self.hazards.values() for h in hs],
                key=lambda d: (d["x"], d["y"], d["kind"]),
            ),
        }

    def root(self) -> str:
        return hash_obj(self.snapshot())
