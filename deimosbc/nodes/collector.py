"""Data Collector (Sec. IV-A.1).

"Their main job is collect and submit traces to a local data processor node.
Once a trace is collected, it is broadcast to all local nodes that are within
range. Then, it waits for confirmation of delivery and successful integration
from at least one Processor. Until the confirmation is received, all
subsequent traces are held in a queue. This process repeats until the data
collector voluntarily stops or the trace management system detects errors and
disables the data collector. If Processors send back error messages, the
Trace Management system tries to identify it and sends an alert to the user.
If the number of errors exceeds a pre-set limit, the user must be
re-authenticated."

Behaviours (for robustness experiments):

* ``honest``    - uploads what its TMS produced from its own GPS.
* ``fabricate`` - replaces its traces with plausible-looking fake ones along
                  roads that are actually blocked (a false "this road is
                  passable" claim) - supplied by the simulator.
* ``garbage``   - uploads physically impossible traces (teleports).
"""

from __future__ import annotations

import logging
import random
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..config import ProtocolParams
from ..contracts.registry import registration_request
from ..contracts.tasks import claim_request
from ..crypto import KeyPair
from ..geo import XY, LocalFrame, dist
from ..packets import build_packet, packet_id
from ..tms import Fix, TraceManager
from . import messages as M

log = logging.getLogger("deimosbc.collector")


class Collector:
    kind = "collector"

    def __init__(
        self,
        name: str,
        keys: KeyPair,
        params: ProtocolParams,
        net,
        position: Callable[[], XY],
        radio_range: float,
        rng: random.Random,
        registered: bool = True,
        behaviour: str = "honest",
        relay: bool = True,
        retry_ticks: int = 15,
        result_timeout_ticks: int = 300,
    ):
        self.name = name
        self.keys = keys
        self.pk = keys.public_hex
        self.params = params
        self.frame = LocalFrame(params.origin_lat, params.origin_lon)
        self.net = net
        self._position = position
        self.rng = rng
        self.behaviour = behaviour
        self.relay_enabled = relay
        self.retry_ticks = retry_ticks
        self.result_timeout = result_timeout_ticks
        self.registered = registered
        self.status = "active"            # active | suspended | disabled | stopped
        self.tms = TraceManager(params)
        self.pending_hazards: List[Tuple[int, int, int, int]] = []
        self.seq = 0
        self.nonce = 0
        self.queue: Deque[Dict[str, Any]] = deque()
        self.inflight: Optional[str] = None
        self.inflight_received = False
        self.last_send = -10**9
        self.relayed_to: Dict[str, set] = {}
        self.carry: Dict[str, Dict[str, Any]] = {}          # DTN buffer of others' packets
        self.task_id: Optional[int] = None
        self.last_task_id: Optional[int] = None
        self.task_xy: Optional[XY] = None
        self.pending_claim: Optional[Tuple[int, int]] = None  # (task_id, tick)
        self.failed_claims: Dict[int, int] = {}
        self.open_tasks: List[List[float]] = []
        self.last_beacon_tick = -10**9
        self.last_reauth_req = -10**9
        self.last_register_req = -10**9
        self.errors_local = 0
        self.dropped_off_task = 0
        self.alerts: List[str] = []
        self.results: Dict[str, str] = {}
        self.created_at: Dict[str, int] = {}
        self.result_at: Dict[str, int] = {}
        self.cached_map: Optional[Dict[str, Any]] = None
        self.freeze_announced = False
        self.fabricator: Optional[Callable[[int], List[Tuple[XY, int]]]] = None
        self.now = 0
        net.join(name, self.kind, position, radio_range, self.on_message, identity=self.pk)

    # -------------------------------------------------------------- helpers
    @property
    def position(self) -> XY:
        return self._position()

    @property
    def backlog(self) -> int:
        return len(self.queue)

    @property
    def active(self) -> bool:
        return self.status == "active" and self.registered

    def needs_processor(self, backlog_limit: int = 4) -> bool:
        """Should the volunteer walk towards a command post?"""
        return (
            not self.registered
            or self.status == "suspended"
            or (self.status == "active" and (self.task_id is None or self.backlog >= backlog_limit))
        )

    def alert(self, text: str) -> None:
        self.alerts.append(f"t={self.now}: {text}")
        log.debug("%s alert: %s", self.name, text)

    # ------------------------------------------------------------- sensing
    def observe(self, fix: Fix) -> None:
        """Feed one raw GPS fix (with optional sensor readings)."""
        if self.status != "active":
            return
        self.tms.feed(fix)
        self._package_finished_traces()

    def report_hazard(self, xy: XY, heading_deg: float, t: int) -> None:
        if self.status != "active":
            return
        lat, lon = self.frame.xy_to_e7(*xy)
        self.pending_hazards.append((lat, lon, t, int(round(heading_deg)) % 360))

    def end_of_shift(self) -> None:
        self.tms.flush()
        self._package_finished_traces()

    def _package_finished_traces(self) -> None:
        for trace in self.tms.pop_traces():
            task = self.task_id if self.task_id is not None else self.last_task_id
            if task is None:
                continue  # never claimed a task: nothing the contract would accept
            if self.behaviour == "fabricate" and self.fabricator is not None:
                fake = self.fabricator(trace[0].t)
                if fake:
                    pts = [(*self.frame.xy_to_e7(*xy), t) for xy, t in fake]
                    self._enqueue(pts, [{} for _ in pts], [])
                continue
            if self.behaviour == "garbage":
                t0 = trace[0].t
                pts = [
                    (*self.frame.xy_to_e7(self.rng.uniform(0, self.params.area_width_m), self.rng.uniform(0, self.params.area_height_m)), t0 + i)
                    for i in range(len(trace))
                ]
                self._enqueue(pts, [{} for _ in pts], [])
                continue
            if not self._overlaps_task(task, trace):
                self.dropped_off_task += 1
                continue  # the contract would reject it as "outside_task"
            pts = [(*self.frame.xy_to_e7(f.x, f.y), f.t) for f in trace]
            sensors = [{k: int(round(v * 1000)) for k, v in f.sensors.items()} for f in trace]
            hazards, self.pending_hazards = self.pending_hazards, []
            self._enqueue(pts, sensors, hazards)

    def _overlaps_task(self, task_id: int, trace: List[Fix]) -> bool:
        """Client-side mirror of the contract's task-area rule."""
        from ..mapping.validation import task_rect_for_cell

        n = self.params.n_cells_x
        x0, y0, x1, y1 = task_rect_for_cell(self.params, (task_id % n, task_id // n))
        tol = self.params.task_overlap_tolerance_m
        return any(x0 - tol <= f.x <= x1 + tol and y0 - tol <= f.y <= y1 + tol for f in trace)

    def _enqueue(self, pts, sensors, hazards) -> None:
        task = self.task_id if self.task_id is not None else self.last_task_id
        signed = build_packet(self.keys, task, self.seq, pts, sensors, hazards)
        self.seq += 1
        pid = packet_id(signed)
        self.created_at[pid] = self.now
        self.queue.append(signed)

    # ------------------------------------------------------------- protocol
    def tick(self, now: int) -> None:
        self.now = now
        if self.status in ("disabled", "stopped"):
            return
        procs = self.net.neighbours(self.name, "processor")
        if not self.registered:
            if procs and now - self.last_register_req >= self.retry_ticks:
                self.last_register_req = now
                self.nonce += 1
                req = registration_request(self.pk, "register", self.nonce)
                self.net.send(self.name, procs[0], {"type": M.REGISTER, "collector": self.pk, "nonce": self.nonce, "collector_sig": self.keys.sign_obj(req)})
            return
        if self.status == "suspended":
            if procs and now - self.last_reauth_req >= 60:
                self.last_reauth_req = now
                self.nonce += 1
                req = registration_request(self.pk, "reauth", self.nonce)
                self.net.send(self.name, procs[0], {"type": M.REAUTH, "collector": self.pk, "nonce": self.nonce, "collector_sig": self.keys.sign_obj(req)})
            return
        self._maybe_claim(now, procs)
        self._upload(now, procs)

    def _maybe_claim(self, now: int, procs: List[str]) -> None:
        if self.task_id is not None or not procs or now - self.last_beacon_tick > 30:
            return
        if self.pending_claim and now - self.pending_claim[1] < 4 * self.params.block_interval_ticks:
            return
        here = self.position
        options = [t for t in self.open_tasks if now - self.failed_claims.get(int(t[0]), -10**9) > 120]
        if not options:
            return
        tid, x, y = min(options, key=lambda t: (dist(here, (t[1], t[2])), t[0]))
        tid = int(tid)
        self.nonce += 1
        sig = self.keys.sign_obj(claim_request(self.pk, tid, self.nonce))
        self.pending_claim = (tid, now)
        self.net.send(self.name, procs[0], {"type": M.CLAIM, "collector": self.pk, "task_id": tid, "nonce": self.nonce, "collector_sig": sig})

    def _upload(self, now: int, procs: List[str]) -> None:
        if self.inflight is None and self.queue:
            self.inflight = packet_id(self.queue[0])
            self.inflight_received = False
            self.last_send = -10**9
        if self.inflight is None:
            return
        wait = self.result_timeout if self.inflight_received else self.retry_ticks
        if now - self.last_send < wait:
            return
        head = self.queue[0]
        if procs:
            self.net.broadcast(self.name, {"type": M.UPLOAD, "packet": head}, kind="processor")
            self.last_send = now
        elif self.relay_enabled:
            sent = self.relayed_to.setdefault(self.inflight, set())
            for n in self.net.neighbours(self.name, "collector"):
                if n not in sent and self.net.send(self.name, n, {"type": M.RELAY, "packet": head}):
                    sent.add(n)
        # Carried packets from other volunteers go out whenever we meet a processor
        if procs and self.carry:
            for pid, pkt in list(self.carry.items()):
                self.net.broadcast(self.name, {"type": M.RELAY, "packet": pkt}, kind="processor")
                del self.carry[pid]

    # -------------------------------------------------------------- inbound
    def on_message(self, msg: Dict[str, Any]) -> None:
        t = msg.get("type")
        if t == M.BEACON:
            self.last_beacon_tick = self.now
            self.open_tasks = msg["open_tasks"]
            if self.task_id is not None and self.task_id in msg["mapped_tasks"]:
                self.alert(f"task {self.task_id} completed")
                self.task_id, self.task_xy = None, None
            if msg["frozen"]:
                self._on_freeze(msg.get("final_height"))
        elif t == M.RECEIVED:
            if msg["packet_id"] == self.inflight:
                self.inflight_received = True
                self.last_send = self.now
        elif t == M.RESULT:
            self._on_result(msg)
        elif t == M.NOTICE:
            self._on_notice(msg)
        elif t == M.RELAY:
            pkt = msg["packet"]
            if pkt["packet"].get("collector") != self.pk and len(self.carry) < 50:
                self.carry[packet_id(pkt)] = pkt
        elif t == M.MAP:
            resp = msg["response"]
            if self.cached_map is None or resp["release"]["version"] > self.cached_map["release"]["version"]:
                self.cached_map = resp
        elif t == M.ASK:
            if self.cached_map is not None:
                self.net.send(self.name, msg["_from"], {"type": M.MAP, "response": self.cached_map})
        elif t == M.FREEZE:
            self._on_freeze(msg.get("final_height"))

    def _on_freeze(self, final_height) -> None:
        if self.status != "stopped":
            self.status = "stopped"
            self.alert(f"chain frozen at height {final_height}; stopping collection")
        if not self.freeze_announced:
            self.freeze_announced = True   # amplify: re-broadcast once to everyone in range
            self.net.broadcast(self.name, {"type": M.FREEZE, "final_height": final_height})

    def _on_result(self, msg: Dict[str, Any]) -> None:
        pid, status, code = msg["packet_id"], msg["status"], msg.get("code")
        if pid is None or pid in self.results:
            return
        idx = next((i for i, p in enumerate(self.queue) if packet_id(p) == pid), None)
        if idx is None:
            return
        if status == "failed" and code in ("collector_not_active", "insufficient_stake"):
            # keep it queued; it will be retried after re-authentication
            if pid == self.inflight:
                self.inflight_received = False
            return
        self.results[pid] = status
        self.result_at[pid] = self.now
        del self.queue[idx]
        if pid == self.inflight:
            self.inflight = None
        if status != "accepted":
            self.errors_local += 1
            self.alert(f"trace {pid[:10]} {status}: {code}")

    def _on_notice(self, msg: Dict[str, Any]) -> None:
        ev = msg["event"]
        if ev == "task_assigned":
            self.task_id = msg["task_id"]
            self.last_task_id = self.task_id
            self.pending_claim = None
            s = self.params.task_cell_size_m
            for t in self.open_tasks:
                if int(t[0]) == self.task_id:
                    self.task_xy = (t[1], t[2])
            if self.task_xy is None:
                n = self.params.n_cells_x
                self.task_xy = ((self.task_id % n + 0.5) * s, (self.task_id // n + 0.5) * s)
        elif ev == "claim_task_failed":
            if msg.get("task_id") is not None:
                self.failed_claims[int(msg["task_id"])] = self.now
            self.pending_claim = None
        elif ev == "collector_suspended":
            self.status = "suspended"
            self.task_id, self.task_xy = None, None
            self.alert("too many errors: re-authentication required")
        elif ev == "collector_disabled":
            self.status = "disabled"
            self.alert("collector disabled by the network")
        elif ev == "collector_reauthenticated":
            if self.status == "suspended":
                self.status = "active"
                self.inflight_received = False
                self.last_send = -10**9
        elif ev == "collector_registered":
            self.registered = True
        elif ev == "trace_disputed":
            self.errors_local += 1
            self.alert(f"earlier trace {msg.get('trace', '')[:10]} discarded after a conflict")
