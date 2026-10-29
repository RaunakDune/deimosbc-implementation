import copy

import pytest

from deimosbc.crypto import KeyPair, verify_obj
from deimosbc.encoding import canonical_json, merkle_root
from deimosbc.ledger import Block, Transaction, TxKind
from deimosbc.replica import ChainError, Replica

from .conftest import Harness


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})


def test_signatures_roundtrip():
    k = KeyPair.generate()
    sig = k.sign_obj({"x": 1})
    assert verify_obj(k.public_hex, {"x": 1}, sig)
    assert not verify_obj(k.public_hex, {"x": 2}, sig)


def test_merkle_root_changes_with_leaves():
    a = merkle_root(["00" * 32, "11" * 32])
    b = merkle_root(["11" * 32, "00" * 32])
    assert a != b and len(a) == 64


def test_commit_requires_majority_quorum(h: Harness):
    b = h.block([h.tx(TxKind.CREATE_TASKS, {})])
    # proposer signature only: 1 of 3 is not a majority
    with pytest.raises(ChainError, match="no_quorum"):
        h.replicas[1].commit(copy.deepcopy(b))
    b.attest(h.procs[1])
    b.attest(h.procs[2])
    h.replicas[1].commit(b)
    assert h.replicas[1].height == 1


def test_attestation_from_non_authority_does_not_count(h: Harness):
    b = h.block([])
    outsider = KeyPair.generate()
    b.attest(outsider)
    assert not b.has_quorum(h.replicas[0].authorities)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda b: setattr(b.header, "app_hash", "f" * 64), "app_hash_mismatch"),
        (lambda b: setattr(b.header, "prev_hash", "a" * 64), "bad_prev_hash"),
        (lambda b: setattr(b.header, "height", 5), "bad_height"),
    ],
)
def test_validators_reject_bad_headers(h: Harness, mutate, reason):
    b = h.block([])
    mutate(b)
    b.attestations = {}
    b.proposer_sig = h.by_pk[b.header.proposer].sign(bytes.fromhex(b.hash))
    b.attestations[b.header.proposer] = b.proposer_sig
    assert h.replicas[1].check_proposal(b) == reason


def test_forged_transaction_is_rejected(h: Harness):
    tx = h.tx(TxKind.CREATE_TASKS, {})
    tx.payload = {"cells": [[0, 0]]}          # tamper after signing
    tx._id = ""
    b = h.block([tx])
    assert h.replicas[1].check_proposal(b) == "bad_tx_signature"


def test_transaction_from_non_authority_is_rejected(h: Harness):
    outsider = KeyPair.generate()
    tx = Transaction.create(TxKind.CREATE_TASKS, outsider, 1, {})
    assert h.replicas[1].check_proposal(h.block([tx])) == "tx_sender_not_authority"


def test_replayed_transaction_is_rejected(h: Harness):
    tx = h.tx(TxKind.CREATE_TASKS, {})
    h.commit([tx])
    r = h.replicas[0]
    prop = h.by_pk[r.expected_proposer(r.height + 1)]
    b = Block.propose(prop, r.height + 1, r.tip.hash, h.t + 5, [tx], r.state.root, r.state.release_root)
    assert h.replicas[1].check_proposal(b) == "duplicate_tx"


def test_export_replay_and_tamper_detection(h: Harness, tmp_path):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    path = tmp_path / "chain.jsonl"
    h.replicas[0].export(str(path))
    blocks = Replica.load_blocks(str(path))
    rep, problems = Replica.replay(blocks)
    assert not problems and rep.state.root == h.state.root

    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace('"create_tasks"', '"claim_task"', 1)
    path.write_text("\n".join(lines) + "\n")
    _, problems = Replica.replay(Replica.load_blocks(str(path)))
    assert problems
