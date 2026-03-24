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
from src.preprocessing.language_normalisation import LanguageNormaliser
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
from src.preprocessing.nlu_processor import NLUProcessor
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
        logger.info(
            "\n═══════════════════════════════════════════════════════════════\n"
            "  TURN START  session=%s  channel=%s\n"
            "  input: %r\n"
            "═══════════════════════════════════════════════════════════════",
            session_id, turn_input.channel, turn_input.user_message[:120],
        )

        # ── Step 1: Read session state ────────────────────────────────
        memory_endpoint = (
            self._config.get("memory_client", {}).get("endpoint", "http://memory_layer:8002")
        )
        logger.info(
            "  [STEP 1] Memory Read  →  POST %s/session/read  (session=%s)",
            memory_endpoint, session_id,
        )
        t1 = time.time()
        state = self._memory.read_session(session_id)
        logger.info(
            "  [STEP 1] Memory Read  ✓  history_turns=%d  confirmed_entities=%s  latency=%dms",
            len(state.history) // 2,
            list(state.confirmed_entities.keys()) if state.confirmed_entities else [],
            int((time.time() - t1) * 1000),
        )

        # ── Step 2: Trust check on input ─────────────────────────────
        trust_endpoint = (
            self._config.get("trust_client", {}).get("endpoint", "http://trust_layer:8003")
        )
        logger.info(
            "  [STEP 2] Trust Input Check  →  POST %s/check/input  (session=%s)",
            trust_endpoint, session_id,
        )
        t2 = time.time()
        trust_input = self._trust.check_input(session_id, turn_input.user_message)
        logger.info(
            "  [STEP 2] Trust Input Check  ✓  action=%s  passed=%s  reason=%s  latency=%dms",
            trust_input.action, trust_input.passed,
            trust_input.reason or "—", int((time.time() - t2) * 1000),
        )

        if trust_input.action == "block":
            logger.info(
                "  [STEP 2] ✗ INPUT BLOCKED — reason=%s  →  returning blocked response",
                trust_input.reason,
            )
            return self._blocked_response(session_id, trust_input, start, trust_input)

        if trust_input.action == "escalate":
            logger.info(
                "  [STEP 2] ⚠ INPUT ESCALATED — reason=%s  →  routing to human agent",
                trust_input.reason,
            )
            return self._escalated_response(session_id, trust_input, start, trust_input)

        # ── Step 3: Language Normalisation ───────────────────────────
        # Uses Haiku (model_override in config) — reads from preprocessing.language_normalisation
        lang_model = (
            self._config.get("preprocessing", {})
            .get("language_normalisation", {})
            .get("model_override", "haiku")
        )
        logger.info(
            "  [STEP 3] Language Normalisation  →  LLM call (model_override=%s)",
            lang_model,
        )
        t3 = time.time()
        normalised_input, detected_language = self._language_normaliser.normalise(
            raw_input=turn_input.user_message,
            config=self._config,
            llm=self._llm,
        )
        logger.info(
            "  [STEP 3] Language Normalisation  ✓  detected_language=%s  normalised=%r  latency=%dms",
            detected_language or "en",
            (normalised_input or turn_input.user_message)[:100],
            int((time.time() - t3) * 1000),
        )

        # ── Step 4: NLU Processor ─────────────────────────────────────
        # Injects recent history for context-aware follow-up classification.
        # Uses Haiku (model_override in config) — reads from preprocessing.nlu_processor
        nlu_model = (
            self._config.get("preprocessing", {})
            .get("nlu_processor", {})
            .get("model_override", "haiku")
        )
        logger.info(
            "  [STEP 4] NLU Processor  →  LLM call (model_override=%s)  history_turns=%d",
            nlu_model, len(state.history) // 2,
        )
        t4 = time.time()
        nlu_result = self._nlu_processor.process(
            normalised_input=normalised_input,
            history=state.history,
            config=self._config,
            llm=self._llm,
        )
        logger.info(
            "  [STEP 4] NLU Processor  ✓  intent=%s  confidence=%.2f  entities=%s  sentiment=%s  latency=%dms",
            nlu_result.intent, nlu_result.confidence,
            nlu_result.entities if nlu_result.entities else {},
            nlu_result.sentiment,
            int((time.time() - t4) * 1000),
        )

        # ── Step 5: Early exit on unknown / low-confidence intent ─────
        confidence_threshold = (
            self._config.get("preprocessing", {})
            .get("nlu_processor", {})
            .get("confidence_threshold", 0.5)
        )
        logger.info(
            "  [STEP 5] Intent Gate  →  intent=%s  confidence=%.2f  threshold=%.2f",
            nlu_result.intent, nlu_result.confidence, confidence_threshold,
        )
        if nlu_result.confidence < confidence_threshold and nlu_result.intent == "unknown":
            # Only exit early if BOTH conditions are true: intent is unknown AND confidence
            # is below threshold. A vague but valid query (e.g. "hi can i get job info?")
            # that NLU can't classify should still reach the LLM — it can ask a clarifying
            # question. True unknown input (gibberish, off-topic) will have intent=unknown
            # AND very low confidence. Trust Layer already blocks harmful content upstream.
            logger.info(
                "  [STEP 5] ✗ EARLY EXIT — intent=unknown and confidence=%.2f below threshold  →  returning unknown-intent response",
                nlu_result.confidence,
            )
            return self._unknown_intent_response(session_id, start)
        logger.info("  [STEP 5] Intent Gate  ✓  proceeding")

        # ── Step 6: Assemble prompt (KE) ─────────────────────────────
        # KE receives pre-computed NLU data — runs only Glossary, Static KB, Multimodal.
        # Returns (messages, system): system goes to llm.call(), messages are the conversation.
        ke_endpoint = (
            self._config.get("knowledge_engine_client", {}).get("endpoint", "http://knowledge_engine:8001")
        )
        logger.info(
            "  [STEP 6] Knowledge Engine  →  POST %s/assemble_prompt"
            "  (intent=%s  entities=%s)",
            ke_endpoint, nlu_result.intent,
            nlu_result.entities if nlu_result.entities else {},
        )
        t6 = time.time()
        messages, system = self._knowledge_engine.assemble_prompt(
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
        rag_chunks = len(messages[-1]["content"].split("--- Relevant knowledge ---")) - 1 if messages else 0
        logger.info(
            "  [STEP 6] Knowledge Engine  ✓  message_count=%d  rag_chunks_found=%d"
            "  system_prompt_len=%d  latency=%dms",
            len(messages), rag_chunks, len(system), int((time.time() - t6) * 1000),
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
        primary_model = self._config.get("agent", {}).get("primary_model", "unknown")
        logger.info(
            "  [STEP 7] LLM Call #1  →  Anthropic API (model=%s)"
            "  tools_available=%d  message_count=%d",
            primary_model, len(tools), len(messages),
        )
        t7 = time.time()
        llm_response = self._llm.call(messages=messages, tools=tools, system=system)
        logger.info(
            "  [STEP 7] LLM Call #1  ✓  stop_reason=%s  model_used=%s"
            "  input_tokens=%d  output_tokens=%d  latency=%dms",
            llm_response.stop_reason, llm_response.model_used,
            llm_response.input_tokens, llm_response.output_tokens,
            int((time.time() - t7) * 1000),
        )
        if llm_response.stop_reason == "tool_use":
            logger.info("  [STEP 7]   → LLM requested tool use — entering tool loop")

        # ── Step 8: Tool-use loop ─────────────────────────────────────
        ag_endpoint = (
            self._config.get("action_gateway_client", {}).get("endpoint", "http://action_gateway:8005")
        )
        logger.info(
            "  [STEP 8] Tool-Use Loop  →  Action Gateway %s (if tool requested)", ag_endpoint,
        )
        t8 = time.time()
        final_text, tool_calls = self._manager_agent.run_turn(
            messages=messages,
            session_id=session_id,
            initial_llm_response=llm_response,
            system=system,
        )
        if tool_calls:
            tool_names = [tc.tool_name for tc in tool_calls]
            logger.info(
                "  [STEP 8] Tool-Use Loop  ✓  tools_called=%s  latency=%dms",
                tool_names, int((time.time() - t8) * 1000),
            )
        else:
            logger.info(
                "  [STEP 8] Tool-Use Loop  ✓  no tool used — direct LLM response  latency=%dms",
                int((time.time() - t8) * 1000),
            )

        # ── Step 9: Trust check on output ────────────────────────────
        logger.info(
            "  [STEP 9] Trust Output Check  →  POST %s/check/output  (session=%s)",
            trust_endpoint, session_id,
        )
        t9 = time.time()
        trust_output = self._trust.check_output(session_id, final_text)
        logger.info(
            "  [STEP 9] Trust Output Check  ✓  action=%s  passed=%s  latency=%dms",
            trust_output.action, trust_output.passed, int((time.time() - t9) * 1000),
        )

        if trust_output.action in ("block", "escalate"):
            logger.info(
                "  [STEP 9] ⚠ OUTPUT %s — replacing with safe fallback",
                trust_output.action.upper(),
            )
            final_text = self._safe_fallback_message()

        # ── Step 10: Build result and return ─────────────────────────
        latency_ms = int((time.time() - start) * 1000)
        logger.info(
            "  [STEP 10] Delivering response to caller  (async: memory write + learning emit follow)",
        )
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
        logger.info(
            "\n═══════════════════════════════════════════════════════════════\n"
            "  TURN COMPLETE  session=%s  intent=%s  tool_used=%s\n"
            "  model=%s  total_latency=%dms\n"
            "  response: %r\n"
            "═══════════════════════════════════════════════════════════════",
            session_id, nlu_result.intent, bool(tool_calls),
            llm_response.model_used, latency_ms,
            final_text[:200],
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
        memory_endpoint = (
            self._config.get("memory_client", {}).get("endpoint", "http://memory_layer:8002")
        )
        learning_endpoint = (
            self._config.get("learning_client", {}).get("endpoint", "http://learning_layer:8004")
        )

        logger.info(
            "  [STEP 11] [async] Memory Write  →  POST %s/session/write  (session=%s)",
            memory_endpoint, session_id,
        )
        try:
            t11 = time.time()
            self._memory.write_session(session_id, updated_state)
            logger.info(
                "  [STEP 11] [async] Memory Write  ✓  history_turns=%d  latency=%dms",
                len(updated_state.history) // 2, int((time.time() - t11) * 1000),
            )
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

        logger.info(
            "  [STEP 12] [async] Learning Emit  →  POST %s/emit/turn  (session=%s)",
            learning_endpoint, session_id,
        )
        try:
            t12 = time.time()
            self._learning.emit_turn(turn_event)
            logger.info(
                "  [STEP 12] [async] Learning Emit  ✓  latency=%dms",
                int((time.time() - t12) * 1000),
            )
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
