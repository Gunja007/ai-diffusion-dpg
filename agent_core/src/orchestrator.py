"""
agent_core/orchestrator.py

Concrete implementation of AgentCoreBase.
Wires all components and executes the 12-step turn sequence.

Design rules enforced here:
- Trust Layer is called exactly twice per turn (input + output). Neither is skippable.
- Agent Core holds zero session state between turns.
- Language Normalisation and NLU Processor run directly in Agent Core (steps 3-4)
  using the primary LLM wrapper with a model_override to Haiku.
- Early exit at step 5 if intent is "unknown" or confidence is below threshold.
- Steps 11-12 (memory write, learning emit) run in a daemon thread after TurnResult is returned.
- This is the only file that imports and coordinates all DPG interfaces together.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.base import AgentCoreBase
from src.interfaces.action_gateway import ActionGatewayBase
from src.interfaces.knowledge_engine import KnowledgeEngineBase
from src.interfaces.learning_layer import LearningLayerBase
from src.interfaces.memory_layer import MemoryLayerBase
from src.interfaces.reach_layer import ReachLayerBase
from src.interfaces.trust_layer import TrustLayerBase
from src.language_normalisation import LanguageNormaliser
from src.llm_wrapper.base import LLMWrapperBase
from src.manager_agent import ManagerAgent
from src.models import (
    LLMResponse,
    NLUResult,
    SessionState,
    ToolCall,
    TrustCheckResult,
    TurnEvent,
    TurnInput,
    TurnResult,
)
from src.nlu_processor import NLUProcessor
from src.tool_registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentCore(AgentCoreBase):
    """
    Stateless orchestrator. Holds references to injected components only —
    no session-scoped data stored as instance state.

    All components are injected at construction time. The startup entrypoint
    (main.py or equivalent) is the only place that instantiates and wires them.

    Args:
        config:           Domain configuration dict. Used for fallback message text.
        llm_wrapper:      LLM inferencing interface.
        memory:           Memory Layer interface.
        trust:            Trust Layer interface.
        knowledge_engine: Knowledge Engine interface.
        tool_registry:    Pre-built tool registry (initialised at startup).
        manager_agent:    Tool-use loop handler.
        learning:         Learning Layer interface (async emit).
    """

    def __init__(
        self,
        config: dict,
        llm_wrapper: LLMWrapperBase,
        memory: MemoryLayerBase,
        trust: TrustLayerBase,
        knowledge_engine: KnowledgeEngineBase,
        tool_registry: ToolRegistry,
        manager_agent: ManagerAgent,
        learning: LearningLayerBase,
    ) -> None:
        if config is None:
            raise ValueError("config must not be None")

        self._config = config
        self._llm = llm_wrapper
        self._memory = memory
        self._trust = trust
        self._knowledge_engine = knowledge_engine
        self._tool_registry = tool_registry
        self._manager_agent = manager_agent
        self._learning = learning

        # Language Normalisation and NLU run directly in Agent Core.
        # Stateless — instantiated once, reused across all sessions.
        self._language_normaliser = LanguageNormaliser()
        self._nlu_processor = NLUProcessor()

    # ------------------------------------------------------------------
    # Public interface — single entry point
    # ------------------------------------------------------------------

    def process_turn(self, turn_input: TurnInput) -> TurnResult:
        """
        Execute one full conversation turn. See AgentCoreBase for full contract.
        """
        if turn_input is None:
            raise ValueError("turn_input must not be None")
        if not turn_input.session_id:
            raise ValueError("turn_input.session_id must not be empty")
        if turn_input.user_message is None:
            raise ValueError("turn_input.user_message must not be None")

        start = time.time()
        session_id = turn_input.session_id

        logger.info(
            "orchestrator.turn_start",
            extra={
                "operation": "orchestrator.process_turn",
                "status": "success",
                "session_id": session_id,
                "channel": turn_input.channel,
            },
        )

        # ── Step 1: Read session state ────────────────────────────────
        state = self._memory.read_session(session_id)

        # ── Step 2: Trust check on input ─────────────────────────────
        trust_input = self._trust.check_input(session_id, turn_input.user_message)

        if trust_input.action == "block":
            return self._blocked_response(session_id, trust_input, start, trust_input)

        if trust_input.action == "escalate":
            return self._escalated_response(session_id, trust_input, start, trust_input)

        # ── Step 3: Language Normalisation ───────────────────────────
        # Uses Haiku (model_override in config) — reads from preprocessing.language_normalisation
        normalised_input, detected_language = self._language_normaliser.normalise(
            raw_input=turn_input.user_message,
            config=self._config,
            llm=self._llm,
        )

        # ── Step 4: NLU Processor ─────────────────────────────────────
        # Injects recent history for context-aware follow-up classification.
        # Uses Haiku (model_override in config) — reads from preprocessing.nlu_processor
        nlu_result = self._nlu_processor.process(
            normalised_input=normalised_input,
            history=state.history,
            config=self._config,
            llm=self._llm,
        )

        # ── Step 5: Early exit on unknown / low-confidence intent ─────
        confidence_threshold = (
            self._config.get("preprocessing", {})
            .get("nlu_processor", {})
            .get("confidence_threshold", 0.5)
        )
        if nlu_result.intent == "unknown" or nlu_result.confidence < confidence_threshold:
            return self._unknown_intent_response(session_id, start)

        # ── Step 6: Assemble prompt (KE) ─────────────────────────────
        # KE receives pre-computed NLU data — runs only Glossary, Static KB, Multimodal.
        messages = self._knowledge_engine.assemble_prompt(
            session_id=session_id,
            user_message=turn_input.user_message,
            session_state=state,
            normalised_input=normalised_input,
            detected_language=detected_language,
            intent=nlu_result.intent,
            entities=nlu_result.entities,
            sentiment=nlu_result.sentiment,
            confidence=nlu_result.confidence,
        )

        # Empty prompt edge case — return safe empty response
        if not messages:
            logger.warning(
                "orchestrator.empty_prompt",
                extra={
                    "operation": "orchestrator.process_turn",
                    "status": "skipped",
                    "session_id": session_id,
                },
            )
            empty_trust = TrustCheckResult(passed=True, action="allow")
            return self._build_result(
                session_id=session_id,
                response_text="",
                was_escalated=False,
                was_tool_used=False,
                model_used="",
                latency_ms=int((time.time() - start) * 1000),
                state=state,
                turn_input=turn_input,
                tool_calls=[],
                trust_input=trust_input,
                trust_output=empty_trust,
            )

        # ── Step 7: LLM call #1 ──────────────────────────────────────
        tools = self._tool_registry.get_tool_definitions()
        llm_response = self._llm.call(messages=messages, tools=tools, system="")

        # ── Step 8: Tool-use loop ─────────────────────────────────────
        final_text, tool_calls = self._manager_agent.run_turn(
            messages=messages,
            session_id=session_id,
            initial_llm_response=llm_response,
        )

        # ── Step 9: Trust check on output ────────────────────────────
        trust_output = self._trust.check_output(session_id, final_text)

        if trust_output.action in ("block", "escalate"):
            final_text = self._safe_fallback_message()

        # ── Step 10: Build result and return ─────────────────────────
        latency_ms = int((time.time() - start) * 1000)
        result = self._build_result(
            session_id=session_id,
            response_text=final_text,
            was_escalated=trust_output.action == "escalate",
            was_tool_used=bool(tool_calls),
            model_used=llm_response.model_used,
            latency_ms=latency_ms,
            state=state,
            turn_input=turn_input,
            tool_calls=tool_calls,
            trust_input=trust_input,
            trust_output=trust_output,
        )

        logger.info(
            "orchestrator.turn_complete",
            extra={
                "operation": "orchestrator.process_turn",
                "status": "success",
                "session_id": session_id,
                "latency_ms": latency_ms,
                "model": llm_response.model_used,
                "tool_used": bool(tool_calls),
                "intent": nlu_result.intent,
            },
        )

        return result

    # ------------------------------------------------------------------
    # Private: blocked / escalated / unknown early exits
    # ------------------------------------------------------------------

    def _blocked_response(
        self,
        session_id: str,
        trust_result: TrustCheckResult,
        start: float,
        trust_input: TrustCheckResult,
    ) -> TurnResult:
        logger.warning(
            "orchestrator.input_blocked",
            extra={
                "operation": "orchestrator.process_turn",
                "status": "skipped",
                "session_id": session_id,
                "reason": trust_result.reason,
            },
        )
        blocked_text = self._config.get(
            "blocked_message",
            "I'm unable to help with that request.",
        )
        return TurnResult(
            session_id=session_id,
            response_text=blocked_text,
            was_escalated=False,
            model_used="",
            latency_ms=int((time.time() - start) * 1000),
        )

    def _escalated_response(
        self,
        session_id: str,
        trust_result: TrustCheckResult,
        start: float,
        trust_input: TrustCheckResult,
    ) -> TurnResult:
        logger.warning(
            "orchestrator.input_escalated",
            extra={
                "operation": "orchestrator.process_turn",
                "status": "skipped",
                "session_id": session_id,
                "reason": trust_result.reason,
            },
        )
        escalation_text = self._config.get(
            "escalation_message",
            "I'm connecting you to a human agent who can better assist you.",
        )
        return TurnResult(
            session_id=session_id,
            response_text=escalation_text,
            was_escalated=True,
            model_used="",
            latency_ms=int((time.time() - start) * 1000),
        )

    def _unknown_intent_response(self, session_id: str, start: float) -> TurnResult:
        """
        Early exit when intent is unknown or NLU confidence is below threshold.
        No KE call, no LLM call, no memory write.
        """
        logger.info(
            "orchestrator.unknown_intent",
            extra={
                "operation": "orchestrator.process_turn",
                "status": "skipped",
                "session_id": session_id,
                "latency_ms": int((time.time() - start) * 1000),
            },
        )
        unknown_text = self._config.get(
            "conversation", {}
        ).get(
            "unknown_intent_message",
            "I didn't quite understand that. Could you tell me more about what you need help with?",
        )
        return TurnResult(
            session_id=session_id,
            response_text=unknown_text,
            was_escalated=False,
            model_used="",
            latency_ms=int((time.time() - start) * 1000),
        )

    def _safe_fallback_message(self) -> str:
        return self._config.get(
            "output_blocked_message",
            "I wasn't able to produce a safe response. Please try rephrasing your question.",
        )

    # ------------------------------------------------------------------
    # Private: result construction + async post-turn
    # ------------------------------------------------------------------

    def _build_result(
        self,
        session_id: str,
        response_text: str,
        was_escalated: bool,
        was_tool_used: bool,
        model_used: str,
        latency_ms: int,
        state: SessionState,
        turn_input: TurnInput,
        tool_calls: list[ToolCall],
        trust_input: TrustCheckResult,
        trust_output: TrustCheckResult,
    ) -> TurnResult:
        """
        Constructs the TurnResult and schedules the async post-turn work
        (memory write + learning emit) in a daemon thread.
        """
        result = TurnResult(
            session_id=session_id,
            response_text=response_text,
            was_escalated=was_escalated,
            was_tool_used=was_tool_used,
            model_used=model_used,
            latency_ms=latency_ms,
        )

        updated_state = self._build_updated_state(
            state, turn_input.user_message, response_text, tool_calls
        )

        turn_event = TurnEvent(
            session_id=session_id,
            response_text=response_text,
            tool_calls=tool_calls,
            trust_input_result=trust_input,
            trust_output_result=trust_output,
            model_used=model_used,
            input_tokens=0,   # populated from llm_response upstream when available
            output_tokens=0,
            latency_ms=latency_ms,
            timestamp_ms=turn_input.timestamp_ms,
        )

        thread = threading.Thread(
            target=self._post_turn,
            args=(session_id, updated_state, turn_event),
            daemon=True,
        )
        thread.start()

        return result

    def _build_updated_state(
        self,
        state: SessionState,
        user_message: str,
        response_text: str,
        tool_calls: list[ToolCall],
    ) -> SessionState:
        """Append the current turn to conversation history in the session state."""
        updated_history = list(state.history)

        if user_message:
            updated_history.append({"role": "user", "content": user_message})
        if response_text:
            updated_history.append({"role": "assistant", "content": response_text})

        return SessionState(
            session_id=state.session_id,
            history=updated_history,
            confirmed_entities=state.confirmed_entities,
            workflow_step=state.workflow_step,
            user_profile=state.user_profile,
        )

    def _post_turn(
        self,
        session_id: str,
        updated_state: SessionState,
        turn_event: TurnEvent,
    ) -> None:
        """
        Runs in a daemon thread after TurnResult is returned to the caller.
        Any exception here is logged and swallowed — must never crash the thread.
        """
        try:
            self._memory.write_session(session_id, updated_state)
        except Exception as e:
            logger.error(
                "orchestrator.memory_write_failed",
                extra={
                    "operation": "orchestrator._post_turn",
                    "status": "failure",
                    "session_id": session_id,
                    "error": str(e),
                },
            )

        try:
            self._learning.emit_turn(turn_event)
        except Exception as e:
            logger.error(
                "orchestrator.learning_emit_failed",
                extra={
                    "operation": "orchestrator._post_turn",
                    "status": "failure",
                    "session_id": session_id,
                    "error": str(e),
                },
            )
