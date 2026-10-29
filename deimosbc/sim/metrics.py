"""Map quality against ground truth.

* **recall**      - share of open-street length (sampled every ``step`` m)
                    that lies within ``tol`` m of a mapped edge.
* **precision**   - share of mapped length within ``tol`` m of an open street.
* **contamination** - share of *blocked*-street length (interior, away from
                    intersections) that the map claims is passable. This is
                    the safety metric for first responders.

Sampling-based edge matching mirrors the "edge accuracy" style evaluation used
in Proteus.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence, Tuple

from ..geo import XY, dist, interpolate, point_segment_distance
from .roadnet import RoadNetwork

Seg = Tuple[XY, XY]


class SegmentIndex:
    def __init__(self, segs: Sequence[Seg], cell: float = 50.0):
        self.cell = cell
        self.segs = list(segs)
        self.grid: Dict[Tuple[int, int], List[int]] = {}
        for i, (a, b) in enumerate(self.segs):
            for c in self._cells(a, b, 0.0):
                self.grid.setdefault(c, []).append(i)

    def _cells(self, a: XY, b: XY, pad: float):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for gx in range(math.floor((x0 - pad) / self.cell), math.floor((x1 + pad) / self.cell) + 1):
            for gy in range(math.floor((y0 - pad) / self.cell), math.floor((y1 + pad) / self.cell) + 1):
                yield (gx, gy)

    def near(self, p: XY, tol: float) -> bool:
        seen = set()
        for c in self._cells(p, p, tol):
            for i in self.grid.get(c, ()):
                if i in seen:
                    continue
                seen.add(i)
                a, b = self.segs[i]
                if point_segment_distance(p, a, b) <= tol:
                    return True
        return False


def map_segments(snapshot: Dict) -> List[Seg]:
    pos = {v["id"]: (v["x"], v["y"]) for v in snapshot["vertices"]}
    return [(pos[e["u"]], pos[e["v"]]) for e in snapshot["edges"] if e["u"] in pos and e["v"] in pos]


def _coverage(samples: Iterable[XY], index: SegmentIndex, tol: float) -> Tuple[int, int]:
    hit = tot = 0
    for p in samples:
        tot += 1
        hit += index.near(p, tol)
    return hit, tot


def evaluate_map(snapshot: Dict, road: RoadNetwork, tol: float = 15.0, step: float = 5.0, end_clearance: float = 25.0) -> Dict[str, float]:
    segs = map_segments(snapshot)
    open_segs = [road.edge_xy(ei) for ei in range(len(road.edges)) if road.is_open(ei)]
    mi = SegmentIndex(segs)
    oi = SegmentIndex(open_segs)

    rec_hit = rec_tot = 0
    for a, b in open_segs:
        h, t = _coverage(interpolate([a, b], step), mi, tol)
        rec_hit, rec_tot = rec_hit + h, rec_tot + t

    pre_hit = pre_tot = 0
    for a, b in segs:
        h, t = _coverage(interpolate([a, b], step), oi, tol)
        pre_hit, pre_tot = pre_hit + h, pre_tot + t

    con_hit = con_tot = 0
    for ei in sorted(road.blocked):
        a, b = road.edge_xy(ei)
        pts = [p for p in interpolate([a, b], step) if dist(p, a) > end_clearance and dist(p, b) > end_clearance]
        h, t = _coverage(pts, mi, tol)
        con_hit, con_tot = con_hit + h, con_tot + t

    mapped_len = sum(dist(a, b) for a, b in segs)
    return {
        "recall": rec_hit / rec_tot if rec_tot else 0.0,
        "precision": pre_hit / pre_tot if pre_tot else 0.0,
        "contamination": con_hit / con_tot if con_tot else 0.0,
        "mapped_length_m": round(mapped_len, 1),
        "open_length_m": round(road.length(open_only=True), 1),
        "edges": len(segs),
    }


def f1(m: Dict[str, float]) -> float:
    p, r = m["precision"], m["recall"]
    return 2 * p * r / (p + r) if p + r else 0.0
