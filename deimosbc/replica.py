"""A node's copy of the ledger: block store + replicated contract state.

``Replica.check_proposal`` is the validation every Data Processor runs before
attesting a block; ``Replica.commit`` appends a block that carries a majority
quorum and executes it. ``Replica.replay`` rebuilds and fully re-verifies a
chain from its genesis block (used by the ``deimosbc verify`` command).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ProtocolParams
from .contracts.state import WorldState
from .encoding import hash_obj
from .ledger.block import Block, BlockHeader
from .ledger.transaction import Transaction

ZERO_HASH = "0" * 64


def make_genesis(
    params: ProtocolParams,
    authorities: List[str],
    collectors: Iterable[str] = (),
    chain_id: str = "deimosbc-sim",
    timestamp: int = 0,
) -> Block:
    """Genesis block: protocol rules, the pre-disaster authority set (Data
    Processors) and the pre-verified volunteers (Data Collectors)."""
    if not authorities:
        raise ValueError("at least one processor is required")
    genesis = {
        "chain_id": chain_id,
        "params": params.to_dict(),
        "authorities": sorted(authorities),
        "collectors": sorted(collectors),
        "timestamp": timestamp,
    }
    header = BlockHeader(
        height=0,
        prev_hash=ZERO_HASH,
        timestamp=timestamp,
        proposer="genesis",
        tx_root=Block.compute_tx_root([]),
        app_hash=hash_obj(genesis),
        release_root=ZERO_HASH,
    )
    return Block(header=header, txs=[], genesis=genesis)


class ChainError(Exception):
    pass


class Replica:
    def __init__(self, genesis_block: Block, merger=None):
        if genesis_block.genesis is None or genesis_block.height != 0:
            raise ChainError("not a genesis block")
        if genesis_block.header.app_hash != hash_obj(genesis_block.genesis):
            raise ChainError("genesis hash mismatch")
        self.blocks: List[Block] = [genesis_block]
        self.state = WorldState(genesis_block.genesis, merger=merger)
        self.tx_ids: set[str] = set()
        self.receipts_by_height: Dict[int, List[Dict[str, Any]]] = {0: []}

    # ----------------------------------------------------------------- views
    @property
    def tip(self) -> Block:
        return self.blocks[-1]

    @property
    def height(self) -> int:
        return self.tip.height

    @property
    def authorities(self) -> List[str]:
        return self.state.authorities

    @property
    def params(self) -> ProtocolParams:
        return self.state.params

    @property
    def genesis(self) -> Block:
        return self.blocks[0]

    def expected_proposer(self, height: int, view: int = 0) -> str:
        """Round-robin proposer schedule; ``view`` advances on timeouts."""
        a = self.authorities
        return a[(height + view) % len(a)]

    # ------------------------------------------------------------ validation
    def check_proposal(self, block: Block) -> Optional[str]:
        """Return a rejection reason, or None if the block may be attested."""
        h = block.header
        st = self.state
        if st.frozen:
            return "chain_frozen"
        if h.height != self.height + 1:
            return "bad_height"
        if h.prev_hash != self.tip.hash:
            return "bad_prev_hash"
        if h.proposer not in st.authorities:
            return "proposer_not_authority"
        if h.proposer not in block.attestations or block.attestations[h.proposer] != block.proposer_sig:
            return "missing_proposer_signature"
        if h.proposer not in block.valid_attesters(st.authorities):
            return "bad_proposer_signature"
        if h.timestamp < self.tip.header.timestamp:
            return "timestamp_regression"
        if h.app_hash != st.root:
            return "app_hash_mismatch"
        if h.release_root != st.release_root:
            return "release_root_mismatch"
        if len(block.txs) > st.params.max_txs_per_block:
            return "too_many_txs"
        if h.tx_root != Block.compute_tx_root(block.txs):
            return "bad_tx_root"
        seen = set()
        for tx in block.txs:
            if tx.tx_id in self.tx_ids or tx.tx_id in seen:
                return "duplicate_tx"
            seen.add(tx.tx_id)
            if tx.sender not in st.authorities:
                return "tx_sender_not_authority"
            if not tx.verify_signature():
                return "bad_tx_signature"
        return None

    def commit(self, block: Block) -> List[Dict[str, Any]]:
        reason = self.check_proposal(block)
        if reason:
            raise ChainError(reason)
        if not block.has_quorum(self.authorities):
            raise ChainError("no_quorum")
        receipts = self.state.apply_block(block.height, block.header.timestamp, block.txs)
        self.blocks.append(block)
        self.tx_ids.update(tx.tx_id for tx in block.txs)
        self.receipts_by_height[block.height] = receipts
        return receipts

    def propose(self, keys, txs: List[Transaction], timestamp: int) -> Block:
        fresh = [t for t in txs if t.tx_id not in self.tx_ids][: self.params.max_txs_per_block]
        return Block.propose(
            keys,
            height=self.height + 1,
            prev_hash=self.tip.hash,
            timestamp=max(timestamp, self.tip.header.timestamp),
            txs=fresh,
            app_hash=self.state.root,
            release_root=self.state.release_root,
        )

    # ------------------------------------------------------- persistence/audit
    def export(self, path: str) -> None:
        with open(path, "w") as f:
            for b in self.blocks:
                f.write(json.dumps(b.to_dict(), sort_keys=True) + "\n")

    @staticmethod
    def load_blocks(path: str) -> List[Block]:
        with open(path) as f:
            return [Block.from_dict(json.loads(line)) for line in f if line.strip()]

    @classmethod
    def replay(cls, blocks: List[Block], merger=None) -> Tuple["Replica", List[str]]:
        """Re-verify every link, signature, quorum and state transition."""
        rep = cls(blocks[0], merger=merger)
        problems: List[str] = []
        for b in blocks[1:]:
            try:
                rep.commit(b)
            except ChainError as e:
                problems.append(f"height {b.height}: {e}")
                break
        return rep, problems
