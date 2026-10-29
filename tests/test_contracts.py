from deimosbc.contracts import ACCEPTED, DISABLED, DISPUTED, MAPPED, REJECTED, SUSPENDED
from deimosbc.crypto import KeyPair
from deimosbc.encoding import hash_obj
from deimosbc.ledger import TxKind
from deimosbc.packets import build_packet

from .conftest import Harness, line


def events(receipt, kind=None):
    return [e for e in receipt["events"] if kind is None or e["type"] == kind]


# ---------------------------------------------------------------- registry
def test_field_registration_needs_collector_signature(h: Harness):
    newbie = KeyPair.generate()
    rc = h.commit([h.register(newbie)])[0]
    assert rc["ok"] and newbie.public_hex in h.state.collectors
    assert h.commit([h.register(newbie, nonce=2)])[0]["code"] == "already_registered"

    impostor = KeyPair.generate()
    tx = h.register(impostor)
    tx.payload["collector"] = KeyPair.generate().public_hex  # key the signer does not own
    tx = h.tx(TxKind.REGISTER_COLLECTOR, tx.payload)
    assert h.commit([tx])[0]["code"] == "bad_collector_signature"


# ------------------------------------------------------------------- tasks
def test_task_grid_and_exclusive_claims(h: Harness):
    h.create_tasks()
    p = h.params
    assert len(h.state.tasks) == p.n_cells_x * p.n_cells_y
    t0 = h.state.tasks[0]
    assert {"start_lat_e7", "start_lon_e7", "direction_deg", "created_at", "status"} <= set(vars(t0))
    rc = h.commit([h.claim(0, 0), h.claim(1, 0)])
    assert rc[0]["ok"] and not rc[1]["ok"] and rc[1]["code"] == "task_unavailable"
    assert h.state.collectors[h.cols[0].public_hex].active_task == 0


def test_claim_switches_active_task(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    h.commit([h.claim(0, 1, nonce=2)])
    assert h.state.tasks[0].claims == {}
    assert h.cols[0].public_hex in h.state.tasks[1].claims


def test_mapped_task_is_no_longer_available():
    h = Harness(task_complete_length_m=60.0)
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    rc = h.commit([h.trace(0, 0, line(20, 50, 120, 50))])[0]
    assert events(rc, "task_mapped")
    assert h.state.tasks[0].status == MAPPED
    assert h.commit([h.claim(1, 0)])[0]["code"] == "task_already_mapped"


def test_claims_expire():
    h = Harness(claim_timeout_blocks=2)
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    for _ in range(3):
        h.commit([])
    assert h.state.tasks[0].claims == {}
    assert h.commit([h.claim(1, 0)])[0]["ok"]


# ---------------------------------------------------------------- uploads
def test_good_trace_is_accepted_and_rewarded(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    acct = h.state.collectors[h.cols[0].public_hex]
    before = acct.balance
    rc = h.commit([h.trace(0, 0, line(20, 50, 120, 50))])[0]
    ev = events(rc, "trace_accepted")[0]
    assert ev["new_m"] > 90 and acct.balance == before + ev["reward"]
    assert len(h.state.map.edges) == 10


def test_unqualified_trace_forfeits_stake(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    acct = h.state.collectors[h.cols[0].public_hex]
    before = acct.balance
    teleport = [(10, 10), (20, 10), (200, 200), (210, 200)]
    rc = h.commit([h.trace(0, 0, teleport)])[0]
    assert events(rc, "trace_rejected")[0]["reason"] == "gap_too_large"
    assert acct.balance == before - h.params.submission_stake
    assert acct.errors == 1 and acct.rejected == 1


def test_trace_for_unclaimed_task_is_rejected(h: Harness):
    h.create_tasks()
    rc = h.commit([h.trace(0, 5, line(20, 50, 120, 50))])[0]
    assert events(rc, "trace_rejected")[0]["reason"] == "task_not_claimed"


def test_suspension_reauth_and_disable():
    h = Harness(max_errors_before_reauth=2, max_reauths=1)
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    acct = h.state.collectors[h.cols[0].public_hex]
    bad = [(10, 10), (12, 10), (14, 10)]  # points too close
    seq = iter(range(100))
    h.commit([h.trace(0, 0, bad, seq=next(seq)), h.trace(0, 0, bad, seq=next(seq))])
    assert acct.status == SUSPENDED
    assert h.commit([h.trace(0, 0, bad, seq=next(seq))])[0]["code"] == "collector_not_active"
    assert h.commit([h.register(h.cols[0], nonce=7, purpose="reauth")])[0]["ok"]
    assert acct.status == "active" and acct.errors == 0
    h.commit([h.trace(0, 0, bad, seq=next(seq)), h.trace(0, 0, bad, seq=next(seq))])
    assert acct.status == DISABLED
    assert h.commit([h.register(h.cols[0], nonce=8, purpose="reauth")])[0]["code"] == "collector_disabled"


def test_author_identification_rejects_forgery_without_side_effects(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    victim = h.cols[0].public_hex
    forger = KeyPair.generate()
    pts = [(*h.frame.xy_to_e7(x, y), i * 8) for i, (x, y) in enumerate(line(20, 50, 120, 50))]
    signed = build_packet(forger, 0, 0, pts)
    signed["packet"]["collector"] = victim
    before = h.state.collectors[victim].balance
    rc = h.commit([h.tx(TxKind.SUBMIT_TRACE, {"packet": signed})])[0]
    assert rc["code"] == "bad_collector_signature"
    assert h.state.collectors[victim].balance == before and not h.state.traces


def test_duplicate_packet_from_two_processors(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    tx1 = h.trace(0, 0, line(20, 50, 120, 50))
    tx2 = h.tx(TxKind.SUBMIT_TRACE, tx1.payload, proc=1)
    rc = h.commit([tx1, tx2])
    assert rc[0]["ok"] and rc[1]["code"] == "duplicate_packet"


# --------------------------------------------------------------- conflicts
def _hazard_scenario(**kw):
    h = Harness(**kw)
    h.create_tasks()
    h.commit([h.claim(0, 0), h.claim(1, 1)])
    h.commit([h.claim(2, 2)])
    return h


def test_conflict_discards_both_traces():
    h = _hazard_scenario()
    # collector 0 claims a street is passable
    rc = h.commit([h.trace(0, 0, line(20, 100, 220, 100))])[0]
    first = events(rc, "trace_accepted")[0]["trace"]
    # collector 1 reports that street as flooded (hazard heading along the street)
    rc = h.commit([h.trace(1, 1, line(200, 160, 200, 230), hazards=[(150, 101, 0)])])[0]
    assert {e["trace"] for e in events(rc, "trace_disputed")} >= {first}
    assert h.state.traces[first].status == DISPUTED
    assert all(t.status != ACCEPTED for t in h.state.traces.values())
    assert not h.state.map.edges and not h.state.map.hazards


def test_crossing_street_is_not_a_conflict(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0), h.claim(1, 1)])
    h.commit([h.trace(0, 0, line(20, 100, 220, 100))])
    rc = h.commit([h.trace(1, 1, line(200, 20, 200, 230))])[0]
    assert events(rc, "trace_accepted")
    # the crossing created a shared intersection vertex
    assert any(len(nb) == 4 for nb in h.state.map.adj.values())


def test_geometric_conflict_on_misaligned_overlap(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0), h.claim(1, 1)])
    h.commit([h.trace(0, 0, line(20, 100, 220, 100))])
    zigzag = [(30 + 10 * i, 100 + (8 if i % 2 else -8)) for i in range(15)]
    rc = h.commit([h.trace(1, 1, zigzag)])[0]
    assert events(rc, "trace_disputed")


def test_evidence_policy_lets_corroborated_hazard_win():
    h = _hazard_scenario(conflict_policy="evidence")
    rc = h.commit([h.trace(0, 0, line(20, 100, 220, 100))])[0]
    fake = events(rc, "trace_accepted")[0]["trace"]
    rc = h.commit([h.trace(1, 1, line(200, 160, 200, 230), hazards=[(150, 101, 0)])])[0]
    assert events(rc, "conflict_won")
    assert h.state.traces[fake].status == DISPUTED
    assert h.state.map.hazards  # the hazard report survived
    # a second attempt to claim the flooded street loses against the hazard
    rc = h.commit([h.trace(0, 0, line(20, 100, 220, 100), seq=5, t0=500)])[0]
    assert events(rc, "trace_disputed")


def test_removing_a_trace_restores_previous_map():
    h = Harness()
    h.create_tasks()
    h.commit([h.claim(0, 0), h.claim(1, 1)])
    h.commit([h.trace(0, 0, line(20, 100, 220, 100))])
    root_before = h.state.map.root()
    h.commit([h.trace(1, 1, line(200, 20, 200, 230))])
    tid = [t for t, r in h.state.traces.items() if r.collector == h.cols[1].public_hex][0]
    assert h.state.map.root() != root_before
    h.state.map.remove_trace(tid)
    # vertices are anchored, so withdrawing a trace restores the earlier map exactly
    assert h.state.map.root() == root_before


# -------------------------------------------------------------- governance
def test_release_needs_majority_and_commits_map_root(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    h.commit([h.trace(0, 0, line(20, 50, 120, 50))])
    rc = h.commit([h.tx(TxKind.VOTE_RELEASE, {"version": 1}, proc=0)])
    assert not h.state.releases
    rc = h.commit([h.tx(TxKind.VOTE_RELEASE, {"version": 1}, proc=1)])
    assert events(rc[0], "map_released")
    rel = h.state.releases[0]
    assert rel.map_root == hash_obj(h.state.release_snapshots[1])
    h.commit([])
    assert h.replicas[0].tip.header.release_root == rel.map_root
    assert h.commit([h.tx(TxKind.VOTE_RELEASE, {"version": 1}, proc=2)])[0]["code"] == "stale_release_version"


def test_release_with_corroboration_filter():
    h = Harness(min_release_support=2)
    h.create_tasks()
    h.commit([h.claim(0, 0), h.claim(1, 1)])
    h.commit([h.trace(0, 0, line(20, 50, 120, 50))])
    h.commit([h.trace(1, 1, line(20, 52, 240, 52))])  # overlaps only the first 100 m
    h.commit([h.tx(TxKind.VOTE_RELEASE, {"version": 1}, proc=p) for p in (0, 1)])
    snap = h.state.release_snapshots[1]
    assert 0 < len(snap["edges"]) < len(h.state.map.edges)
    assert all(e["support"] >= 2 for e in snap["edges"])


def test_freeze_by_majority_vote_marks_final_block(h: Harness):
    rc = h.commit([h.tx(TxKind.PROPOSE_FREEZE, {"reason": "test"}, proc=0)])[0]
    pid = events(rc, "freeze_proposed")[0]["proposal_id"]
    rc = h.commit([h.tx(TxKind.VOTE_FREEZE, {"proposal_id": pid, "approve": True}, proc=1)])[0]
    assert events(rc, "chain_frozen")
    assert h.state.frozen and h.state.final_height == h.replicas[0].height
    b = h.block([])
    assert h.replicas[1].check_proposal(b) == "chain_frozen"


def test_freeze_rejected_by_majority(h: Harness):
    rc = h.commit([h.tx(TxKind.PROPOSE_FREEZE, {"reason": "test"}, proc=0)])[0]
    pid = events(rc, "freeze_proposed")[0]["proposal_id"]
    h.commit([h.tx(TxKind.VOTE_FREEZE, {"proposal_id": pid, "approve": False}, proc=1)])
    rc = h.commit([h.tx(TxKind.VOTE_FREEZE, {"proposal_id": pid, "approve": False}, proc=2)])[0]
    assert events(rc, "freeze_rejected") and not h.state.frozen


def test_rejected_counts_feed_integrity_window(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    h.commit([h.trace(0, 0, [(10, 10), (12, 10), (14, 10)])])
    rate, n = h.state.error_rate()
    assert n == 1 and rate == 1.0
    assert h.state.traces[next(iter(h.state.traces))].status == REJECTED
