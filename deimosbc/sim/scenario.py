"""Discrete-time simulation of DEIMOSBC after a Gulf Coast hurricane (Sec. II).

One tick = one second. Each tick:

1. every volunteer walks (open streets only), gets a noisy GPS fix and feeds
   it to its Trace Management System; at intersections it may report a
   blocked street;
2. radio messages due this tick are delivered;
3. processors beacon (task list, freeze state) and flush their outboxes;
4. collectors claim tasks, upload/retry queued traces, relay for others;
5. every ``block_interval_ticks`` the processors run a consensus round:
   round-robin proposer, majority attestation, commit, then governance
   (release votes, integrity check / freeze);
6. requesters periodically ask nearby worker nodes for the map and verify it.
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..config import ProtocolParams
from ..crypto import KeyPair
from ..geo import XY, dist
from ..ledger.transaction import TxKind
from ..mapping.validation import task_rect_for_cell
from ..network import RadioNetwork
from ..nodes.collector import Collector
from ..nodes.processor import Processor
from ..nodes.requester import Requester
from ..replica import ChainError, make_genesis
from ..tms import Fix
from .metrics import evaluate_map, f1
from .mobility import FAULTY_SENSOR, GpsNoise, Walker, fabricate_along_blocked
from .roadnet import RoadNetwork

log = logging.getLogger("deimosbc.sim")


@dataclass
class SimConfig:
    seed: int = 7
    duration_ticks: int = 3600
    # --- ground truth -----------------------------------------------------
    roadnet: str = "synthetic"           # "synthetic" or path to a ProteusData-style edge CSV
    roadnet_snap_m: float = 1.0          # merge CSV endpoints closer than this
    area_m: float = 2000.0               # synthetic grid extent (square)
    block_m: float = 100.0
    n_floods: int = 2
    flood_radius_m: Tuple[float, float] = (180.0, 320.0)
    debris_frac: float = 0.04
    # --- participants -----------------------------------------------------
    n_processors: int = 5
    processor_range_m: float = 400.0
    n_collectors: int = 40
    field_registrations: int = 4          # volunteers not in genesis, registered on site
    collector_range_m: float = 120.0
    walk_speed_mps: Tuple[float, float] = (1.1, 1.7)
    frac_fabricate: float = 0.10
    frac_garbage: float = 0.05
    frac_faulty: float = 0.05
    n_requesters: int = 3
    requester_interval: int = 120
    # --- data quality (Proteus vocabulary) --------------------------------
    jitter_m: float = 4.0
    offset_m: float = 2.0
    duplication: float = 0.02
    deletion: float = 0.03
    hazard_report_prob: float = 0.7
    # --- network / faults ------------------------------------------------
    loss_prob: float = 0.05
    relay: bool = True
    byzantine: Dict[int, str] = field(default_factory=dict)          # processor index -> tamper|censor|forge
    crashes: List[Tuple[int, int, int]] = field(default_factory=list)  # (processor index, down_at, up_at)
    beacon_interval: int = 5
    # --- evaluation --------------------------------------------------------
    eval_tolerance_m: float = 15.0
    protocol: Dict[str, Any] = field(default_factory=dict)            # ProtocolParams overrides

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SimConfig":
        d = dict(d)
        if "byzantine" in d:
            d["byzantine"] = {int(k): v for k, v in d["byzantine"].items()}
        if "crashes" in d:
            d["crashes"] = [tuple(c) for c in d["crashes"]]
        for k in ("flood_radius_m", "walk_speed_mps"):
            if k in d:
                d[k] = tuple(d[k])
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown simulation settings: {sorted(unknown)}")
        return cls(**d)


def _seed_keys(seed: int, label: str) -> KeyPair:
    return KeyPair.from_seed(hashlib.sha256(f"{seed}:{label}".encode()).digest())


class Simulation:
    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        self.now = 0
        t0 = time.time()

        # ---- ground truth + protocol parameters ---------------------------
        proto = dict(cfg.protocol)
        if cfg.roadnet == "synthetic":
            self.road = RoadNetwork.synthetic_grid(cfg.area_m, cfg.area_m, cfg.block_m, rng=random.Random(cfg.seed + 1))
            proto.setdefault("area_width_m", cfg.area_m)
            proto.setdefault("area_height_m", cfg.area_m)
        else:
            self.road, lat0, lon0, w, h = RoadNetwork.from_edge_csv(cfg.roadnet, snap_m=cfg.roadnet_snap_m)
            proto.update(origin_lat=lat0, origin_lon=lon0, area_width_m=w, area_height_m=h)
        self.params = ProtocolParams.from_dict({**ProtocolParams().to_dict(), **proto})
        self.road.apply_hurricane(
            random.Random(cfg.seed + 2), self.params.area_width_m, self.params.area_height_m,
            n_floods=cfg.n_floods, flood_radius=cfg.flood_radius_m, debris_frac=cfg.debris_frac,
        )
        comps = self.road.open_components()
        self.component = comps[0] if comps else set()

        # ---- identities & genesis ------------------------------------------
        self.net = RadioNetwork(random.Random(cfg.seed + 3), loss_prob=cfg.loss_prob)
        pkeys = [_seed_keys(cfg.seed, f"processor-{i}") for i in range(cfg.n_processors)]
        ckeys = [_seed_keys(cfg.seed, f"collector-{i}") for i in range(cfg.n_collectors)]
        n_genesis = max(0, cfg.n_collectors - cfg.field_registrations)
        self.genesis = make_genesis(self.params, [k.public_hex for k in pkeys], [k.public_hex for k in ckeys[:n_genesis]])

        # ---- processors ------------------------------------------------------
        self.processors: List[Processor] = []
        for i, (k, xy) in enumerate(zip(pkeys, self._processor_sites(cfg.n_processors))):
            self.processors.append(
                Processor(f"P{i}", k, self.genesis, xy, cfg.processor_range_m, self.net, behaviour=cfg.byzantine.get(i, "honest"))
            )
        self.by_pk = {p.pk: p for p in self.processors}

        # ---- collectors ------------------------------------------------------
        n_fab = round(cfg.frac_fabricate * cfg.n_collectors)
        n_gar = round(cfg.frac_garbage * cfg.n_collectors)
        n_fau = round(cfg.frac_faulty * cfg.n_collectors)
        roles = ["fabricate"] * n_fab + ["garbage"] * n_gar + ["faulty"] * n_fau
        roles += ["honest"] * (cfg.n_collectors - len(roles))
        self.rng.shuffle(roles)
        self.roles: Dict[str, str] = {}
        self.collectors: List[Collector] = []
        self.walkers: Dict[str, Walker] = {}
        self.noise: Dict[str, GpsNoise] = {}
        self.bias: Dict[str, XY] = {}
        self.last_fix: Dict[str, Optional[Fix]] = {}
        self.mode: Dict[str, str] = {}
        nodes = sorted(self.component)
        base = GpsNoise(cfg.jitter_m, cfg.offset_m, cfg.duplication, cfg.deletion)
        for i, (k, role) in enumerate(zip(ckeys, roles)):
            name = f"C{i}"
            w = Walker(self.road, self.rng.choice(nodes), random.Random(self.rng.random()), self.rng.uniform(*cfg.walk_speed_mps), self.component)
            self.walkers[name] = w
            behaviour = role if role in ("fabricate", "garbage") else "honest"
            c = Collector(
                name, k, self.params, self.net, (lambda w=w: w.pos), cfg.collector_range_m,
                random.Random(self.rng.random()), registered=i < n_genesis, behaviour=behaviour, relay=cfg.relay,
            )
            if behaviour == "fabricate":
                c.fabricator = self._make_fabricator(c)
            self.collectors.append(c)
            self.roles[name] = role
            self.noise[name] = FAULTY_SENSOR if role == "faulty" else base
            self.bias[name] = self.noise[name].device_bias(self.rng)
            self.last_fix[name] = None
            self.mode[name] = "idle"

        # ---- requesters ------------------------------------------------------
        # the first requester stands at a command post, the rest anywhere
        self.requesters = []
        for i in range(cfg.n_requesters):
            if i == 0 and self.processors:
                px, py = self.processors[0].position
                where = (px + 30.0, py)
            else:
                where = self.road.nodes[self.rng.choice(nodes)]
            self.requesters.append(Requester(f"R{i}", self.net, where, [k.public_hex for k in pkeys]))

        # ---- bootstrap: release the task grid ------------------------------
        self.processors[0]._tx(TxKind.CREATE_TASKS, {})
        self.timeline: List[Dict[str, Any]] = []
        self.consensus_log = {"blocks": 0, "rejected_proposals": {}, "view_changes": 0, "stalled_rounds": 0}
        self._seen_releases = 0
        self.setup_seconds = time.time() - t0

    # ------------------------------------------------------------------ setup
    def _processor_sites(self, n: int) -> List[XY]:
        w, h = self.params.area_width_m, self.params.area_height_m
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        sites = []
        for r in range(rows):
            for c in range(cols):
                sites.append(((c + 0.5) * w / cols, (r + 0.5) * h / rows))
        sites.sort(key=lambda p: dist(p, (w / 2, h / 2)))
        return [self.road.nodes[self.road.nearest_node(p, self.component)] for p in sites[:n]]

    def _make_fabricator(self, c: Collector):
        rng = random.Random(self.rng.random())

        def fab(t0: int):
            near = c.task_xy or c.position
            return fabricate_along_blocked(self.road, rng, near, radius=self.params.task_cell_size_m, t0=t0)

        return fab

    # ------------------------------------------------------------------ world
    def _gps(self, name: str, true_xy: XY) -> Optional[Fix]:
        nz = self.noise[name]
        r = self.rng
        if r.random() < nz.deletion:
            return None
        if r.random() < nz.duplication and self.last_fix[name] is not None:
            lf = self.last_fix[name]
            return Fix(lf.x, lf.y, self.now, lf.sensors)
        bx, by = self.bias[name]
        x = min(max(true_xy[0] + bx + r.gauss(0, nz.jitter_m), 0.0), self.params.area_width_m)
        y = min(max(true_xy[1] + by + r.gauss(0, nz.jitter_m), 0.0), self.params.area_height_m)
        fix = Fix(x, y, self.now, {"accel_rms": round(r.gauss(1.2, 0.15), 3)})
        self.last_fix[name] = fix
        return fix

    def _nearest_processor(self, xy: XY) -> Optional[Processor]:
        alive = [p for p in self.processors if p.alive]
        return min(alive, key=lambda p: dist(p.position, xy)) if alive else None

    def _move(self, c: Collector) -> None:
        w = self.walkers[c.name]
        if c.status in ("disabled", "stopped"):
            return
        if c.needs_processor():
            p = self._nearest_processor(w.pos)
            if p is not None and (self.mode[c.name] != "processor" or w.idle) and dist(w.pos, p.position) > 0.5 * self.cfg.collector_range_m:
                self.mode[c.name] = "processor"
                w.go_to(p.position)
        elif c.task_id is not None:
            cell = (c.task_id % self.params.n_cells_x, c.task_id // self.params.n_cells_x)
            if self.mode[c.name] != f"task{c.task_id}" or w.idle:
                self.mode[c.name] = f"task{c.task_id}"
                if not w.explore(task_rect_for_cell(self.params, cell)):
                    w.path = []

        def at_node(node: int) -> None:
            for xy, heading in w.hazards_seen(node, self.cfg.hazard_report_prob):
                c.report_hazard((xy[0] + self.rng.gauss(0, 3), xy[1] + self.rng.gauss(0, 3)), heading, self.now)

        moving = not w.idle
        w.step(1.0, at_node)
        if moving:
            fix = self._gps(c.name, w.pos)
            if fix is not None:
                c.observe(fix)

    # -------------------------------------------------------------- consensus
    def _consensus_round(self) -> None:
        alive = [p for p in self.processors if p.alive]
        if not alive:
            return
        top = max(alive, key=lambda p: p.replica.height)
        for p in alive:
            if p.replica.height < top.replica.height:
                p.sync_from(top)
        ref = top.replica
        if ref.state.frozen:
            return
        auth = ref.authorities
        quorum = len(auth) // 2 + 1
        if len(alive) < quorum:
            self.consensus_log["stalled_rounds"] += 1
            return
        for view in range(len(auth)):
            proposer = self.by_pk[ref.expected_proposer(ref.height + 1, view)]
            if not proposer.alive:
                self.consensus_log["view_changes"] += 1
                continue
            blk = proposer.build_block(self.now)
            for q in alive:
                if q is proposer:
                    continue
                reason = q.validate(blk)
                if reason is None:
                    blk.attest(q.keys)
                else:
                    rp = self.consensus_log["rejected_proposals"]
                    rp[reason] = rp.get(reason, 0) + 1
            if blk.has_quorum(auth):
                for q in alive:
                    try:
                        q.commit(blk)
                    except ChainError as e:  # pragma: no cover - honest replicas agree
                        log.error("%s failed to commit %s: %s", q.name, blk.height, e)
                self.consensus_log["blocks"] += 1
                return
            self.consensus_log["view_changes"] += 1
        self.consensus_log["stalled_rounds"] += 1

    # ------------------------------------------------------------------- run
    def reference(self) -> Processor:
        honest = [p for p in self.processors if p.behaviour == "honest"]
        return max(honest or self.processors, key=lambda p: p.replica.height)

    def step(self) -> None:
        self.now += 1
        now = self.now
        cfg = self.cfg
        for idx, down, up in cfg.crashes:
            p = self.processors[idx]
            if now == down:
                p.alive = False
                self.net.set_online(p.name, False)
            elif now == up:
                p.alive = True
                self.net.set_online(p.name, True)
        for c in self.collectors:
            self._move(c)
        self.net.step(now)
        beacon = now % cfg.beacon_interval == 0
        for p in self.processors:
            p.tick(now, beacon)
        for c in self.collectors:
            c.tick(now)
        if now % self.params.block_interval_ticks == 0:
            self._consensus_round()
            self._record_releases()
        if now % cfg.requester_interval == 0:
            for r in self.requesters:
                r.ask()

    def _record_releases(self) -> None:
        st = self.reference().state
        while self._seen_releases < len(st.releases):
            rel = st.releases[self._seen_releases]
            m = evaluate_map(st.release_snapshots[rel.version], self.road, tol=self.cfg.eval_tolerance_m)
            self.timeline.append({"version": rel.version, "t": rel.timestamp, "height": rel.height, **m, "f1": round(f1(m), 4)})
            self._seen_releases += 1

    def run(self, progress: bool = False) -> Dict[str, Any]:
        t0 = time.time()
        while self.now < self.cfg.duration_ticks:
            self.step()
            if progress and self.now % 600 == 0:
                st = self.reference().state
                log.info("t=%ds height=%d traces=%d releases=%d", self.now, self.reference().replica.height, len(st.traces), len(st.releases))
            if all(p.state.frozen for p in self.processors if p.alive) and self.now % 60 == 0:
                # let the freeze propagate briefly, then stop
                if self.now - (self.reference().replica.tip.header.timestamp) > 120:
                    break
        for c in self.collectors:
            c.end_of_shift()
        return self.report(time.time() - t0)

    # ---------------------------------------------------------------- report
    def report(self, run_seconds: float = 0.0) -> Dict[str, Any]:
        ref = self.reference()
        st = ref.state
        live = evaluate_map(st.map.snapshot(), self.road, tol=self.cfg.eval_tolerance_m)
        released = None
        if st.releases:
            released = evaluate_map(st.release_snapshots[st.releases[-1].version], self.road, tol=self.cfg.eval_tolerance_m)

        tx_kinds: Dict[str, int] = {}
        outcomes: Dict[str, int] = {}
        for b in ref.replica.blocks[1:]:
            for tx in b.txs:
                tx_kinds[tx.kind] = tx_kinds.get(tx.kind, 0) + 1
        for receipts in ref.replica.receipts_by_height.values():
            for rc in receipts:
                key = f"{rc['kind']}:{rc['code']}"
                outcomes[key] = outcomes.get(key, 0) + 1
        trace_status: Dict[str, int] = {}
        for tr in st.traces.values():
            trace_status[tr.status] = trace_status.get(tr.status, 0) + 1

        by_role: Dict[str, Dict[str, Any]] = {}
        for c in self.collectors:
            role = self.roles[c.name]
            acct = st.collectors.get(c.pk)
            d = by_role.setdefault(role, {"count": 0, "accepted": 0, "rejected": 0, "disputed": 0, "status": {}, "balance": []})
            d["count"] += 1
            if acct is not None:
                d["accepted"] += acct.accepted
                d["rejected"] += acct.rejected
                d["disputed"] += acct.disputed
                d["status"][acct.status] = d["status"].get(acct.status, 0) + 1
                d["balance"].append(acct.balance)
            else:
                d["status"]["unregistered"] = d["status"].get("unregistered", 0) + 1
        for d in by_role.values():
            bal = d.pop("balance")
            d["mean_balance"] = round(statistics.mean(bal), 1) if bal else None

        lat = [c.result_at[p] - c.created_at[p] for c in self.collectors for p in c.result_at if p in c.created_at]
        lat.sort()
        latency = {
            "n": len(lat),
            "mean_s": round(statistics.mean(lat), 1) if lat else None,
            "median_s": lat[len(lat) // 2] if lat else None,
            "p90_s": lat[int(0.9 * (len(lat) - 1))] if lat else None,
        }
        req = {
            "verified_responses": sum(len(r.responses) for r in self.requesters),
            "rejected_responses": sum(len(r.rejected) for r in self.requesters),
            "best_version": max((r.best_map["release"]["version"] for r in self.requesters if r.best_map), default=0),
        }
        return {
            "config": asdict(self.cfg),
            "protocol": self.params.to_dict(),
            "ground_truth": {
                "nodes": len(self.road.nodes),
                "edges": len(self.road.edges),
                "blocked_edges": len(self.road.blocked),
                "open_length_m": round(self.road.length(open_only=True), 1),
                "blocked_length_m": round(self.road.length(open_only=False), 1),
                "floods": [asdict(z) for z in self.road.floods],
            },
            "final_released_map": released,
            "live_chain_map": live,
            "timeline": self.timeline,
            "chain": {
                "height": ref.replica.height,
                "frozen": st.frozen,
                "final_height": st.final_height,
                "releases": len(st.releases),
                "txs": tx_kinds,
                "receipts": outcomes,
                "traces": trace_status,
                "tasks_mapped": sum(1 for t in st.tasks.values() if t.status != "open"),
                "tasks_total": len(st.tasks),
                "error_rate_window": round(st.error_rate()[0], 3),
            },
            "consensus": self.consensus_log,
            "collectors_by_role": by_role,
            "upload_latency": latency,
            "requesters": req,
            "network": asdict(self.net.stats),
            "processors": {p.name: {"behaviour": p.behaviour, "height": p.replica.height, **p.stats} for p in self.processors},
            "runtime_s": {"setup": round(self.setup_seconds, 2), "run": round(run_seconds, 2), "ticks": self.now},
        }
