"""Proposals: how the loop asks for a human approval of a write or irreversible call.

The loop never runs such a call. It hands the call to a Proposals store, tells the
client with a `proposal` event, and tells the model the call awaits approval. Only
an approval, outside the loop, executes the call, and at most once (ADR 0001 §5).
"""

import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from vessel_mind.core.capabilities import ProposedEffect
from vessel_mind.core.chat import ToolCall

# A proposal that nobody approves or rejects in this time expires.
PROPOSAL_TTL = datetime.timedelta(minutes=10)


class Proposal(BaseModel):
    """A stored proposal, as the loop sees it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    expires_at: datetime.datetime


class Proposals(Protocol):
    """Stores proposals. The server implements it over the state file, per turn."""

    def propose(self, call: ToolCall, effect: ProposedEffect) -> Proposal: ...
