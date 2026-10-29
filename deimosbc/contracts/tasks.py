"""Task contract (workflow steps *Task Assignment* and *Smart Contract Creation*).

Tasks partition the disaster area into square cells. A collector claims a task
through a processor; the contract enforces the availability rule from
Sec. IV-C: "they need to ensure a task is still available before assigning it
to themselves. Availability in this case means a physical location that has
not been mapped already." A cell becomes *mapped* when the new map length
accepted inside it reaches ``task_complete_length_m``.
"""

from __future__ import annotations

from ..crypto import verify_obj
from .state import ACTIVE, MAPPED, OPEN, ContractError, ExecContext, Task, WorldState


def claim_request(collector_pk: str, task_id: int, nonce: int) -> dict:
    return {"collector": collector_pk, "claim": task_id, "nonce": nonce}


def create_tasks(state: WorldState, tx, ctx: ExecContext) -> None:
    """Release the task grid. Idempotent per cell: existing cells are skipped."""
    p = state.params
    cells = tx.payload.get("cells")
    if cells is None:
        cells = [[i, j] for j in range(p.n_cells_y) for i in range(p.n_cells_x)]
    created = 0
    existing = {t.cell for t in state.tasks.values()}
    for i, j in cells:
        if not (0 <= i < p.n_cells_x and 0 <= j < p.n_cells_y):
            raise ContractError("cell_outside_area")
        if (i, j) in existing:
            continue
        tid = len(state.tasks)
        cx, cy = (i + 0.5) * p.task_cell_size_m, (j + 0.5) * p.task_cell_size_m
        lat, lon = state.frame.xy_to_e7(cx, cy)
        state.tasks[tid] = Task(
            task_id=tid,
            cell=(i, j),
            start_lat_e7=lat,
            start_lon_e7=lon,
            direction_deg=90 * ((i + j) % 4),     # alternate sweep directions
            created_at=ctx.timestamp,
        )
        existing.add((i, j))
        created += 1
    ctx.emit(type="tasks_released", count=created)


def claim_task(state: WorldState, tx, ctx: ExecContext) -> None:
    pl = tx.payload
    c = pl["collector"]
    acct = state.collectors.get(c)
    if acct is None:
        raise ContractError("unknown_collector")
    if not verify_obj(c, claim_request(c, pl["task_id"], pl["nonce"]), pl["collector_sig"]):
        raise ContractError("bad_collector_signature")
    if acct.status != ACTIVE:
        raise ContractError("collector_not_active")
    task = state.tasks.get(pl["task_id"])
    if task is None:
        raise ContractError("unknown_task")
    if task.status != OPEN:
        raise ContractError("task_already_mapped")
    if c not in task.claims and len(task.claims) >= state.params.max_claims_per_task:
        raise ContractError("task_unavailable")
    # one active task per collector
    if acct.active_task is not None and acct.active_task != task.task_id:
        old = state.tasks.get(acct.active_task)
        if old is not None:
            old.claims.pop(c, None)
    task.claims[c] = ctx.height
    acct.active_task = task.task_id
    acct.claimed_tasks.add(task.task_id)
    ctx.emit(type="task_assigned", collector=c, task_id=task.task_id)


def expire_claims(state: WorldState) -> None:
    """Claims time out so an absent volunteer cannot hold a cell forever."""
    timeout = state.params.claim_timeout_blocks
    for t in sorted(state.tasks.values(), key=lambda t: t.task_id):
        for c, h in sorted(t.claims.items()):
            if state.height - h > timeout:
                del t.claims[c]
                acct = state.collectors.get(c)
                if acct is not None and acct.active_task == t.task_id:
                    acct.active_task = None


def credit_mapped_length(state: WorldState, by_cell: dict, ctx: ExecContext) -> None:
    by_task = {t.cell: t for t in state.tasks.values()}
    for cell, length in sorted(by_cell.items()):
        t = by_task.get(tuple(cell))
        if t is None or t.status == MAPPED:
            continue
        t.mapped_length += length
        if t.mapped_length >= state.params.task_complete_length_m:
            t.status = MAPPED
            for c in sorted(t.claims):
                acct = state.collectors.get(c)
                if acct is not None and acct.active_task == t.task_id:
                    acct.active_task = None
            t.claims.clear()
            ctx.emit(type="task_mapped", task_id=t.task_id, cell=list(t.cell))
