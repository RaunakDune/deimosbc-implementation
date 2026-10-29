"""Geometry helpers.

On-chain coordinates are integers in units of 1e-7 degrees ("E7", the same
convention as OpenStreetMap / MAVLink) so that signed payloads never contain
floats. All map-building math happens in a local equirectangular frame
(metres east / north of an origin), which is accurate to well under a metre
over the few-kilometre extent of a disaster response area.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

EARTH_RADIUS_M = 6_371_008.8
E7 = 10_000_000

XY = Tuple[float, float]


def to_e7(deg: float) -> int:
    return int(round(deg * E7))


def from_e7(v: int) -> float:
    return v / E7


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class LocalFrame:
    """Equirectangular projection around an origin (lat0, lon0)."""

    lat0: float
    lon0: float

    @property
    def _kx(self) -> float:
        return math.radians(1.0) * EARTH_RADIUS_M * math.cos(math.radians(self.lat0))

    @property
    def _ky(self) -> float:
        return math.radians(1.0) * EARTH_RADIUS_M

    def to_xy(self, lat: float, lon: float) -> XY:
        return ((lon - self.lon0) * self._kx, (lat - self.lat0) * self._ky)

    def to_latlon(self, x: float, y: float) -> Tuple[float, float]:
        return (self.lat0 + y / self._ky, self.lon0 + x / self._kx)

    def e7_to_xy(self, lat_e7: int, lon_e7: int) -> XY:
        return self.to_xy(from_e7(lat_e7), from_e7(lon_e7))

    def xy_to_e7(self, x: float, y: float) -> Tuple[int, int]:
        lat, lon = self.to_latlon(x, y)
        return to_e7(lat), to_e7(lon)


def dist(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def bearing(a: XY, b: XY) -> float:
    """Compass-free heading of segment a->b in degrees, [0, 360)."""
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 360.0


def angle_diff(h1: float, h2: float) -> float:
    """Smallest absolute difference between two headings, [0, 180]."""
    d = abs(h1 - h2) % 360.0
    return 360.0 - d if d > 180.0 else d


def undirected_angle_diff(h1: float, h2: float) -> float:
    """Difference between two *lines* (direction ignored), [0, 90]."""
    d = angle_diff(h1, h2)
    return 180.0 - d if d > 90.0 else d


def project_on_segment(p: XY, a: XY, b: XY) -> Tuple[float, XY]:
    """Return (t, closest point) of p onto segment ab, t in [0, 1]."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    l2 = dx * dx + dy * dy
    if l2 == 0.0:
        return 0.0, a
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2
    t = max(0.0, min(1.0, t))
    return t, (ax + t * dx, ay + t * dy)


def point_segment_distance(p: XY, a: XY, b: XY) -> float:
    return dist(p, project_on_segment(p, a, b)[1])


def polyline_length(points: Sequence[XY]) -> float:
    return sum(dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def interpolate(points: Sequence[XY], step: float) -> list[XY]:
    """Resample a polyline at a fixed spacing (used by evaluation metrics)."""
    if not points:
        return []
    out = [points[0]]
    carry = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        seg = dist(a, b)
        if seg == 0:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            d += step
        carry = seg - (d - step)
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


def bbox(points: Iterable[XY]) -> Tuple[float, float, float, float]:
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)
