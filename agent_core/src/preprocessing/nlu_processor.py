"""
agent_core/src/nlu_processor.py

NLU intent classification, entity extraction, and sentiment detection —
executed in Agent Core before the KE call.

Uses a single LLM call (Haiku). Recent session history is included in the LLM
messages so the model can resolve context-dependent intents such as follow-up
questions ("tell me more", "what about plumber?").

On any LLM failure or JSON parse error, degrades gracefully:
    returns NLUResult(intent="unknown", confidence=0.0, ...).

Config section read: preprocessing.nlu_processor
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from src.llm_wrapper.base import LLMWrapperBase
from src.models import NLUResult

logger = logging.getLogger(__name__)

_NLU_SYSTEM_PROMPT = """You are an NLU (Natural Language Understanding) classifier for an employment assistance chatbot.
Classify the user's latest message and return a JSON object only — no explanation, no markdown.

Valid intents: {intents}
Valid entity types: {entities}
Valid sentiment classes: {sentiment_classes}

Return exactly this JSON structure:
{{
  "intent": "<one of the valid intents>",
  "entities": {{ "<entity_type>": "<value>", ... }},
  "sentiment": "<one of the valid sentiment classes>",
  "confidence": <float between 0.0 and 1.0>
}}

Rules:
- Use "unknown" intent if no intent matches or confidence is below 0.5.
- Only include entity types that are clearly present in the message.
- Return an empty dict {{}} for entities if none are found.
- If conversation history is provided, use it to resolve follow-up or ambiguous messages.
- Never include keys outside the four specified (intent, entities, sentiment, confidence)."""


class NLUProcessor:
    """
    Classifies intent, extracts entities, and detects sentiment via a single LLM call.

    Injects recent session history into the LLM prompt so the model can resolve
    context-dependent follow-up messages (e.g. "tell me more about that").

    Instantiated once by AgentCore at startup — stateless, reused across all sessions.
    Config section: preprocessing.nlu_processor
    """

    def process(
        self,
        normalised_input: str,
        history: list[dict],
        config: dict,
        llm: LLMWrapperBase,
    ) -> NLUResult:
        """
        Run NLU classification with conversation history context.

        Args:
            normalised_input: Cleaned text from Language Normaliser.
            history:          Session history from Memory Layer.
                              Last `history_turns` turns are injected into the LLM
                              messages for context-aware intent classification.
            config:           Full agent_core config dict.
            llm:              LLM wrapper for direct LLM calls.

        Returns:
            NLUResult. On any failure: NLUResult(intent="unknown", confidence=0.0).
            Never raises.
        """
        start = time.time()

        if not normalised_input:
            return _fallback_nlu_result()

        block_cfg = (
            config.get("preprocessing", {})
            .get("nlu_processor", {})
        )
        intents = block_cfg.get("intents", ["unknown"])
        entities_list = block_cfg.get("entities", [])
        sentiment_classes = block_cfg.get("sentiment_classes", ["neutral"])
        model_override = block_cfg.get("model")
        history_turns = block_cfg.get("history_turns", 2)

        try:
            system_prompt = _NLU_SYSTEM_PROMPT.format(
                intents=", ".join(intents),
                entities=", ".join(entities_list),
                sentiment_classes=", ".join(sentiment_classes),
            )

            # Include recent history so the LLM can resolve follow-up intents
            messages: list[dict] = []
            if history:
                recent = history[-(history_turns * 2):]
                messages.extend(recent)
            messages.append({"role": "user", "content": normalised_input})

            llm_response = llm.call(
                messages=messages,
                tools=[],
                system=system_prompt,
                model_override=model_override,
            )

            if llm_response.stop_reason == "error" or not llm_response.content:
                logger.warning(
                    "nlu_processor.llm_failure",
                    extra={
                        "operation": "nlu_processor.process",
                        "status": "failure",
                        "stop_reason": llm_response.stop_reason,
                        "latency_ms": int((time.time() - start) * 1000),
                    },
                )
                return _fallback_nlu_result()

            parsed = self._parse_nlu_json(llm_response.content)

            # Validate intent is in allowed list
            intent = parsed.get("intent", "unknown")
            if intent not in intents:
                logger.warning(
                    "nlu_processor.invalid_intent",
                    extra={
                        "operation": "nlu_processor.process",
                        "status": "failure",
                        "intent": intent,
                    },
                )
                intent = "unknown"

            extracted_entities = parsed.get("entities", {})
            if not isinstance(extracted_entities, dict):
                extracted_entities = {}

            result = NLUResult(
                intent=intent,
                entities=extracted_entities,
                sentiment=parsed.get("sentiment", "neutral"),
                confidence=float(parsed.get("confidence", 0.0)),
            )

            logger.info(
                "nlu_processor.process",
                extra={
                    "operation": "nlu_processor.process",
                    "status": "success",
                    "intent": result.intent,
                    "sentiment": result.sentiment,
                    "confidence": result.confidence,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return result

        except Exception as e:
            logger.error(
                "nlu_processor.error",
                extra={
                    "operation": "nlu_processor.process",
                    "status": "failure",
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return _fallback_nlu_result()

    def _parse_nlu_json(self, raw: str) -> dict[str, Any]:
        """Parse LLM JSON response. Returns empty dict on any parse error."""
        try:
            return json.loads(raw.strip())
        except json.JSONDecodeError:
            pass

        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning(
            "nlu_processor.json_parse_failure",
            extra={
                "operation": "nlu_processor._parse_nlu_json",
                "status": "failure",
            },
        )
        return {}


def _fallback_nlu_result() -> NLUResult:
    """Default NLUResult when LLM call fails or JSON cannot be parsed."""
    return NLUResult(intent="unknown", entities={}, sentiment="neutral", confidence=0.0)
