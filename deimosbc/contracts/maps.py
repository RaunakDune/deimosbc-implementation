"""Map contract (workflow step *Upload Sensory Data*, Sec. IV-C, and the
on-chain half of *Trace Processing*, Sec. IV-A.2.3).

Order of checks for an uploaded trace:

1. *Package Verification* - structure and ordering of the packet.
2. *Author Identification* - the collector is registered, active and the
   packet carries its valid signature.
3. Stake - the collector locks ``submission_stake``.
4. Quality rules (:mod:`deimosbc.mapping.validation`). Unqualified data
   forfeits the stake ("the worker loses their deposit").
5. Data Summarization conflict check. On conflict "both traces are
   discarded": the incoming trace is not added, already-accepted
   conflicting traces are withdrawn from the map, and every involved
   collector is notified (and charged an error). With the optional
   ``conflict_policy = "evidence"`` extension, hazard conflicts are instead
   settled by counting distinct collectors on each side.
6. Otherwise the trace is chained into the map and the collector is rewarded.

Failures in steps 1-3 raise :class:`ContractError` and leave state untouched
(they are attributable to the submitting processor, not provably to the
collector). Steps 4-6 always succeed as transactions and record their outcome.
"""

from __future__ import annotations

from ..geo import from_e7
from ..mapping.merger import HazardReport
from ..mapping.validation import check_hazards, check_trace_quality, task_rect_for_cell
from ..packets import packet_id, verify_author, verify_package
from . import registry, tasks
from .state import ACCEPTED, ACTIVE, DISPUTED, REJECTED, ContractError, ExecContext, TraceRecord, WorldState


def submit_trace(state: WorldState, tx, ctx: ExecContext) -> None:
    p = state.params
    signed = tx.payload.get("packet")
    err = verify_package(signed, max_points=10 * p.max_trace_points, max_sensor_fields=p.max_sensor_fields)
    if err:
        raise ContractError(f"package:{err}")
    pkt = signed["packet"]
    acct = state.collectors.get(pkt["collector"])
    if acct is None:
        raise ContractError("unknown_collector")
    if not verify_author(signed):
        raise ContractError("bad_collector_signature")
    if acct.status != ACTIVE:
        raise ContractError("collector_not_active")
    tid = packet_id(signed)
    if tid in state.traces:
        raise ContractError("duplicate_packet")
    if acct.balance < p.submission_stake:
        raise ContractError("insufficient_stake")

    acct.last_seq = max(acct.last_seq, pkt["seq"])
    frame = state.frame
    pts = [frame.e7_to_xy(a, b) for a, b, _ in pkt["points"]]
    times = [t for _, _, t in pkt["points"]]
    hz_xy = [frame.e7_to_xy(a, b) for a, b, _, _ in pkt["hazards"]]
    hazards = [HazardReport(x, y, float(h % 360)) for (x, y), (_, _, _, h) in zip(hz_xy, pkt["hazards"])]
    sensors = [{k: v / 1000.0 for k, v in s.items()} for s in pkt["sensors"]]

    # ---- 4. quality rules -------------------------------------------------
    reason = None
    task = state.tasks.get(pkt["task_id"])
    if task is None or pkt["task_id"] not in acct.claimed_tasks:
        reason = "task_not_claimed"
    else:
        reason = check_trace_quality(p, pts, times, task_rect_for_cell(p, task.cell)) or check_hazards(p, hz_xy)
    if reason:
        acct.balance -= p.submission_stake
        acct.rejected += 1
        state.traces[tid] = TraceRecord(tid, acct.pubkey, pkt["task_id"], ctx.height, REJECTED, reason)
        state.recent_outcomes.append(True)
        ctx.emit(type="trace_rejected", collector=acct.pubkey, trace=tid, seq=pkt["seq"], reason=reason, slashed=p.submission_stake)
        registry.record_error(state, acct, ctx, reason)
        return

    # ---- 5. conflicts -----------------------------------------------------
    rep = state.merger.find_conflicts(state.map, pts, hazards, tid)
    if rep:
        losers, new_loses = _resolve(state, rep, tid, acct.pubkey)
        removed = 0.0
        for other in sorted(losers):
            removed += _discard(state, other, tid, ctx)
        state.recent_outcomes.append(True)
        state.last_map_change_height = ctx.height
        if new_loses:
            state.traces[tid] = TraceRecord(tid, acct.pubkey, pkt["task_id"], ctx.height, DISPUTED, "conflict")
            acct.disputed += 1
            ctx.emit(type="trace_disputed", collector=acct.pubkey, trace=tid, seq=pkt["seq"], against=rep.all(), removed_m=round(removed, 2))
            registry.record_error(state, acct, ctx, "conflict")
            return
        ctx.emit(type="conflict_won", collector=acct.pubkey, trace=tid, against=sorted(losers))

    # ---- 6. accept --------------------------------------------------------
    res = state.merger.integrate(state.map, pts, hazards, tid, acct.pubkey, sensors)
    reward = p.reward_per_trace + int(res.new_length * p.reward_per_new_metre)
    acct.balance += reward
    acct.accepted += 1
    state.traces[tid] = TraceRecord(tid, acct.pubkey, pkt["task_id"], ctx.height, ACCEPTED, "", res.new_length)
    state.recent_outcomes.append(False)
    if res.new_length > 0 or hazards:
        state.last_map_change_height = ctx.height
    tasks.credit_mapped_length(state, res.new_length_by_cell, ctx)
    ctx.emit(
        type="trace_accepted",
        collector=acct.pubkey,
        trace=tid,
        seq=pkt["seq"],
        new_m=round(res.new_length, 2),
        matched_m=round(res.matched_length, 2),
        reward=reward,
    )


def _resolve(state: WorldState, rep, tid: str, collector: str):
    """Decide which side of a conflict is discarded.

    Returns (existing trace ids to discard, whether the new trace is discarded).
    """
    existing = set(rep.all())
    if state.params.conflict_policy != "evidence" or rep.geometric or (rep.hazard and rep.traversal):
        return existing, True                       # paper: "both traces are discarded"

    def who(t: str) -> str:
        return collector if t == tid else state.traces[t].collector

    hazard_side = set(rep.hazard) | ({tid} if rep.traversal else set())
    traversal_side = set(rep.traversal) | ({tid} if rep.hazard else set())
    n_haz = len({who(t) for t in hazard_side})
    n_trav = len({who(t) for t in traversal_side})
    losers = hazard_side if n_trav > n_haz else traversal_side   # tie: the hazard stands
    return losers - {tid}, tid in losers


def _discard(state: WorldState, other: str, against: str, ctx: ExecContext) -> float:
    rec = state.traces.get(other)
    if rec is None or rec.status != ACCEPTED:
        return 0.0
    removed = state.map.remove_trace(other)
    rec.status, rec.reason = DISPUTED, f"conflict_with:{against[:16]}"
    acct = state.collectors[rec.collector]
    acct.disputed += 1
    ctx.emit(type="trace_disputed", collector=rec.collector, trace=other, against=against)
    registry.record_error(state, acct, ctx, "conflict")
    return removed


def describe_point(lat_e7: int, lon_e7: int) -> str:  # small helper for logs
    return f"({from_e7(lat_e7):.6f}, {from_e7(lon_e7):.6f})"
