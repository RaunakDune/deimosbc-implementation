"""Command-line interface.

    deimosbc simulate [--config FILE] [--set key=value ...] [--protocol key=value ...] [--out DIR]
    deimosbc sweep    --grid key=v1,v2 [--grid ...] [--seeds N] [--out FILE.csv]
    deimosbc verify   CHAIN.jsonl
    deimosbc params
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Dict, List

from .config import ProtocolParams
from .export import ground_truth_geojson, map_to_geojson, map_to_proteus_csv, plot_run, write_json
from .geo import LocalFrame

if TYPE_CHECKING:
    from .sim.scenario import SimConfig


def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def _kv(items: List[str]) -> Dict[str, Any]:
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"expected key=value, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = _parse_value(v.strip())
    return out


def load_config(path: str | None) -> Dict[str, Any]:
    if not path:
        return {}
    if path.endswith(".toml"):
        try:
            import tomllib
        except ImportError:  # Python 3.10
            raise SystemExit("TOML configs need Python 3.11+; use a .json config instead")
        with open(path, "rb") as f:
            return tomllib.load(f)
    with open(path) as f:
        return json.load(f)


def _build_cfg(args) -> "SimConfig":
    from .sim.scenario import SimConfig

    d = load_config(args.config)
    d.update(_kv(args.set))
    proto = dict(d.pop("protocol", {}))
    proto.update(_kv(args.protocol))
    d["protocol"] = proto
    if args.seed is not None:
        d["seed"] = args.seed
    return SimConfig.from_dict(d)


def cmd_simulate(args) -> int:
    from .sim.metrics import evaluate_map
    from .sim.scenario import Simulation

    cfg = _build_cfg(args)
    sim = Simulation(cfg)
    report = sim.run(progress=not args.quiet)
    ref = sim.reference()
    st = ref.state
    frame = LocalFrame(sim.params.origin_lat, sim.params.origin_lon)
    os.makedirs(args.out, exist_ok=True)
    snap = st.release_snapshots[st.releases[-1].version] if st.releases else st.map.snapshot()
    write_json(report, os.path.join(args.out, "report.json"))
    write_json(map_to_geojson(snap, frame, {"source": "released" if st.releases else "live"}), os.path.join(args.out, "map.geojson"))
    write_json(ground_truth_geojson(sim.road, frame), os.path.join(args.out, "ground_truth.geojson"))
    map_to_proteus_csv(snap, frame, os.path.join(args.out, "map_edges.csv"))
    ref.replica.export(os.path.join(args.out, "chain.jsonl"))
    plotted = plot_run(sim, snap, os.path.join(args.out, "map.png"),
                       title=f"DEIMOSBC - released map v{len(st.releases)} at t={sim.now}s")

    m = report["final_released_map"] or evaluate_map(snap, sim.road)
    print(f"ticks={sim.now}  height={ref.replica.height}  releases={len(st.releases)}  frozen={st.frozen}")
    print(f"traces: {report['chain']['traces']}")
    print(f"map:    recall={m['recall']:.3f}  precision={m['precision']:.3f}  contamination={m['contamination']:.3f}  "
          f"edges={m['edges']}  length={m['mapped_length_m']:.0f}m / {m['open_length_m']:.0f}m open")
    print(f"tasks mapped: {report['chain']['tasks_mapped']}/{report['chain']['tasks_total']}   "
          f"upload latency median={report['upload_latency']['median_s']}s")
    for role, d in report["collectors_by_role"].items():
        print(f"  {role:<9} n={d['count']:<3} accepted={d['accepted']:<5} rejected={d['rejected']:<4} disputed={d['disputed']:<4} status={d['status']}")
    print(f"outputs written to {args.out}/ (report.json, map.geojson, ground_truth.geojson, map_edges.csv, chain.jsonl"
          + (", map.png)" if plotted else ")"))
    return 0


def cmd_verify(args) -> int:
    from .replica import Replica

    blocks = Replica.load_blocks(args.chain)
    rep, problems = Replica.replay(blocks)
    st = rep.state
    print(f"blocks: {len(blocks)}  verified height: {rep.height}  state root: {st.root[:16]}...")
    print(f"authorities: {len(st.authorities)}  collectors: {len(st.collectors)}  traces: {len(st.traces)}  releases: {len(st.releases)}")
    if st.frozen:
        print(f"chain frozen at final height {st.final_height}")
    if problems:
        print("INVALID:")
        for p in problems:
            print("  " + p)
        return 1
    print("OK: every block link, signature, quorum and state transition re-verified from genesis")
    return 0


def cmd_sweep(args) -> int:
    """Full-factorial parameter sweep (one CSV row per run), e.g. for ANOVA as in Proteus."""
    from .sim.metrics import f1
    from .sim.scenario import SimConfig, Simulation

    base = load_config(args.config)
    base_proto = dict(base.pop("protocol", {}))
    grid = {}
    for g in args.grid:
        k, vs = g.split("=", 1)
        grid[k] = [_parse_value(v) for v in vs.split(",")]
    keys = sorted(grid)
    rows = []
    combos = list(itertools.product(*[grid[k] for k in keys]))
    for ci, combo in enumerate(combos):
        for s in range(args.seeds):
            d = dict(base)
            proto = dict(base_proto)
            for k, v in zip(keys, combo):
                if k.startswith("protocol."):
                    proto[k.split(".", 1)[1]] = v
                else:
                    d[k] = v
            d["protocol"] = proto
            d["seed"] = base.get("seed", 7) + s
            cfg = SimConfig.from_dict(d)
            rep = Simulation(cfg).run()
            m = rep["final_released_map"] or rep["live_chain_map"]
            row = {k: v for k, v in zip(keys, combo)}
            row.update(seed=d["seed"], recall=round(m["recall"], 4), precision=round(m["precision"], 4),
                       contamination=round(m["contamination"], 4), f1=round(f1(m), 4),
                       releases=rep["chain"]["releases"], frozen=rep["chain"]["frozen"],
                       accepted=rep["chain"]["traces"].get("accepted", 0), rejected=rep["chain"]["traces"].get("rejected", 0),
                       disputed=rep["chain"]["traces"].get("disputed", 0), latency_median_s=rep["upload_latency"]["median_s"])
            rows.append(row)
            print(f"[{ci * args.seeds + s + 1}/{len(combos) * args.seeds}] {row}", flush=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


def cmd_params(args) -> int:
    print(json.dumps(ProtocolParams().to_dict(), indent=2))
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="deimosbc", description="DEIMOSBC reference implementation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("simulate", help="run the hurricane crowdsensing scenario")
    s.add_argument("--config", help="JSON or TOML file with SimConfig fields (and a [protocol] table)")
    s.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a SimConfig field")
    s.add_argument("--protocol", action="append", default=[], metavar="KEY=VALUE", help="override a ProtocolParams field")
    s.add_argument("--seed", type=int)
    s.add_argument("--out", default="runs/latest")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_simulate)

    w = sub.add_parser("sweep", help="full-factorial parameter sweep to CSV")
    w.add_argument("--config")
    w.add_argument("--grid", action="append", required=True, metavar="KEY=V1,V2", help="SimConfig field or protocol.<field>")
    w.add_argument("--seeds", type=int, default=1)
    w.add_argument("--out", default="sweep.csv")
    w.set_defaults(fn=cmd_sweep)

    v = sub.add_parser("verify", help="re-verify an exported chain from genesis")
    v.add_argument("chain")
    v.set_defaults(fn=cmd_verify)

    p = sub.add_parser("params", help="print default protocol parameters")
    p.set_defaults(fn=cmd_params)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if getattr(args, "quiet", True) is False else logging.WARNING,
                        format="%(message)s")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
