"""Governance contract: map release and *Retirement* (Sec. IV-A, IV-C).

Release - "Once all the processors are satisfied that no more updates are
required, we make the map available for general public use. This process will
repeat as many times as needed." A new map version is released when a strict
majority of processors vote for it. The release records the canonical
``map_root`` so requesters can verify what they are served (it is committed in
the next block header as ``release_root``).

Retirement - "Once the number of errors exceeds a pre-arranged threshold, the
blockchain is frozen and the final block is marked to ensure all subsequent
blocks get automatically discarded ... Other Processors may run their own
integrity tests to ensure the decision is correct. A majority vote is then
undertaken to finalize the decision to freeze or not."
"""

from __future__ import annotations

from ..encoding import hash_obj
from .state import ContractError, ExecContext, FreezeProposal, MapRelease, WorldState


def _majority(state: WorldState, n: int) -> bool:
    return 2 * n > len(state.authorities)


def propose_freeze(state: WorldState, tx, ctx: ExecContext) -> None:
    if state.freeze is not None and not state.freeze.closed:
        raise ContractError("proposal_already_open")
    state.n_freeze_proposals += 1
    state.freeze = FreezeProposal(
        proposal_id=state.n_freeze_proposals,
        proposer=tx.sender,
        reason=str(tx.payload.get("reason", ""))[:200],
        height=ctx.height,
        votes={tx.sender: True},
    )
    ctx.emit(type="freeze_proposed", proposal_id=state.freeze.proposal_id, by=tx.sender, reason=state.freeze.reason)
    _tally(state, ctx)


def vote_freeze(state: WorldState, tx, ctx: ExecContext) -> None:
    fp = state.freeze
    if fp is None or fp.closed or fp.proposal_id != tx.payload.get("proposal_id"):
        raise ContractError("no_such_open_proposal")
    if tx.sender in fp.votes:
        raise ContractError("already_voted")
    fp.votes[tx.sender] = bool(tx.payload.get("approve"))
    ctx.emit(type="freeze_vote", proposal_id=fp.proposal_id, by=tx.sender, approve=fp.votes[tx.sender])
    _tally(state, ctx)


def _tally(state: WorldState, ctx: ExecContext) -> None:
    fp = state.freeze
    yes = sum(1 for v in fp.votes.values() if v)
    no = sum(1 for v in fp.votes.values() if not v)
    if _majority(state, yes):
        fp.closed = True
        state.frozen = True
        state.final_height = ctx.height
        ctx.emit(type="chain_frozen", proposal_id=fp.proposal_id, final_height=ctx.height, yes=yes, no=no)
    elif 2 * no >= len(state.authorities):
        fp.closed = True
        ctx.emit(type="freeze_rejected", proposal_id=fp.proposal_id, yes=yes, no=no)


def _snapshot_length(snap) -> float:
    import math

    pos = {v["id"]: (v["x"], v["y"]) for v in snap["vertices"]}
    return sum(math.dist(pos[e["u"]], pos[e["v"]]) for e in snap["edges"])


def vote_release(state: WorldState, tx, ctx: ExecContext) -> None:
    version = tx.payload.get("version")
    next_version = len(state.releases) + 1
    if version != next_version:
        raise ContractError("stale_release_version")
    state.release_votes[tx.sender] = version
    voters = sorted(pk for pk, v in state.release_votes.items() if v == next_version)
    if _majority(state, len(voters)):
        snap = state.map.snapshot(min_support=state.params.min_release_support)
        rel = MapRelease(
            version=next_version,
            height=ctx.height,
            timestamp=ctx.timestamp,
            map_root=hash_obj(snap),
            n_vertices=len(snap["vertices"]),
            n_edges=len(snap["edges"]),
            length_m=round(_snapshot_length(snap), 2),
            voters=voters,
        )
        state.releases.append(rel)
        state.release_snapshots[rel.version] = snap
        state.release_votes = {}
        ctx.emit(type="map_released", version=rel.version, map_root=rel.map_root, edges=rel.n_edges, length_m=rel.length_m)
