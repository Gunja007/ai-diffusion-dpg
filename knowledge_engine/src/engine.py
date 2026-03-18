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
    ) -> list[dict]:
        """
        Run Glossary, Static KB, and Multimodal blocks, then build the messages list.

        Agent Core has already run Language Normalisation and NLU — their results
        are passed in as parameters and used to pre-populate KEContext so Glossary
        and Static KB can use them immediately.

        Returns:
            list[dict]: Complete messages in Anthropic format.
            Empty list if user_message is empty string — never raises.
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
            return []

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

        # Build the final messages list from enriched context
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

        return messages

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

    # ------------------------------------------------------------------
    # Private: prompt assembly
    # ------------------------------------------------------------------

    def _build_messages(self, context: KEContext) -> list[dict]:
        """
        Build the complete Anthropic messages array from the enriched KEContext.

        Message structure:
        [1] System context message (role: "user"):
              a. Persona block (from YAML conversation.persona.text)
              b. Language instruction
              c. Always-include chunks (e.g. market truth framing)
              d. RAG-retrieved chunks (top-k by similarity)
              e. Guardrail reminders
        [2] Conversation history messages (last N turns from session_state.history)
        [3] Current user message (role: "user", content: context.raw_input)

        Note: Agent Core currently passes system="" to the LLM wrapper. The persona
        and context are embedded in the first user message instead. When Agent Core
        is updated to pass the system prompt directly, the persona block should be
        moved to the `system` field.
        """
        messages: list[dict] = []

        # Build system context block (injected as first user message)
        system_parts = []

        conversation_cfg = self._config.get("conversation", {})
        persona_cfg = conversation_cfg.get("persona", {})
        persona_text = persona_cfg.get("text", "").strip()
        if persona_text:
            system_parts.append(persona_text)

        # Language instruction
        system_parts.append(
            "Respond in the same language the user is using. "
            "If they mix Hindi and English (Hinglish), respond in Hinglish."
        )

        # Always-include chunks (e.g. ONEST market truth framing)
        if context.always_include_chunks:
            system_parts.append("\n--- Always include context ---")
            for chunk in context.always_include_chunks:
                system_parts.append(chunk.get("text", ""))

        # RAG-retrieved chunks
        if context.retrieval_chunks:
            system_parts.append("\n--- Relevant knowledge ---")
            for chunk in context.retrieval_chunks:
                system_parts.append(chunk.get("text", ""))

        # Guardrail reminders
        guardrails = conversation_cfg.get("guardrail_reminders", [])
        if guardrails:
            system_parts.append("\n--- Guidelines ---")
            for reminder in guardrails:
                system_parts.append(f"- {reminder}")

        if system_parts:
            messages.append({
                "role": "user",
                "content": "\n\n".join(system_parts),
            })
            # Add assistant acknowledgement turn to maintain valid alternating structure
            messages.append({
                "role": "assistant",
                "content": "Understood. I'm ready to help.",
            })

        # Conversation history (last N turns)
        max_turns = (
            self._config.get("knowledge", {})
            .get("conversation", {})
            .get("max_history_turns", 10)
        )
        history = context.session_state.history
        if history:
            # Take last max_turns * 2 messages (each turn = 1 user + 1 assistant)
            recent_history = history[-(max_turns * 2):]
            messages.extend(recent_history)

        # Current user message
        messages.append({
            "role": "user",
            "content": context.raw_input,
        })

        return messages
