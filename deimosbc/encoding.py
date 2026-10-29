"""Canonical serialisation and hashing.

Everything that is signed or hashed goes through :func:`canonical_json` so that
every node derives byte-identical encodings for the same logical object.
Floats are not allowed in signed payloads (coordinates travel as integer
micro-degrees, see :mod:`deimosbc.geo`); derived floating-point state is
rounded before hashing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def _normalise(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, float):
        # Derived state only. Round to a millimetre / micro-unit so that
        # replicas that agree on the math agree on the hash.
        return round(obj, 6)
    return obj


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding (sorted keys, no whitespace)."""
    return json.dumps(_normalise(obj), sort_keys=True, separators=(",", ":")).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256(canonical_json(obj))


def merkle_root(leaves: Iterable[str]) -> str:
    """Binary Merkle root over hex digests (Bitcoin-style odd-leaf duplication)."""
    level = [bytes.fromhex(x) for x in leaves]
    if not level:
        return sha256(b"")
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i + 1]).digest() for i in range(0, len(level), 2)]
    return level[0].hex()
