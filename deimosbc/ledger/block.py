"""Blocks and quorum attestations.

Execution is *deferred* (as in Tendermint): the header of block ``h`` commits
to ``app_hash`` and ``release_root`` - the contract state and the latest
released map *after block h-1*. A proposer therefore never has to execute a
speculative copy of the state, and every validator checks the proposer's view
of the state against its own replica before attesting.

A block is final once it carries attestations from a strict majority of the
authority set (the Data Processors named in genesis).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

from ..crypto import KeyPair, verify
from ..encoding import hash_obj, merkle_root
from .transaction import Transaction


@dataclass
class BlockHeader:
    height: int
    prev_hash: str
    timestamp: int
    proposer: str
    tx_root: str
    app_hash: str
    release_root: str

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    @property
    def hash(self) -> str:
        return hash_obj(self.to_dict())


@dataclass
class Block:
    header: BlockHeader
    txs: List[Transaction]
    proposer_sig: str = ""
    attestations: Dict[str, str] = field(default_factory=dict)  # pubkey -> sig(block hash)
    genesis: Dict[str, Any] | None = None                       # only for height 0

    @property
    def hash(self) -> str:
        return self.header.hash

    @property
    def height(self) -> int:
        return self.header.height

    @staticmethod
    def compute_tx_root(txs: Iterable[Transaction]) -> str:
        return merkle_root(tx.tx_id for tx in txs)

    @classmethod
    def propose(
        cls,
        keys: KeyPair,
        height: int,
        prev_hash: str,
        timestamp: int,
        txs: List[Transaction],
        app_hash: str,
        release_root: str,
    ) -> "Block":
        header = BlockHeader(
            height=height,
            prev_hash=prev_hash,
            timestamp=timestamp,
            proposer=keys.public_hex,
            tx_root=cls.compute_tx_root(txs),
            app_hash=app_hash,
            release_root=release_root,
        )
        blk = cls(header=header, txs=list(txs))
        blk.proposer_sig = keys.sign(bytes.fromhex(blk.hash))
        blk.attestations[keys.public_hex] = blk.proposer_sig
        return blk

    def attest(self, keys: KeyPair) -> str:
        sig = keys.sign(bytes.fromhex(self.hash))
        self.attestations[keys.public_hex] = sig
        return sig

    def valid_attesters(self, authorities: Iterable[str]) -> List[str]:
        auth = set(authorities)
        msg = bytes.fromhex(self.hash)
        return sorted(pk for pk, sig in self.attestations.items() if pk in auth and verify(pk, msg, sig))

    def has_quorum(self, authorities: List[str]) -> bool:
        return len(self.valid_attesters(authorities)) * 2 > len(authorities)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "header": self.header.to_dict(),
            "txs": [t.to_dict() for t in self.txs],
            "proposer_sig": self.proposer_sig,
            "attestations": dict(sorted(self.attestations.items())),
        }
        if self.genesis is not None:
            d["genesis"] = self.genesis
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Block":
        return cls(
            header=BlockHeader(**d["header"]),
            txs=[Transaction.from_dict(t) for t in d["txs"]],
            proposer_sig=d.get("proposer_sig", ""),
            attestations=dict(d.get("attestations", {})),
            genesis=d.get("genesis"),
        )
