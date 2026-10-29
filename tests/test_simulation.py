import pytest

from deimosbc.cli import main
from deimosbc.sim import RoadNetwork, SimConfig, Simulation


def small(**kw):
    base = dict(duration_ticks=900, n_collectors=14, field_registrations=2, area_m=1000.0, n_floods=1,
                flood_radius_m=(120.0, 160.0), n_processors=3, processor_range_m=300.0)
    base.update(kw)
    return SimConfig(**base)


def test_end_to_end_replicas_agree_and_map_is_released():
    sim = Simulation(small())
    rep = sim.run()
    roots = {p.state.root for p in sim.processors}
    assert len(roots) == 1
    assert rep["chain"]["releases"] >= 1
    assert rep["chain"]["traces"].get("accepted", 0) > 50
    m = rep["final_released_map"]
    assert m["recall"] > 0.1 and m["precision"] > 0.6
    # field registrations went through
    assert all(c.registered for c in sim.collectors)
    # garbage uploaders are caught by the quality rules
    if "garbage" in rep["collectors_by_role"]:
        assert rep["collectors_by_role"]["garbage"]["accepted"] == 0


def test_byzantine_proposer_cannot_stall_or_corrupt_chain():
    sim = Simulation(small(n_processors=5, byzantine={1: "tamper", 3: "forge"}))
    rep = sim.run()
    assert rep["consensus"]["rejected_proposals"].get("app_hash_mismatch", 0) > 0
    honest = [p for p in sim.processors if p.behaviour != "tamper"]
    assert len({p.state.root for p in honest}) == 1
    assert rep["chain"]["height"] >= 0.7 * (sim.now // sim.params.block_interval_ticks)
    assert rep["chain"]["receipts"].get("submit_trace:bad_collector_signature", 0) > 0


def test_crashes_stall_without_majority_and_recover():
    cfg = small(n_processors=5, duration_ticks=600, crashes=[(0, 100, 300), (1, 100, 300), (2, 100, 300)])
    sim = Simulation(cfg)
    rep = sim.run()
    assert rep["consensus"]["stalled_rounds"] > 0
    assert len({p.replica.height for p in sim.processors}) == 1


def test_integrity_failure_freezes_the_chain():
    cfg = small(frac_garbage=0.6, frac_fabricate=0.0, frac_faulty=0.0, duration_ticks=1500,
                protocol={"freeze_error_rate": 0.4, "integrity_min_samples": 8, "integrity_window": 16})
    sim = Simulation(cfg)
    rep = sim.run()
    assert rep["chain"]["frozen"]
    assert rep["chain"]["receipts"].get("vote_freeze:ok", 0) + rep["chain"]["receipts"].get("propose_freeze:ok", 0) >= 2
    assert any(c.status == "stopped" for c in sim.collectors)


def test_proteus_edge_csv_roundtrip(tmp_path):
    p = tmp_path / "edges.csv"
    rows = ["x0,y0,x1,y1"]
    lon0, lat0, d = -95.36, 29.72, 0.001
    for i in range(6):
        for j in range(6):
            x, y = lon0 + i * d, lat0 + j * d
            if i < 5:
                rows.append(f"{x},{y},{x + d},{y}")
            if j < 5:
                rows.append(f"{x},{y},{x},{y + d}")
    p.write_text("\n".join(rows) + "\n")
    road, lat, lon, w, h = RoadNetwork.from_edge_csv(str(p))
    assert len(road.nodes) == 36 and len(road.edges) == 60
    assert 450 < w < 650 and 500 < h < 700
    sim = Simulation(SimConfig(roadnet=str(p), duration_ticks=300, n_collectors=6, n_processors=3, n_floods=0,
                               protocol={"task_cell_size_m": 200.0}))
    sim.run()
    assert sim.reference().replica.height > 0


def test_cli_simulate_and_verify(tmp_path):
    out = tmp_path / "run"
    assert main(["simulate", "--quiet", "--out", str(out), "--set", "duration_ticks=300", "--set", "n_collectors=8",
                 "--set", "area_m=800", "--set", "n_processors=3", "--protocol", "release_interval_blocks=10"]) == 0
    for f in ("report.json", "map.geojson", "ground_truth.geojson", "map_edges.csv", "chain.jsonl"):
        assert (out / f).exists()
    assert main(["verify", str(out / "chain.jsonl")]) == 0


def test_unknown_setting_is_an_error():
    with pytest.raises(ValueError):
        SimConfig.from_dict({"n_colectors": 3})


def test_execution_is_independent_of_hash_seed():
    import os
    import subprocess
    import sys

    code = (
        "from deimosbc.sim import SimConfig, Simulation\n"
        "s = Simulation(SimConfig(duration_ticks=400, n_collectors=10, area_m=1000.0, n_processors=3))\n"
        "s.run(); print(s.reference().state.root)\n"
    )
    roots = set()
    for seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
        roots.add(out.stdout.strip())
    assert len(roots) == 1
