"""
knowledge_engine/src/blocks/glossary.py

Block 3 — Glossary & Domain Vocabulary

Maps colloquial user terms to canonical domain concepts before retrieval queries
are formed. Runs after NLU (Block 2) so it can also normalise entity values that
Block 2 extracted.

Design:
- Pure string matching — no LLM call, no DB call.
- All mappings are read from YAML config at construction time.
- Applies substitutions to context.normalised_input (substring replacement).
- Also normalises matching values in context.entities (exact value match).
- If disabled in config, passes KEContext through unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from src.base import KnowledgeBlock, KEContext, LLMWrapperBase

logger = logging.getLogger(__name__)


class GlossaryBlock(KnowledgeBlock):
    """
    Applies configurable colloquial-to-canonical term mappings.

    YAML config section read: knowledge.blocks.glossary

    Example mapping (from config/config.yaml):
        - colloquial: ["kaam chahiye", "naukri chahiye", "job chahiye"]
          canonical: "market_truth_query"

    After this block runs:
        context.normalised_input — all colloquial phrases replaced with canonical terms
        context.entities         — entity values normalised (e.g. "bijli wala" → "trade:electrician")
    """

    def process(
        self,
        context: KEContext,
        llm: LLMWrapperBase,
        config: dict,
    ) -> KEContext:
        """
        Apply glossary substitutions to normalised_input and entity values.
        Returns context unchanged if disabled or if mappings list is empty.
        Never raises.
        """
        start = time.time()

        block_cfg = (
            config.get("knowledge", {})
            .get("blocks", {})
            .get("glossary", {})
        )

        if not block_cfg.get("enabled", True):
            logger.info(
                "glossary.skipped",
                extra={
                    "operation": "glossary.process",
                    "status": "skipped",
                    "session_id": context.session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return context

        mappings = block_cfg.get("mappings", [])
        apply_to = block_cfg.get("apply_to", ["normalised_input", "entities"])

        if not mappings:
            logger.info(
                "glossary.no_mappings",
                extra={
                    "operation": "glossary.process",
                    "status": "skipped",
                    "session_id": context.session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            return context

        try:
            if "normalised_input" in apply_to:
                context.normalised_input = self._apply_to_text(
                    context.normalised_input, mappings
                )

            if "entities" in apply_to:
                context.entities = self._apply_to_entities(
                    context.entities, mappings
                )

            logger.info(
                "glossary.process",
                extra={
                    "operation": "glossary.process",
                    "status": "success",
                    "session_id": context.session_id,
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )

        except Exception as e:
            logger.error(
                "glossary.error",
                extra={
                    "operation": "glossary.process",
                    "status": "failure",
                    "session_id": context.session_id,
                    "error": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.time() - start) * 1000),
                },
            )
            # Return context unchanged — caller must not crash

        return context

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _apply_to_text(self, text: str, mappings: list[dict]) -> str:
        """
        Replace all colloquial phrases in text with their canonical equivalents.
        Uses case-insensitive substring replacement. Processes all mappings in order.
        """
        if not text:
            return text

        result = text
        for mapping in mappings:
            colloquial_list = mapping.get("colloquial", [])
            canonical = mapping.get("canonical", "")
            if not canonical:
                continue
            for phrase in colloquial_list:
                if not phrase:
                    continue
                # Case-insensitive replace while preserving original case elsewhere
                result = _case_insensitive_replace(result, phrase, canonical)

        return result

    def _apply_to_entities(
        self,
        entities: dict[str, Any],
        mappings: list[dict],
    ) -> dict[str, Any]:
        """
        Normalise entity values by exact (case-insensitive) match against colloquial phrases.
        Returns a new dict with normalised values. Keys are unchanged.
        Preserves entries whose values do not match any mapping.
        """
        if not entities:
            return entities

        normalised = {}
        for key, value in entities.items():
            if not isinstance(value, str):
                normalised[key] = value
                continue

            matched = False
            for mapping in mappings:
                colloquial_list = mapping.get("colloquial", [])
                canonical = mapping.get("canonical", "")
                if not canonical:
                    continue
                for phrase in colloquial_list:
                    if phrase and value.lower() == phrase.lower():
                        normalised[key] = canonical
                        matched = True
                        break
                if matched:
                    break

            if not matched:
                normalised[key] = value

        return normalised


# ---------------------------------------------------------------------------
# Module-level helper (private — not part of public interface)
# ---------------------------------------------------------------------------


def _case_insensitive_replace(text: str, old: str, new: str) -> str:
    """Replace all case-insensitive occurrences of `old` in `text` with `new`."""
    if not old:
        return text
    lower_text = text.lower()
    lower_old = old.lower()
    result_parts = []
    start = 0
    while True:
        idx = lower_text.find(lower_old, start)
        if idx == -1:
            result_parts.append(text[start:])
            break
        result_parts.append(text[start:idx])
        result_parts.append(new)
        start = idx + len(old)
    return "".join(result_parts)
