"""Data Processor (Sec. IV-A.2).

Responsibilities, as listed in the paper:

1. *Package Verification* - integrity of incoming data packets.
2. *Author Identification* - the packet comes from a registered collector;
   then "send a message to the sender certifying that the data has been
   received".
3. *Trace Processing* - submit the trace to the map contract (which runs the
   Data Summarization System) and alert the collector on errors.
4. *Result Provisioning* - serve the latest global map to any requester; the
   requester "need not be verified".

Processors are also the consensus authorities: they gossip transactions over
the backhaul mesh, propose and attest blocks, vote on map releases and run
the integrity check that can freeze the chain (*Retirement*).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..contracts.state import OPEN, SUSPENDED
from ..crypto import KeyPair
from ..geo import XY
from ..ledger.block import Block
from ..ledger.transaction import Transaction, TxKind
from ..packets import packet_id, verify_author, verify_package
from ..replica import Replica
from . import messages as M

log = logging.getLogger("deimosbc.processor")


class Processor:
    kind = "processor"

    def __init__(
        self,
        name: str,
        keys: KeyPair,
        genesis: Block,
        position: XY,
        radio_range: float,
        net,
        behaviour: str = "honest",     # honest | tamper | censor | forge
        merger_factory=None,
    ):
        self.name = name
        self.keys = keys
        self.pk = keys.public_hex
        self.position = position
        self.radio_range = radio_range
        self.net = net
        self.behaviour = behaviour
        self.replica = Replica(genesis, merger=merger_factory() if merger_factory else None)
        self.mempool: Dict[str, Transaction] = {}
        self.nonce = 0
        self.alive = True
        self.outbox: Dict[str, List[Dict[str, Any]]] = {}
        self.pushed_version: Dict[str, int] = {}
        self.seen_packets: set[str] = set()
        self.voted_release_version = 0
        self.freeze_votes_cast: set[int] = set()
        self.last_freeze_proposal_at = -10**9
        self.stats = {"uploads": 0, "package_errors": 0, "author_errors": 0, "txs_created": 0, "blocks_proposed": 0}
        net.join(name, self.kind, lambda: self.position, radio_range, self.on_message, mesh=True, identity=self.pk)

    # ------------------------------------------------------------ utilities
    @property
    def state(self):
        return self.replica.state

    def _tx(self, kind: str, payload: Dict[str, Any]) -> Transaction:
        self.nonce += 1
        tx = Transaction.create(kind, self.keys, self.nonce, payload)
        self.mempool[tx.tx_id] = tx
        self.stats["txs_created"] += 1
        self.net.mesh_send(self.name, {"type": M.TX, "tx": tx.to_dict()})
        return tx

    def _notify(self, collector: str, msg: Dict[str, Any]) -> None:
        self.outbox.setdefault(collector, []).append(msg)

    # -------------------------------------------------------------- messages
    def on_message(self, msg: Dict[str, Any]) -> None:
        if not self.alive:
            return
        t = msg.get("type")
        if t == M.TX:
            tx = Transaction.from_dict(msg["tx"])
            if tx.tx_id not in self.replica.tx_ids:
                self.mempool.setdefault(tx.tx_id, tx)
        elif t in (M.UPLOAD, M.RELAY):
            self._on_upload(msg)
        elif t == M.CLAIM:
            self._tx(TxKind.CLAIM_TASK, {k: msg[k] for k in ("collector", "task_id", "nonce", "collector_sig")})
        elif t == M.REAUTH:
            acct = self.state.collectors.get(msg["collector"])
            if acct is not None and acct.status == SUSPENDED:
                self._tx(TxKind.REAUTH_COLLECTOR, {k: msg[k] for k in ("collector", "nonce", "collector_sig")})
        elif t == M.REGISTER:
            if msg["collector"] not in self.state.collectors:
                self._tx(TxKind.REGISTER_COLLECTOR, {k: msg[k] for k in ("collector", "nonce", "collector_sig")})
        elif t == M.ASK:
            resp = self.map_response()
            if resp is not None:
                self.net.send(self.name, msg["_from"], {"type": M.MAP, "response": resp})

    def _on_upload(self, msg: Dict[str, Any]) -> None:
        signed = msg["packet"]
        self.stats["uploads"] += 1
        err = verify_package(signed, max_points=10 * self.state.params.max_trace_points)
        if err:
            self.stats["package_errors"] += 1
            if isinstance(signed, dict) and isinstance(signed.get("packet"), dict):
                c = signed["packet"].get("collector")
                if isinstance(c, str):
                    self._notify(c, {"type": M.RESULT, "packet_id": None, "status": "failed", "code": f"package:{err}"})
            return
        pkt = signed["packet"]
        collector = pkt["collector"]
        if collector not in self.state.collectors or not verify_author(signed):
            self.stats["author_errors"] += 1
            return  # cannot attribute: silently drop (do not certify receipt)
        pid = packet_id(signed)
        # delivery certificate to the original author (directly if in range,
        # otherwise via the outbox when the collector next comes by)
        cert = {"type": M.RECEIVED, "packet_id": pid, "by": self.pk}
        if self.net.identity.get(msg.get("_from")) == collector:
            self.net.send(self.name, collector, cert)
        else:
            self._notify(collector, cert)
        if pid in self.seen_packets or pid in self.state.traces:
            return
        self.seen_packets.add(pid)
        self._tx(TxKind.SUBMIT_TRACE, {"packet": signed})

    # ------------------------------------------------------------- periodic
    def tick(self, now: int, beacon: bool) -> None:
        if not self.alive:
            return
        if self.behaviour == "forge" and now % 60 == 0:
            self._forge_trace(now)
        if not beacon:
            return
        st = self.state
        open_tasks = []
        for t in st.open_tasks():
            open_tasks.append([t.task_id, (t.cell[0] + 0.5) * st.params.task_cell_size_m, (t.cell[1] + 0.5) * st.params.task_cell_size_m])
        beacon_msg = {
            "type": M.BEACON,
            "proc": self.name,
            "height": self.replica.height,
            "frozen": st.frozen,
            "final_height": st.final_height,
            "open_tasks": open_tasks,
            "mapped_tasks": sorted(t.task_id for t in st.tasks.values() if t.status != OPEN),
            "release_version": len(st.releases),
        }
        resp = self.map_response()
        for c in self.net.neighbours(self.name, "collector"):
            cpk = self.net.identity.get(c, c)
            self.net.send(self.name, c, beacon_msg)
            pending = self.outbox.get(cpk)
            if pending:
                still = []
                for m in pending:
                    if not self.net.send(self.name, c, m):
                        still.append(m)
                self.outbox[cpk] = still
            if resp is not None and self.pushed_version.get(cpk, 0) < resp["release"]["version"]:
                if self.net.send(self.name, c, {"type": M.MAP, "response": resp}):
                    self.pushed_version[cpk] = resp["release"]["version"]

    def _forge_trace(self, now: int) -> None:
        """Byzantine behaviour: inject a trace in a registered collector's name."""
        from ..packets import build_packet

        victims = sorted(self.state.collectors)
        if not victims:
            return
        fake = KeyPair.generate()
        signed = build_packet(fake, 0, now, [(0, 0, now), (1, 1, now + 1), (2, 2, now + 2)])
        signed["packet"]["collector"] = victims[now % len(victims)]
        self._tx(TxKind.SUBMIT_TRACE, {"packet": signed})

    # ------------------------------------------------------------ consensus
    def build_block(self, now: int) -> Block:
        st = self.state
        seen_pids = set()
        chosen: List[Transaction] = []
        for tx in sorted(self.mempool.values(), key=lambda t: (t.sender, t.nonce)):
            if tx.tx_id in self.replica.tx_ids:
                continue
            if tx.kind == TxKind.SUBMIT_TRACE:
                try:
                    pid = packet_id(tx.payload["packet"])
                except Exception:
                    pid = tx.tx_id
                if pid in seen_pids or pid in st.traces:
                    continue
                seen_pids.add(pid)
            chosen.append(tx)
        if self.behaviour == "censor":
            chosen = [t for t in chosen if t.sender == self.pk]
        blk = self.replica.propose(self.keys, chosen, now)
        if self.behaviour == "tamper":
            blk.header.app_hash = "f" * 64
            blk.attestations = {}
            blk.proposer_sig = self.keys.sign(bytes.fromhex(blk.hash))
            blk.attestations[self.pk] = blk.proposer_sig
        self.stats["blocks_proposed"] += 1
        return blk

    def validate(self, block: Block) -> Optional[str]:
        if self.behaviour == "tamper":
            return None  # rubber-stamps everything
        return self.replica.check_proposal(block)

    def commit(self, block: Block) -> None:
        receipts = self.replica.commit(block)
        for tx in block.txs:
            self.mempool.pop(tx.tx_id, None)
        self._after_commit(block, receipts)

    def sync_from(self, peer: "Processor") -> int:
        n = 0
        for b in peer.replica.blocks[self.replica.height + 1 :]:
            self.commit(b)
            n += 1
        return n

    # ---------------------------------------------------- receipts -> notices
    def _after_commit(self, block: Block, receipts: List[Dict[str, Any]]) -> None:
        for tx, rc in zip(block.txs, receipts):
            if not rc["ok"]:
                c = tx.payload.get("collector")
                if tx.kind == TxKind.SUBMIT_TRACE:
                    pkt = tx.payload.get("packet", {}).get("packet", {})
                    c = pkt.get("collector") if isinstance(pkt, dict) else None
                    if c and rc["code"] != "duplicate_packet":
                        self._notify(c, {"type": M.RESULT, "packet_id": packet_id(tx.payload["packet"]), "status": "failed", "code": rc["code"]})
                elif c and tx.kind in (TxKind.CLAIM_TASK, TxKind.REAUTH_COLLECTOR, TxKind.REGISTER_COLLECTOR):
                    self._notify(c, {"type": M.NOTICE, "event": f"{tx.kind}_failed", "code": rc["code"], "task_id": tx.payload.get("task_id")})
                continue
            for ev in rc["events"]:
                et = ev["type"]
                c = ev.get("collector")
                if et in ("trace_accepted", "trace_rejected") or (et == "trace_disputed" and "seq" in ev):
                    status = et.split("_")[1]
                    self._notify(c, {"type": M.RESULT, "packet_id": ev["trace"], "status": status, "code": ev.get("reason", "ok")})
                elif c is not None:
                    self._notify(c, {"type": M.NOTICE, "event": et, **{k: v for k, v in ev.items() if k != "type"}})
        self._governance()

    # ----------------------------------------------------------- governance
    def integrity_failed(self) -> bool:
        """This processor's own integrity test (used both to propose and to vote)."""
        rate, n = self.state.error_rate()
        return n >= self.state.params.integrity_min_samples and rate >= self.state.params.freeze_error_rate

    def _governance(self) -> None:
        st = self.state
        if st.frozen:
            return
        fp = st.freeze
        if fp is not None and not fp.closed:
            if self.pk not in fp.votes and fp.proposal_id not in self.freeze_votes_cast:
                self.freeze_votes_cast.add(fp.proposal_id)
                self._tx(TxKind.VOTE_FREEZE, {"proposal_id": fp.proposal_id, "approve": self.integrity_failed()})
        elif self.integrity_failed() and len(st.traces) - self.last_freeze_proposal_at >= st.params.integrity_window:
            self.last_freeze_proposal_at = len(st.traces)
            rate, n = st.error_rate()
            self._tx(TxKind.PROPOSE_FREEZE, {"reason": f"error rate {rate:.2f} over last {n} submissions"})

        nxt = len(st.releases) + 1
        if self.voted_release_version >= nxt:
            return
        last_rel_h = st.releases[-1].height if st.releases else 0
        changed = st.last_map_change_height > last_rel_h
        if not changed:
            return
        h = self.replica.height
        quiet = h - st.last_map_change_height >= st.params.release_quiet_blocks
        periodic = h - last_rel_h >= st.params.release_interval_blocks
        all_mapped = st.tasks and all(t.status != OPEN for t in st.tasks.values())
        if quiet or periodic or all_mapped:
            self.voted_release_version = nxt
            self._tx(TxKind.VOTE_RELEASE, {"version": nxt})

    # ---------------------------------------------------- result provisioning
    def map_response(self) -> Optional[Dict[str, Any]]:
        """Latest released map + a proof requesters can check against genesis.

        The proof is the header of the block right after the release (its
        ``release_root`` commits to the map) with its quorum attestations.
        """
        st = self.state
        for rel in reversed(st.releases):
            h = rel.height + 1
            if h > self.replica.height:
                continue
            blk = self.replica.blocks[h]
            if blk.header.release_root != rel.map_root:
                continue
            return {
                "release": {"version": rel.version, "height": rel.height, "timestamp": rel.timestamp, "map_root": rel.map_root},
                "map": st.release_snapshots[rel.version],
                "origin": [st.params.origin_lat, st.params.origin_lon],
                "proof": {"header": blk.header.to_dict(), "attestations": dict(blk.attestations)},
            }
        return None
