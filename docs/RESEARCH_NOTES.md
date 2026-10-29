# Research notes: what implementing our 2021 design revealed

Building DEIMOSBC end to end forced every open question in our original design to get
an answer (see `DESIGN.md` §8). Running it surfaced the behaviours below. They
are starting points for an improved version of our work and for the
connection to Proteus (`PROTEUS_INTEGRATION.md`).

All numbers come from the default scenario unless stated otherwise: a 2 km
grid near Houston, two flood zones plus debris, 40 volunteers (4 fabricators,
2 garbage uploaders, 2 faulty GPS units), 5 processors, 1 simulated hour.
They were produced with:

```bash
deimosbc sweep --grid protocol.conflict_policy='"discard_both"','"evidence"' \
               --grid protocol.min_release_support=1,2 --seeds 3 --out policies.csv
deimosbc sweep --grid frac_fabricate=0.0,0.1 --grid jitter_m=4,12 --seeds 2 --out noise.csv
```

| Run | Recall | Precision | Contamination | Disputed traces |
|---|---|---|---|---|
| no fabricators, jitter 4 m | 0.34 | 0.91 | 0.01 | 5 |
| no fabricators, jitter 12 m | 0.32 | 0.80 | 0.02 | 38 |
| original design (10 % fabricators, jitter 4 m) | 0.35 | 0.85 | 0.19 | 55 |
| original design, jitter 12 m | 0.32 | 0.74 | 0.30 | 122 |
| evidence policy | 0.35 | 0.85 | 0.16 | 33 |
| corroboration (≥ 2 collectors per edge) | 0.15 | 0.99 | 0.01 | 55 |
| evidence + corroboration | 0.15 | 0.99 | 0.003 | 33 |

Contamination varies a lot between seeds: it is 0.04–0.40 under the original
design, depending on whether the fabricators' task cells overlap a flood
zone. Run more seeds before drawing fine-grained conclusions.

## 1. Single-source acceptance is the main safety gap

Our original design accepts any well-formed trace that does not conflict with an
existing one. A fabricated "this flooded street is passable" trace is
therefore accepted whenever no honest volunteer has reported that street as
blocked. With 10 % fabricators, contamination (blocked street length shown
as passable) rises from about 1 % to about 19 %. For a rescue team this is
the costly error.

Requiring two independent collectors per released edge removes
contamination almost entirely (to 0.3–1 %), but recall falls from 35 % to
15 % after one hour, because most streets have been walked only once at that
point. Options to explore:

* release both layers, with *corroborated* edges marked as verified and
  single-source edges as provisional;
* weight corroboration by collector reputation, which already exists on-chain
  as the accepted / rejected / disputed history;
* send volunteers to re-walk single-source edges, using the task system as
  coverage guidance (see Proteus).

## 2. "Discard both" erases the evidence it needs

When a hazard report conflicts with a traversal, our original design discards both.
Two consequences follow:

* the hazard report disappears, so the next fabricated trace on the same
  street is accepted again;
* the honest reporter is charged an error, the same as the fabricator. In
  the default runs some honest volunteers accumulate errors this way.

The `evidence` policy settles hazard conflicts by counting distinct
collectors on each side, and a tie keeps the hazard. This reduces disputes
by about 40 % and contamination modestly (0.19 → 0.16). Contamination stays
high because, in one hour, most blocked streets have no hazard report at
all. Hazard reports are only made by volunteers who happen to pass an
intersection next to a blocked street, which is a coverage problem.

## 3. Quality rules catch impossible data, not wrong data

The per-trace rules (spacing, speed, gaps, area, task) reliably catch garbage
uploaders: all are disabled within minutes. A phone with a *consistent*
35 m GPS offset produces physically plausible traces, so they are accepted
and pull precision down. Catching this needs cross-validation against
the consensus geometry, which is what Proteus' distance and angle
constraints provide.

## 4. The geometric conflict heuristic is grid-friendly

On the synthetic grid, the corridor-based geometric conflict rarely fires on
honest data. On the irregular ProteusData university network it produced
about 59 honest disputes in one hour and disabled 2 honest volunteers,
because nearby streets at odd angles fall inside each other's 15 m
corridors. A principled definition of "two paths disagree" is open, and
Proteus' constraints are a natural candidate.

## 5. Latency is a radio problem, not a blockchain problem

Blocks are produced every 5 s, but the median time from a trace being
finished to the volunteer receiving confirmation is about 3 minutes. Almost
all of that time is spent waiting to come within radio range of a processor.
Delay-tolerant relaying through other volunteers helps, and processor
placement matters more than consensus speed. Moving processors (vehicles)
would be an easy experiment.

## 6. Liveness needs a processor majority

Safety holds with any honest majority, and a Byzantine minority cannot stall
the chain (tested with block tampering and trace forgery). If a *majority*
of processors is offline, no block can be finalised and uploads queue up. The
our decentralisation goal argues for many cheap processors. That raises
the authority-set question: our design says processors "register", but adding
an authority after genesis needs its own governance protocol.

## 7. Other gaps worth a paragraph in our follow-up work

* **Privacy.** Traces reveal volunteers' movements on a ledger that every
  processor replicates. We listed confidentiality as future work.
  Options include coarsening, delayed publication, or committing only to
  edges.
* **Requester trust anchor.** Requesters can verify a map only if they know
  the genesis authority keys. These must be distributed before the disaster.
* **Time.** The map has no notion of *when* a street was passable. Edge
  support should decay, and a later hazard report should be able to
  supersede an earlier traversal without punishing anyone.
* **Freeze thresholds.** The error-rate trigger works (see
  `examples/freeze_under_attack.json`), but a coordinated group of garbage
  uploaders can trigger it deliberately and stop mapping for everyone. Rate
  limits per collector, or excluding already-suspended collectors from the
  window, would harden it.
* **Energy.** Ed25519 and the per-trace contract are cheap. The dominant cost
  on the phone is radio time, which a real deployment should measure.
