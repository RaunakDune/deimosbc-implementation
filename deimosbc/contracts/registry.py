"""Registry contract (workflow step *Register*, Sec. IV-C).

"All collectors and processors will need to register with the blockchain
system at initialization. Each of these registered collectors will be assigned
a pair of cryptographic keys, and their identities are noted. All the
registration information is recorded as a transaction in the blocks."

Processors (the authority set) and pre-verified volunteers are written into
genesis - these identities "existed since before the disaster" (Sec. III-B.3).
Additional volunteers can be registered in the field by a processor; the
collector proves key possession by signing the registration request.

Re-authentication (Sec. IV-A.1): "If the number of errors exceeds a pre-set
limit, the user must be re-authenticated."
"""

from __future__ import annotations

from ..crypto import verify_obj
from .state import ACTIVE, DISABLED, SUSPENDED, CollectorAccount, ContractError, ExecContext, WorldState


def registration_request(collector_pk: str, purpose: str, nonce: int) -> dict:
    return {"collector": collector_pk, "purpose": purpose, "nonce": nonce}


def register_collector(state: WorldState, tx, ctx: ExecContext) -> None:
    p = tx.payload
    req = registration_request(p["collector"], "register", p["nonce"])
    if not verify_obj(p["collector"], req, p["collector_sig"]):
        raise ContractError("bad_collector_signature")
    if p["collector"] in state.collectors or p["collector"] in state.authorities:
        raise ContractError("already_registered")
    state.collectors[p["collector"]] = CollectorAccount(
        pubkey=p["collector"],
        sponsor=tx.sender,
        registered_height=ctx.height,
        balance=state.params.initial_collector_balance,
    )
    ctx.emit(type="collector_registered", collector=p["collector"], sponsor=tx.sender)


def reauth_collector(state: WorldState, tx, ctx: ExecContext) -> None:
    p = tx.payload
    acct = state.collectors.get(p["collector"])
    if acct is None:
        raise ContractError("unknown_collector")
    req = registration_request(p["collector"], "reauth", p["nonce"])
    if not verify_obj(p["collector"], req, p["collector_sig"]):
        raise ContractError("bad_collector_signature")
    if acct.status == DISABLED:
        raise ContractError("collector_disabled")
    if acct.status != SUSPENDED:
        raise ContractError("not_suspended")
    acct.reauths += 1
    acct.errors = 0
    acct.status = ACTIVE
    acct.balance = max(acct.balance, state.params.submission_stake)
    ctx.emit(type="collector_reauthenticated", collector=acct.pubkey, by=tx.sender, reauths=acct.reauths)


def record_error(state: WorldState, acct: CollectorAccount, ctx: ExecContext, reason: str) -> None:
    acct.errors += 1
    ctx.emit(type="collector_error", collector=acct.pubkey, reason=reason, errors=acct.errors)
    if acct.status == ACTIVE and acct.errors >= state.params.max_errors_before_reauth:
        if acct.reauths >= state.params.max_reauths:
            acct.status = DISABLED
            ctx.emit(type="collector_disabled", collector=acct.pubkey)
        else:
            acct.status = SUSPENDED
            ctx.emit(type="collector_suspended", collector=acct.pubkey)
        _release_claim(state, acct)


def _release_claim(state: WorldState, acct: CollectorAccount) -> None:
    if acct.active_task is not None:
        t = state.tasks.get(acct.active_task)
        if t is not None:
            t.claims.pop(acct.pubkey, None)
        acct.active_task = None
