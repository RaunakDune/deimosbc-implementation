import copy

from deimosbc.config import ProtocolParams
from deimosbc.crypto import KeyPair
from deimosbc.ledger import TxKind
from deimosbc.mapping import MapGraph, OverlapChainMerger
from deimosbc.nodes.requester import verify_map_response
from deimosbc.packets import build_packet, verify_author, verify_package
from deimosbc.tms import DisplacementSampler, Fix, TraceManager, TrajectorySegmenter

from .conftest import Harness, line


# --------------------------------------------------------------------- TMS
def test_displacement_sampling_ignores_standing_still():
    s = DisplacementSampler(10.0)
    out = [s.feed(Fix(x, 0, t)) for t, x in enumerate([0, 2, 4, 9, 11, 13, 25])]
    assert [f.x for f in out if f] == [0, 11, 25]


def test_displacement_sampler_drops_gps_spikes():
    s = DisplacementSampler(10.0, max_speed=5.0)
    assert s.feed(Fix(0, 0, 0))
    assert s.feed(Fix(80, 0, 1)) is None and s.outliers == 1
    assert s.feed(Fix(12, 0, 4))


def test_segmentation_splits_on_u_turn_and_keeps_turning_point():
    seg = TrajectorySegmenter(angle_deg=120, gap_m=60, min_points=3, max_points=50, smooth=False)
    pts = [(0, 0), (10, 0), (20, 0), (30, 0), (20, 0.5), (10, 1), (0, 1)]
    for t, (x, y) in enumerate(pts):
        seg.feed(Fix(x, y, t))
    seg.flush()
    traces = seg.pop_traces()
    assert len(traces) == 2
    assert traces[0][-1].x == 30 and traces[1][0].x == 30


def test_segmentation_keeps_right_angle_corners():
    seg = TrajectorySegmenter(angle_deg=120, gap_m=60, min_points=3, max_points=50)
    for t, (x, y) in enumerate([(0, 0), (10, 0), (20, 0), (20, 10), (20, 20)]):
        seg.feed(Fix(x, y, t))
    seg.flush()
    assert len(seg.pop_traces()) == 1


def test_segmentation_splits_on_gap_and_length():
    seg = TrajectorySegmenter(angle_deg=120, gap_m=30, min_points=2, max_points=4, smooth=False)
    xs = [0, 10, 20, 30, 40, 100, 110]
    for t, x in enumerate(xs):
        seg.feed(Fix(x, 0, t))
    seg.flush()
    lens = [len(t) for t in seg.pop_traces()]
    assert lens == [4, 2, 2]


def test_trace_manager_pipeline():
    tm = TraceManager(ProtocolParams())
    for t in range(200):
        tm.feed(Fix(t * 1.4, 0.0, t))
    tm.flush()
    traces = tm.pop_traces()
    assert traces and all(len(tr) >= 3 for tr in traces)


# ----------------------------------------------------------------- packets
def test_package_verification():
    k = KeyPair.generate()
    good = build_packet(k, 0, 0, [(1, 2, 0), (3, 4, 1), (5, 6, 2)])
    assert verify_package(good) is None and verify_author(good)
    bad = copy.deepcopy(good)
    bad["packet"]["points"][1][2] = -5
    assert verify_package(bad) == "points_out_of_order"
    bad = copy.deepcopy(good)
    del bad["packet"]["seq"]
    assert verify_package(bad) == "missing_fields"
    bad = copy.deepcopy(good)
    bad["packet"]["points"][0][0] = 1.5
    assert verify_package(bad) == "bad_point_encoding"
    bad = copy.deepcopy(good)
    bad["packet"]["points"][0][0] = 99
    assert verify_package(bad) is None and not verify_author(bad)


# ----------------------------------------------------------------- merging
def _merger():
    return OverlapChainMerger(merge_radius=15, max_angle_deviation=35, conflict_min_run=40, hazard_radius=15)


def test_overlapping_traces_share_edges():
    g, m = MapGraph(15), _merger()
    m.integrate(g, line(0, 0, 200, 0), [], "t1", "c1", [])
    n_edges = len(g.edges)
    res = m.integrate(g, [(x, y + 4) for x, y in line(0, 0, 200, 0)], [], "t2", "c2", [])
    assert res.new_length < 1e-9 and len(g.edges) == n_edges
    assert all(e.collectors == {"c1", "c2"} for e in g.edges.values())


def test_chaining_extends_and_joins():
    g, m = MapGraph(15), _merger()
    m.integrate(g, line(0, 0, 100, 0), [], "t1", "c1", [])
    m.integrate(g, line(100, 0, 100, 100), [], "t2", "c2", [])
    # one connected component
    seen, stack = set(), [next(iter(g.adj))]
    while stack:
        u = stack.pop()
        if u not in seen:
            seen.add(u)
            stack.extend(g.adj[u])
    assert seen == set(g.vertices)


def test_consecutive_samples_do_not_collapse():
    g, m = MapGraph(15), _merger()
    m.integrate(g, line(0, 0, 100, 0), [], "t1", "c1", [])
    assert len(g.vertices) == 11


# --------------------------------------------------------------- requester
def _released(h: Harness):
    h.create_tasks()
    h.commit([h.claim(0, 0)])
    h.commit([h.trace(0, 0, line(20, 50, 120, 50))])
    h.commit([h.tx(TxKind.VOTE_RELEASE, {"version": 1}, proc=p) for p in (0, 1)])
    h.commit([])
    st = h.state
    rel = st.releases[0]
    blk = h.replicas[0].blocks[rel.height + 1]
    return {
        "release": {"version": 1, "height": rel.height, "timestamp": rel.timestamp, "map_root": rel.map_root},
        "map": st.release_snapshots[1],
        "origin": [0, 0],
        "proof": {"header": blk.header.to_dict(), "attestations": dict(blk.attestations)},
    }


def test_requester_verifies_authentic_map(h: Harness):
    resp = _released(h)
    auth = [k.public_hex for k in h.procs]
    assert verify_map_response(resp, auth) is None


def test_requester_rejects_tampered_map(h: Harness):
    resp = _released(h)
    auth = [k.public_hex for k in h.procs]
    bad = copy.deepcopy(resp)
    bad["map"]["edges"].append({"u": 1, "v": 99, "local_map": [0, 0], "support": 1})
    assert verify_map_response(bad, auth) == "map_hash_mismatch"
    bad = copy.deepcopy(resp)
    bad["proof"]["attestations"] = dict(list(bad["proof"]["attestations"].items())[:1])
    assert verify_map_response(bad, auth) == "no_quorum"
    assert verify_map_response(resp, [KeyPair.generate().public_hex for _ in range(3)]) == "no_quorum"
