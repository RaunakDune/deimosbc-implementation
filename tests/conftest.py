import itertools

import pytest

from deimosbc.config import ProtocolParams
from deimosbc.contracts import claim_request, registration_request
from deimosbc.crypto import KeyPair
from deimosbc.ledger import Transaction, TxKind
from deimosbc.packets import build_packet
from deimosbc.replica import Replica, make_genesis


class Harness:
    """Three processors + a few collectors, committing blocks by hand."""

    def __init__(self, n_proc=3, n_col=3, **param_overrides):
        self.params = ProtocolParams(**param_overrides)
        self.procs = [KeyPair.from_seed(bytes([i + 1]) * 32) for i in range(n_proc)]
        self.cols = [KeyPair.from_seed(bytes([100 + i]) * 32) for i in range(n_col)]
        self.genesis = make_genesis(self.params, [k.public_hex for k in self.procs], [c.public_hex for c in self.cols])
        self.replicas = [Replica(self.genesis) for _ in self.procs]
        self.by_pk = {k.public_hex: k for k in self.procs}
        self._nonce = itertools.count(1)
        self.t = 0

    @property
    def state(self):
        return self.replicas[0].state

    @property
    def frame(self):
        return self.state.frame

    def tx(self, kind, payload, proc=0):
        return Transaction.create(kind, self.procs[proc], next(self._nonce), payload)

    def block(self, txs):
        r0 = self.replicas[0]
        prop = self.by_pk[r0.expected_proposer(r0.height + 1)]
        self.t += self.params.block_interval_ticks
        return r0.propose(prop, txs, self.t)

    def commit(self, txs):
        b = self.block(txs)
        for k, r in zip(self.procs, self.replicas):
            assert r.check_proposal(b) is None, r.check_proposal(b)
            b.attest(k)
        receipts = [r.commit(b) for r in self.replicas]
        assert len({r.state.root for r in self.replicas}) == 1, "replicas diverged"
        return receipts[0]

    # convenience builders -------------------------------------------------
    def create_tasks(self):
        return self.commit([self.tx(TxKind.CREATE_TASKS, {})])

    def claim(self, col, task_id, nonce=1):
        c = self.cols[col]
        sig = c.sign_obj(claim_request(c.public_hex, task_id, nonce))
        return self.tx(TxKind.CLAIM_TASK, {"collector": c.public_hex, "task_id": task_id, "nonce": nonce, "collector_sig": sig})

    def register(self, keys, nonce=1, purpose="register"):
        kind = TxKind.REGISTER_COLLECTOR if purpose == "register" else TxKind.REAUTH_COLLECTOR
        sig = keys.sign_obj(registration_request(keys.public_hex, purpose, nonce))
        return self.tx(kind, {"collector": keys.public_hex, "nonce": nonce, "collector_sig": sig})

    def trace(self, col, task_id, xys, seq=0, t0=0, dt=8, hazards=()):
        pts = [(*self.frame.xy_to_e7(x, y), t0 + i * dt) for i, (x, y) in enumerate(xys)]
        hz = [(*self.frame.xy_to_e7(x, y), t0, int(h)) for x, y, h in hazards]
        return self.tx(TxKind.SUBMIT_TRACE, {"packet": build_packet(self.cols[col], task_id, seq, pts, hazards=hz)})


def line(x0, y0, x1, y1, step=10.0):
    import math

    n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
    return [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1)]


@pytest.fixture
def h():
    return Harness()
