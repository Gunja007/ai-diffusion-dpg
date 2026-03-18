"""
agent_core/src/language_normalisation.py

Language detection and normalisation — executed in Agent Core before the KE call.

Runs after Trust check (step 2), before NLU Processor (step 4).

Provider options (from YAML config preprocessing.language_normalisation.provider):
- llm_native: Single LLM call using the configured model (Haiku). Default for PoC.
- bhashini:   Stub only — raises NotImplementedError. Real integration is post-PoC.

On any LLM failure or JSON parse error, degrades gracefully:
    returns (raw_input, "") — original text, no language detected.

Config section read: preprocessing.language_normalisation
"""

from __future__ import annotations

import json
import logging
import re
import time

from src.llm_wrapper.base import LLMWrapperBase

logger = logging.getLogger(__name__)

_LANG_NORM_SYSTEM = """You are a language processing assistant for a Hindi/Kannada/Hinglish employment chatbot.
Analyse the user's message and return a JSON object only — no explanation, no markdown.

Supported languages: {supported_languages}

Return exactly this JSON structure:
{{
  "detected_language": "<one of: hindi, kannada, english, hinglish>",
  "normalised_text": "<cleaned, normalised version of the input>"
}}

Normalisation rules:
1. If the input is Roman-script Hindi (e.g. "bijli ka kaam chahiye"), keep it as-is — do not transliterate to Devanagari.
2. If the input mixes Hindi and English (Hinglish), keep the mix but clean spelling inconsistencies.
3. If the input is Kannada, keep it unchanged.
4. If the input is pure English, keep it unchanged.
5. Correct obvious typos only if clearly unambiguous.
6. NEVER change the meaning or add words not present in the original."""


class LanguageNormaliser:
    """
    Detects language and normalises raw user input.

    Instantiated once by AgentCore at startup — stateless, reused across all sessions.
    Config section: preprocessing.language_normalisation
    """

    def normalise(
        self,
        raw_input: str,
        config: dict,
        llm: LLMWrapperBase,
    ) -> tuple[str, str]:
        """
        Detect language and normalise input text.

        Args:
            raw_input: Original user message.
            config:    Full agent_core config dict.
            llm:       LLM wrapper for direct LLM calls.

        Returns:
            (normalised_input, detected_language)
            On any failure: (raw_input, "") — original text unchanged, no language detected.
            Never raises.
        """
        start = time.time()

        if not raw_input:
            return raw_input, ""

        block_cfg = (
            config.get("preprocessing", {})
            .get("language_normalisation", {})
        )
        supported_languages = block_cfg.get(
            "supported_languages", ["hindi", "kannada", "english", "hinglish"]
        )
        model_override = block_cfg.get("model")
        provider = block_cfg.get("provider", "llm_native")

        try:
            if provider == "bhashini":
                raise NotImplementedError(
                    "Bhashini provider is not yet implemented. "
                    "Set preprocessing.language_normalisation.provider=llm_native for PoC."
                )

            system_prompt = _LANG_NORM_SYSTEM.format(
                supported_languages=", ".join(supported_languages)
            )
            messages = [{"role": "user", "content": raw_input}]

            llm_response = llm.call(
                messages=messages,
                tools=[],
                system=system_prompt,
                model_override=model_override,
            )

            if llm_response.stop_reason == "error" or not llm_response.content:
                logger.warning(
                    "language_normalisation.llm_failure",
                    extra={
                        "operation": "language_normalisation.normalise",
                        "status": "failure",
                        "stop_reason": llm_response.stop_reason,
                        "latency_ms": int((time.time() - start) * 1000),
                    },
                )
                return raw_input, ""

            normalised, detected = self._parse_response(
                llm_response.content.strip(), raw_input
            )

            logger.info(
                "language_normalisation.normalise",
                extra={
                    "operation": "language_normalisation.normalise",
                    "status": "success",
                    "detected_language": detected,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return normalised, detected

        except NotImplementedError:
            raise  # re-raise config errors — these are programmer errors, not runtime errors

        except Exception as e:
            logger.error(
                "language_normalisation.error",
                extra={
                    "operation": "language_normalisation.normalise",
                    "status": "failure",
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return raw_input, ""

    def _parse_response(self, raw_content: str, raw_input: str) -> tuple[str, str]:
        """Parse LLM JSON response. Falls back to (raw_input, "") on any parse error."""
        # Try direct parse
        try:
            data = json.loads(raw_content)
            return data.get("normalised_text", raw_input), data.get("detected_language", "")
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from prose
        json_match = re.search(r"\{.*\}", raw_content, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return data.get("normalised_text", raw_input), data.get("detected_language", "")
            except json.JSONDecodeError:
                pass

        logger.warning(
            "language_normalisation.parse_failure",
            extra={
                "operation": "language_normalisation._parse_response",
                "status": "failure",
            },
        )
        return raw_input, ""
