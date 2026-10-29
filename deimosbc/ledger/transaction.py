"""Signed transactions.

Every transaction is submitted by a Data Processor (the nodes that "connect to
the blockchain and run the smart contracts", Sec. IV-A.2). Actions that a
Data Collector authorises (registration, task claims, trace uploads) carry the
collector's own signature inside the payload, so the contract can perform
*Author Identification* on-chain and no processor can forge collector data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict

from ..crypto import KeyPair, verify_obj
from ..encoding import hash_obj


class TxKind:
    REGISTER_COLLECTOR = "register_collector"
    REAUTH_COLLECTOR = "reauth_collector"
    CREATE_TASKS = "create_tasks"
    CLAIM_TASK = "claim_task"
    SUBMIT_TRACE = "submit_trace"
    PROPOSE_FREEZE = "propose_freeze"
    VOTE_FREEZE = "vote_freeze"
    VOTE_RELEASE = "vote_release"

    ALL = (
        REGISTER_COLLECTOR,
        REAUTH_COLLECTOR,
        CREATE_TASKS,
        CLAIM_TASK,
        SUBMIT_TRACE,
        PROPOSE_FREEZE,
        VOTE_FREEZE,
        VOTE_RELEASE,
    )


@dataclass
class Transaction:
    kind: str
    sender: str                      # processor public key (hex)
    nonce: int
    payload: Dict[str, Any]
    signature: str = ""
    _id: str = field(default="", repr=False, compare=False)

    def body(self) -> Dict[str, Any]:
        return {"kind": self.kind, "sender": self.sender, "nonce": self.nonce, "payload": self.payload}

    @property
    def tx_id(self) -> str:
        if not self._id:
            self._id = hash_obj(self.body())
        return self._id

    @classmethod
    def create(cls, kind: str, keys: KeyPair, nonce: int, payload: Dict[str, Any]) -> "Transaction":
        tx = cls(kind=kind, sender=keys.public_hex, nonce=nonce, payload=payload)
        tx.signature = keys.sign_obj(tx.body())
        return tx

    def verify_signature(self) -> bool:
        return self.kind in TxKind.ALL and verify_obj(self.sender, self.body(), self.signature)

    def to_dict(self) -> Dict[str, Any]:
        return {**self.body(), "signature": self.signature}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Transaction":
        return cls(kind=d["kind"], sender=d["sender"], nonce=d["nonce"], payload=d["payload"], signature=d["signature"])
