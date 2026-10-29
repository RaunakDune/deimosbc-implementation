"""Data Summarization System (Sec. IV-B.1).

"The system looks at incoming traces and checks for overlapping positions. In
case of overlap, the system attempts to chain the traces together to create a
large, interconnected trace. In case of conflict, both traces are discarded
and a message confirming such is sent back to the original Collectors."

The merge strategy is pluggable through :class:`MapMerger` so that alternative
path-combination algorithms (e.g. Proteus) can be dropped into the smart
contract without touching the ledger, see ``docs/PROTEUS_INTEGRATION.md``.
A merger must be deterministic: every Data Processor re-executes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

from ..geo import XY, bearing, dist, point_segment_distance, project_on_segment, undirected_angle_diff
from .graph import Hazard, MapGraph, ekey


@dataclass
class HazardReport:
    x: float
    y: float
    heading: Optional[float] = None
    kind: str = "blocked"


@dataclass
class ConflictReport:
    """Already-accepted traces that conflict with an incoming one.

    ``geometric``  - traces whose edges the newcomer contradicts in shape.
    ``hazard``     - traces whose hazard reports the newcomer traverses.
    ``traversal``  - traces whose edges the newcomer reports as blocked.
    """

    geometric: Set[str] = field(default_factory=set)
    hazard: Set[str] = field(default_factory=set)
    traversal: Set[str] = field(default_factory=set)

    def all(self) -> List[str]:
        return sorted(self.geometric | self.hazard | self.traversal)

    def __bool__(self) -> bool:
        return bool(self.geometric or self.hazard or self.traversal)


@dataclass
class MergeResult:
    new_length: float = 0.0
    matched_length: float = 0.0
    new_length_by_cell: Dict[Tuple[int, int], float] = field(default_factory=dict)
    path: List[int] = field(default_factory=list)


class MapMerger(Protocol):
    name: str

    def find_conflicts(
        self, graph: MapGraph, points: Sequence[XY], hazards: Sequence[HazardReport], trace_id: str
    ) -> ConflictReport:
        """Report already-accepted traces that conflict with this one."""

    def integrate(
        self,
        graph: MapGraph,
        points: Sequence[XY],
        hazards: Sequence[HazardReport],
        trace_id: str,
        collector: str,
        sensors: Sequence[Dict[str, float]],
    ) -> MergeResult:
        """Chain the trace into the graph."""


class OverlapChainMerger:
    """Default merger, a direct reading of Sec. IV-B.1.

    *Overlap*: a trace point within ``merge_radius`` of an existing vertex is
    snapped to it; otherwise, if it lies within ``merge_radius`` of an existing
    edge, that edge is split and the point is snapped to the new vertex.

    *Chaining*: consecutive snapped vertices are joined. If the map already
    has a short path between them (<= 4 hops, <= 1.3x the straight-line
    distance + radius) the trace adds support to that path instead of
    creating a parallel shortcut edge.

    *Conflict* (either of):
      1. geometric - the trace runs inside the corridor of existing edges
         for at least ``conflict_min_run`` metres while disagreeing with all
         of them in heading by more than ``max_angle_deviation`` degrees
         (a crossing street is too short to qualify);
      2. hazard - the trace traverses, in the blocked direction, a point
         that another trace reported as impassable, or reports a hazard on a
         path another trace traversed.
    """

    name = "overlap-chain"

    def __init__(
        self,
        merge_radius: float,
        max_angle_deviation: float,
        conflict_min_run: float,
        hazard_radius: float,
    ):
        self.r = merge_radius
        self.max_dev = max_angle_deviation
        self.min_run = conflict_min_run
        self.hazard_r = hazard_radius

    # ------------------------------------------------------------- conflicts
    def find_conflicts(self, graph, points, hazards, trace_id) -> ConflictReport:
        rep = ConflictReport()
        run: Dict[str, float] = {}
        for i in range(len(points) - 1):
            a, b = points[i], points[i + 1]
            seg_len = dist(a, b)
            if seg_len == 0:
                continue
            h = bearing(a, b)
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            misaligned: Set[str] = set()
            aligned_any = False
            for _, k in graph.edges_near(mid, self.r):
                ea, eb = graph.edge_points(k)
                if point_segment_distance(a, ea, eb) > self.r or point_segment_distance(b, ea, eb) > self.r:
                    continue  # not co-located, just nearby
                if graph.edge_length(k) == 0:
                    continue
                if undirected_angle_diff(h, bearing(ea, eb)) <= self.max_dev:
                    aligned_any = True
                    break
                misaligned.update(t for t in graph.edges[k].support if t != trace_id)
            current = set() if aligned_any else misaligned
            for t in list(run):
                if t not in current:
                    del run[t]
            for t in current:
                run[t] = run.get(t, 0.0) + seg_len
                if run[t] >= self.min_run:
                    rep.geometric.add(t)

            # 2a. traversing a reported hazard (in the blocked direction)
            for hz in graph.hazards_near(mid, self.hazard_r + seg_len / 2):
                if hz.trace_id == trace_id:
                    continue
                if hz.heading is not None and undirected_angle_diff(hz.heading, h) > self.max_dev:
                    continue  # crossing in front of the obstruction, not through it
                if point_segment_distance((hz.x, hz.y), a, b) <= self.hazard_r:
                    rep.hazard.add(hz.trace_id)

        # 2b. reporting a hazard on a traversed path
        for hz in hazards:
            for _, k in graph.edges_near((hz.x, hz.y), self.hazard_r):
                if hz.heading is not None:
                    ea, eb = graph.edge_points(k)
                    if undirected_angle_diff(hz.heading, bearing(ea, eb)) > self.max_dev:
                        continue  # a cross street at the hazard, not the blocked path
                rep.traversal.update(t for t in graph.edges[k].support if t != trace_id)
        return rep

    # ------------------------------------------------------------ integration
    def _snap(self, graph: MapGraph, p: XY, exclude: Set[int], trace_id: str) -> Tuple[int, bool]:
        """Return (vertex id, created?)."""
        best, best_d = None, self.r
        for c in graph._cells_around(p, self.r):
            for vid in graph._vgrid.get(c, ()):
                if vid in exclude:
                    continue
                d = dist(p, graph.vertices[vid].xy)
                if d < best_d or (d == best_d and best is not None and vid < best):
                    best, best_d = vid, d
        if best is not None:
            return best, False
        for d, k in graph.edges_near(p, self.r):
            if k[0] in exclude or k[1] in exclude:
                continue
            ea, eb = graph.edge_points(k)
            _, q = project_on_segment(p, ea, eb)
            # never split right next to an endpoint: snap to it instead
            if dist(q, ea) < 1.0:
                return k[0], False
            if dist(q, eb) < 1.0:
                return k[1], False
            return graph.split_edge(k, q), True
        return graph.add_vertex(p), True

    def integrate(self, graph, points, hazards, trace_id, collector, sensors) -> MergeResult:
        res = MergeResult()
        created: Set[int] = set()
        along: List[float] = []      # cumulative along-trace distance per path entry
        cum = 0.0
        for i, p in enumerate(points):
            if i:
                cum += dist(points[i - 1], p)
            # Vertices this trace placed within 3r along the trace are excluded
            # so consecutive samples don't collapse onto each other.
            exclude = {res.path[j] for j in range(len(res.path)) if cum - along[j] < 3 * self.r and res.path[j] in created}
            vid, was_created = self._snap(graph, p, exclude, trace_id)
            if was_created:
                created.add(vid)
            v = graph.vertices[vid]
            v.traces.add(trace_id)
            if i < len(sensors) and sensors[i]:
                v.sensors[trace_id] = dict(sensors[i])
            if res.path and res.path[-1] == vid:
                continue
            if res.path:
                self._connect(graph, res.path[-1], vid, trace_id, collector, res)
            res.path.append(vid)
            along.append(cum)
        for hz in hazards:
            graph.add_hazard(Hazard(hz.x, hz.y, trace_id, collector, hz.kind, hz.heading))
        for vid in sorted(created):
            if vid in graph.vertices and not graph.adj.get(vid):
                graph._remove_vertex(vid)
        return res

    def _connect(self, graph: MapGraph, u: int, v: int, trace_id: str, collector: str, res: MergeResult) -> None:
        if u not in graph.vertices or v not in graph.vertices:
            return
        if ekey(u, v) not in graph.edges:
            d = dist(graph.pos(u), graph.pos(v))
            path = graph.shortest_hop_path(u, v, max_len=1.3 * d + self.r, max_hops=4)
            if path and len(path) > 2:
                for a, b in zip(path, path[1:]):
                    _, length = graph.add_edge_support(a, b, trace_id, collector)
                    res.matched_length += length
                return
        is_new, length = graph.add_edge_support(u, v, trace_id, collector)
        if is_new:
            res.new_length += length
            cell = graph.edges[ekey(u, v)].cell
            res.new_length_by_cell[cell] = res.new_length_by_cell.get(cell, 0.0) + length
        else:
            res.matched_length += length


def _overlap_chain(params) -> MapMerger:
    return OverlapChainMerger(
        merge_radius=params.merge_radius_m,
        max_angle_deviation=params.max_angle_deviation_deg,
        conflict_min_run=params.conflict_min_run_m,
        hazard_radius=params.hazard_radius_m,
    )


#: Registry of merge strategies. The genesis parameter ``merger`` selects one,
#: so every processor provably runs the same algorithm. Register additional
#: strategies (e.g. a Proteus-based merger) with :func:`register_merger`.
MERGERS: Dict[str, Callable[..., MapMerger]] = {"overlap-chain": _overlap_chain}


def register_merger(name: str, factory: Callable[..., MapMerger]) -> None:
    MERGERS[name] = factory


def build_merger(params) -> MapMerger:
    try:
        factory = MERGERS[params.merger]
    except KeyError:
        raise ValueError(f"unknown merger {params.merger!r}; registered: {sorted(MERGERS)}") from None
    return factory(params)
