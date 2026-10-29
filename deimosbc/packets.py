"""Trace packets exchanged between Data Collectors and Data Processors.

Wire format (all integers, so it can be signed canonically)::

    {
      "packet": {
        "v": 1,
        "collector": <hex pubkey>,
        "task_id":   <int>,
        "seq":       <int, per-collector sequence number>,
        "points":    [[lat_e7, lon_e7, t], ...],
        "sensors":   [{name: milli-units}, ...]   # one dict per point (may be {})
        "hazards":   [[lat_e7, lon_e7, t, heading_deg], ...]
      },
      "sig": <collector signature over canonical(packet)>
    }

:func:`verify_package` implements *Package Verification* (Sec. IV-A.2.1):
"whether all the requisite information was included and in the correct order".
:func:`verify_author` implements the signature half of *Author Identification*
(the registration half is a contract lookup).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .crypto import KeyPair, verify_obj
from .encoding import hash_obj

PACKET_VERSION = 1
_REQUIRED = ("v", "collector", "task_id", "seq", "points", "sensors", "hazards")


def build_packet(
    keys: KeyPair,
    task_id: int,
    seq: int,
    points: List[Tuple[int, int, int]],
    sensors: Optional[List[Dict[str, int]]] = None,
    hazards: Optional[List[Tuple[int, int, int, int]]] = None,
) -> Dict[str, Any]:
    packet = {
        "v": PACKET_VERSION,
        "collector": keys.public_hex,
        "task_id": int(task_id),
        "seq": int(seq),
        "points": [[int(a), int(b), int(t)] for a, b, t in points],
        "sensors": [dict(s) for s in (sensors or [{} for _ in points])],
        "hazards": [[int(a), int(b), int(t), int(h)] for a, b, t, h in (hazards or [])],
    }
    return {"packet": packet, "sig": keys.sign_obj(packet)}


def packet_id(signed: Dict[str, Any]) -> str:
    return hash_obj(signed["packet"])


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def verify_package(signed: Any, max_points: int = 10_000, max_sensor_fields: int = 16) -> Optional[str]:
    """Structural integrity check. Returns an error code, or None if well-formed."""
    if not isinstance(signed, dict) or set(signed) != {"packet", "sig"}:
        return "malformed_envelope"
    p = signed["packet"]
    if not isinstance(p, dict) or any(k not in p for k in _REQUIRED) or len(p) != len(_REQUIRED):
        return "missing_fields"
    if p["v"] != PACKET_VERSION:
        return "bad_version"
    if not isinstance(p["collector"], str) or not isinstance(signed["sig"], str):
        return "bad_identity_fields"
    if not (_is_int(p["task_id"]) and _is_int(p["seq"])):
        return "bad_header_types"
    pts = p["points"]
    if not isinstance(pts, list) or not pts or len(pts) > max_points:
        return "bad_points"
    for q in pts:
        if not (isinstance(q, list) and len(q) == 3 and all(_is_int(c) for c in q)):
            return "bad_point_encoding"
    if any(pts[i + 1][2] < pts[i][2] for i in range(len(pts) - 1)):
        return "points_out_of_order"
    sensors = p["sensors"]
    if not isinstance(sensors, list) or len(sensors) != len(pts):
        return "sensor_length_mismatch"
    for s in sensors:
        if not isinstance(s, dict) or len(s) > max_sensor_fields:
            return "bad_sensor_record"
        if not all(isinstance(k, str) and _is_int(v) for k, v in s.items()):
            return "bad_sensor_record"
    hz = p["hazards"]
    if not isinstance(hz, list):
        return "bad_hazards"
    for h in hz:
        if not (isinstance(h, list) and len(h) == 4 and all(_is_int(c) for c in h)):
            return "bad_hazards"
    return None


def verify_author(signed: Dict[str, Any]) -> bool:
    return verify_obj(signed["packet"]["collector"], signed["packet"], signed["sig"])
