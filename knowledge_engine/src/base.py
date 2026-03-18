"""
knowledge_engine/src/base.py

Base classes and the shared context object for all Knowledge Engine components:

- LLMWrapperBase   — ABC for the LLM client injected into KE (HttpLLMWrapper)
- KEContext        — shared data object passed through all 5 processing blocks
- KnowledgeBlock   — ABC every block must inherit and implement
- KnowledgeEngineBase — ABC the KnowledgeEngine orchestrator must inherit

These are the only types that cross module boundaries inside Knowledge Engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from src.models import LLMResponse, SessionState


# ---------------------------------------------------------------------------
# LLM wrapper interface
# ---------------------------------------------------------------------------


class LLMWrapperBase(ABC):
    """
    Abstract interface for the LLM client used by Knowledge Engine blocks.

    Concrete implementation: HttpLLMWrapper (knowledge_engine/src/llm_proxy_client.py).
    It forwards all calls to Agent Core's POST /internal/llm/call endpoint so that
    KE never holds an Anthropic API key.

    For testing, any class that implements call() and get_active_model() can be
    substituted without changing block code.
    """

    @abstractmethod
    def call(
        self,
        messages: list[dict],
        tools: list[dict] = None,
        system: str = "",
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """
        Execute a single LLM call.

        Args:
            messages:       Conversation messages in Anthropic format.
            tools:          Tool definitions. Pass empty list if no tools needed.
            system:         System prompt string.
            model_override: Override the active model — used to call Haiku for NLU tasks.

        Returns:
            LLMResponse — always. Never raises.
            On failure: LLMResponse(content=None, stop_reason="error").
        """

    @abstractmethod
    def get_active_model(self) -> str:
        """Return the name of the currently active model."""


# ---------------------------------------------------------------------------
# Shared context object
# ---------------------------------------------------------------------------


@dataclass
class KEContext:
    """
    Shared data object passed through all 5 Knowledge Engine blocks in sequence.
    Created at the start of assemble_prompt() with defaults, then enriched by each block.

    Immutable fields (set once, never changed):
        session_id      — identifies the session
        raw_input       — original user_message; blocks must never modify this
        session_state   — full session context from Memory Layer; read-only

    Mutable fields (enriched by blocks in order):
        normalised_input    — Block 1 (Language Normalisation): cleaned text used downstream
        detected_language   — Block 1: "hindi" | "kannada" | "english" | "hinglish"
        intent              — Block 2 (NLU Processor): classified intent label
        entities            — Block 2: extracted entities dict; Block 3 normalises values
        sentiment           — Block 2: sentiment class
        confidence          — Block 2: NLU confidence score 0.0–1.0
        retrieval_chunks    — Block 4 (Static KB): top-k relevant chunks
        always_include_chunks — Block 4: always-present chunks (bypass similarity filter)
    """

    session_id: str
    raw_input: str
    normalised_input: str
    detected_language: str
    intent: str
    entities: dict[str, Any]
    sentiment: str
    confidence: float
    retrieval_chunks: list[dict]
    always_include_chunks: list[dict]
    session_state: SessionState


# ---------------------------------------------------------------------------
# Block base class
# ---------------------------------------------------------------------------


class KnowledgeBlock(ABC):
    """
    Abstract base class for all 5 Knowledge Engine processing blocks.

    Every block receives the current KEContext, enriches it, and returns it.
    Blocks are stateless — all configuration is read from the config dict passed
    at process() time. The same block instance handles all sessions.

    Contract:
    - process() must never raise. On failure, log the error and return context unchanged.
    - process() must return KEContext — the same instance modified in-place is fine.
    - process() must not modify context.raw_input or context.session_state.
    """

    @abstractmethod
    def process(
        self,
        context: KEContext,
        llm: LLMWrapperBase,
        config: dict,
    ) -> KEContext:
        """
        Enrich the context with this block's output.

        Args:
            context: The shared KEContext object for this turn.
            llm:     The LLM client (HttpLLMWrapper) injected by the engine.
                     Blocks that don't need LLM (e.g. Glossary) ignore this parameter.
            config:  The full config dict from config/config.yaml.
                     Each block reads its own section: config["knowledge"]["blocks"][<name>].

        Returns:
            KEContext — enriched. Must never return None.
        """


# ---------------------------------------------------------------------------
# Knowledge Engine interface
# ---------------------------------------------------------------------------


class KnowledgeEngineBase(ABC):
    """
    Public contract for the Knowledge Engine.

    This mirrors agent_core/src/interfaces/knowledge_engine.py exactly.
    Agent Core depends on this interface; KnowledgeEngine must implement it.

    Language Normalisation and NLU Processor now run in Agent Core before this call.
    KE receives their results as parameters and runs only Glossary, Static KB, Multimodal.
    """

    @abstractmethod
    def assemble_prompt(
        self,
        session_id: str,
        user_message: str,
        session_state: SessionState,
        normalised_input: str = "",
        detected_language: str = "",
        intent: str = "unknown",
        entities: Optional[dict] = None,
        sentiment: str = "neutral",
        confidence: float = 0.0,
    ) -> tuple[list[dict], str]:
        """
        Build and return the messages list and system prompt for the LLM call.

        Agent Core runs Language Normalisation and NLU before calling this method
        and passes the results as parameters. KE uses these to drive Glossary
        normalisation and Static KB intent-based filtering, then assembles the
        messages list and system prompt from the enriched KEContext.

        Returns:
            tuple[list[dict], str]:
                - messages: conversation messages in Anthropic format (RAG context +
                  history + current user message). Empty list if user_message is empty.
                - system: system prompt string (persona + language instruction +
                  guardrails). Empty string if no persona is configured.
            Never raises.
        """
