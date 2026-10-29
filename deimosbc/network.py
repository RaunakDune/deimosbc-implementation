"""Short-range ad-hoc radio model (Sec. III-A).

"The device must have at least a Bluetooth transmitter/receiver or a WiFi
chipset, enabling it to have short-range, ad-hoc wireless communication with
similar devices." Cellular infrastructure is assumed to be down.

* Device-to-device links exist only within radio range (the smaller of the
  two endpoints' ranges), with per-message loss and latency.
* Data Processors additionally share a backhaul mesh (e.g. a satellite or
  long-range point-to-point link at each command post) used for transaction
  gossip and consensus. The mesh can be disabled per processor to model
  crashes.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .geo import XY, dist


@dataclass(order=True)
class _Pending:
    due: int
    seq: int
    dst: str = field(compare=False)
    msg: Dict[str, Any] = field(compare=False)


@dataclass
class RadioStats:
    sent: int = 0
    delivered: int = 0
    lost: int = 0
    out_of_range: int = 0
    mesh: int = 0


class RadioNetwork:
    def __init__(self, rng: random.Random, loss_prob: float = 0.05, latency: int = 1, mesh_latency: int = 1):
        self.rng = rng
        self.loss_prob = loss_prob
        self.latency = latency
        self.mesh_latency = mesh_latency
        self.pos: Dict[str, Callable[[], XY]] = {}
        self.range: Dict[str, float] = {}
        self.kind: Dict[str, str] = {}
        self.handler: Dict[str, Callable[[Dict[str, Any]], None]] = {}
        self.online: Dict[str, bool] = {}
        self.identity: Dict[str, str] = {}      # radio name -> public key (if any)
        self.mesh_members: List[str] = []
        self.queue: List[_Pending] = []
        self.now = 0
        self._seq = 0
        self.stats = RadioStats()

    def join(self, node_id: str, kind: str, pos: Callable[[], XY], radio_range: float, handler,
             mesh: bool = False, identity: Optional[str] = None):
        if identity is not None:
            self.identity[node_id] = identity
        self.pos[node_id] = pos
        self.range[node_id] = radio_range
        self.kind[node_id] = kind
        self.handler[node_id] = handler
        self.online[node_id] = True
        if mesh:
            self.mesh_members.append(node_id)

    def set_online(self, node_id: str, online: bool) -> None:
        self.online[node_id] = online

    def in_range(self, a: str, b: str) -> bool:
        if not (self.online.get(a) and self.online.get(b)):
            return False
        return dist(self.pos[a](), self.pos[b]()) <= min(self.range[a], self.range[b])

    def neighbours(self, node_id: str, kind: Optional[str] = None) -> List[str]:
        return sorted(
            n for n in self.pos if n != node_id and (kind is None or self.kind[n] == kind) and self.in_range(node_id, n)
        )

    def _enqueue(self, dst: str, msg: Dict[str, Any], delay: int) -> None:
        self._seq += 1
        heapq.heappush(self.queue, _Pending(self.now + delay, self._seq, dst, msg))

    def send(self, src: str, dst: str, msg: Dict[str, Any]) -> bool:
        self.stats.sent += 1
        if not self.in_range(src, dst):
            self.stats.out_of_range += 1
            return False
        if self.rng.random() < self.loss_prob:
            self.stats.lost += 1
            return False
        self._enqueue(dst, {**msg, "_from": src}, self.latency)
        return True

    def broadcast(self, src: str, msg: Dict[str, Any], kind: Optional[str] = None) -> List[str]:
        reached = []
        for n in self.neighbours(src, kind):
            if self.send(src, n, msg):
                reached.append(n)
        return reached

    def mesh_send(self, src: str, msg: Dict[str, Any]) -> None:
        """Gossip over the processor backhaul to every other online member."""
        if not self.online.get(src):
            return
        for n in self.mesh_members:
            if n != src and self.online.get(n):
                self.stats.mesh += 1
                self._enqueue(n, {**msg, "_from": src}, self.mesh_latency)

    def step(self, now: int) -> None:
        self.now = now
        while self.queue and self.queue[0].due <= now:
            p = heapq.heappop(self.queue)
            if self.online.get(p.dst):
                self.stats.delivered += 1
                self.handler[p.dst](p.msg)


__all__: List[str] = ["RadioNetwork", "RadioStats"]
