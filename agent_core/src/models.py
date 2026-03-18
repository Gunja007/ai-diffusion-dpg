"""
agent_core/models.py

Shared dataclasses used as the data contract between all components of Agent Core
and the interfaces it calls. Every other file in agent_core/ imports from here.
No business logic. No imports from within agent_core/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


@dataclass
class TurnInput:
    """Normalised user message received from the Reach Layer."""

    session_id: str
    user_message: str
    channel: str          # "cli" | "whatsapp" | "web" | "voip"
    timestamp_ms: int


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """
    Full session context loaded from Memory Layer at the start of every turn.
    Passed read-only to Knowledge Engine for prompt assembly.
    Updated copy written back to Memory Layer after response is delivered.
    """

    session_id: str
    history: list[dict]               # [{"role": "user"|"assistant", "content": str}, ...]
    confirmed_entities: dict[str, Any] # Things we already know about the user 
    workflow_step: Optional[str]
    user_profile: dict[str, Any]

    @staticmethod
    def empty(session_id: str) -> SessionState:
        """Return a blank SessionState for a brand-new session."""
        return SessionState(
            session_id=session_id,
            history=[],
            confirmed_entities={},
            workflow_step=None,
            user_profile={},
        )


# ---------------------------------------------------------------------------
# NLU (Language Normalisation + Intent Classification)
# ---------------------------------------------------------------------------


@dataclass
class NLUResult:
    """
    Combined output of Language Normalisation and NLU Processor steps run in Agent Core.
    Produced before the Knowledge Engine call and passed to KE's assemble_prompt().
    """

    intent: str                    # classified intent label from config intents list
    entities: dict[str, Any]       # extracted entity key→value pairs
    sentiment: str                 # one of the configured sentiment classes
    confidence: float              # 0.0–1.0; below threshold triggers early exit


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


@dataclass
class TrustCheckResult:
    """Result returned by TrustLayer.check_input() and check_output()."""

    passed: bool
    action: str                       # "allow" | "block" | "escalate"
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Tool use
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """A single tool call expressed by the LLM in its response."""

    tool_name: str
    tool_use_id: str
    input_params: dict[str, Any]


@dataclass
class ToolResult:
    """Normalised result returned by Action Gateway after executing a tool call."""

    tool_use_id: str
    tool_name: str
    result: dict[str, Any]
    success: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """
    Normalised response from the LLM wrapper.
    Never raises — failures are expressed via stop_reason="error".
    """

    content: Optional[str]
    tool_calls: list[ToolCall]        = field(default_factory=list)
    stop_reason: str                  = "end_turn"   # "end_turn" | "tool_use" | "max_tokens" | "error"
    model_used: str                   = ""
    input_tokens: int                 = 0
    output_tokens: int                = 0


# ---------------------------------------------------------------------------
# Turn output
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    """Final result returned to the Reach Layer after a completed turn."""

    session_id: str
    response_text: str
    was_escalated: bool               = False
    was_tool_used: bool               = False
    model_used: str                   = ""
    latency_ms: int                   = 0


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


@dataclass
class TurnEvent:
    """
    Audit payload emitted to the Learning Layer after every turn.
    Emitted asynchronously — never in the response path.

    NOTE: user_message is intentionally excluded.
    PII is routed only through the Learning Layer's designated audit log path.
    """

    session_id: str
    response_text: str
    tool_calls: list[ToolCall]
    trust_input_result: TrustCheckResult
    trust_output_result: TrustCheckResult
    model_used: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    timestamp_ms: int
