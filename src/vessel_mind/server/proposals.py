"""The proposal endpoints: an approval executes a proposed call once; a rejection never does.

POST /proposals/{id}/approve and /reject. Both are idempotent by id: a repeated
approve returns the same outcome and executes nothing more. In demo mode (ADR 0002)
every approval is refused with 403 `demo_mode`, and the proposal stays pending.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, JsonValue

from vessel_mind.agent.loop import run_tool
from vessel_mind.agent.proposals import PROPOSAL_TTL, Proposal
from vessel_mind.core.capabilities import CapabilityRegistry, ProposedEffect
from vessel_mind.core.chat import ToolCall
from vessel_mind.server.state import (
    ProposalStatus,
    SessionStore,
    StoredProposal,
    UnknownProposal,
)


class TurnProposals:
    """The loop's Proposals for one turn, stored in the state file."""

    def __init__(self, store: SessionStore, turn_id: str) -> None:
        self._store = store
        self._turn_id = turn_id

    def propose(self, call: ToolCall, effect: ProposedEffect) -> Proposal:
        stored = self._store.create_proposal(self._turn_id, call, effect, PROPOSAL_TTL)
        return Proposal(id=stored.id, expires_at=stored.expires_at)


class ProposalView(BaseModel):
    """A proposal as approve and reject return it."""

    proposal_id: str
    status: ProposalStatus
    name: str
    input: dict[str, Any]
    # What the call returned; set once an approved proposal has executed.
    result: JsonValue | None
    is_error: bool | None


def _view(proposal: StoredProposal) -> ProposalView:
    return ProposalView(
        proposal_id=proposal.id,
        status=proposal.status,
        name=proposal.name,
        input=proposal.input,
        result=proposal.result,
        is_error=proposal.is_error,
    )


_REFUSALS: dict[str, str] = {
    "rejected": "This proposal was rejected, so it cannot be approved.",
    "expired": "This proposal expired before anyone approved it.",
    "withdrawn": "This proposal's turn was cancelled or failed, so it was withdrawn.",
    "approved": "This proposal was approved, so it cannot be rejected.",
}


def _conflict(status: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": status, "message": _REFUSALS[status]})


def proposal_routes(
    store: Callable[[], SessionStore],
    registry: CapabilityRegistry,
    *,
    demo_mode: bool,
) -> APIRouter:
    router = APIRouter()

    def find(proposal_id: str) -> StoredProposal:
        try:
            return store().proposal(proposal_id)
        except UnknownProposal:
            raise HTTPException(status_code=404, detail="unknown proposal") from None

    @router.post("/proposals/{proposal_id}/approve")
    async def approve(proposal_id: str) -> ProposalView:
        if demo_mode:
            find(proposal_id)
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "demo_mode",
                    "message": "This is the public demo: approvals are turned off, "
                    "so no write tool runs here.",
                },
            )
        try:
            proposal, claimed = store().claim_proposal(proposal_id)
        except UnknownProposal:
            raise HTTPException(status_code=404, detail="unknown proposal") from None
        if claimed:
            # Only the request that claimed it executes it: at most once.
            result, is_error = await run_tool(registry, proposal.name, proposal.input)
            proposal = store().record_result(proposal_id, result, is_error)
        elif proposal.status != "approved":
            raise _conflict(proposal.status)
        # A repeated approve returns the outcome; result is null while the call runs.
        return _view(proposal)

    @router.post("/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str) -> ProposalView:
        try:
            proposal = store().reject_proposal(proposal_id)
        except UnknownProposal:
            raise HTTPException(status_code=404, detail="unknown proposal") from None
        if proposal.status != "rejected":
            raise _conflict(proposal.status)
        return _view(proposal)

    return router
