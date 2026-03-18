"""
knowledge_engine/src/models.py

Shared dataclasses used across all Knowledge Engine components.
These mirror the equivalent types in agent_core but are defined independently
so Knowledge Engine has no compile-time dependency on agent_core.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Session state (mirrors agent_core/src/models.py:SessionState)
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """
    Session context received from Agent Core at the start of every turn.
    Passed through to KEContext unchanged — KE reads it but never writes it.
    The structure mirrors agent_core.src.models.SessionState exactly so that
    Agent Core can pass its SessionState instances without conversion.
    """

    session_id: str
    history: list[dict]                 # [{"role": "user"|"assistant", "content": str}, ...]
    confirmed_entities: dict[str, Any]  # entities already confirmed across prior turns
    workflow_step: Optional[str]        # current workflow step, if any
    user_profile: dict[str, Any]        # persistent user profile data

    @staticmethod
    def empty(session_id: str) -> "SessionState":
        """Return a blank SessionState for brand-new sessions (used in tests)."""
        return SessionState(
            session_id=session_id,
            history=[],
            confirmed_entities={},
            workflow_step=None,
            user_profile={},
        )


# ---------------------------------------------------------------------------
# LLM response (mirrors agent_core/src/models.py:LLMResponse)
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """
    Normalised response from the LLM, returned by HttpLLMWrapper.
    Mirrors agent_core.src.models.LLMResponse so Agent Core and KE share
    compatible structures without a shared package.

    KE blocks only make direct completion calls (no tool_use), so tool_calls
    is always an empty list in KE usage.
    """

    content: Optional[str]
    stop_reason: str = "end_turn"   # "end_turn" | "max_tokens" | "error"
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
