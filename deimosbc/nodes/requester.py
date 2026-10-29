"""Requester (Fig. "Framework Requester"; Sec. IV-A.2.4 *Result Provisioning*
and Sec. IV-C *Broadcast*).

A requester - a first responder, an official, a member of the public - sends
an *Ask Message* to any worker node in range (collector or processor) and
gets the latest released global map back. Requesters are not registered
("This requester need not be verified"), but they *can* verify what they are
served: the response carries the header of a block whose ``release_root``
commits to the map, signed by a majority of the genesis authorities.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..encoding import hash_obj
from ..geo import XY
from ..ledger.block import Block, BlockHeader
from . import messages as M


def verify_map_response(resp: Dict[str, Any], authorities: List[str]) -> Optional[str]:
    """Return None if the response is authentic, else the reason it is not."""
    try:
        header = BlockHeader(**resp["proof"]["header"])
    except (KeyError, TypeError):
        return "malformed_proof"
    blk = Block(header=header, txs=[], attestations=dict(resp["proof"].get("attestations", {})))
    if not blk.has_quorum(sorted(authorities)):
        return "no_quorum"
    if header.release_root != resp["release"]["map_root"]:
        return "release_root_mismatch"
    if hash_obj(resp["map"]) != resp["release"]["map_root"]:
        return "map_hash_mismatch"
    return None


class Requester:
    kind = "requester"

    def __init__(self, name: str, net, position: XY, authorities: List[str], radio_range: float = 100.0):
        self.name = name
        self.net = net
        self.position = position
        self.authorities = sorted(authorities)
        self.responses: List[Dict[str, Any]] = []
        self.rejected: List[str] = []
        net.join(name, self.kind, lambda: self.position, radio_range, self.on_message)

    def ask(self) -> int:
        """Send an Ask Message to every worker node in range."""
        n = 0
        for node in self.net.neighbours(self.name):
            if self.net.kind[node] in ("collector", "processor"):
                n += self.net.send(self.name, node, {"type": M.ASK})
        return n

    def on_message(self, msg: Dict[str, Any]) -> None:
        if msg.get("type") != M.MAP:
            return
        resp = msg["response"]
        reason = verify_map_response(resp, self.authorities)
        if reason:
            self.rejected.append(reason)
        else:
            self.responses.append(resp)

    @property
    def best_map(self) -> Optional[Dict[str, Any]]:
        if not self.responses:
            return None
        return max(self.responses, key=lambda r: r["release"]["version"])
