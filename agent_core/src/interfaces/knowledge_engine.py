"""
agent_core/interfaces/knowledge_engine.py

Contract that Agent Core requires from the Knowledge Engine DPG.
The Knowledge Engine assembles the full prompt (persona + RAG + glossary + history)
before every LLM call. Agent Core provides pre-computed NLU context; KE returns messages.

Language Normalisation and NLU Processor now run in Agent Core (steps 3-4 of process_turn).
KE receives their results as parameters and runs only Glossary, Static KB, and Multimodal.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional

from src.models import SessionState


class KnowledgeEngineBase(ABC):

    @abstractmethod
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
        Build and return the complete messages list for the LLM call.

        Format:
            [{"role": "user" | "assistant", "content": str}, ...]

        Agent Core runs Language Normalisation and NLU before calling this method
        and passes the results as parameters. KE uses these to drive Glossary
        normalisation and Static KB intent-based filtering.

        Args:
            session_id:        Session identifier.
            user_message:      Original raw user message (used as final message in prompt).
            session_state:     Full session context including conversation history.
            normalised_input:  Cleaned text from Language Normaliser (Agent Core step 3).
            detected_language: Language detected by Language Normaliser.
            intent:            Classified intent from NLU Processor (Agent Core step 4).
            entities:          Extracted entities dict from NLU Processor.
            sentiment:         Sentiment class from NLU Processor.
            confidence:        NLU confidence score 0.0–1.0.

        Returns an empty list only if user_message is empty — never raises.
        """
