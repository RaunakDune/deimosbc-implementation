# Connecting DEIMOSBC and Proteus

> Sarbajna, R., Eick, C. F., & Ni, J. (2026). *Proteus: A System for Combining
> Paths Collected Using Crowdsensing.* GeoInformatica 30, 7.
> [doi:10.1007/s10707-026-00567-1](https://link.springer.com/article/10.1007/s10707-026-00567-1) ·
> data: [UH-DAIS/ProteusData](https://github.com/UH-DAIS/ProteusData)

The two systems fit together naturally. DEIMOSBC provides the trust and
coordination layer: who may contribute, how contributions are verified,
rewarded or punished, and how the result is agreed on and released without a
central authority. Proteus solves the problem that DEIMOSBC's *Data
Summarization System* only sketches: turning noisy, partial, conflicting GPS
paths into a connected road network. It does this with distance thresholds, a
maximum angle-deviation constraint, road-building heuristics and prior-map
context, and it gives volunteers coverage guidance.

This repository already has the integration points in place. Proteus' own
algorithms are not reimplemented here.

## 1. Swap the merger: Proteus as the on-chain Data Summarization System

The map contract calls a `MapMerger` (`deimosbc/mapping/merger.py`). The
genesis parameter `merger` selects which one, so every processor provably
runs the same algorithm:

```python
from deimosbc.mapping.merger import ConflictReport, MergeResult, register_merger

class ProteusMerger:
    name = "proteus"

    def __init__(self, params):
        self.dist_threshold = params.merge_radius_m
        self.max_angle = params.max_angle_deviation_deg
        # ... Proteus' road-building parameters

    def find_conflicts(self, graph, points, hazards, trace_id) -> ConflictReport:
        # Proteus' validation step: which accepted traces does this one contradict?
        ...

    def integrate(self, graph, points, hazards, trace_id, collector, sensors) -> MergeResult:
        # Proteus' merge step, writing into the MapGraph:
        #   graph.add_vertex / split_edge / add_edge_support(u, v, trace_id, collector)
        ...

register_merger("proteus", ProteusMerger)
```

Then run with `--protocol merger='"proteus"'`.

A merger runs inside block execution on every processor, so it must:

* **be deterministic**: iterate in sorted order, no unseeded randomness, no
  wall-clock time, no dependence on hash-set order. `PYTHONHASHSEED` changes
  must not change the result. The existing merger is tested for this.
* **support withdrawal**: our DEIMOSBC conflict rule removes already-accepted
  traces. Recording per-edge trace support (`Edge.support`) and calling
  `MapGraph.remove_trace` handles this. If Proteus moves vertices (for
  example by averaging positions), it needs either per-trace contributions
  or a deterministic recomputation from the supporting traces.
* **have bounded cost**: each trace is processed once per processor. Global
  re-optimisation passes belong at release time, not per trace. One option
  is to run them in `vote_release`, so the released map is "Proteus-cleaned"
  while the live map stays incremental.

## 2. Prior maps

Proteus uses information from prior maps. In DEIMOSBC the natural place for
this is genesis. Its content is agreed before the disaster, which our DEIMOSBC work
treats as the only trustworthy point in time. A suggested extension:

* add `prior_map_root` (hash) to the genesis dictionary, and distribute the
  prior map itself (for example the pre-storm OSM extract) with the software;
* the merger receives it at construction and uses it for snapping, or as the
  "expected" geometry against which deviations are judged;
* a released edge can then be labelled *confirmed*, *new* or *contradicted*
  relative to the prior map. *Contradicted* is exactly the "blocked by the
  disaster" signal first responders need.

## 3. Coverage guidance → task assignment

Proteus gives volunteers spatial coverage guidance, and DEIMOSBC assigns
tasks. `CREATE_TASKS` already accepts an explicit list of cells
(`{"cells": [[i, j], ...]}`). A coverage policy can therefore release tasks
where Proteus reports low coverage or low confidence, instead of the uniform
grid. Because task availability is enforced on-chain, two volunteers are
never sent to the same gap.

## 4. Shared data and evaluation

| Concern | DEIMOSBC | Proteus |
|---|---|---|
| Edge list format | `map_edges.csv` written by every `simulate` run; `RoadNetwork.from_edge_csv` reads the same format | `*_map_gen.csv` (`x0,y0,x1,y1`, lon/lat) |
| Noise knobs | `jitter_m`, `offset_m`, `duplication`, `deletion` (`SimConfig`); segmentation in the TMS | Jitter, Offset, Duplication Constant, Split, Deletion |
| Accuracy metric | sampled edge coverage: recall / precision / contamination (`sim/metrics.py`) | edge accuracy (2/3/5-edge) |
| Factorial experiments | `deimosbc sweep --grid … --seeds N` → CSV | ANOVA over input parameters (R) |

To run DEIMOSBC on a ProteusData network:

```bash
git clone https://github.com/UH-DAIS/ProteusData data/ProteusData
deimosbc simulate --config examples/proteus_university.json --out runs/uh
```

The ProteusData CSVs are *map outputs*, not surveyed ground truth. The
downtown file in particular is fragmented: at 1 m endpoint snapping its
largest connected piece has only about a dozen intersections, so volunteers
cannot move through it. `university_map_gen.csv` is better connected. For
proper experiments, use a connected ground-truth network, for example the
OSM street graph of the same area exported to the same four-column CSV.
`roadnet_snap_m` merges endpoints closer than the given distance.

The `sweep` CSV has one row per run, so it loads directly into the existing R
workflow (`aov(recall ~ jitter_m * frac_fabricate, data = …)`).

## 5. A combined research agenda

1. **Proteus inside the contract.** Replace `overlap-chain` with Proteus and
   measure, against the same adversaries and noise, the change in recall,
   precision and contamination, and the cost per block.
2. **Conflict semantics.** Proteus' angle and distance constraints give a
   principled definition of when two paths *disagree*, which our 2021 DEIMOSBC
   work left open. Wire them into `find_conflicts`.
3. **Trust-weighted merging.** DEIMOSBC knows *who* supports each edge, and
   how reliable each collector has been (accepted / rejected / disputed
   history on-chain). Proteus can weight paths by contributor reliability,
   which a central system without accountable identities cannot do.
4. **Safety metric.** Report contamination (blocked streets shown as
   passable) next to accuracy. For first responders it is the costlier error.
