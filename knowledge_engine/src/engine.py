"""
knowledge_engine/src/engine.py

KnowledgeEngine — the orchestrator for the 3 retrieval/assembly blocks.

Implements KnowledgeEngineBase. Called by Agent Core after Language Normalisation
and NLU Processor have already run (Agent Core steps 3-4). KE receives the NLU
results as parameters and runs only the retrieval and assembly blocks.

Block execution order (fixed — cannot be changed by config):
    [1] Glossary             — normalises entity values using domain vocabulary
    [2] Static Knowledge Base — RAG retrieval using intent + entities
    [3] Multimodal Input Handler — processes attached images/PDFs (if present)

Design:
- This is the only public entry point for Knowledge Engine.
- All 3 blocks are instantiated at startup; disabled blocks are skipped at process time.
- assemble_prompt() never raises — all errors are absorbed by blocks.
- llm is optional: Glossary and Static KB do not use it. Multimodal may use it
  for image description when enabled. Pass None if multimodal is disabled.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.base import (
    KnowledgeEngineBase,
    KnowledgeBlock,
    KEContext,
    LLMWrapperBase,
)
from src.models import SessionState
from src.blocks.glossary import GlossaryBlock
from src.blocks.static_knowledge_base import StaticKnowledgeBaseBlock
from src.blocks.multimodal_input_handler import MultimodalInputHandlerBlock

logger = logging.getLogger(__name__)

# Fixed execution order — block name maps to class.
# Language Normalisation and NLU Processor have been moved to Agent Core.
_BLOCK_REGISTRY: list[tuple[str, type[KnowledgeBlock]]] = [
    ("glossary", GlossaryBlock),
    ("static_knowledge_base", StaticKnowledgeBaseBlock),
    ("multimodal_input_handler", MultimodalInputHandlerBlock),
]


class KnowledgeEngine(KnowledgeEngineBase):
    """
    Stateless Knowledge Engine orchestrator.

    Instantiated once at service startup. Blocks are also instantiated once and
    reused across all sessions — they hold no session-scoped state.

    Args:
        config: Full config dict from config/config.yaml.
        llm:    Optional LLMWrapperBase instance. Only needed when multimodal block
                is enabled. Pass None (or omit) if multimodal is disabled.
    """

    def __init__(self, config: dict, llm: Optional[LLMWrapperBase] = None) -> None:
        if config is None:
            raise ValueError("config must not be None")

        self._config = config
        self._llm = llm
        self._blocks: list[KnowledgeBlock] = self._init_blocks()
        self._warmup_blocks()

        logger.info(
            "knowledge_engine.init",
            extra={
                "operation": "knowledge_engine.init",
                "status": "success",
                "blocks_loaded": len(self._blocks),
            },
        )

    def assemble_prompt(
        self,
        session_id: str,
        user_message: str,
        session_state: SessionState,
        normalised_input: str = "",
        detected_language: str = "",
        intent: str = "unknown",
        entities: Optional[dict[str, Any]] = None,
        sentiment: str = "neutral",
        confidence: float = 0.0,
    ) -> tuple[list[dict], str]:
        """
        Run Glossary, Static KB, and Multimodal blocks, then build messages and system prompt.

        Agent Core has already run Language Normalisation and NLU — their results
        are passed in as parameters and used to pre-populate KEContext so Glossary
        and Static KB can use them immediately.

        Returns:
            tuple[list[dict], str]:
                - messages: RAG context + history + current user message.
                  Empty list if user_message is empty string.
                - system: persona + language instruction + guardrails.
                  Empty string if no persona configured.
            Never raises.
        """
        if not user_message:
            logger.info(
                "knowledge_engine.empty_input",
                extra={
                    "operation": "knowledge_engine.assemble_prompt",
                    "status": "skipped",
                    "session_id": session_id,
                },
            )
            return [], ""

        if session_id is None:
            raise ValueError("session_id must not be None")

        start = time.time()

        # Initialise shared context — pre-populated with NLU results from Agent Core
        context = KEContext(
            session_id=session_id,
            raw_input=user_message,
            normalised_input=normalised_input or user_message,
            detected_language=detected_language,
            intent=intent,
            entities=dict(entities) if entities else {},
            sentiment=sentiment,
            confidence=confidence,
            retrieval_chunks=[],
            always_include_chunks=[],
            session_state=session_state,
        )

        # Run retrieval and assembly blocks in fixed order
        for block in self._blocks:
            try:
                context = block.process(context, self._llm, self._config)
            except Exception as e:
                logger.error(
                    "knowledge_engine.block_failure",
                    extra={
                        "operation": "knowledge_engine.assemble_prompt",
                        "status": "failure",
                        "session_id": session_id,
                        "block": type(block).__name__,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )

        system = self._build_system_prompt(context.detected_language)
        messages = self._build_messages(context)

        logger.info(
            "knowledge_engine.assemble_prompt",
            extra={
                "operation": "knowledge_engine.assemble_prompt",
                "status": "success",
                "session_id": session_id,
                "intent": context.intent,
                "retrieval_count": len(context.retrieval_chunks),
                "message_count": len(messages),
                "latency_ms": int((time.time() - start) * 1000),
            },
        )

        return messages, system

    # ------------------------------------------------------------------
    # Private: block initialisation
    # ------------------------------------------------------------------

    def _init_blocks(self) -> list[KnowledgeBlock]:
        """
        Instantiate all 3 blocks.
        All blocks are always instantiated — disabled blocks are skipped
        inside their own process() method based on config.
        """
        return [cls() for _, cls in _BLOCK_REGISTRY]

    def _warmup_blocks(self) -> None:
        """Pre-warm blocks that have expensive cold-start initialisation (e.g. embedding model load)."""
        block_cfg = (
            self._config.get("knowledge", {})
            .get("blocks", {})
            .get("static_knowledge_base", {})
        )
        for block in self._blocks:
            if hasattr(block, "warmup"):
                block.warmup(block_cfg)

    # ------------------------------------------------------------------
    # Private: prompt assembly
    # ------------------------------------------------------------------

    def _build_system_prompt(self, detected_language: str = "") -> str:
        """
        Build the system prompt string: persona + language instruction + guardrails.

        This is passed to the LLM's `system` field — not embedded in the messages list.
        Returns an empty string if no persona is configured.

        Args:
            detected_language: Language detected by Language Normaliser (e.g. "english",
                                "hindi", "hinglish", "kannada"). When present, a specific
                                instruction is appended so the LLM responds in that language
                                rather than inferring it from context.
        """
        conversation_cfg = self._config.get("conversation", {})
        parts = []

        persona_text = conversation_cfg.get("persona", {}).get("text", "").strip()
        if persona_text:
            parts.append(persona_text)

        language_instruction = conversation_cfg.get("language_instruction", "").strip()
        if language_instruction:
            parts.append(language_instruction)

        if detected_language:
            parts.append(f"The user's current message is in {detected_language}. Respond in {detected_language}.")

        guardrails = conversation_cfg.get("guardrail_reminders", [])
        if guardrails:
            parts.append("\n--- Guidelines ---")
            for reminder in guardrails:
                parts.append(f"- {reminder}")

        return "\n\n".join(parts)

    def _build_messages(self, context: KEContext) -> list[dict]:
        """
        Build the Anthropic messages array from the enriched KEContext.

        Message structure:
        [1..N] Conversation history (last N turns from session_state.history)
        [last] Current user message — with RAG context appended if any chunks exist

        RAG chunks (always-include + retrieved) are appended to the current user
        message so they appear as close as possible to the question they inform.
        No fake assistant turn is needed because there is no standalone context
        message at the start of the array.
        """
        messages: list[dict] = []

        # Conversation history (last N turns)
        max_turns = (
            self._config.get("knowledge", {})
            .get("conversation", {})
            .get("max_history_turns", 10)
        )
        history = context.session_state.history
        if history:
            recent_history = history[-(max_turns * 2):]
            messages.extend(recent_history)

        # Build the current user message content — raw input + RAG context appended
        content_parts = [context.raw_input]

        if context.always_include_chunks:
            content_parts.append("\n--- Always include context ---")
            for chunk in context.always_include_chunks:
                content_parts.append(chunk.get("text", ""))

        if context.retrieval_chunks:
            content_parts.append("\n--- Relevant knowledge ---")
            for chunk in context.retrieval_chunks:
                content_parts.append(chunk.get("text", ""))

        messages.append({
            "role": "user",
            "content": "\n\n".join(content_parts),
        })

        return messages
