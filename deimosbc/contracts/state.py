"""Replicated contract state and the transaction dispatcher.

Every Data Processor keeps a :class:`WorldState` replica and applies committed
blocks to it. Execution is deterministic: given the same genesis and blocks,
every honest replica arrives at the same ``root``.

``root`` is a rolling commitment: ``H(prev_root, height, merkle(receipts))``,
where each receipt includes a digest of the map change it caused. It is cheap
to maintain per block and any divergence between replicas surfaces in the next
block header's ``app_hash``. A full canonical snapshot hash of the map
(``map_root``) is computed whenever a map version is released.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from ..config import ProtocolParams
from ..encoding import hash_obj, merkle_root
from ..geo import LocalFrame
from ..ledger.transaction import Transaction, TxKind
from ..mapping.graph import MapGraph
from ..mapping.merger import MapMerger, build_merger
from ..mapping.validation import cell_of_xy

ACTIVE, SUSPENDED, DISABLED = "active", "suspended", "disabled"
OPEN, MAPPED = "open", "mapped"
ACCEPTED, REJECTED, DISPUTED = "accepted", "rejected", "disputed"


@dataclass
class CollectorAccount:
    pubkey: str
    sponsor: str
    registered_height: int
    status: str = ACTIVE
    balance: int = 0
    errors: int = 0
    reauths: int = 0
    accepted: int = 0
    rejected: int = 0
    disputed: int = 0
    active_task: Optional[int] = None
    claimed_tasks: Set[int] = field(default_factory=set)
    last_seq: int = -1


@dataclass
class Task:
    """Sec. IV-C: "The task includes the starting trajectory point (which may be
    changed as per their need), the direction of the trace, the current time and
    the status." """

    task_id: int
    cell: Tuple[int, int]
    start_lat_e7: int
    start_lon_e7: int
    direction_deg: int
    created_at: int                  # block timestamp
    status: str = OPEN
    claims: Dict[str, int] = field(default_factory=dict)   # collector -> claim height
    mapped_length: float = 0.0


@dataclass
class TraceRecord:
    trace_id: str
    collector: str
    task_id: int
    height: int
    status: str
    reason: str = ""
    new_length: float = 0.0


@dataclass
class FreezeProposal:
    proposal_id: int
    proposer: str
    reason: str
    height: int
    votes: Dict[str, bool] = field(default_factory=dict)
    closed: bool = False


@dataclass
class MapRelease:
    version: int
    height: int
    timestamp: int
    map_root: str
    n_vertices: int
    n_edges: int
    length_m: float
    voters: List[str]


@dataclass
class ExecContext:
    height: int
    timestamp: int
    events: List[Dict[str, Any]] = field(default_factory=list)

    def emit(self, **ev) -> None:
        self.events.append(ev)


class ContractError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorldState:
    def __init__(self, genesis: Dict[str, Any], merger: Optional[MapMerger] = None):
        self.genesis = genesis
        self.params = ProtocolParams.from_dict(genesis["params"])
        self.frame = LocalFrame(self.params.origin_lat, self.params.origin_lon)
        self.authorities: List[str] = list(genesis["authorities"])
        self.merger: MapMerger = merger or build_merger(self.params)
        self.map = MapGraph(index_cell=self.params.merge_radius_m, cell_of=lambda xy: cell_of_xy(self.params, xy))
        self.collectors: Dict[str, CollectorAccount] = {}
        self.tasks: Dict[int, Task] = {}
        self.traces: Dict[str, TraceRecord] = {}
        self.recent_outcomes: Deque[bool] = deque(maxlen=self.params.integrity_window)  # True = error
        self.freeze: Optional[FreezeProposal] = None
        self.n_freeze_proposals = 0
        self.frozen = False
        self.final_height: Optional[int] = None
        self.releases: List[MapRelease] = []
        self.release_snapshots: Dict[int, Dict[str, Any]] = {}  # version -> canonical map
        self.release_votes: Dict[str, int] = {}         # processor -> version voted for
        self.last_map_change_height = 0
        self.height = 0
        self.timestamp = int(genesis.get("timestamp", 0))
        self.root = hash_obj(genesis)
        self.receipts: List[Dict[str, Any]] = []        # receipts of the last applied block
        for c in genesis.get("collectors", []):
            self.collectors[c] = CollectorAccount(
                pubkey=c, sponsor="genesis", registered_height=0, balance=self.params.initial_collector_balance
            )

    # ---------------------------------------------------------------- views
    @property
    def release_root(self) -> str:
        return self.releases[-1].map_root if self.releases else "0" * 64

    def is_authority(self, pk: str) -> bool:
        return pk in self.authorities

    def error_rate(self) -> Tuple[float, int]:
        n = len(self.recent_outcomes)
        return ((sum(self.recent_outcomes) / n) if n else 0.0, n)

    def open_tasks(self) -> List[Task]:
        return [t for t in self.tasks.values() if t.status == OPEN and len(t.claims) < self.params.max_claims_per_task]

    # -------------------------------------------------------------- execution
    def apply_block(self, height: int, timestamp: int, txs: List[Transaction]) -> List[Dict[str, Any]]:
        from . import governance, maps, registry, tasks  # local import avoids cycles

        handlers: Dict[str, Callable[[WorldState, Transaction, ExecContext], None]] = {
            TxKind.REGISTER_COLLECTOR: registry.register_collector,
            TxKind.REAUTH_COLLECTOR: registry.reauth_collector,
            TxKind.CREATE_TASKS: tasks.create_tasks,
            TxKind.CLAIM_TASK: tasks.claim_task,
            TxKind.SUBMIT_TRACE: maps.submit_trace,
            TxKind.PROPOSE_FREEZE: governance.propose_freeze,
            TxKind.VOTE_FREEZE: governance.vote_freeze,
            TxKind.VOTE_RELEASE: governance.vote_release,
        }
        self.height = height
        self.timestamp = timestamp
        tasks.expire_claims(self)
        receipts = []
        for tx in txs:
            ctx = ExecContext(height=height, timestamp=timestamp)
            ok, code = True, "ok"
            if self.frozen:
                ok, code = False, "chain_frozen"
            elif not self.is_authority(tx.sender):
                ok, code = False, "sender_not_authority"
            else:
                try:
                    handlers[tx.kind](self, tx, ctx)
                except ContractError as e:
                    ok, code = False, e.code
            receipts.append({"tx": tx.tx_id, "kind": tx.kind, "ok": ok, "code": code, "events": ctx.events})
        self.receipts = receipts
        self.root = hash_obj({"prev": self.root, "height": height, "receipts": merkle_root(hash_obj(r) for r in receipts)})
        return receipts
